import contextlib
import dataclasses
import functools
import inspect
import typing
import weakref

from torchgen.model import FunctionSchema, OperatorName, SchemaKind

import torch
import torch._C as _C
import torch.library as library
import torch.utils._pytree as pytree

"""
There are various APIs for defining custom-operator-like things in PyTorch:
- [user-facing] autograd.Function (Python)
- [user-facing] custom_op (Python)
- [for power users] torch.library (Python)
- [for power users] TORCH_LIBRARY (C++)

This file contains the implementation for a Simple Custom Operator API (CustomOp).
Using CustomOp, you are able to define a custom operator and implement interactions
between the CustomOp and various PyTorch subsystems, including all the subsystems
that are necessary for a custom operator to work with torch.compile (i.e.,
autograd, FakeTensor, functionalization).

CustomOp is positioned as being safer and easier to use than
torch.library/TORCH_LIBRARY, which require deep understanding of PyTorch internals.
In additional, it supports torch.compile better than and is in general more
comprehensive than autograd.Function, which only supports implementing gradient
computation and vmap rules.
"""

__all__ = ["custom_op", "CustomOp", "get_ctx", "FakeTensorImplCtx"]


SUPPORTED_DEVICE_TYPE_TO_KEY = {
    "cpu": "CPU",
    "cuda": "CUDA",
}

# We will not let users register CustomOps with anything that could look like
# PyTorch internals to avoid confusion.
RESERVED_NS = {
    "prim",
    "prims",
    "aten",
    "at",
    "torch",
    "pytorch",
}


def custom_op(schema: str, *, ns: str) -> typing.Callable:
    r"""Creates a new CustomOp object.

    In PyTorch, defining an op (short for "operator") is a two step-process:
    - we need to define (create) the op
    - we need to implement behavior for how the operator interacts with
      various PyTorch subsystems, like CPU/CUDA Tensors, Autograd, etc.

    This entrypoint defines the CustomOp object (the first step);
    you must then perform the second step by calling various methods on
    the CustomOp object.

    This API is used as a decorator (see examples).

    Arguments:
        schema (str): The schema of the CustomOp.
        ns (str): The namespace of the CustomOp. PyTorch operators need a
            namespace; a given operator may only be created once. If you
            are writing a Python library, we recommend the namespace to be
            the name of your top-level module.

    Example::
        >>> import numpy as np
        >>>
        >>> # Step 1: define the CustomOp.
        >>> # We need to provide the decorator a "prototype function"
        >>> # (a function with Python ellipses as the body).
        >>> @custom_op('(Tensor x) -> Tensor')
        >>> def numpy_sin(x):
        >>>     ...
        >>>
        >>> # numpy_sin is now an instance of class CustomOp
        >>> print(type(numpy_sin))
        >>>
        >>> # Step 2: Register an implementation for various PyTorch subsystems
        >>>
        >>> # Register an implementation for CPU tensors
        >>> @numpy_sin.impl('cpu'):
        >>> def numpy_sin_impl_cpu(x):
        >>>     return torch.from_numpy(np.sin(x.numpy()))
        >>>
        >>> # Register an implementation for CUDA tensors
        >>> @numpy_sin.impl('cuda'):
        >>> def numpy_sin_impl_cuda(x):
        >>>     return torch.from_numpy(np.sin(x.cpu().numpy())).to(x.device)
        >>>
        >>> x = torch.randn(3)
        >>> numpy_sin(x)  # calls numpy_sin_impl_cpu
        >>>
        >>> x_cuda = x.cuda()
        >>> numpy_sin(x)  # calls numpy_sin_impl_cuda

    """

    def inner(func):
        if not inspect.isfunction(func):
            raise ValueError(
                f"custom_op(...)(func): Expected `func` to be a Python "
                f"function, got: {type(func)}"
            )

        validate_namespace(ns)
        schema_str = f"{func.__name__}{schema}"
        function_schema = FunctionSchema.parse(schema_str)
        validate_schema(function_schema)
        validate_function_matches_schema(function_schema, func)

        lib = library.Library(ns, "FRAGMENT")
        lib.define(schema_str)
        ophandle = find_ophandle_or_throw(ns, function_schema.name)
        result = CustomOp(lib, ns, function_schema.name, ophandle, _private_access=True)

        result.__name__ = func.__name__
        result.__module__ = func.__module__
        result.__doc__ = func.__doc__

        # NYI: autograd not supported
        # In the near future we will either directly use the
        # autograd_not_implemented kernels or make those the default fallback
        # for the Autograd and ADInplaceOrView keys. Both of those are a bit tricky.
        library.impl(lib, result._opname, "Autograd")(
            get_autograd_not_implemented_kernel(weakref.proxy(result))
        )

        return result

    return inner


