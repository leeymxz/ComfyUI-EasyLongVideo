# -*- coding: utf-8 -*-
"""comfy_kitchen Turing (RTX 20 系 / sm_75) 兼容补丁工具。

用途：ComfyUI 升级会覆盖 comfy_kitchen，导致本补丁丢失，采样/音频编码时
报 `CUDA INT8 convrot dequantization failed: invalid argument` 或
`CUDA kernel launch failed: invalid argument`（nvfp4）。

用法（在 ComfyUI 便携版环境执行）：
    python_embeded\\python.exe custom_nodes\\ComfyUI-EasyLongVideo\\tools\\patch_comfy_kitchen.py

或指定 comfy_kitchen 的 cuda 后端文件路径：
    python patch_comfy_kitchen.py "H:\\...\\site-packages\\comfy_kitchen\\backends\\cuda\\__init__.py"

原理：把 Turing 上不稳定的 INT8 convrot / rowwise / nvfp4 / fp8 反量化
改为纯 PyTorch eager 实现（数值等价，速度略慢但稳定）。
"""
import argparse
import re
import shutil
import sys
from pathlib import Path

MARK = "EasyLongVideo Turing patch"

PATCHES = [
    ("1. dequant kernel 选择（Turing 走 eager）",
     '''    # Dequant rotates each group independently, so it does not need the
    # whole row staged in shared memory like ConvRot quantization does.
    return group_size in (64, 256) and k % group_size == 0 and k <= _CONVROT_FUSED_MAX_K''',
     '''    # Dequant rotates each group independently, so it does not need the
    # whole row staged in shared memory like ConvRot quantization does.
    # [EasyLongVideo Turing patch] sm_75 kernels unreliable under dynamic VRAM loading.
    try:
        if x.is_cuda and _cuda_device_is_turing(x.get_device()):
            return False
    except Exception:
        pass
    return group_size in (64, 256) and k % group_size == 0 and k <= _CONVROT_FUSED_MAX_K'''),

    ("2. rowwise 量化（Turing 走 eager）",
     '''    """Quantize tensor to INT8 with per-row scales (for activations)."""
    orig_shape = x.shape
    x_2d = x.reshape(-1, x.shape[-1]).contiguous()
    q_2d = torch.empty_like(x_2d, dtype=torch.int8)''',
     '''    """Quantize tensor to INT8 with per-row scales (for activations)."""
    orig_shape = x.shape
    x_2d = x.reshape(-1, x.shape[-1]).contiguous()
    # [EasyLongVideo Turing patch] eager rowwise quantize on sm_75.
    try:
        if x_2d.is_cuda and _cuda_device_is_turing(x_2d.get_device()):
            amax = x_2d.to(torch.float32).abs().amax(dim=1, keepdim=True).clamp_min(1e-12)
            scales_2d = amax / 127.0
            qf = x_2d.to(torch.float32) / scales_2d
            if stochastic_rounding is not None and stochastic_rounding > 0:
                qf = torch.floor(qf + torch.rand_like(qf))
            else:
                qf = torch.round(qf)
            q_2d = qf.clamp(-127, 127).to(torch.int8)
            return q_2d.reshape(orig_shape), scales_2d.reshape(*orig_shape[:-1], 1)
    except Exception:
        pass
    q_2d = torch.empty_like(x_2d, dtype=torch.int8)'''),

    ("3. simple 反量化（Turing 走 eager）",
     '''    if scale_mode < 0:
        return eager_dequantize_int8_simple(q, scale).to(DTYPE_CODE_TO_DTYPE[output_dtype_code])

    scale = scale.to(device=q.device, dtype=torch.float32).contiguous()''',
     '''    if scale_mode < 0:
        return eager_dequantize_int8_simple(q, scale).to(DTYPE_CODE_TO_DTYPE[output_dtype_code])

    # [EasyLongVideo Turing patch] eager dequant on sm_75.
    try:
        if q.is_cuda and _cuda_device_is_turing(q.get_device()):
            return eager_dequantize_int8_simple(q, scale).to(DTYPE_CODE_TO_DTYPE[output_dtype_code])
    except Exception:
        pass

    scale = scale.to(device=q.device, dtype=torch.float32).contiguous()'''),

    ("4. convrot 量化（Turing 走 rotate + rowwise）",
     '''    if weight.dim() != 2:
        raise ValueError("ConvRot INT8 weight quantization expects a 2D weight tensor")

    weight_2d = weight.contiguous()
    k = weight_2d.shape[-1]''',
     '''    if weight.dim() != 2:
        raise ValueError("ConvRot INT8 weight quantization expects a 2D weight tensor")

    weight_2d = weight.contiguous()
    k = weight_2d.shape[-1]
    # [EasyLongVideo Turing patch] rotate + eager rowwise quantize on sm_75.
    try:
        turing = weight_2d.is_cuda and _cuda_device_is_turing(weight_2d.get_device())
    except Exception:
        turing = False
    if turing and group_size and group_size % 4 == 0:
        h = _build_hadamard(group_size, device=weight_2d.device, dtype=weight_2d.dtype)
        return quantize_int8_rowwise(_rotate_weight(weight_2d, h, group_size), stochastic_rounding=stochastic_rounding)'''),

    ("5. convrot 反量化（Turing 走完整 eager）",
     '''    q_2d = q.contiguous()
    k = q_2d.shape[-1]
    output_dtype = DTYPE_CODE_TO_DTYPE[output_dtype_code]
    if _should_use_convrot_dequant_kernel(q_2d, k, group_size):''',
     '''    q_2d = q.contiguous()
    k = q_2d.shape[-1]
    output_dtype = DTYPE_CODE_TO_DTYPE[output_dtype_code]
    # [EasyLongVideo Turing patch] full eager convrot dequant on sm_75.
    try:
        turing = q_2d.is_cuda and _cuda_device_is_turing(q_2d.get_device())
    except Exception:
        turing = False
    if turing:
        scale_arg = scale.to(device=q_2d.device, dtype=torch.float32).reshape(-1)
        if scale_arg.numel() == q_2d.shape[0] and group_size and group_size % 4 == 0:
            x = q_2d.to(torch.float32) * scale_arg.reshape(-1, 1)
            h = _build_hadamard(group_size, device=q_2d.device, dtype=torch.float32)
            return _rotate_weight(x, h, group_size).to(output_dtype)
    if _should_use_convrot_dequant_kernel(q_2d, k, group_size):'''),

    ("6. fp8 反量化（Turing 走 eager）",
     '''def dequantize_per_tensor_fp8(
    x: torch.Tensor, scale: torch.Tensor, output_type: torch.dtype = torch.bfloat16
) -> torch.Tensor:
    assert scale.numel() == 1, "Scale must be a scalar tensor"''',
     '''def dequantize_per_tensor_fp8(
    x: torch.Tensor, scale: torch.Tensor, output_type: torch.dtype = torch.bfloat16
) -> torch.Tensor:
    # [EasyLongVideo Turing patch] sm_75 has no FP8 hardware; use eager path.
    try:
        turing = x.is_cuda and _cuda_device_is_turing(x.get_device())
    except Exception:
        turing = False
    if turing:
        from ..eager.quantization import dequantize_per_tensor_fp8 as _eager_dq_fp8
        return _eager_dq_fp8(x, scale, output_type)
    assert scale.numel() == 1, "Scale must be a scalar tensor"'''),

    ("7. nvfp4 反量化（Turing 走 eager）",
     '''    assert qx.is_contiguous(), "Input tensor must be contiguous"''',
     '''    # [EasyLongVideo Turing patch] sm_75: nvfp4 CUDA kernel unreliable -> eager.
    try:
        turing = qx.is_cuda and _cuda_device_is_turing(qx.get_device())
    except Exception:
        turing = False
    if turing:
        from ..eager.quantization import dequantize_nvfp4 as _eager_dq_nvfp4
        return _eager_dq_nvfp4(qx, per_tensor_scale, block_scales, output_type, hi_first)
    assert qx.is_contiguous(), "Input tensor must be contiguous"'''),
]


