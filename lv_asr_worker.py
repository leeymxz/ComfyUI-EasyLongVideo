# -*- coding: utf-8 -*-
"""语音识别子进程工作脚本（由 lv_asr.py 以当前 Python 启动）。

只在 faster-whisper 已安装时被调用；模型首次使用会自动下载到
ComfyUI/models/EasyLongVideo/asr/，下载失败不影响主流程。
"""
import argparse
import json
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--model", default="small")
    parser.add_argument("--device", default="auto")
    parser.add_argument("--download-root", default=None)
    parser.add_argument("--cpu-threads", type=int, default=0)
    args = parser.parse_args()

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("faster_whisper not installed", file=sys.stderr)
        return 2

    compute_type = "auto"
    if args.device == "cpu":
        compute_type = "int8"  # CPU 上用 int8 更快且省内存
    model = WhisperModel(args.model, device=args.device,
                         compute_type=compute_type,
                         download_root=args.download_root,
                         cpu_threads=max(1, args.cpu_threads))
    segments, info = model.transcribe(
        args.audio, word_timestamps=True, vad_filter=True, beam_size=1)
    out = {"segments": [], "words": [],
           "language": getattr(info, "language", None),
           "duration": getattr(info, "duration", None)}
    for seg in segments:
        out["segments"].append({"start": float(seg.start), "end": float(seg.end),
                                "text": (seg.text or "").strip()})
        for word in (seg.words or []):
            if word.start is None or word.end is None:
                continue
            out["words"].append({"start": float(word.start), "end": float(word.end),
                                 "word": (word.word or "").strip()})
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(out, handle, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
