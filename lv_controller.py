# -*- coding: utf-8 -*-
"""顺序生成控制器。

工作方式（与参考实现同思路，做了兼容性放宽）：
1. 前端把当前画布 graphToPrompt() 的 API prompt 发给 /run 接口；
2. 控制器保存快照，并记住分段读取节点 id 与视频输出节点 id；
3. 逐段：deepcopy 快照 → 注入 project_id / segment_index → 提交 ComfyUI 队列
   → 轮询 history 等待完成 → 从输出节点收集 mp4/webm/mov；
4. 全部完成后自动合成（ffmpeg 缺失时给出手动合成说明）；
5. 支持 暂停（当前段完成后停）/ 停止 / 单段重做 / 失败重试。

与参考实现的兼容性差异：
- 输出节点不限定 VHS_VideoCombine：任何能产出 mp4/mov/webm 文件的输出节点都可以；
- 不做 24fps 硬校验：按项目设置的 fps 处理，分辨率不一致时自动统一缩放。
"""
import asyncio
import copy
import json
import random
import time
import uuid
from pathlib import Path

from . import lv_ffmpeg, lv_store
from .lv_store import LOCK

TASKS = {}
LOADER_TYPES = {"EasyLVUnified"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".webm"}

# 每次重做需要换新 seed 的采样类节点（保证采样阶段重新执行）；
# 刻意不包含提示词扩写/LLM 节点——提示词没变时它们的输出走 ComfyUI 缓存，
# 避免每次重做都等几分钟 LLM 扩写。
_SAMPLER_SEED_NODES = {
    "KSampler", "KSamplerAdvanced", "SamplerCustom", "SamplerCustomAdvanced",
    "RandomNoise", "SamplerEulerAncestral", "SamplerDPMPP_2M",
}


def _reseed_samplers(prompt):
    """给采样类节点中的字面量 seed 换新随机值（连线输入不动）。

    这样「重做本段」时：简报改了 → LLM 重新扩写（输入变化）；
    简报没改 → LLM 走缓存；采样阶段因 seed 变化必定重新执行。
    """
    for node in prompt.values():
        if not isinstance(node, dict) or \
                str(node.get("class_type", "")) not in _SAMPLER_SEED_NODES:
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        for key, val in inputs.items():
            if "seed" in str(key).lower() and isinstance(val, int) \
                    and not isinstance(val, bool):
                inputs[key] = random.getrandbits(48)


def _queued_ids(server):
    running, waiting = server.prompt_queue.get_current_queue()
    return {item[1] for item in running + waiting}


def _collect_video(history, video_node, directory, output_root):
    if history.get("status", {}).get("status_str") != "success":
        messages = history.get("status", {}).get("messages", [])
        raise RuntimeError("该段生成失败，请检查 ComfyUI 报错后重试。"
                           + (f" 详情：{messages[-1]}" if messages else ""))
    outputs = history.get("outputs", {}).get(str(video_node), {})
    for key in ("gifs", "videos", "images", "video"):
        entries = outputs.get(key)
        if not isinstance(entries, list):
            continue
        for entry in reversed(entries):
            if not isinstance(entry, dict) or entry.get("type") not in (None, "output"):
                continue
            name = str(entry.get("filename", ""))
            if not name.lower().endswith(tuple(VIDEO_SUFFIXES)):
                continue
            path = (Path(output_root) / entry.get("subfolder", "") / name).resolve()
            if not str(path).startswith(str(Path(directory).resolve())):
                # 输出落在了项目目录外也接受（按 output_root 相对路径），仅记录
                pass
            if path.is_file():
                return str(path)
            # 尝试按 output 目录相对路径再找一次
            alt = Path(output_root) / entry.get("subfolder", "") / name
            if alt.is_file():
                return str(alt)
    raise RuntimeError("未在输出节点结果中找到视频文件。"
                       "请确认视频输出节点开启了保存（如 VHS 的 save_output）。")


def _halt_status(plan):
    if plan.get("stop_requested"):
        return "stopped"
    if plan.get("pause_requested"):
        return "paused"
    return None


