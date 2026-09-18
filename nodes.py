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
import json
import time
import uuid

import numpy as np
import torch

from . import lv_asr, lv_camera, lv_ffmpeg, lv_prompt, lv_segment, lv_separate, lv_store
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


def _segment_audio(directory, row, sr, fps):
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
    return outputs[0], outputs[1]


class EasyLVUnified:
    """长视频一体化节点：分析 → 面板确认 → 逐段输出。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "audio": ("AUDIO",),
                "mode": (["singing", "speaking"],),
                "target_seconds": ("FLOAT", {"default": 11.0, "min": 3.0, "max": 20.0,
                                             "step": 0.1}),
                "max_seconds": ("FLOAT", {"default": 15.0, "min": 3.0, "max": 60.0,
                                          "step": 0.1}),
                "fps": (["24", "25", "30", "16"],),
                "frame_align": (["h3", "none"],),
                "asr_mode": (["auto", "off"],),
                "asr_model": (lv_asr.MODEL_CHOICES,),
                "asr_device": (["auto", "cuda", "cpu"],),
                "camera_activity": (["auto", "moderate", "dynamic", "steady"],),
                "widest_framing": (["medium close-up", "medium shot", "full shot",
                                    "close-up"],),
                "project_id": ("STRING", {"default": ""}),
                "segment_index": ("INT", {"default": 0, "min": 0, "max": 10000}),
            },
            "optional": {
                "vocals": ("AUDIO",),
                "voice_separation": (["auto", "off"],),
            },
        }

    RETURN_TYPES = ("AUDIO", "AUDIO", "STRING", "INT", "STRING")
    RETURN_NAMES = ("original_audio_padded", "vocals_padded", "segment_brief",
                    "generation_frames", "filename_prefix")
    FUNCTION = "run"
    CATEGORY = "长视频/EasyLongVideo"
    OUTPUT_NODE = True

    @classmethod
    def IS_CHANGED(cls, project_id="", **kwargs):
        if str(project_id).strip():
            try:
                plan = lv_store.read_plan(lv_store.projects_root(), project_id)
                if plan.get("approved"):
                    return lv_store.fingerprint(plan)
            except Exception:
                pass
        return float("nan")

    # ------------------------------------------------------------ 分析

    def _analyze(self, audio, mode, target_seconds, max_seconds, fps, frame_align,
                 asr_mode, asr_model, asr_device, camera_activity, widest_framing,
                 vocals, voice_separation="auto"):
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
        if separation_used is None and mode == "singing" and voice_separation != "off":
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
        if asr_mode != "off":
            if lv_asr.is_available():
                try:
                    transcript = lv_asr.transcribe(
                        lv_store.audio_file(directory, "vocals.wav"),
                        lv_store.state_file(directory, "transcript.json"),
                        model_root=lv_store.models_root() / "asr",
                        model=asr_model, device=asr_device,
                        log_path=lv_store.state_file(directory, "asr.log"),
                        interrupt_check=lv_asr.interrupt_guard())
                except Exception as exc:  # ASR 失败不阻塞，降级为纯声学
                    warnings.append(f"语音识别未完成，已降级为纯声学分段：{exc}")
                    _interrupt_check()
            else:
                warnings.append("未安装 faster-whisper，已使用纯声学分段。"
                                "可选安装后无需其他改动即可自动启用。")

        rows, analysis = lv_segment.segmentation(
            voice, sr, transcript, maximum=float(max_seconds),
            target=float(target_seconds), mix_audio=mix, return_analysis=True)

        fps_int = int(fps)
        seed = project_id[:12]
        user_rules = lv_camera.load_rules(lv_store.rules_path())
        states = lv_camera.camera_sequence(mode, rows, activity=camera_activity,
                                           widest=widest_framing, seed=seed,
                                           rules=user_rules)
        for row, state in zip(rows, states):
            seconds = (row["end_sample"] - row["start_sample"]) / sr
            row["edit_frames"] = lv_segment.edit_frames_for(seconds, fps_int)
            row["generation_frames"] = lv_segment.align_frames(
                seconds, fps_int, frame_align, row["edit_frames"])
            row["brief"] = lv_camera.segment_brief(mode, state)
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

        plan = {
            "id": project_id, "schema": lv_store.SCHEMA,
            "revision": 1, "created": time.time(),
            "mode": mode, "fps": fps_int, "frame_align": frame_align,
            "max_seconds": float(max_seconds), "target_seconds": float(target_seconds),
            "sample_rate": sr, "samples": int(len(mix)),
            "duration": len(mix) / sr,
            "asr": {"available": lv_asr.is_available(),
                    "used": transcript is not None,
                    "model": lv_asr.resolve_model(asr_model) if transcript else None},
            "separation": separation_note or "未分离（原曲即人声）",
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
                                        int(plan.get("fps") or 24))
        prefix = f"EasyLongVideo/projects/{project_id}/takes/seg_{int(segment_index):04d}"
        return (_torch_audio(source, int(plan["sample_rate"])),
                _torch_audio(vocals, int(plan["sample_rate"])),
                row.get("brief", ""), int(row["generation_frames"]), prefix)

    # ------------------------------------------------------------ 入口

    def run(self, audio, mode, target_seconds, max_seconds, fps, frame_align,
            asr_mode, asr_model, asr_device, camera_activity, widest_framing,
            project_id="", segment_index=0, vocals=None, voice_separation="auto"):
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
                audio, mode, target_seconds, max_seconds, fps, frame_align,
                asr_mode, asr_model, asr_device, camera_activity, widest_framing,
                vocals, voice_separation=voice_separation)
        except lv_ffmpeg.FFmpegNotFound:  # 理论上分析阶段不会触发；防御性兜底
            raise
        note = f"分析完成：共 {count} 段。已弹出分段审核面板，请试听并确认后开始生成。"
        if warnings:
            note += " 注意：" + "；".join(warnings)
        return {"ui": {"elv_project": [project_id], "text": [note]},
                "result": (audio, vocals if vocals is not None else audio, "",
                           0, "")}


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
                "language": (["english", "chinese"],),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("video_prompt",)
    FUNCTION = "convert"
    CATEGORY = "长视频/EasyLongVideo"

    def convert(self, segment_brief, reference_notes="", language="english"):
        text = lv_prompt.to_prompt(segment_brief, reference_notes=reference_notes,
                                   language=language)
        return (text,)


NODE_CLASS_MAPPINGS = {"EasyLVUnified": EasyLVUnified,
                       "EasyLVBriefToPrompt": EasyLVBriefToPrompt}
NODE_DISPLAY_NAME_MAPPINGS = {"EasyLVUnified": "长视频 · 音频分析与顺序生成",
                              "EasyLVBriefToPrompt": "长视频 · 简报转视频提示词"}