def find_target(explicit=None):
    if explicit:
        return Path(explicit)
    try:
        import comfy_kitchen
        return Path(comfy_kitchen.__file__).parent / "backends" / "cuda" / "__init__.py"
    except Exception:
        pass
    here = Path(__file__).resolve()
    for base in (here.parents[2], here.parents[3]):
        cand = base / "python_embeded" / "Lib" / "site-packages" / "comfy_kitchen" \
            / "backends" / "cuda" / "__init__.py"
        if cand.is_file():
            return cand
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("target", nargs="?", default=None)
    args = parser.parse_args()
    target = find_target(args.target)
    if not target or not target.is_file():
        print("未找到 comfy_kitchen cuda 后端文件，请显式传入路径。")
        return 1
    src = target.read_text(encoding="utf-8")
    if src.count(MARK) >= len(PATCHES):
        print(f"已全部打好（{src.count(MARK)} 处），无需重复。")
        return 0
    backup = target.with_suffix(".py.bak_elv")
    if not backup.exists():
        shutil.copy2(target, backup)
        print(f"已备份: {backup.name}")
    applied = src.count(MARK)
    for tag, old, new in PATCHES:
        if MARK in old or old not in src:
            if old not in src:
                print(f"[跳过] {tag}（未匹配，可能已打或版本不同）")
            continue
        src = src.replace(old, new, 1)
        applied += 1
        print(f"[OK] {tag}")
    target.write_text(src, encoding="utf-8")
    print(f"完成：共 {applied} 处补丁。请重启 ComfyUI。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
