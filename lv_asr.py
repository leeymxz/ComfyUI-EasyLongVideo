# -*- coding: utf-8 -*-
"""可选语音识别：检测到 faster-whisper 就用，没有就优雅跳过。

与参考实现的关键差异：
- 不强制安装任何依赖：is_available() 为 False 时调用方直接走纯声学分段；
- 默认模型从 large-v3-turbo 降为 small（约 460MB，普通电脑也能跑；
  CPU 亦可，自动切 int8），可通过节点参数改为 base/medium/large-v3-turbo；
- 模型下载目录固定在 ComfyUI/models/EasyLongVideo/asr/，便于清理。
"""
import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

MODEL_ALIASES = {
    "auto": "small",
    "": "small",
}

MODEL_CHOICES = ["auto", "tiny", "base", "small", "medium",
                 "large-v2", "large-v3", "large-v3-turbo"]


def is_available():
    """当前 Python 环境是否带有 faster-whisper。"""
    try:
        return importlib.util.find_spec("faster_whisper") is not None
    except (ImportError, ValueError):
        return False


def resolve_model(name):
    name = str(name or "auto").strip() or "auto"
    return MODEL_ALIASES.get(name, name)


def transcribe(audio_path, output_json, model_root, model="auto", device="auto",
               log_path=None, timeout_seconds=3600, interrupt_check=None):
    """以子进程方式跑 ASR；失败抛 RuntimeError（调用方可降级）。

    interrupt_check: 可选回调，ComfyUI 中断时抛出异常以终止等待。
    """
    if not is_available():
        raise RuntimeError("当前 Python 环境未安装 faster-whisper。")
    audio_path, output_json = Path(audio_path), Path(output_json)
    if not audio_path.is_file():
        raise FileNotFoundError(str(audio_path))
    output_json.parent.mkdir(parents=True, exist_ok=True)

    worker = Path(__file__).with_name("lv_asr_worker.py")
    command = [sys.executable, "-B", str(worker),
               "--audio", str(audio_path), "--output", str(output_json),
               "--model", resolve_model(model), "--device", str(device or "auto"),
               "--download-root", str(model_root)]
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    log_handle = (Path(log_path) if log_path else output_json.with_suffix(".log")) \
        .open("w", encoding="utf-8")
    try:
        child = subprocess.Popen(
            command, stdout=log_handle, stderr=log_handle, env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        deadline = time.monotonic() + timeout_seconds
        while child.poll() is None:
            if interrupt_check is not None:
                interrupt_check()
            if time.monotonic() > deadline:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
                raise TimeoutError("语音识别超时，已停止本次分析。")
            time.sleep(0.2)
    finally:
        log_handle.close()

    if child.returncode != 0:
        log = Path(log_path or output_json.with_suffix(".log"))
        tail = ""
        try:
            tail = log.read_text(encoding="utf-8", errors="replace")[-1500:]
        except OSError:
            pass
        raise RuntimeError("语音识别失败（首次运行可能在下载模型）。日志末尾：\n" + tail)

    import json
    return json.loads(output_json.read_text(encoding="utf-8"))


def interrupt_guard():
    """返回可在等待循环中使用的 ComfyUI 中断检查函数（无 ComfyUI 时为 None）。"""
    try:
        import comfy.model_management as mm
        return mm.throw_exception_if_processing_interrupted
    except Exception:
        return None