class CustomOp:
    r"""Class for custom operators in PyTorch.

    Use the CustomOp API to create user-defined custom operators that behave
    just like regular PyTorch operators (e.g. torch.sin, torch.mm) when it
    comes to various PyTorch subsystems (like torch.compile).

    To construct a `CustomOp`, use `custom_op`.
    """

    def __init__(self, lib, cpp_ns, operator_name, ophandle, *, _private_access=False):
        super(CustomOp, self).__init__()
        if not _private_access:
            raise RuntimeError(
                "The CustomOp constructor is private and we do not guarantee "
                "BC for it. Please use custom_op(...) to create a CustomOp object"
            )
        name = f"{cpp_ns}::{str(operator_name.name)}"
        self._lib: library.Library = lib
        self._ophandle: _C._DispatchOperatorHandle = ophandle
        # Has the name of the op, e.g. "foo". We cache here for convenience.
        self._opname: str = str(operator_name)
        # this is _opname but with namespace. e.g. "custom::foo"
        self._qualname: str = name
        self.__name__ = None  # mypy requires this
        self._fake_impl: typing.Optional[FuncAndLocation] = None

        global_registry[self._qualname] = self

    def __repr__(self):
        return f'<CustomOp(op="{self._qualname}")>'

    def __call__(self, *args, **kwargs):
        # Bypass torch.ops.* and directly do OperatorHandle::callBoxed.
        # Using torch.ops.* is a bit of a pain (it can be slow and it has lifetime
        # issues from caching operators that make testing CustomOp difficult).
        result = _C._dispatch_call_boxed(self._ophandle, *args, **kwargs)
        return result

    def impl(
        self, device_types: typing.Union[str, typing.Iterable[str]]
    ) -> typing.Callable:
        r"""Register an implementation for a device type for this CustomOp object.

        If the CustomOp is passed multiple Tensor inputs with different device
        types, it will dispatch to the registered implementation for the highest
        priority device type among those present.
        The supported device types, in order of priority, are {'cuda', 'cpu'}.

        This API is used as a decorator (see examples).

        Arguments:
            device_types (str or Iterable[str]): the device type(s) to register the function for.

        Examples::
            >>> import numpy as np
            >>>
            >>> @custom_op('(Tensor x) -> Tensor', ns='custom')
            >>> def numpy_sin(x):
            >>>     ...
            >>>
            >>> # Register an implementation for CPU Tensors
            >>> @numpy_sin.impl('cpu'):
            >>> def numpy_sin_impl_cpu(x):
            >>>     return torch.from_numpy(np.sin(x.numpy()))
            >>>
            >>> # Register an implementation for CUDA Tensors
            >>> @numpy_sin.impl('cuda'):
            >>> def numpy_sin_impl_cuda(x):
            >>>     return torch.from_numpy(np.sin(x.cpu().numpy())).to(x.device)
            >>>
            >>> x = torch.randn(3)
            >>> numpy_sin(x)  # calls numpy_sin_impl_cpu
            >>>
            >>> x_cuda = x.cuda()
            >>> numpy_sin(x)  # calls numpy_sin_impl_cuda

        """
        if isinstance(device_types, str):
            device_types = [device_types]
        for device_type in device_types:
            validate_device_type(device_type)

        def inner(f):
            for device_type in set(device_types):
                dispatch_key = SUPPORTED_DEVICE_TYPE_TO_KEY[device_type]
                library.impl(self._lib, self._opname, dispatch_key)(f)
            return f

        return inner

    def impl_fake(self) -> typing.Callable:
        r"""Register a fake implementation for this operator.

        A "fake implementation" specifies the behavior of this operator on
        Tensors that carry no data. Given some input Tensors with certain properties
        (sizes/strides/storage_offset/device), it specifies what the properties of
        the output Tensors are.

        The fake implementation has the same signature as the operator.
        It is run for both FakeTensors and meta tensors. To write a fake
        implementation, assume that all Tensor inputs to the operator are
        instead FakeTensors and that you are trying to return a FakeTensor
        with the same properties (sizes/strides/storage_offset/device) that
        would be returned if all Tensor inputs were instead regular Tensors.

        This API is used as a decorator (see examples).

        Examples::
            >>> import numpy as np
            >>>
            >>> # Example 1: an operator without data-dependent output shape
            >>> @custom_op('(Tensor x, Tensor weight, Tensor bias) -> Tensor', ns='custom')
            >>> def custom_linear(x, weight, bias):
            >>>     ...
            >>>
            >>> @custom_linear.impl_fake():
            >>> def custom_linear_fake(x, weight):
            >>>     assert x.dim() == 2
            >>>     assert weight.dim() == 2
            >>>     assert bias.dim() == 1
            >>>     assert x.shape[1] == weight.shape[1]
            >>>     assert weight.shape[0] == bias.shape[0]
            >>>
            >>>     return (x @ weight.t()) + bias
            >>>
            >>> # Example 2: an operator with data-dependent output shape
            >>> @custom_op('(Tensor x) -> Tensor', ns='custom')
            >>> def custom_nonzero(x):
            >>>     ...
            >>>
            >>> @custom_nonzero.impl_fake():
            >>> def custom_nonzero_fake(x):
            >>>     # Number of nonzero-elements is data-dependent
            >>>     ctx = torch._custom_op.get_ctx()
            >>>     nnz = ctx.new_data_dependent_symint()
            >>>     # symbolic ints in PyTorch must be >= 2, so we constrain the
            >>>     # range to at least 2. Note that the operator implementation
            >>>     # must also do this.
            >>>     ctx.constrain_range(nnz, min=2)
            >>>     shape = [x.dim(), nnz]
            >>>     result = x.new_empty(shape, dtype=torch.long)
            >>>     return result
            >>>
            >>> @numpy_nonzero.impl(['cpu', 'cuda'])
            >>> def custom_nonzero_impl(x):
            >>>     x_np = to_numpy(x)
            >>>     res = np.stack(np.nonzero(x_np), axis=1)
            >>>     # symbolic ints in PyTorch must be >= 2, so we constrain the
            >>>     # range to at least 2.
            >>>     if res.shape[0] <= 1:
            >>>         raise RuntimeError("not supported")
            >>>     return torch.tensor(res, device=x.device)

        """

        def inner(f):
            frame = inspect.stack()[1]
            if self._fake_impl is not None:
                raise RuntimeError(
                    f"Attempting to register a FakeTensor rule for operator {self._qualname} "
                    f"that already has a FakeTensor rule registered from Python at "
                    f"{self._fake_impl.location}. This is not supported."
                )
            new_location = f"{frame.filename}:{frame.lineno}"

            # FakeTensor will look at _fake_impl
            self._fake_impl = FuncAndLocation(f, new_location)

            qualname = self._qualname

            # Handle DispatchKey.Meta registration
            @functools.wraps(f)
            def f_with_ctx(*args, **kwargs):
                def error_on_ctx():
                    raise RuntimeError(
                        f"Attempted to call get_ctx() for the meta implementation "
                        f"for {qualname}."
                        f"You have presumably called get_ctx() because the operator "
                        f"has a data-dependent output shape; if so, there is no "
                        f"such meta implementation and this error is the correct "
                        f"behavior. Otherwise, please remove the call to get_ctx() "
                        f"in the implementation registered with impl_fake "
                        f"at {new_location}"
                    )

                with set_ctx_getter(error_on_ctx):
                    return f(*args, **kwargs)

            self._lib.impl(self._opname, f_with_ctx, "Meta")
            return f

        return inner


