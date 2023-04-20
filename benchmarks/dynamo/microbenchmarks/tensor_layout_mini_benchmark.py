import torch
from torch._inductor import ir
from torch._inductor.utils import do_bench


def to_channels_last(x):
    assert x.dim() == 4

    # NCHW -> NHWC
    stride_order = [3, 0, 2, 1]
    y = x.clone().as_strided(
        x.shape,
        ir.FlexibleLayout.stride_ordered(x.shape, stride_order),
    )
    y.copy_(x)
    assert torch.allclose(x, y)
    return y


def bench_conv(with_stack=True):
    x = torch.rand(256, 3, 224, 224).cuda()
    weight = torch.rand(64, 3, 7, 7).cuda()

    x_chan = to_channels_last(x)
    weight_chan = to_channels_last(weight)
    kwargs = {
        "stride": [2, 2],
        "padding": [3, 3],
        "dilation": [1, 1],
        "transposed": False,
        "output_padding": [0, 0],
        "groups": 1,
    }

    def baseline_fn():
        return torch.convolution(x, weight, bias=None, **kwargs)

    def test_fn():
        # return torch.convolution(x_chan, weight_chan, bias=None, **kwargs) # 1.419x
        return torch.convolution(x_chan, weight, bias=None, **kwargs) # 1.417x

    # warmup
    baseline_fn()
    test_fn()

    torch.cuda.synchronize()
    with torch.profiler.profile(with_stack=with_stack) as p:
        baseline_out = baseline_fn()
        test_out = test_fn()
        torch.cuda.synchronize()

    p.export_chrome_trace("/tmp/chrome.json")
    assert torch.allclose(baseline_out, test_out, atol=1e-3, rtol=1e-3), (
        baseline_out[0][0][0][:32],
        test_out[0][0][0][:32],
    )

    baseline_ms = do_bench(baseline_fn, rep=40)
    test_ms = do_bench(test_fn, rep=40)
    print(f"conv baseline {baseline_ms} test {test_ms} speedup {baseline_ms / test_ms:.3f}x")

def bench_conv_backward():
    out_channel, in_channel = 64, 3  # 1.139x
    out_channel, in_channel = 128, 6 # 0.966x
    out_channel, in_channel = 32, 3 # 0.955x
    out_channel, in_channel = 32, 6 # 1.059x
    grad_out = torch.rand(16, out_channel, 112, 112).cuda()
    x = torch.rand(16, in_channel, 224, 224).cuda()
    weight = torch.rand(out_channel, in_channel, 7, 7).cuda()

    grad_out_chan = to_channels_last(grad_out)
    x_chan = to_channels_last(x)
    weight_chan = to_channels_last(weight)

    kwargs = {
        "bias_sizes": [0],
        "stride": [2, 2],
        "padding": [3, 3],
        "dilation": [1, 1],
        "transposed": False,
        "output_padding": [0, 0],
        "groups": 1,
        "output_mask": [False, True, False],
    }

    def baseline_fn():
        return torch.ops.aten.convolution_backward.default(
            grad_out, x, weight, **kwargs
        )

    def test_fn():
        return torch.ops.aten.convolution_backward.default(
            # grad_out_chan, x_chan, weight_chan, **kwargs,
            grad_out_chan, x_chan, weight, **kwargs,
        )

    baseline_ms = do_bench(baseline_fn, rep=40)
    test_ms = do_bench(test_fn, rep=40)
    print(f"conv backward baseline {baseline_ms} test {test_ms} speedup {baseline_ms / test_ms:.3f}x")

def main():
    bench_conv()
    # bench_conv_backward()


if __name__ == "__main__":
    main()
