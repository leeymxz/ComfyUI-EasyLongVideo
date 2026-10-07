# -*- coding: utf-8 -*-
"""ComfyUI 节点定义。

EasyLVUnified（一体化节点）：
- project_id 为空时：执行音频分析（分段 + 镜头简报），并把 project_id
  通过 UI 消息推给前端面板；
- project_id 非空且方案已确认时：加载指定分段（供顺序生成控制器注入运行），
  输出补齐后的音频、镜头简报文本、生成帧数与文件名前缀。

输出协议（与常见"参考图/音频 → 视频"工作流兼容，不绑定特定模型）：
    original_audio_padded : AUDIO   本段原声（补齐到整帧）
    vocals_padded         : AUDIO   本段人声（未提供时等于原声）
    segment_brief         : STRING  中文镜头简报（可再经提示词扩写节点加工）
    generation_frames     : INT     建议生成帧数
    filename_prefix       : STRING  建议传给视频输出节点的 filename_prefix
"""
import hashlib
import json
import time
import uuid

import numpy as np
import torch

from . import (lv_abc, lv_asr, lv_camera, lv_ffmpeg, lv_prompt,
               lv_segment, lv_separate, lv_store)
from .lv_audio import audio_tensor_to_numpy, read_wav, write_wav


def _torch_audio(array_2d, sr):
    return {"waveform": torch.from_numpy(np.ascontiguousarray(array_2d.T.copy()))
            .float().unsqueeze(0), "sample_rate": int(sr)}


def _interrupt_check():
    """ComfyUI 中断检查；缺失该环境时静默跳过（保证核心可独立测试）。"""
    try:
        import comfy.model_management as mm
        mm.throw_exception_if_processing_interrupted()
    except ImportError:
        pass


def _apply_vocal_gate(vocals, sr, ref_rms, scale=0.22):
    """间奏/泄漏压制：人声块 RMS 低于演唱参考值 × scale 时渐变静音。

    Demucs 分离的间奏段会残留和声/混响/呼吸声，H3 会把它们当人声驱动
    口型（人物在间奏开口）。加载段音频时在输出端做平滑能量门限，
    让无演唱部分近似无声。ref_rms 为分析时统计的演唱中位 RMS；
    scale 可由面板调节（越小越严格，默认 0.22）。
    """
    mono = vocals.mean(axis=1)
    hop = max(1, int(sr * 0.05))
    n = len(mono) // hop
    if n < 4:
        return vocals
    rms = np.sqrt((mono[:n * hop].reshape(n, hop) ** 2).mean(axis=1))
    thr = max(float(ref_rms) * float(scale), 0.004)
    mask = (rms > thr).astype(np.float32)
    kernel = np.ones(7) / 7.0
    mask = np.convolve(np.pad(mask, 3, mode="edge"), kernel, mode="valid")[:n]
    gain = np.repeat(np.clip(mask, 0.0, 1.0), hop)[: len(vocals)]
    return (vocals * gain[:, None]).astype(np.float32)


def _load_image_tensor(name):
    """从 ComfyUI input 目录加载图片为 IMAGE 张量（[1, H, W, 3]）。"""
    import folder_paths
    from PIL import Image as _Image
    path = folder_paths.get_annotated_filepath(str(name))
    with _Image.open(path) as img:
        arr = np.array(img.convert("RGB")).astype(np.float32) / 255.0
    return torch.from_numpy(arr)[None,]


def _segment_images(plan, row, count=6):
    """当前段参考图文件名列表（段自定义优先，其次项目默认图），最多 count 张。"""
    files = list(row.get("images") or []) or list(plan.get("default_images") or [])
    return files[:count]


def _segment_audio(directory, row, sr, fps, plan=None):
    """读取某段音频并补齐到整帧采样数，返回 (source, vocals)。"""
    target = lv_segment.padded_samples(row["generation_frames"], sr, fps)
    outputs = []
    for name in ("source.wav", "vocals.wav"):
        try:
            audio, rate = read_wav(lv_store.audio_file(directory, name),
                                   start=row["start_sample"], stop=row["end_sample"])
        except (FileNotFoundError, OSError):
            audio, rate = read_wav(lv_store.audio_file(directory, "source.wav"),
                                   start=row["start_sample"], stop=row["end_sample"])
        if rate != sr:
            raise ValueError("项目音频采样率异常，请重新分析。")
        if len(audio) < target:
            audio = np.pad(audio, ((0, target - len(audio)), (0, 0)))
        outputs.append(audio)
    source, vocals = outputs[0], outputs[1]
    # 用户标记"本段无人声"：人声驱动输出全零（间奏/和声段人物强制不开口）
    if row.get("mute_vocals"):
        vocals = np.zeros_like(vocals)
        return source, vocals
    # 唱歌模式：间奏泄漏压制（人声轨中弱于演唱水平的部分渐变静音）
    if plan is not None and plan.get("mode") == "singing" \
            and plan.get("vocals_gate", True) and plan.get("vocals_rms_p50"):
        vocals = _apply_vocal_gate(vocals, sr, float(plan["vocals_rms_p50"]),
                                   scale=plan.get("vocals_gate_thr_scale", 0.22))
    return source, vocals