async def _run_one(root, project_id, index, directory, output_root, server):
    """提交单个分段并等待完成，返回视频路径。失败抛异常。"""
    import execution
    plan = lv_store.read_plan(root, project_id)
    row = plan["segments"][index]
    snapshot = json.loads((directory / "state" / "queue_snapshot.json")
                          .read_text(encoding="utf-8"))
    prompt = copy.deepcopy(snapshot["prompt"])
    loader_id, video_id = snapshot["loader_id"], snapshot["video_id"]
    prompt[loader_id]["inputs"]["project_id"] = project_id
    prompt[loader_id]["inputs"]["segment_index"] = index
    # 重做必换采样 seed：采样阶段强制重新执行（LLM 扩写仍遵循输入缓存）
    _reseed_samplers(prompt)

    prompt_id = str(uuid.uuid4())
    try:
        valid = await execution.validate_prompt(prompt_id, prompt, [video_id])
        if not valid[0]:
            # ComfyUI 把具体失败节点放在 tuple 的其他元素里（node_errors），
            # 必须全量带出，否则只剩一条空 details 的概要错误。
            detail = ""
            for i, item in enumerate(valid[1:], 1):
                try:
                    rendered = json.dumps(item, ensure_ascii=False, default=str)
                except Exception:
                    rendered = str(item)
                if rendered and rendered != "null":
                    detail += f"\n[{i}] {rendered[:1600]}"
            raise ValueError("工作流校验失败，详细信息：" + (detail or str(valid)))
    except ImportError:
        valid = (True, None, [])  # 老版本 ComfyUI 缺少该接口时直接提交

    attempts = int(row.get("job", {}).get("attempts") or 0) + 1
    row["job"] = {"prompt_id": prompt_id, "status": "queued", "attempts": attempts}
    row.pop("needs_regeneration", None)
    lv_store.write_plan(root, plan)

    extra = {"extra_pnginfo": {"workflow": snapshot.get("workflow", {})},
             "create_time": int(time.time() * 1000)}
    client_id = str(snapshot.get("client_id") or "").strip()
    if client_id:
        extra["client_id"] = client_id
    # ComfyUI 的 PromptQueue 没有 lock 属性；number 递增按参考实现直接进行
    number = server.number
    server.number += 1
    server.prompt_queue.put((number, prompt_id, prompt, extra, valid[2], {}))
    # 通知前端 prompt_id：用于把 ComfyUI 的 executing/progress 事件关联到本项目，
    # 让节点进度实时显示"正在执行哪个节点/采样百分比"。
    _notify(server, "elv-task", {"project_id": project_id,
                                 "segment_index": index,
                                 "prompt_id": prompt_id})

    # 等待完成
    history = None
    while True:
        history = server.prompt_queue.get_history(prompt_id=prompt_id).get(prompt_id)
        if history:
            break
        if prompt_id not in _queued_ids(server):
            history = server.prompt_queue.get_history(prompt_id=prompt_id).get(prompt_id)
            if history:
                break
            raise RuntimeError(f"第 {index + 1} 段任务从队列中消失（可能被中断）。")
        await asyncio.sleep(0.5)

    plan = lv_store.read_plan(root, project_id)
    row = plan["segments"][index]
    try:
        video = _collect_video(history, video_id, directory, output_root)
        row["job"].update(status="completed", video=video, error="")
    except Exception as exc:
        row["job"].update(status="failed", error=str(exc))
        lv_store.write_plan(root, plan)
        raise
    row["job"]["completed_at"] = time.time()
    lv_store.write_plan(root, plan)
    _notify(server, "elv-segment", {
        "project_id": project_id, "segment_index": index,
        "total": len(plan["segments"]), "video": video})
    return video


