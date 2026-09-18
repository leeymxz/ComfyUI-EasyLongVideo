# -*- coding: utf-8 -*-
"""项目持久化：plan.json + 音频/状态文件的原子读写。

目录布局（全部位于 ComfyUI 的 output 目录内，删除 output 不影响 ComfyUI 本体）：
    output/EasyLongVideo/
        projects/<32位hex>/
            state/plan.json          分段方案与生成状态
            state/analysis.json      声学诊断（波形峰值、候选切点等）
            state/queue_snapshot.json 顺序生成的画布快照
            state/asr.log            语音识别日志（可选）
            state/transcript.json    语音识别结果（可选）
            audio/source.wav         原始音频（PCM16）
            audio/vocals.wav         人声音频（PCM16，未提供时与 source 相同）
            takes/                   各段生成结果记录
            work/  cache/            合成临时文件与归一化缓存
        final_videos/                最终成片
"""
import hashlib
import json
import re
import threading
import uuid
from pathlib import Path

LOCK = threading.RLock()
SCHEMA = 1

_HEX32 = re.compile(r"[0-9a-f]{32}")


def storage_root():
    import folder_paths
    return Path(folder_paths.get_output_directory()) / "EasyLongVideo"


def projects_root():
    return storage_root() / "projects"


def final_root():
    return storage_root() / "final_videos"


def models_root():
    """可选模型（ASR 等）的存放位置：ComfyUI/models/EasyLongVideo/"""
    import folder_paths
    models_dir = getattr(folder_paths, "models_dir", None)
    base = Path(models_dir) if models_dir else Path(folder_paths.base_path) / "models"
    return base / "EasyLongVideo"


def rules_path():
    """运镜规则文件：ComfyUI 用户目录/EasyLongVideo/camera_rules.json。"""
    try:
        import folder_paths
        get_user = getattr(folder_paths, "get_user_directory", None)
        root = Path(get_user()) if get_user else Path(folder_paths.base_path) / "user" / "default"
    except Exception:
        root = storage_root()
    return Path(root) / "EasyLongVideo" / "camera_rules.json"


def project_dir(root, project_id):
    """校验并解析项目目录，防止路径越界。"""
    if not _HEX32.fullmatch(str(project_id or "")):
        raise ValueError("项目编号无效，请先运行长视频分析节点。")
    root = Path(root).resolve()
    result = (root / str(project_id)).resolve()
    if result.parent != root:
        raise ValueError("项目路径越界。")
    return result


def _named_file(directory, sub, name):
    if Path(name).name != str(name) or not str(name):
        raise ValueError("文件名无效。")
    return Path(directory) / sub / str(name)


def state_file(directory, name):
    return _named_file(directory, "state", name)


def audio_file(directory, name):
    return _named_file(directory, "audio", name)


def read_plan(root, project_id):
    with LOCK:
        path = state_file(project_dir(root, project_id), "plan.json")
        return json.loads(path.read_text(encoding="utf-8"))


def write_plan(root, plan):
    with LOCK:
        directory = project_dir(root, plan["id"])
        state = directory / "state"
        state.mkdir(parents=True, exist_ok=True)
        temp = state / f".plan-{uuid.uuid4().hex}.tmp"
        temp.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
        temp.replace(state / "plan.json")


def fingerprint(plan):
    """方案指纹：分段定义变化即变化（不包含生成状态字段）。"""
    core = {
        "segments": [{"start_sample": r["start_sample"], "end_sample": r["end_sample"],
                      "brief": r.get("brief", "")} for r in plan.get("segments", [])],
        "fps": plan.get("fps"), "mode": plan.get("mode"),
    }
    return hashlib.sha256(json.dumps(core, sort_keys=True, ensure_ascii=False)
                          .encode("utf-8")).hexdigest()


def init_project_dirs(directory):
    for name in ("state", "audio", "takes", "cache", "work"):
        (Path(directory) / name).mkdir(parents=True, exist_ok=True)


def list_projects(root):
    result = []
    root = Path(root)
    if not root.exists():
        return result
    for path in root.glob("*/state/plan.json"):
        try:
            plan = json.loads(path.read_text(encoding="utf-8"))
            result.append({
                "id": plan.get("id", path.parent.parent.name),
                "created": plan.get("created", 0),
                "duration": plan.get("duration", 0),
                "count": len(plan.get("segments", [])),
                "mode": plan.get("mode", "singing"),
                "status": plan.get("run_status", "draft"),
                "approved": bool(plan.get("approved")),
            })
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return sorted(result, key=lambda p: p.get("created", 0), reverse=True)
