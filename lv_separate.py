# -*- coding: utf-8 -*-
"""可选人声分离（torchaudio HTDemucs，ComfyUI 自带 torchaudio，无需 pip）。

用途：唱歌模式下，把伴奏从人声中分离出来。H3 的口型/表演驱动必须用
干净人声——否则人物会跟着鼓点贝斯乱开口、乱做动作；成片音轨仍用原曲。

- 权重约 319MB，首次使用自动下载到 ComfyUI/models/EasyLongVideo/hub/；
- GPU 优先（2080 Ti 上 4 分钟歌曲约 1 分钟），失败自动降级并返回 None；
- 返回 None 表示"分离不可用"，调用方应回退为"用原曲当人声"并提示。
"""
import shutil
from pathlib import Path

import numpy as np


def is_available() -> bool:
    try:
        import torchaudio  # noqa: F401
        return True
    except Exception:
        return False


def _load_model(device, hub_root: Path):
    import torch
    import torchaudio
    hub_root.mkdir(parents=True, exist_ok=True)
    torch.hub.set_dir(str(hub_root))
    bundle = torchaudio.pipelines.HDEMUCS_HIGH_MUSDB_PLUS
    model = bundle.get_model()
    model.to(device).eval()
    return model, int(bundle.sample_rate)


def separate_vocals(audio: np.ndarray, sr: int, hub_root,
                    device: str = "cuda", interrupt_check=None):
    """分离人声。audio: float32 [n, ch]。返回 float32 [n, ch] 或 None。

    采样率不是 44100 时先重分离前的重采样，完成后再转回 sr，
    保证输出与输入逐样本对齐（分段系统要求 length 对齐）。
    """
    if not is_available():
        return None
    import torch
    import torchaudio

    # 解析设备别名："auto" -> 有 GPU 用 cuda，否则 cpu
    if str(device).strip().lower() in ("auto", "", "none"):
        device = "cuda" if torch.cuda.is_available() else "cpu"

    model, model_sr = _load_model(device, Path(hub_root))
    try:
        # [n, ch] -> [1, ch, n]，模型要求 44100Hz 双声道
        x = np.asarray(audio, dtype=np.float32)
        if x.ndim == 1:
            x = x[:, None]
        if x.shape[1] == 1:
            x = np.repeat(x, 2, axis=1)
        elif x.shape[1] > 2:
            x = x[:, :2]

        wav = torch.from_numpy(x.T.copy())  # [ch, n]
        if sr != model_sr:
            wav = torchaudio.functional.resample(wav, sr, model_sr)
        wav = wav.unsqueeze(0).to(device)  # [1, ch, n]

        with torch.no_grad():
            sources = model(wav)  # [1, stems, ch, n]
        # 按 stem 名称定位人声轨（HTDemucs 顺序是 drums/bass/other/vocals，
        # 人声在最后——不能硬编码索引 0）
        names = [str(s).lower() for s in getattr(model, "sources", [])]
        stem_idx = names.index("vocals") if "vocals" in names else 0
        vocals = sources[0, stem_idx]  # 人声轨
        vocals = vocals.cpu()

        if sr != model_sr:
            vocals = torchaudio.functional.resample(vocals, model_sr, sr)
        out = vocals.T.numpy().astype(np.float32)  # [n, ch]

        # 与输入逐样本对齐（Demucs 输出长度可能有微小偏差）
        n = min(len(out), len(audio))
        result = np.zeros_like(audio, dtype=np.float32)
        result[:n] = out[:n]
        return result
    except Exception:
        if interrupt_check is not None:
            try:
                interrupt_check()
            except Exception:
                pass
        return None
    finally:
        try:
            model.to("cpu")
        except Exception:
            pass
        try:
            if device.startswith("cuda"):
                torch.cuda.empty_cache()
        except Exception:
            pass
