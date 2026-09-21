# -*- coding: utf-8 -*-
"""HTTP API：分段审核面板所需的全部端点。

所有端点挂在 /elv/ 前缀下，错误统一返回 {"error": "..."}。
"""
import asyncio
import time

from . import lv_camera, lv_controller, lv_ffmpeg, lv_segment, lv_separate, lv_store
from .lv_audio import read_wav, write_wav


def register_routes():
    from aiohttp import web
    from server import PromptServer
    server = PromptServer.instance
    routes = server.routes

    def endpoint(fn):
        async def wrapped(request):
            try:
                return await fn(request)
            except FileNotFoundError:
                return web.json_response({"error": "项目文件不完整或已被移动，"
                                                   "请重新运行分析节点。"}, status=400)
            except (ValueError, KeyError, IndexError) as exc:
                return web.json_response({"error": str(exc)}, status=400)
            except Exception as exc:
                return web.json_response({"error": f"{type(exc).__name__}: {exc}"},
                                         status=500)
        return wrapped

    # ------------------------------------------------------------ 项目

    @routes.get("/elv/projects")
    @endpoint
    async def projects(request):
        return web.json_response(lv_store.list_projects(lv_store.projects_root()))

    @routes.get("/elv/project/{project_id}")
    @endpoint
    async def get_project(request):
        root = lv_store.projects_root()
        pid = request.match_info["project_id"]
        plan = lv_store.read_plan(root, pid)
        plan["controller_active"] = pid in lv_controller.TASKS
        if plan.get("final_video"):
            plan["final_ready"] = (lv_store.final_root().parent / plan["final_video"]
                                   if not str(plan["final_video"]).startswith("/")
                                   and ":" not in str(plan["final_video"])
                                   else plan["final_video"])
        return web.json_response(plan)

    @routes.get("/elv/project/{project_id}/analysis")
    @endpoint
    async def get_analysis(request):
        directory = lv_store.project_dir(lv_store.projects_root(),
                                         request.match_info["project_id"])
        path = lv_store.state_file(directory, "analysis.json")
        result = {"available": False}
        if path.is_file():
            import json
            result = json.loads(path.read_text(encoding="utf-8"))
            result["available"] = True
        result["ffmpeg"] = bool(lv_ffmpeg.find_ffmpeg())
        return web.json_response(result)

    # ------------------------------------------------------------ 运镜规则

    @routes.get("/elv/rules")
    @endpoint
    async def get_rules(request):
        return web.json_response(lv_camera.load_rules(lv_store.rules_path()))

    @routes.post("/elv/rules")
    @endpoint
    async def save_rules(request):
        payload = await request.json()
        return web.json_response(lv_camera.save_rules(lv_store.rules_path(), payload))

    @routes.post("/elv/rules/reset")
    @endpoint
    async def reset_rules(request):
        return web.json_response(lv_camera.save_rules(
            lv_store.rules_path(), lv_camera.default_rules()))

    # ------------------------------------------------------------ 审核/编辑

    @routes.post("/elv/project/{project_id}/approve")
    @endpoint
    async def approve(request):
        payload = await request.json()
        root, pid = lv_store.projects_root(), request.match_info["project_id"]
        with lv_store.LOCK:
            plan = lv_store.read_plan(root, pid)
            if plan.get("run_status") in {"running", "merging"}:
                raise ValueError("任务运行中，不能修改确认状态。")
            if int(payload.get("revision", -1)) != int(plan["revision"]):
                raise ValueError("方案已在别处修改，请刷新面板后重试。")
            plan["approved"] = True
            plan["approved_fingerprint"] = lv_store.fingerprint(plan)
            lv_store.write_plan(root, plan)
        return web.json_response(plan)

    @routes.post("/elv/project/{project_id}/edit")
    @endpoint
    async def edit(request):
        payload = await request.json()
        root, pid = lv_store.projects_root(), request.match_info["project_id"]
        with lv_store.LOCK:
            plan = lv_store.read_plan(root, pid)
            if plan.get("run_status") in {"running", "merging"}:
                raise ValueError("任务运行中，不能编辑分段。")
            if int(payload.get("revision", -1)) != int(plan["revision"]):
                raise ValueError("方案已在别处修改，请刷新面板后重试。")
            sr = int(plan["sample_rate"])
            segments = plan["segments"]
            for op in payload.get("operations", []):
                idx = int(op["index"])
                if not 0 <= idx < len(segments):
                    raise ValueError("分段编号超出范围。")
                kind = op.get("op")
                if kind == "move_boundary":
                    # 拖拽切点：boundary 为切点编号（1..len-1），移动共享边界
                    bid = int(op["boundary"])
                    at = float(op["at"])
                    if not 1 <= bid < len(segments):
                        raise ValueError("切点编号超出范围。")
                    point = int(round(at * sr))
                    left, right = segments[bid - 1], segments[bid]
                    min_gap = int(3.0 * sr)
                    if not left["start_sample"] + min_gap <= point <= right["end_sample"] - min_gap:
                        raise ValueError("切点位置需保证两侧分段至少 3 秒。")
                    left["end_sample"] = point
                    right["start_sample"] = point
                    left["warnings"] = ["手动拖拽切点，请试听"]
                    right["warnings"] = ["手动拖拽切点，请试听"]
                elif kind == "brief":
                    segments[idx]["brief"] = str(op.get("brief", ""))[:8000]
                    segments[idx]["brief_edited"] = True
                elif kind == "reset_brief":
                    default = segments[idx].get("brief_default")
                    if not default:
                        raise ValueError("该段没有保存的默认简报（旧项目请在重新分析后使用）。")
                    segments[idx]["brief"] = default
                    segments[idx]["brief_edited"] = False
                elif kind == "append_note":
                    # 批量为所有段落追加固定约束（如四视图参考图画面约束）
                    note = str(op.get("text", "")).strip()[:1200]
                    if not note:
                        raise ValueError("追加内容为空。")
                    for row_all in segments:
                        if note not in row_all.get("brief", ""):
                            row_all["brief"] = (row_all.get("brief", "").rstrip()
                                                + "\n" + note)[:8000]
                            row_all["brief_edited"] = True
                elif kind == "merge_next":
                    if idx >= len(segments) - 1:
                        raise ValueError("最后一段没有下一段可合并。")
                    merged_seconds = ((segments[idx + 1]["end_sample"]
                                       - segments[idx]["start_sample"]) / sr)
                    if merged_seconds > float(plan["max_seconds"]) + 1e-6:
                        raise ValueError("合并后超过最长分段时长，无法合并。")
                    segments[idx]["end_sample"] = segments[idx + 1]["end_sample"]
                    segments[idx]["text"] = " / ".join(
                        t for t in (segments[idx].get("text", ""),
                                    segments[idx + 1].get("text", "")) if t)
                    segments[idx]["warnings"] = ["由人工合并，请重新试听"]
                    del segments[idx + 1]
                elif kind == "split":
                    at = float(op.get("at"))
                    duration = total = None
                    start, end = segments[idx]["start_sample"], segments[idx]["end_sample"]
                    point = int(round(at * sr))
                    if not start + int(0.5 * sr) <= point <= end - int(0.5 * sr):
                        raise ValueError("拆分点必须落在段内且两侧至少保留 0.5 秒。")
                    text = segments[idx].get("text", "")
                    for field in ("start_framing", "end_framing", "start_angle",
                                  "end_angle", "move_family", "move_direction",
                                  "move_type", "band", "reason"):
                        segments[idx].pop(field, None)
                    left = dict(segments[idx])
                    right = dict(segments[idx])
                    left["end_sample"] = point
                    right["start_sample"] = point
                    left["warnings"] = ["人工拆分，请试听新切点"]
                    right["warnings"] = ["人工拆分，请试听新切点"]
                    segments[idx:idx + 1] = [left, right]
                else:
                    raise ValueError(f"未知编辑操作：{kind}")

            # 重排 index + 重算帧数与简报（人工编辑过的简报不覆盖）
            user_rules = lv_camera.load_rules(lv_store.rules_path())
            states = lv_camera.camera_sequence(
                plan["mode"], plan["segments"], activity=plan.get("camera_activity", "auto"),
                widest=plan.get("widest_framing", "medium close-up"),
                seed=str(plan["id"])[:12], rules=user_rules)
            for i, (row, state) in enumerate(zip(plan["segments"], states)):
                row["index"] = i
                seconds = (row["end_sample"] - row["start_sample"]) / sr
                row["edit_frames"] = lv_segment.edit_frames_for(seconds, int(plan["fps"]))
                row["generation_frames"] = lv_segment.align_frames(
                    seconds, int(plan["fps"]), plan.get("frame_align", "h3"),
                    row["edit_frames"])
                if not row.get("brief_edited"):
                    row["brief"] = lv_camera.segment_brief(plan["mode"], state)
                    for key in ("start_framing", "end_framing", "start_angle",
                                "end_angle", "move_family", "move_direction",
                                "move_type", "band"):
                        if key in state:
                            row[key] = state[key]
            plan["revision"] = int(plan.get("revision", 1)) + 1
            plan["approved"] = False
            plan["approved_fingerprint"] = None
            if plan.get("final_video"):
                plan["final_stale"] = True
            lv_store.write_plan(root, plan)
        return web.json_response(plan)

    @routes.post("/elv/project/{project_id}/settings")
    @endpoint
    async def settings(request):
        payload = await request.json()
        root, pid = lv_store.projects_root(), request.match_info["project_id"]
        with lv_store.LOCK:
            plan = lv_store.read_plan(root, pid)
            if "auto_retry" in payload:
                plan["auto_retry"] = max(0, min(int(payload["auto_retry"] or 0), 9))
            if "vocals_gate_thr_scale" in payload:
                plan["vocals_gate_thr_scale"] = max(0.05, min(
                    float(payload["vocals_gate_thr_scale"] or 0.22), 0.6))
            lv_store.write_plan(root, plan)
        return web.json_response({"auto_retry": plan.get("auto_retry", 0),
                                  "vocals_gate_thr_scale": plan.get("vocals_gate_thr_scale", 0.22)})

    # ------------------------------------------------------------ 音频试听

    @routes.get("/elv/project/{project_id}/audio")
    @endpoint
    async def audio(request):
        from pathlib import Path
        root, pid = lv_store.projects_root(), request.match_info["project_id"]
        plan = lv_store.read_plan(root, pid)
        directory = lv_store.project_dir(root, pid)
        sr = int(plan["sample_rate"])
        idx = request.query.get("index")
        name = "vocals.wav" if request.query.get("vocals") == "1" else "source.wav"
        path = lv_store.audio_file(directory, name)
        if idx is None and request.query.get("t0") is None:
            a, b = 0, int(plan["samples"])
        elif idx is None:
            # 任意区间试听（切点精听：t0/t1 为秒）
            t0 = float(request.query.get("t0", 0))
            t1 = float(request.query.get("t1", plan["duration"]))
            a = int(max(0.0, t0) * sr)
            b = int(min(float(plan["duration"]), t1) * sr)
            a = max(0, min(a, int(plan["samples"])))
            b = max(a, min(b, int(plan["samples"])))
        else:
            row = plan["segments"][int(idx)]
            a, b = int(row["start_sample"]), int(row["end_sample"])
        from .lv_audio import read_wav, wav_bytes
        samples, _ = read_wav(path, start=a, stop=b)
        data = wav_bytes(samples, sr)
        return web.Response(body=data, content_type="audio/wav",
                            headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------------ 运行控制

    @routes.post("/elv/project/{project_id}/run")
    @endpoint
    async def run(request):
        payload = await request.json()
        root, pid = lv_store.projects_root(), request.match_info["project_id"]
        lv_controller.start(root, pid, payload, server)
        return web.json_response({"started": True})

    @routes.post("/elv/project/{project_id}/pause")
    @endpoint
    async def pause(request):
        root, pid = lv_store.projects_root(), request.match_info["project_id"]
        with lv_store.LOCK:
            plan = lv_store.read_plan(root, pid)
            plan["pause_requested"] = True
            if pid in lv_controller.TASKS and plan["run_status"] == "running":
                plan["run_status"] = "pausing"
            lv_store.write_plan(root, plan)
        return web.json_response({"message": "当前片段完成后暂停。"})

    @routes.post("/elv/project/{project_id}/stop")
    @endpoint
    async def stop(request):
        root, pid = lv_store.projects_root(), request.match_info["project_id"]
        with lv_store.LOCK:
            plan = lv_store.read_plan(root, pid)
            if plan.get("run_status") == "merging":
                raise ValueError("正在合成成片，请等待完成。")
            plan["stop_requested"] = True
            if pid in lv_controller.TASKS:
                plan["run_status"] = "stopping"
            else:
                plan["run_status"] = "stopped"
            lv_store.write_plan(root, plan)
        return web.json_response({"message": "已停止；当前片段完成后不再提交后续片段。"})

    @routes.post("/elv/project/{project_id}/retry")
    @endpoint
    async def retry(request):
        payload = await request.json()
        root, pid = lv_store.projects_root(), request.match_info["project_id"]
        with lv_store.LOCK:
            if pid in lv_controller.TASKS:
                raise ValueError("请等待当前任务结束后再操作。")
            plan = lv_store.read_plan(root, pid)
            row = plan["segments"][int(payload["index"])]
            job = row.get("job") or {}
            if job.get("status") == "completed" and job.get("video"):
                # 版本管理：重做前归档当前完成版本，供"恢复上一版"
                takes = row.setdefault("takes", [])
                takes.append({"status": "completed", "video": job["video"],
                              "archived_at": __import__("time").time(),
                              "brief_snapshot": row.get("brief", "")})
                del takes[:-5]  # 最多保留 5 个历史版本
            row["needs_regeneration"] = True
            plan["run_status"] = "paused"
            plan["error"] = ""
            if plan.get("final_video"):
                plan["final_stale"] = True
            lv_store.write_plan(root, plan)
        payload.setdefault("replace_snapshot", False)
        payload["only_segment_index"] = int(payload["index"])
        lv_controller.start(root, pid, payload, server)
        return web.json_response({"started": True, "index": int(payload["index"])})

    @routes.post("/elv/project/{project_id}/restore")
    @endpoint
    async def restore(request):
        """恢复某段的上一版生成结果（从 takes 归档中弹出）。"""
        import copy as _copy
        payload = await request.json()
        root, pid = lv_store.projects_root(), request.match_info["project_id"]
        with lv_store.LOCK:
            if pid in lv_controller.TASKS:
                raise ValueError("请等待当前任务结束后再操作。")
            plan = lv_store.read_plan(root, pid)
            row = plan["segments"][int(payload["index"])]
            takes = row.get("takes") or []
            if not takes:
                raise ValueError("该段没有可恢复的历史版本。")
            previous = takes.pop()
            current = row.get("job") or {}
            if current.get("status") == "completed" and current.get("video"):
                takes.append({"status": "completed", "video": current["video"],
                              "archived_at": __import__("time").time(),
                              "brief_snapshot": row.get("brief", "")})
            row["job"] = {"status": "completed", "video": previous["video"],
                          "restored_from": previous.get("archived_at")}
            row["needs_regeneration"] = False
            plan["run_status"] = "paused"
            if plan.get("final_video"):
                plan["final_stale"] = True
            lv_store.write_plan(root, plan)
        return web.json_response(plan)

    @routes.get("/elv/project/{project_id}/segment/{index}/video")
    @endpoint
    async def segment_video(request):
        """流式输出某段已生成的视频（面板预览用，限制在 output 目录内）。"""
        from pathlib import Path
        root, pid = lv_store.projects_root(), request.match_info["project_id"]
        plan = lv_store.read_plan(root, pid)
        idx = int(request.match_info["index"])
        if not 0 <= idx < len(plan["segments"]):
            raise ValueError("分段编号超出范围。")
        job = plan["segments"][idx].get("job") or {}
        video = job.get("video")
        if not video:
            raise ValueError("该段尚未生成。")
        video_path = Path(video)
        if not video_path.is_file():
            raise FileNotFoundError(str(video_path))
        output_root = Path(__import__("folder_paths").get_output_directory()).resolve()
        if not str(video_path.resolve()).startswith(str(output_root)):
            raise ValueError("视频文件不在 output 目录内，拒绝访问。")
        return web.FileResponse(video_path)

    @routes.post("/elv/project/{project_id}/re-separate")
    @endpoint
    async def re_separate(request):
        """重新分离人声（不改变分段切点，段音频输出即时更新）。"""
        import asyncio
        root, pid = lv_store.projects_root(), request.match_info["project_id"]
        with lv_store.LOCK:
            plan = lv_store.read_plan(root, pid)
            if plan.get("separation_status") == "running":
                raise ValueError("分离任务进行中，请等待完成。")
            if plan.get("mode") != "singing":
                raise ValueError("口播模式没有人声分离需求。")
            plan["separation_status"] = "running"
            plan["separation_error"] = ""
            lv_store.write_plan(root, plan)

        async def job():
            try:
                def work():
                    directory = lv_store.project_dir(root, pid)
                    mix, sr = read_wav(lv_store.audio_file(directory, "source.wav"))
                    out = lv_separate.separate_vocals(
                        mix, sr, lv_store.models_root() / "hub", device="auto",
                        interrupt_check=lv_asr.interrupt_guard())
                    return out, mix, sr

                out, mix, sr = await asyncio.to_thread(work)
                with lv_store.LOCK:
                    plan = lv_store.read_plan(root, pid)
                    directory = lv_store.project_dir(root, pid)
                    if out is None or not len(out):
                        plan["separation_status"] = "failed"
                        plan["separation_error"] = "分离未产出结果（模型加载或推理失败）。"
                    else:
                        write_wav(lv_store.audio_file(directory, "vocals.wav"), out, sr)
                        plan["separation"] = "HTDemucs 自动分离（重新分离）"
                        plan["separation_status"] = "done"
                        v_hop = max(1, int(sr * 0.05))
                        v_blocks = len(out) // v_hop
                        if v_blocks > 0:
                            import numpy as _np
                            v_mono = out.mean(axis=1)
                            v_rms = _np.sqrt((v_mono[:v_blocks * v_hop]
                                              .reshape(v_blocks, v_hop) ** 2).mean(axis=1))
                            plan["vocals_rms_p50"] = float(_np.percentile(v_rms, 50))
                        # 刷新面板波形下轨（人声峰值为新分离结果）
                        try:
                            import json as _json
                            from .lv_segment import waveform_peaks as _wp
                            an_path = lv_store.state_file(directory, "analysis.json")
                            if an_path.is_file():
                                an = _json.loads(an_path.read_text(encoding="utf-8"))
                                an.setdefault("waveform", {})["vocals"] = _wp(out)
                                an_path.write_text(_json.dumps(an, ensure_ascii=False),
                                                   encoding="utf-8")
                        except Exception:
                            pass
                        lv_store.write_plan(root, plan)
            except Exception as exc:
                with lv_store.LOCK:
                    plan = lv_store.read_plan(root, pid)
                    plan["separation_status"] = "failed"
                    plan["separation_error"] = str(exc)[:400]
                    lv_store.write_plan(root, plan)

        asyncio.create_task(job())
        return web.json_response({"started": True})

    @routes.post("/elv/project/{project_id}/segment/{index}/re-separate")
    @endpoint
    async def segment_reseparate(request):
        """只对单个分段重新分离人声（±5 秒上下文提升边缘质量）。

        结果写回 vocals.wav 的该段区间；其他段不受影响。切点不变。
        """
        import asyncio
        root, pid = lv_store.projects_root(), request.match_info["project_id"]
        idx = int(request.match_info["index"])
        plan = lv_store.read_plan(root, pid)
        if not 0 <= idx < len(plan["segments"]):
            raise ValueError("分段编号超出范围。")
        if plan.get("separation_status") == "running":
            raise ValueError("已有分离任务进行中。")
        row = plan["segments"][idx]
        row["resep_status"] = "running"
        lv_store.write_plan(root, pid and plan)

        async def job():
            try:
                def work():
                    directory = lv_store.project_dir(root, pid)
                    sr = int(plan["sample_rate"])
                    total = int(plan["samples"])
                    ctx = sr * 5  # 上下文：Demucs 对边缘效应敏感
                    a = max(0, row["start_sample"] - ctx)
                    b = min(total, row["end_sample"] + ctx)
                    seg_mix, sr2 = read_wav(lv_store.audio_file(directory, "source.wav"),
                                            start=a, stop=b)
                    out = lv_separate.separate_vocals(
                        seg_mix, sr2, lv_store.models_root() / "hub", device="auto")
                    return out, sr2, a, b, total, directory

                out, sr2, a, b, total, directory = await asyncio.to_thread(work)
                with lv_store.LOCK:
                    plan2 = lv_store.read_plan(root, pid)
                    row2 = plan2["segments"][idx]
                    if out is None or not len(out):
                        row2["resep_status"] = "failed"
                        lv_store.write_plan(root, plan2)
                        return
                    # 写回该段区间：读全量 vocals -> 替换 -> 原子写回
                    old_vocals, sr3 = read_wav(lv_store.audio_file(directory, "vocals.wav"))
                    if sr3 != sr2:
                        raise ValueError("采样率异常。")
                    inner_start = row2["start_sample"] - a
                    inner_end = row2["end_sample"] - a
                    new_vocals = old_vocals.copy()
                    new_vocals[row2["start_sample"]:row2["end_sample"]] = \
                        out[inner_start:inner_end]
                    write_wav(lv_store.audio_file(directory, "vocals.wav"), new_vocals, sr2)
                    row2["resep_status"] = "done"
                    row2["resep_at"] = time.time()
                    # 更新面板波形下轨的该段区间（视觉同步）
                    try:
                        import json as _json
                        import numpy as _np
                        from .lv_segment import waveform_peaks as _wp
                        an_path = lv_store.state_file(directory, "analysis.json")
                        if an_path.is_file():
                            an = _json.loads(an_path.read_text(encoding="utf-8"))
                            peaks = an.setdefault("waveform", {}).setdefault("vocals", [])
                            if peaks:
                                dur = float(plan2["duration"])
                                i0 = int(row2["start_sample"] / sr2 / dur * len(peaks))
                                i1 = int(row2["end_sample"] / sr2 / dur * len(peaks))
                                if i1 > i0:
                                    seg_v = _wp(out[inner_start:inner_end])
                                    an["waveform"]["vocals"][i0:i1] = list(
                                        _np.interp(_np.linspace(0, max(1, len(seg_v) - 1),
                                                                i1 - i0),
                                                   range(len(seg_v)), seg_v))
                            an_path.write_text(_json.dumps(an, ensure_ascii=False),
                                               encoding="utf-8")
                    except Exception:
                        pass
                    lv_store.write_plan(root, plan2)
            except Exception as exc:
                with lv_store.LOCK:
                    plan2 = lv_store.read_plan(root, pid)
                    row2 = plan2["segments"][idx]
                    row2["resep_status"] = "failed"
                    row2["resep_error"] = str(exc)[:300]
                    lv_store.write_plan(root, plan2)

        asyncio.create_task(job())
        return web.json_response({"started": True, "index": idx})

    @routes.post("/elv/project/{project_id}/segment/{index}/mute")
    @endpoint
    async def segment_mute(request):
        """标记/取消某段"无人声"：人声驱动输出全零 + 简报追加间奏表演约束。

        音频静音防止音频驱动口型；简报约束防止提示词驱动唱歌——双保险。
        不影响分段与指纹，重做本段即生效。
        """
        payload = await request.json()
        root, pid = lv_store.projects_root(), request.match_info["project_id"]
        idx = int(request.match_info["index"])
        interlude_note = ("本段为间奏（无人声演唱）：人物嘴部保持自然闭合，不唱歌不张嘴，"
                          "不做出任何演唱口型，仅随音乐轻微律动。")
        with lv_store.LOCK:
            plan = lv_store.read_plan(root, pid)
            if not 0 <= idx < len(plan["segments"]):
                raise ValueError("分段编号超出范围。")
            row = plan["segments"][idx]
            row["mute_vocals"] = bool(payload.get("mute"))
            brief = row.get("brief", "")
            if row["mute_vocals"]:
                if interlude_note not in brief:
                    row["brief"] = (brief.rstrip() + "\n" + interlude_note)[:8000]
                row["brief_edited"] = True  # 防止镜头重排时覆盖间奏约束
            else:
                row["brief"] = "\n".join(
                    ln for ln in brief.splitlines() if interlude_note not in ln)
            lv_store.write_plan(root, plan)
        return web.json_response({"index": idx, "mute_vocals": row["mute_vocals"]})

    @routes.post("/elv/project/{project_id}/reveal-final")
    @endpoint
    async def reveal_final(request):
        """在资源管理器中打开并定位成片文件。"""
        import subprocess
        from pathlib import Path
        root, pid = lv_store.projects_root(), request.match_info["project_id"]
        plan = lv_store.read_plan(root, pid)
        if not plan.get("final_video"):
            raise ValueError("尚未合成成片。")
        final = Path(plan["final_video"])
        if not final.is_absolute():
            final = lv_store.final_root() / final.name
        if not final.is_file():
            raise FileNotFoundError(str(final))
        target = str(final)
        if subprocess.os.name == "nt":
            subprocess.Popen(["explorer.exe", "/select,", target],
                             creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            subprocess.Popen(["xdg-open", str(final.parent)])
        return web.json_response({"path": target})

    # ------------------------------------------------------------ 合成与成片

    @routes.post("/elv/project/{project_id}/assemble")
    @endpoint
    async def assemble_only(request):
        root, pid = lv_store.projects_root(), request.match_info["project_id"]
        with lv_store.LOCK:
            if pid in lv_controller.TASKS:
                raise ValueError("仍有生成任务运行中。")
            plan = lv_store.read_plan(root, pid)
            plan["run_status"] = "merging"
            lv_store.write_plan(root, plan)
        try:
            final = await asyncio.to_thread(
                lv_ffmpeg.assemble, root, pid, lv_store.read_plan, lv_store.project_dir)
        except lv_ffmpeg.FFmpegNotFound as exc:
            with lv_store.LOCK:
                plan = lv_store.read_plan(root, pid)
                plan["run_status"] = "completed_no_ffmpeg"
                plan["error"] = str(exc)
                lv_ffmpeg.manual_hints(lv_store.project_dir(root, pid), plan)
                lv_store.write_plan(root, plan)
            return web.json_response({"error": str(exc)}, status=400)
        except Exception:
            with lv_store.LOCK:
                plan = lv_store.read_plan(root, pid)
                plan["run_status"] = "failed"
                lv_store.write_plan(root, plan)
            raise
        with lv_store.LOCK:
            plan = lv_store.read_plan(root, pid)
            plan.update(run_status="completed", final_video=final, error="")
            lv_store.write_plan(root, plan)
        return web.json_response(plan)

    @routes.get("/elv/project/{project_id}/final")
    @endpoint
    async def final(request):
        from pathlib import Path
        root, pid = lv_store.projects_root(), request.match_info["project_id"]
        plan = lv_store.read_plan(root, pid)
        if not plan.get("final_video"):
            raise ValueError("尚未合成成片。")
        final = Path(plan["final_video"])
        if not final.is_absolute():
            final = lv_store.final_root() / final.name
        if not final.is_file():
            raise FileNotFoundError(str(final))
        return web.FileResponse(final, headers={"Cache-Control": "no-store"})

    print("[EasyLongVideo] API 路由已注册（/elv/*）")