@dataclasses.dataclass
class FuncAndLocation:
    func: typing.Callable
    location: str


def find_ophandle_or_throw(cpp_ns: str, operator_name: OperatorName):
    overload_name = (
        "" if operator_name.overload_name is None else operator_name.overload_name
    )
    return _C._dispatch_find_schema_or_throw(
        f"{cpp_ns}::{str(operator_name.name)}", overload_name
    )


def validate_namespace(ns: str) -> None:
    if "." in ns:
        raise ValueError(
            f'custom_op(..., ns="{ns}"): expected ns to not contain any . (and be a '
            f"valid variable name)"
        )
    if ns in RESERVED_NS:
        raise ValueError(
            f"custom_op(..., ns='{ns}'): '{ns}' is a reserved namespace, "
            f"please choose something else. "
        )


def validate_schema(schema: FunctionSchema) -> None:
    # Coming in the future. Requires us to have correct logic for
    # the ADInplaceOrView key
    if schema.kind() != SchemaKind.functional:
        raise ValueError(
            f"custom_op does not support non-functional function schema. Got: {schema}"
        )

    rets = schema.returns
    is_non_mutating_view = len(rets) > 0 and any(
        r.annotation is not None and not r.annotation.is_write for r in rets
    )
    if is_non_mutating_view:
        raise ValueError(f"custom_op does not support view functions. Got: {schema}")

    # Requires us to have handling for factory functions
    if not schema.arguments.has_tensor_arg():
        raise ValueError(
            f"custom_op does not support function schema with no Tensor inputs. Got: {schema}"
        )
    # Just seems weird so banning for now
    if not schema.returns:
        raise ValueError(
            f"custom_op does not support function schema with no outputs. Got: {schema}"
        )

    # For simplicity: don't allow self arguments
    if schema.arguments.self_arg is not None:
        raise ValueError(
            f"custom_op does not support arguments named 'self'. Please "
            f"rename your argument. Got: {schema}"
        )


