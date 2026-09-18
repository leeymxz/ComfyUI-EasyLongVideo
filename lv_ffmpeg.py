# -*- coding: utf-8 -*-
"""FFmpeg 自动探测与成片合成。

探测顺序（命中即缓存）：
1. 系统 PATH；
2. ComfyUI 根目录及其常见子目录（便携版用户经常把 ffmpeg 放在根目录）；
3. 环境变量 ELV_FFMPEG 指定的路径；
4. imageio-ffmpeg 包（ComfyUI 官方 requirements 自带，绝大多数环境都可用）。

找不到 ffmpeg 时：不崩溃，仍可生成与下载各段素材，并给出 concat 清单与
手动合成命令，交给用户现有的剪辑/转码工具完成最后一步。
"""
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

_CACHE = {"ffmpeg": None, "probed": False}


class FFmpegNotFound(RuntimeError):
    pass


def _exe_name(name):
    return f"{name}.exe" if os.name == "nt" else name


def _works(path):
    try:
        result = subprocess.run(
            [str(path), "-version"], capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        return result.returncode == 0 and "ffmpeg" in (result.stdout or "").lower()
    except (OSError, subprocess.TimeoutExpired):
        return False


def _comfy_base():
    try:
        import folder_paths
        return Path(getattr(folder_paths, "base_path", ".")).resolve()
    except Exception:
        return None


def find_ffmpeg(force=False):
    if _CACHE["probed"] and not force:
        return _CACHE["ffmpeg"]
    _CACHE["probed"] = True

    # 1. PATH
    exe = shutil.which("ffmpeg")
    if exe and _works(exe):
        _CACHE["ffmpeg"] = Path(exe)
        return _CACHE["ffmpeg"]

    # 2. ComfyUI 常见位置
    base = _comfy_base()
    if base:
        candidates = [base / _exe_name("ffmpeg"),
                      base / "ComfyUI" / _exe_name("ffmpeg"),
                      base / "python_embeded" / _exe_name("ffmpeg"),
                      base / "python_embeded" / "Scripts" / _exe_name("ffmpeg"),
                      base / "custom_nodes" / _exe_name("ffmpeg")]
        for cand in candidates:
            if cand.is_file() and _works(cand):
                _CACHE["ffmpeg"] = cand
                return _CACHE["ffmpeg"]

    # 3. 环境变量
    env_path = os.environ.get("ELV_FFMPEG")
    if env_path and Path(env_path).is_file() and _works(env_path):
        _CACHE["ffmpeg"] = Path(env_path)
        return _CACHE["ffmpeg"]

    # 4. imageio-ffmpeg（ComfyUI 官方依赖）
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and _works(exe):
            _CACHE["ffmpeg"] = Path(exe)
            return _CACHE["ffmpeg"]
    except Exception:
        pass

    _CACHE["ffmpeg"] = None
    return None


def run(args, timeout=1800):
    """执行 ffmpeg/ffprobe 命令，失败抛 RuntimeError（带 stderr 尾部）。"""
    result = subprocess.run(
        [str(a) for a in args], capture_output=True, text=True, errors="replace",
        timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if result.returncode:
        raise RuntimeError("ffmpeg 命令失败：" + (result.stderr or "")[-2000:])
    return result.stdout


_VIDEO_RE = re.compile(r"Video:.*?(?:(\d{2,5})x(\d{2,5}))?.*?, (\d+(?:\.\d+)?) fps")


def probe_video(path):
    """探测视频宽高与帧率。有 ffprobe 用 ffprobe，否则解析 ffmpeg -i 输出。"""
    path = Path(path)
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        data = json.loads(run([ffprobe, "-v", "error", "-select_streams", "v:0",
                               "-show_entries", "stream=width,height,r_frame_rate",
                               "-of", "json", str(path)]))
        stream = (data.get("streams") or [{}])[0]
        rate = str(stream.get("r_frame_rate", "24/1")).split("/")
        fps = float(rate[0]) / max(1e-6, float(rate[1])) if len(rate) == 2 else 24.0
        return {"width": int(stream.get("width", 0)), "height": int(stream.get("height", 0)),
                "fps": fps}
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise FFmpegNotFound("未找到可用的 FFmpeg。")
    info = run([ffmpeg, "-hide_banner", "-i", str(path)])
    match = _VIDEO_RE.search(info or "")
    if not match:
        raise RuntimeError("无法解析视频参数：" + str(path))
    width = int(match.group(1) or 0)
    height = int(match.group(2) or 0)
    return {"width": width, "height": height, "fps": float(match.group(3))}


def manual_hints(directory, plan):
    """找不到 ffmpeg 时，写出手动合成所需的清单与命令说明。"""
    directory = Path(directory)
    listing = directory / "work" / "concat.txt"
    listing.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for row in plan["segments"]:
        job = row.get("job") or {}
        video = job.get("video")
        if video:
            lines.append(f"file '{Path(video).as_posix()}'")
    listing.write_text("\n".join(lines), encoding="utf-8")
    (directory / "work" / "如何手动合成.txt").write_text(
        "本机未找到 FFmpeg，插件已把各段视频归档。\n"
        "方式一：把各段视频按顺序导入剪映/Premiere 等剪辑软件，再对齐原音频即可。\n"
        "方式二：安装 FFmpeg 后回到面板点击「仅重新合成」，插件会自动完成。\n"
        "命令参考（需 ffmpeg 在 PATH 中）：\n"
        f"  ffmpeg -f concat -safe 1 -i {listing} -i audio/source.wav "
        "-map 0:v:0 -map 1:a:0 -c:v copy -c:a aac -movflags +faststart final.mp4\n",
        encoding="utf-8")
    return listing


def assemble(root, project_id, read_plan, project_dir):
    """把所有已完成的分段视频拼接并配回原音频，返回成片路径。

    read_plan/project_dir 由调用方注入（避免循环依赖 lv_store）。
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise FFmpegNotFound(
            "未找到可用的 FFmpeg，无法自动合成。"
            "可安装 FFmpeg 后重试，或查看项目 work 目录里的手动合成说明。")
    plan = read_plan(root, project_id)
    directory = Path(project_dir(root, project_id))
    work = directory / "work" / ("assembly_" + uuid.uuid4().hex[:8])
    work.mkdir(parents=True, exist_ok=True)
    cache = directory / "cache"
    cache.mkdir(exist_ok=True)

    fps = float(plan.get("fps") or 24)
    size = None
    clips = []
    try:
        for row in plan["segments"]:
            job = row.get("job") or {}
            if job.get("status") != "completed" or not job.get("video"):
                raise ValueError(f"第 {row['index'] + 1} 段尚未完成，暂不能合成。")
            path = Path(job["video"])
            if not path.is_file():
                raise ValueError(f"第 {row['index'] + 1} 段的视频文件丢失。")
            info = probe_video(path)
            dimensions = (info["width"], info["height"])
            if not all(dimensions):
                dimensions = size or (960, 544)
            if size is None:
                size = dimensions
            norm = work / f"{row['index']:04d}.mp4"
            vf = (f"scale={size[0]}:{size[1]}:force_original_aspect_ratio=decrease,"
                  f"pad={size[0]}:{size[1]}:(ow-iw)/2:(oh-ih)/2,"
                  f"fps={fps},trim=end_frame={int(row['edit_frames'])},"
                  f"setpts=N/({fps}*TB)")
            run([ffmpeg, "-y", "-v", "error", "-i", str(path), "-an",
                 "-vf", vf, "-c:v", "libx264", "-crf", "18",
                 "-pix_fmt", "yuv420p", str(norm)], timeout=1200)
            clips.append(norm)

        listing = work / "concat.txt"
        listing.write_text("\n".join(f"file '{p.name}'" for p in clips), encoding="utf-8")
        final_tmp = work / "final.mp4"
        run([ffmpeg, "-y", "-v", "error",
             "-f", "concat", "-safe", "1", "-i", str(listing),
             "-i", str(directory / "audio" / "source.wav"),
             "-map", "0:v:0", "-map", "1:a:0",
             "-c:v", "copy", "-c:a", "aac", "-b:a", "256k",
             "-t", f"{float(plan['duration']):.6f}",
             "-movflags", "+faststart", str(final_tmp)], timeout=1800)

        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(
            float(plan.get("created") or time.time())))
        mode = "speaking" if plan.get("mode") == "speaking" else "singing"
        final_dir = Path(root).resolve().parent / "final_videos"
        final_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{stamp}_{mode}_{project_id[:8]}"
        final = final_dir / f"{stem}.mp4"
        version = 2
        while final.exists():
            final = final_dir / f"{stem}_v{version}.mp4"
            version += 1
        final_tmp.replace(final)
        return str(final)
    finally:
        shutil.rmtree(work, ignore_errors=True)