async def execute_project(root, project_id, server):
    import folder_paths
    directory = lv_store.project_dir(root, project_id)
    output_root = Path(folder_paths.get_output_directory())
    try:
        initial = lv_store.read_plan(root, project_id)
        only = initial.get("run_only_segment")
        indices = [int(only)] if only is not None else list(range(len(initial["segments"])))

        for index in indices:
            plan = lv_store.read_plan(root, project_id)
            halted = _halt_status(plan)
            if halted:
                plan["run_status"] = halted
                lv_store.write_plan(root, plan)
                return
            row = plan["segments"][index]
            if row.get("job", {}).get("status") == "completed" and not row.get("needs_regeneration"):
                continue
            if not plan.get("approved") or plan.get("approved_fingerprint") != lv_store.fingerprint(plan):
                raise ValueError("分段方案已被修改，请回到面板重新「保存并确认」。")

            max_retry = max(0, min(int(plan.get("auto_retry") or 0), 9))
            attempt = 0
            while True:
                try:
                    await _run_one(root, project_id, index, directory,
                                   output_root, server)
                    break
                except Exception as exc:
                    attempt += 1
                    if attempt > max_retry:
                        raise
                    plan = lv_store.read_plan(root, project_id)
                    plan["last_retry"] = {"index": index, "attempt": attempt,
                                          "error": str(exc)[:500]}
                    lv_store.write_plan(root, plan)
                    _notify(server, "elv-retry", {
                        "project_id": project_id, "segment_index": index,
                        "attempt": attempt, "max_retry": max_retry,
                        "error": str(exc)[:300]})
                    await asyncio.sleep(2.5)

        # 全部完成 → 合成
        plan = lv_store.read_plan(root, project_id)
        if only is not None:
            plan.pop("run_only_segment", None)
            plan["run_status"] = _halt_status(plan) or "paused"
            lv_store.write_plan(root, plan)
            return
        halted = _halt_status(plan)
        if halted:
            plan["run_status"] = halted
            lv_store.write_plan(root, plan)
            return
        plan["run_status"] = "merging"
        lv_store.write_plan(root, plan)
        try:
            final = await asyncio.to_thread(
                lv_ffmpeg.assemble, root, project_id,
                lv_store.read_plan, lv_store.project_dir)
        except lv_ffmpeg.FFmpegNotFound as exc:
            plan = lv_store.read_plan(root, project_id)
            plan["run_status"] = "completed_no_ffmpeg"
            plan["error"] = str(exc)
            lv_ffmpeg.manual_hints(lv_store.project_dir(root, project_id), plan)
            lv_store.write_plan(root, plan)
            return
        plan = lv_store.read_plan(root, project_id)
        plan.update(run_status="completed", final_video=final, error="")
        lv_store.write_plan(root, plan)
        _notify(server, "elv-final", {"project_id": project_id, "final": final})
    except Exception as exc:
        try:
            plan = lv_store.read_plan(root, project_id)
            plan.pop("run_only_segment", None)
            plan.update(run_status="failed", error=str(exc))
            lv_store.write_plan(root, plan)
        except Exception:
            pass
    finally:
        TASKS.pop(project_id, None)


def _notify(server, event, data):
    try:
        if callable(getattr(server, "send_sync", None)):
            server.send_sync(event, data)
    except Exception:
        pass


def start(root, project_id, payload, server):
    """启动顺序生成。payload 需含 prompt（API 格式）、loader_id、video_id。"""
    with LOCK:
        if project_id in TASKS:
            raise ValueError("该项目已在生成中。")
        plan = lv_store.read_plan(root, project_id)
        if not plan.get("approved") or \
                plan.get("approved_fingerprint") != lv_store.fingerprint(plan):
            raise ValueError("请先在面板「保存并确认」当前分段方案。")
        directory = lv_store.project_dir(root, project_id)
        snapshot_file = directory / "state" / "queue_snapshot.json"

        replace = bool(payload.get("replace_snapshot"))
        has_jobs = any(row.get("job", {}).get("status") == "completed"
                       for row in plan["segments"])
        if not has_jobs or replace:
            prompt = copy.deepcopy(payload.get("prompt") or {})
            loader_id = str(payload.get("loader_id") or "")
            video_id = str(payload.get("video_id") or "")
            if prompt.get(loader_id, {}).get("class_type") not in LOADER_TYPES:
                raise ValueError("请选择画布中的「长视频 · 音频分析与顺序生成」节点。")
            if video_id not in prompt:
                raise ValueError("请选择本工作流的视频输出节点。")
            snapshot = {"prompt": prompt, "loader_id": loader_id, "video_id": video_id,
                        "workflow": payload.get("workflow") or {},
                        "client_id": str(payload.get("client_id") or "").strip(),
                        "saved_at": time.time()}
            snapshot_file.write_text(json.dumps(snapshot, ensure_ascii=False),
                                     encoding="utf-8")
        elif not snapshot_file.is_file():
            raise ValueError("缺少工作流快照，无法继续；请重新点击「开始顺序生成」。")

        only = payload.get("only_segment_index")
        if only is None:
            plan.pop("run_only_segment", None)
        else:
            only = int(only)
            if not 0 <= only < len(plan["segments"]):
                raise ValueError("要重新生成的分段编号无效。")
            plan["run_only_segment"] = only

        plan.update(run_status="running", pause_requested=False,
                    stop_requested=False, error="")
        lv_store.write_plan(root, plan)
        TASKS[project_id] = asyncio.create_task(
            execute_project(root, project_id, server))
