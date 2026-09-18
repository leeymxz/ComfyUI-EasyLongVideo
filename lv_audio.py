# -*- coding: utf-8 -*-
"""零依赖音频工具：只依赖 numpy 与 Python 标准库（wave）。

设计目标：
- 不需要 soundfile / librosa / torchaudio 等任何第三方音频库；
- 项目内部音频一律保存为 16-bit PCM WAV（体积可控、兼容性最好）；
- 读取时兼容 8/16/24/32-bit PCM WAV，方便用户手动放入外部素材。
"""
import io
import wave
from pathlib import Path

import numpy as np

__all__ = ["audio_tensor_to_numpy", "write_wav", "read_wav", "wav_frames", "wav_bytes"]


def audio_tensor_to_numpy(audio):
    """把 ComfyUI 的 AUDIO 字典转换为 (float32 [n, ch], sample_rate)。

    ComfyUI 的 AUDIO = {"waveform": Tensor[1, ch, n], "sample_rate": int}
    """
    x = audio["waveform"].detach().cpu().float()
    if x.ndim != 3 or x.shape[0] != 1:
        raise ValueError("请一次只输入一条音频（不支持 AUDIO batch）。")
    x = x[0].T.numpy().astype(np.float32).copy()  # -> [n, ch]
    if x.ndim == 1:
        x = x[:, None]
    if x.shape[1] > 2:
        x = x[:, :2]
    if not len(x) or not np.isfinite(x).all():
        raise ValueError("音频为空或包含无效数据。")
    return x, int(audio["sample_rate"])


def write_wav(path, samples, sr):
    """写入 16-bit PCM WAV（标准库实现，无需 soundfile）。

    samples: [n, ch] 或 [n] 的 float 数组，取值范围 -1 ~ 1。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = np.asarray(samples, dtype=np.float32)
    if data.ndim == 1:
        data = data[:, None]
    if data.ndim != 2:
        raise ValueError("音频数据必须是 [n] 或 [n, ch]。")
    if not np.isfinite(data).all():
        raise ValueError("音频包含无效数值（NaN/Inf）。")
    data = np.clip(data, -1.0, 1.0)
    pcm = (data * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(pcm.shape[1])
        w.setsampwidth(2)
        w.setframerate(int(sr))
        w.writeframes(pcm.tobytes())


def _decode_pcm(raw, sampwidth, channels):
    """把 wave.readframes 的原始字节解码为 float32，取值 -1 ~ 1。"""
    if sampwidth == 1:  # 8-bit 无符号
        a = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
        a = (a - 128.0) / 128.0
    elif sampwidth == 2:  # 16-bit 有符号（本插件内部格式）
        a = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif sampwidth == 3:  # 24-bit 有符号（手动做符号扩展）
        b = np.frombuffer(raw, dtype=np.uint8)
        usable = (len(b) // 3) * 3
        b = b[:usable].reshape(-1, 3).astype(np.int32)
        vals = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        vals = np.where(vals & 0x800000, vals - 0x1000000, vals)
        a = vals.astype(np.float32) / 8388608.0
    elif sampwidth == 4:  # 32-bit 有符号
        a = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"暂不支持 {sampwidth * 8}-bit WAV。")
    if channels > 1:
        a = a.reshape(-1, channels)
    else:
        a = a.reshape(-1, 1)
    return a.astype(np.float32)


def read_wav(path, start=0, stop=None):
    """读取 WAV 的 [start, stop) 采样区间，返回 (float32 [n, ch], sr)。

    stop=None 表示读到文件末尾。越界会被自动裁剪。
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(str(path))
    with wave.open(str(path), "rb") as w:
        sr = int(w.getframerate())
        channels = int(w.getnchannels())
        sampwidth = int(w.getsampwidth())
        total = int(w.getnframes())
        start = max(0, min(int(start), total))
        if stop is None:
            stop = total
        stop = max(start, min(int(stop), total))
        w.setpos(start)
        raw = w.readframes(stop - start)
    if not raw:
        return np.zeros((0, channels), dtype=np.float32), sr
    return _decode_pcm(raw, sampwidth, channels), sr


def wav_bytes(samples, sr):
    """把 float 音频编码为 16-bit PCM WAV 字节（HTTP 试听用，不落盘）。"""
    data = np.asarray(samples, dtype=np.float32)
    if data.ndim == 1:
        data = data[:, None]
    data = np.clip(data, -1.0, 1.0)
    pcm = (data * 32767.0).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(pcm.shape[1])
        w.setsampwidth(2)
        w.setframerate(int(sr))
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def wav_frames(path):
    """返回 WAV 文件的 (总采样帧数, 采样率, 声道数)。"""
    with wave.open(str(Path(path)), "rb") as w:
        return int(w.getnframes()), int(w.getframerate()), int(w.getnchannels())