def parse_namespace(namespaced_entity: str) -> typing.Tuple[str, str]:
    names = namespaced_entity.split("::", 1)
    if len(names) != 2:
        raise ValueError(f"Expected there to be a namespace in {namespaced_entity}.")
    return names[0], names[1]


def validate_device_type(device_type: str) -> None:
    if device_type not in SUPPORTED_DEVICE_TYPE_TO_KEY:
        raise ValueError(
            f"CustomOp.impl(device_types=[{device_type}, ...]): we only support device_type "
            f"in {SUPPORTED_DEVICE_TYPE_TO_KEY.keys()}."
        )


def get_autograd_not_implemented_kernel(custom_op) -> typing.Callable:
    def autograd_not_implemented(*args, **kwargs) -> None:
        if pytree.tree_any(
            lambda x: isinstance(x, torch.Tensor) and x.requires_grad, (args, kwargs)
        ):
            raise RuntimeError("Autograd has not been implemented for operator")
        guard = _C._AutoDispatchBelowAutograd()
        try:
            return custom_op(*args, **kwargs)
        finally:
            del guard

    return autograd_not_implemented


def validate_function_matches_schema(
    schema: FunctionSchema, func: typing.Callable
) -> None:
    arg_spec = inspect.getfullargspec(func)

    arg_names = tuple(arg.name for arg in schema.arguments.post_self_positional)
    if arg_names != tuple(arg_spec.args):
        raise ValueError(
            f"custom_op: Expected the schema to match the signature of `func`. "
            f"Schema has arg names {arg_names} but function has {arg_spec.args}."
        )

    kwonlyarg_names = tuple(
        arg.name for arg in schema.arguments.pre_tensor_options_kwarg_only
    )
    if kwonlyarg_names != tuple(arg_spec.kwonlyargs):
        raise ValueError(
            f"custom_op: Expected the schema to match the signature of `func`. "
            f"Schema has kwonlyarg names {kwonlyarg_names} but function has "
            f"{arg_spec.kwonlyargs}."
        )


# Global dictionary holding weak references to all CustomOp objects
# Used to query the CustomOp associated with a specific C++ dispatcher operator.
# An example usage is FakeTensor: FakeTensor checks if a specific operator
# has an implementation registered via the CustomOp API.
global_registry: weakref.WeakValueDictionary = weakref.WeakValueDictionary({})


def get_none():
    return None


global_ctx_getter: typing.Callable = get_none


# NOTE [ctx inside the fake implementation]
# If a user has an operator with data-dependent output shape, then when writing
# a fake implementation they must query the current ctx and use methods on the
# ctx to construct a new unbacked symint.
#
# This is done via us setting the global_ctx_getter function every time a fake
# implementation is invoked.
def get_ctx() -> "FakeTensorImplCtx":
    return global_ctx_getter()


@contextlib.contextmanager
def set_ctx_getter(ctx_getter):
    global global_ctx_getter
    prev = global_ctx_getter
    try:
        global_ctx_getter = ctx_getter
        yield
    finally:
        global_ctx_getter = prev


class FakeTensorImplCtx:
    """
    Context object for writing FakeTensor rules for custom operators.
    """

    def __init__(self, _shape_env, _op):
        self._shape_env = _shape_env
        self._op = _op

    def new_data_dependent_symint(self) -> torch.SymInt:
        if (
            self._shape_env is None
            or not self._shape_env.allow_dynamic_output_shape_ops
        ):
            raise torch._subclasses.fake_tensor.DynamicOutputShapeException(self._op)

        result = self._shape_env.create_unbacked_symint()
        return result

    def constrain_range(
        self, symint: torch.SymInt, *, min: int, max: typing.Optional[int] = None
    ) -> None:
        return torch.fx.experimental.symbolic_shapes.constrain_range(
            symint, min=min, max=max
        )