class EasyLVUnified:
    """长视频一体化节点：分析 → 面板确认 → 逐段输出。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "audio": ("AUDIO",),
                "mode": (["singing", "speaking"],),
                "max_seconds": ("FLOAT", {"default": 15, "min": 5, "max": 15,
                                          "step": 0.1}),
                "target_seconds": ("FLOAT", {"default": 11, "min": 5, "max": 15,
                                             "step": 0.1}),
                "asr_python": ("STRING", {"default": ""}),
                "asr_model": ("STRING", {"default": ""}),
                "asr_device": (["auto", "cuda", "cpu"],),
                "director_mode": ("STRING", {"default": "本地规则"}),
                "project_id": ("STRING", {"default": ""}),
                "segment_index": ("INT", {"default": 0, "min": 0, "max": 10000}),
            },
            "optional": {
                "vocals": ("AUDIO",),
                "abc_text": ("STRING", {"multiline": True, "default": "",
                    "tooltip": "可选 ABC 谱文本：解析段落结构（Intro/Interlude/Verse 等），"
                               "增强间奏判定并给简报注入音乐上下文。"}),
            },
        }

    RETURN_TYPES = (("AUDIO", "AUDIO", "INT", "STRING", "H3LV_MATERIAL")
                    + ("IMAGE",) * 6 + ("STRING", "FLOAT"))
    RETURN_NAMES = (("original_audio_padded", "vocals_padded", "generation_frames",
                     "filename_prefix", "segment_material")
                    + tuple(f"image_{i+1}" for i in range(6))
                    + ("segment_prompt", "fps"))
    FUNCTION = "run"
    CATEGORY = "像素幻想/EasyLongVideo"
    OUTPUT_NODE = True

    @classmethod
    def IS_CHANGED(cls, project_id="", **kwargs):
        if str(project_id).strip():
            try:
                plan = lv_store.read_plan(lv_store.projects_root(), project_id)
                if plan.get("approved"):
                    base = lv_store.fingerprint(plan)
                    extra = json.dumps(
                        [[r.get("mute_vocals"), r.get("images"),
                          r.get("visual_type")] for r in plan.get("segments", [])],
                        ensure_ascii=False)
                    return base + hashlib.md5(extra.encode("utf-8")).hexdigest()
            except Exception:
                pass
        return float("nan")

    # ------------------------------------------------------------ 分析

    # 与原版对齐的固定值（fps 恒 24 / 帧对齐恒 h3 / ASR 恒自动启用）
    FPS_INT = 24
    FRAME_ALIGN = "h3"

    def _analyze(self, audio, mode, max_seconds, target_seconds, asr_python,
                 asr_model, asr_device, director_mode, vocals=None, abc_text=""):
        mix, sr = audio_tensor_to_numpy(audio)
        if float(max_seconds) < float(target_seconds):
            raise ValueError("最长时长不能小于目标时长。")
        separation_used = None
        if vocals is not None:
            voice, vsr = audio_tensor_to_numpy(vocals)
            if vsr != sr or len(voice) != len(mix):
                raise ValueError("人声与原声必须采样率一致且长度相同；"
                                 "请提供同一版本的未裁切音频。")
            separation_used = "external"
        else:
            voice = mix.copy()

        project_id = uuid.uuid4().hex
        root = lv_store.projects_root()
        directory = lv_store.project_dir(root, project_id)
        lv_store.init_project_dirs(directory)
        write_wav(lv_store.audio_file(directory, "source.wav"), mix, sr)

        warnings = []
        if separation_used is None and mode == "singing":
            # 唱歌模式自动分离人声：口型/表演驱动必须用干净人声，
            # 否则人物会跟着鼓点贝斯乱开口；成片音轨仍用原曲。
            if lv_separate.is_available():
                try:
                    separated = lv_separate.separate_vocals(
                        mix, sr, lv_store.models_root() / "hub",
                        device=asr_device,
                        interrupt_check=_interrupt_check)
                except Exception as exc:
                    separated = None
                    warnings.append(f"人声分离异常：{exc}")
                if separated is not None and np.isfinite(separated).all() and \
                        float(np.abs(separated).max()) > 1e-4:
                    voice = separated
                    separation_used = "hdemucs"
                else:
                    warnings.append("人声分离未完成（首次运行需下载约319MB模型），"
                                    "本次已用原曲做人声分析，切点请试听。")
            else:
                warnings.append("当前环境缺少 torchaudio，无法自动分离人声，"
                                "已用原曲做人声分析。")
        write_wav(lv_store.audio_file(directory, "vocals.wav"), voice, sr)
        separation_note = {"external": "外部输入", "hdemucs": "HTDemucs 自动分离",
                           None: "未分离（原曲即人声）"}.get(separation_used)
        transcript = None
        if lv_asr.is_available():  # ASR 恒自动启用（与原版一致）
            try:
                transcript = lv_asr.transcribe(
                    lv_store.audio_file(directory, "vocals.wav"),
                    lv_store.state_file(directory, "transcript.json"),
                    model_root=lv_store.models_root() / "asr",
                    model=asr_model, device=asr_device,
                    log_path=lv_store.state_file(directory, "asr.log"),
                    interrupt_check=lv_asr.interrupt_guard(),
                    python_path=str(asr_python or "").strip() or None)
            except Exception as exc:  # ASR 失败不阻塞，降级为纯声学
                warnings.append(f"语音识别未完成，已降级为纯声学分段：{exc}")
                _interrupt_check()
        else:
            warnings.append("未安装 faster-whisper，已使用纯声学分段。"
                                "可选安装后无需其他改动即可自动启用。")

        rows, analysis = lv_segment.segmentation(
            voice, sr, transcript, maximum=float(max_seconds),
            target=float(target_seconds), mix_audio=mix, return_analysis=True)

        # ---- ABC 谱（可选）：解析段落结构，增强间奏判定 ----
        abc_info = None
        if str(abc_text or "").strip():
            abc_info = lv_abc.parse_abc(str(abc_text))
            if abc_info:
                warnings.append("已加载 ABC 谱：{} 个小节 / {} 个乐段（结构感知增强分段）。"
                                .format(abc_info["bar_count"], len(abc_info["structure"])))
            else:
                warnings.append("ABC 谱解析失败，已使用纯声学分段。")
        duration_s = len(mix) / sr
        abc_sections = []
        if abc_info:
            for s in abc_info["structure"]:
                if s["kind"] in ("intro", "interlude", "outro"):
                    abc_sections.append({
                        "start": (s["bar_start"] - 1) / abc_info["bar_count"] * duration_s,
                        "end": s["bar_end"] / abc_info["bar_count"] * duration_s,
                        "kind": s["kind"], "zh": s["zh"], "label": s["label"]})

        # 音频角色标注（吸收原版 7c1241f 思路）：段中点落在无人声区 → 间奏段
        # 自动施加"静音驱动 + 氛围表演"双保险；其余段按识别文字标注 vocal/待确认
        sections = abc_sections or analysis.get("sections", [])
        for row in rows:
            mid = (row["start_sample"] + row["end_sample"]) / 2 / sr
            hit = next((s for s in sections
                        if float(s["start"]) <= mid <= float(s["end"])), None)
            if hit:
                row["audio_role"] = "instrumental"
                row["audio_section"] = str(hit.get("kind", "interlude"))
                row["audio_role_reason"] = (
                    f"段中点位于{hit.get('kind')}（{hit.get('start')}s-{hit.get('end')}s）")
                row["mute_vocals"] = True
                row["visual_type"] = "performance"  # 间奏默认氛围表演（不张嘴），可手动改回
            else:
                row["audio_role"] = "vocal" if row.get("text") else "uncertain"
                row["audio_section"] = ""
                row["audio_role_reason"] = ("含识别人声" if row.get("text")
                                            else "未识别出文字，人声状态需试听确认")
                row["mute_vocals"] = False

        fps_int = self.FPS_INT
        seed = project_id[:12]
        user_rules = lv_camera.load_rules(lv_store.rules_path())
        camera_activity = user_rules.get("singing", {}).get("activity", "auto")
        widest_framing = user_rules.get("singing", {}).get("widest_framing",
                                                           "medium close-up")
        states = lv_camera.camera_sequence(mode, rows, activity=camera_activity,
                                           widest=widest_framing, seed=seed,
                                           rules=user_rules)
        for row, state in zip(rows, states):
            seconds = (row["end_sample"] - row["start_sample"]) / sr
            row["edit_frames"] = lv_segment.edit_frames_for(seconds, fps_int)
            row["generation_frames"] = lv_segment.align_frames(
                seconds, fps_int, self.FRAME_ALIGN, row["edit_frames"])
            row.setdefault("visual_type", "performer")
            row.setdefault("images", [])
            row["brief"] = lv_camera.segment_brief(
                mode, state, row.get("visual_type", "performer"))
            if row.get("audio_role") == "instrumental":
                note = lv_camera.SEGMENT_INTERLUDE_NOTE
                if note not in row["brief"]:
                    row["brief"] = (row["brief"].rstrip() + "\n" + note)[:8000]
                row["brief_edited"] = True
            # ABC 音乐上下文：段中点落在的 ABC 乐段 → 注入结构/旋律信息
            if abc_info:
                mid_t = (row["start_sample"] + row["end_sample"]) / 2 / sr
                for s in abc_info["structure"]:
                    a = (s["bar_start"] - 1) / abc_info["bar_count"] * duration_s
                    b = s["bar_end"] / abc_info["bar_count"] * duration_s
                    if a <= mid_t <= b:
                        feat = s["features"]
                        ctx = ("音乐上下文：此段为{}（{}），小节 {}-{}，音符密度 {}"
                               .format(s["zh"], s["label"] or s["kind"],
                                       s["bar_start"], s["bar_end"], feat["note_count"])
                               + (f"，音高跨度 {feat['pitch_span']} 半音"
                                  if feat["pitch_span"] else ""))
                        if ctx not in row["brief"]:
                            row["brief"] = (row["brief"].rstrip() + "\n" + ctx)[:8000]
                        row["brief_edited"] = True
                        break
            row["brief_default"] = row["brief"]  # 供面板"恢复默认简报"
            row.update({k: state[k] for k in
                        ("start_framing", "end_framing", "start_angle", "end_angle",
                         "move_family", "move_direction", "move_type", "band")})
            row["job"] = {"status": "pending"}
            row["takes"] = []

        # 覆盖完整性自检：分段必须首尾相接铺满整条音频
        cursor = 0
        for row in rows:
            if row["start_sample"] != cursor:
                raise AssertionError("分段覆盖检查失败（存在空洞）。")
            cursor = row["end_sample"]
        if cursor != len(mix):
            raise AssertionError("分段覆盖检查失败（未覆盖到结尾）。")

        # 演唱水平参考（间奏门限阈值用）：人声逐块 RMS 的中位数
        v_hop = max(1, int(sr * 0.05))
        v_blocks = len(voice) // v_hop
        if v_blocks > 0:
            v_mono = voice.mean(axis=1)
            v_rms = np.sqrt((v_mono[:v_blocks * v_hop].reshape(v_blocks, v_hop) ** 2).mean(axis=1))
            vocals_rms_p50 = float(np.percentile(v_rms, 50))
        else:
            vocals_rms_p50 = 0.0

        plan = {
            "id": project_id, "schema": lv_store.SCHEMA,
            "revision": 1, "created": time.time(),
            "mode": mode, "fps": fps_int, "frame_align": self.FRAME_ALIGN,
            "max_seconds": float(max_seconds), "target_seconds": float(target_seconds),
            "sample_rate": sr, "samples": int(len(mix)),
            "duration": len(mix) / sr,
            "bpm": lv_segment.estimate_bpm(voice, sr),
            "abc_structure": ([{"kind": s["kind"], "zh": s["zh"],
                                "label": s["label"] or "",
                                "bar_start": s["bar_start"],
                                "bar_end": s["bar_end"]}
                               for s in abc_info["structure"]]
                              if abc_info else None),
            "asr": {"available": lv_asr.is_available(),
                    "used": transcript is not None,
                    "model": lv_asr.resolve_model(asr_model) if transcript else None},
            "separation": separation_note or "未分离（原曲即人声）",
            "vocals_gate": True,
            "vocals_rms_p50": vocals_rms_p50,
            "camera_activity": camera_activity, "widest_framing": widest_framing,
            "segments": rows,
            "approved": False, "approved_fingerprint": None,
            "run_status": "draft", "final_video": None,
            "warnings": warnings + ["切点与识别文字未经人工校对，生成前请试听各段。"],
        }
        lv_store.write_plan(root, plan)
        analysis_path = lv_store.state_file(directory, "analysis.json")
        analysis_path.write_text(json.dumps(analysis, ensure_ascii=False),
                                 encoding="utf-8")
        return project_id, len(rows), warnings

    # ------------------------------------------------------------ 输出

    def _load_segment(self, project_id, segment_index):
        import folder_paths
        root = lv_store.projects_root()
        plan = lv_store.read_plan(root, project_id)
        if not plan.get("approved"):
            raise ValueError("分段尚未确认。请先在分段审核面板点击「保存并确认」。")
        if not 0 <= int(segment_index) < len(plan["segments"]):
            raise ValueError("分段编号超出范围。")
        row = plan["segments"][int(segment_index)]
        directory = lv_store.project_dir(root, project_id)
        source, vocals = _segment_audio(directory, row, int(plan["sample_rate"]),
                                        int(plan.get("fps") or 24), plan)
        prefix = f"EasyLongVideo/projects/{project_id}/takes/seg_{int(segment_index):04d}"
        files = _segment_images(plan, row)
        images = []
        for name in files:
            try:
                images.append(_load_image_tensor(name))
            except Exception:
                images.append(None)
        images += [None] * (6 - len(images))
        material = {"images": files,
                    "default_images": plan.get("default_images", []),
                    "project_id": project_id,
                    "segment_index": int(segment_index)}
        return (_torch_audio(source, int(plan["sample_rate"])),
                _torch_audio(vocals, int(plan["sample_rate"])),
                int(row["generation_frames"]), prefix, material,
                *images, row.get("brief", ""), float(plan.get("fps") or 24))

    # ------------------------------------------------------------ 入口

    def run(self, audio, mode, max_seconds, target_seconds, asr_python, asr_model,
            asr_device, director_mode, project_id="", segment_index=0, vocals=None,
            abc_text=""):
        project_id = str(project_id or "").strip()
        if project_id:
            try:
                plan = lv_store.read_plan(lv_store.projects_root(), project_id)
                if plan.get("approved"):
                    return {"result": self._load_segment(project_id, int(segment_index))}
            except FileNotFoundError:
                pass
        try:
            project_id, count, warnings = self._analyze(
                audio, mode, max_seconds, target_seconds, asr_python,
                asr_model, asr_device, director_mode, vocals=vocals,
                abc_text=abc_text)
        except lv_ffmpeg.FFmpegNotFound:  # 理论上分析阶段不会触发；防御性兜底
            raise
        note = f"分析完成：共 {count} 段。已弹出分段审核面板，请试听并确认后开始生成。"
        if warnings:
            note += " 注意：" + "；".join(warnings)
        return {"ui": {"elv_project": [project_id], "text": [note]},
                "result": (audio, vocals if vocals is not None else audio,
                           0, "", {}, None, None, None, None, None, None,
                           "", 0.0)}


class EasyLVBriefToPrompt:
    """镜头简报 → 视频提示词（纯规则转换，零依赖、无需 LLM）。

    输入 EasyLVUnified 的 segment_brief，输出结构化提示词文本，
    可直接接到 H3 / Wan / Hunyuan 等视频模型的提示词输入。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "segment_brief": ("STRING", {"multiline": True, "default": ""}),
            },
            "optional": {
                "reference_notes": ("STRING", {"multiline": True, "default": "",
                    "tooltip": "参考图职责说明，如：Picture 1 is the performer together "
                               "with the scene; Picture 2 is the environment."}),
                "manual_prompt": ("STRING", {"multiline": True, "default": "",
                    "tooltip": "手动指定提示词内容：填了就直接用它作为输出（跳过规则转换），"
                               "适合高级用户完全自控。"}),
                "language": (["english", "chinese"],),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("video_prompt",)
    FUNCTION = "convert"
    CATEGORY = "长视频/EasyLongVideo"

    def convert(self, segment_brief, reference_notes="", language="english",
                manual_prompt=""):
        if str(manual_prompt or "").strip():
            return (str(manual_prompt).strip(),)
        text = lv_prompt.to_prompt(segment_brief, reference_notes=reference_notes,
                                   language=language)
        return (text,)


NODE_CLASS_MAPPINGS = {"EasyLVUnified": EasyLVUnified,
                       "EasyLVBriefToPrompt": EasyLVBriefToPrompt}
NODE_DISPLAY_NAME_MAPPINGS = {"EasyLVUnified": "长视频 · 音频分析与顺序生成",
                              "EasyLVBriefToPrompt": "长视频 · 简报转视频提示词"}
