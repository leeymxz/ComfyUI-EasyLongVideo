# -*- coding: utf-8 -*-
"""镜头简报 → 视频提示词（纯规则转换，零依赖、不调用 LLM）。

把 EasyLVUnified 输出的中文镜头简报（模式/镜头方案/表演节奏三行协议）
转换为视频模型友好的结构化提示词。默认输出英文（H3/Wan/Hunyuan 等模型
对英文提示词响应更稳定），也可输出整理后的中文版。

因为简报由 lv_camera 固定模板生成，关键词解析是确定性的：
- 景别：近景 / 中近景 / 中景 / 全身景
- 机位角：正面 / 正面左前侧约30度 / 正面右前侧约30度
- 运镜：固定机位 / 横移 / 环绕 / 推进至 / 拉远至 / 构图微调
- 表演：克制的音乐表演 / 自然且受控 / 投入而受控 / 自然口播
"""
import re

MODE_EN = {
    "singing": "singing a song live on stage",
    "speaking": "talking directly to the camera in a natural spoken delivery",
}
MODE_KEY = {"唱歌": "singing", "口播": "speaking"}

SIZE_EN = {
    "近景": "a close-up framing (head and shoulders)",
    "中近景": "a medium close-up framing (chest-up)",
    "中景": "a medium shot framing (waist-up)",
    "全身景": "a full-shot framing (the complete figure from head to feet inside the frame)",
}

ANGLE_EN = {
    "正面左前侧约30度": "at a front-left three-quarter camera angle (about 30 degrees)",
    "正面右前侧约30度": "at a front-right three-quarter camera angle (about 30 degrees)",
    "正面": "facing the camera frontally",
}

PERF_EN = {
    "restrained": ("restrained, music-driven performance: small head, shoulder and "
                   "free-hand movements; keep the body relaxed and natural"),
    "natural": ("natural, controlled music-driven performance: moderate head, shoulder "
                "and free-hand movements; stay believable and continuous"),
    "energetic": ("engaged but controlled music-driven performance: clearly visible head, "
                  "shoulder and free-hand movements that follow the music"),
    "spoken": ("a natural spoken delivery with accurate lip sync to the audio, natural "
               "blinking and breathing, and small restrained gestures"),
}
PERF_KEY = [("克制的音乐表演", "restrained"), ("投入而受控", "energetic"),
            ("自然且受控", "natural"), ("自然口播", "spoken")]

_AUDIO_EN = {
    "singing": ("Audio 1 is the singing reference for Subject 1; the on-screen mouth "
                "movement must stay aligned with the audio at every frame."),
    "speaking": ("Audio 1 is the speech reference for Subject 1; the on-screen mouth "
                 "movement must stay aligned with the audio at every frame."),
}

_CONSTRAINTS = (
    "Keep the performer's identity, face, hairstyle, outfit and its original colors "
    "consistent with the reference pictures; keep the environment continuous with the "
    "reference scene. Do not add new subtitles, captions, lyrics, watermarks or any "
    "text overlays. Do not switch to a different person or a different scene."
)


def parse_brief(text):
    """解析中文简报 → 结构化字段；无法识别的字段为 None。"""
    text = str(text or "").strip()
    result = {"mode": None, "camera": "", "performance": "",
              "start_framing": None, "end_framing": None,
              "start_angle": None, "end_angle": None,
              "move": None, "move_dir": None}
    mode = re.search(r"模式：\s*(唱歌|口播)", text)
    if mode:
        result["mode"] = MODE_KEY.get(mode.group(1))
    camera_line = re.search(r"镜头方案：(.+)", text)
    result["camera"] = camera_line.group(1).strip() if camera_line else ""
    perf_line = re.search(r"表演节奏：(.+)", text)
    result["performance"] = perf_line.group(1).strip() if perf_line else ""

    camera = result["camera"]
    for zh, key in SIZE_EN.items():
        if zh in camera:
            if result["start_framing"] is None:
                result["start_framing"] = zh
            # 结束景别：优先"结束于/到达"之后的第二个景别词
            tail = re.split(r"结束于|恰好到达", camera)
            if len(tail) > 1 and zh in camera:
                if zh in tail[1]:
                    result["end_framing"] = zh
                elif result["end_framing"] is None:
                    result["end_framing"] = zh
    if result["end_framing"] is None:
        result["end_framing"] = result["start_framing"]

    angles = [a for a in ANGLE_EN if a in camera]
    if angles:
        result["start_angle"] = angles[0]
        tail = re.split(r"结束于|恰好到达", camera)
        result["end_angle"] = angles[-1] if len(tail) > 1 and angles[-1] in tail[1] \
            else angles[0]

    if "固定机位" in camera:
        result["move"], result["move_dir"] = "steady", None
    elif "环绕" in camera:
        result["move"] = "arc"
        result["move_dir"] = "left" if "左侧" in camera else "right"
    elif "推进至" in camera or "前移" in camera:
        result["move"], result["move_dir"] = "dolly_in", None
    elif "拉远至" in camera or "后移" in camera:
        result["move"], result["move_dir"] = "dolly_out", None
    elif "构图微调" in camera:
        result["move"], result["move_dir"] = "micro", None
    elif "横移" in camera:
        result["move"] = "lateral"
        result["move_dir"] = "left" if "左侧" in camera else "right"

    for keyword, key in PERF_KEY:
        if result["performance"].startswith(keyword):
            result["perf_kind"] = key
            break
    else:
        result["perf_kind"] = "spoken" if result["mode"] == "speaking" else "natural"
    return result


def _move_en(parsed):
    size = SIZE_EN.get(parsed["end_framing"], "the established framing")
    move, direction = parsed["move"], parsed["move_dir"]
    if parsed["mode"] == "speaking" or move == "steady":
        return ("a locked-off camera: the framing, subject placement and crop stay "
                "stable for the whole segment, like a single continuous tripod shot")
    if move == "lateral":
        side = "left" if direction == "left" else "right"
        return (f"a smooth physical camera truck (lateral tracking move) to the {side}, "
                f"keeping {size}; background landmarks shift with clear parallax relative "
                "to the performer")
    if move == "arc":
        side = "left" if direction == "left" else "right"
        return (f"a shallow physical arc around the performer toward the {side} (about "
                f"20 degrees), keeping {size} while revealing coherent background parallax")
    if move == "dolly_in":
        return (f"a smooth physical dolly in, gradually tightening from the opening "
                f"framing toward {size}, with readable subject-scale change and parallax")
    if move == "dolly_out":
        return (f"a smooth physical dolly out, gradually widening from the opening "
                f"framing toward {size}, progressively revealing more of the environment")
    if move == "micro":
        return ("a barely perceptible breathing reframe around the established "
                "composition; the performer stays readable within the same environment")
    return "a steady camera with only natural handheld-free micro movement"


def _performance_en(parsed):
    return PERF_EN.get(parsed.get("perf_kind") or "natural", PERF_EN["natural"])


def to_prompt(brief, reference_notes="", language="english"):
    """主入口：简报 (+可选参考图说明) → 结构化提示词文本。"""
    parsed = parse_brief(brief)
    mode = parsed["mode"] or "singing"
    if language == "chinese":
        action = "唱歌" if mode == "singing" else "说话"
        parts = [f"主体：画面中的表演者正在{action}，"
                 "保持与参考图一致的人物身份、服装与场景。"]
        parts.append(f"镜头：{parsed['camera'] or '固定机位，构图保持稳定'}")
        parts.append(f"表演：{parsed['performance'] or '自然表演，口型跟随音频'}")
        parts.append("音频：Audio 1 是该人物的歌声/说话参考，口型必须与音频逐帧对齐。")
        parts.append("约束：不新增字幕、歌词、水印或任何文字；不切换人物或场景；"
                     "与前后段落保持场景与服装连续。")
        if reference_notes:
            parts.append(f"参考图说明：{str(reference_notes).strip()}")
        return "\n".join(parts)

    lines = []
    if reference_notes:
        lines.append(f"Reference: {str(reference_notes).strip()}")
    lines.append(
        f"Summary: a single continuous live-action shot of the performer {MODE_EN[mode]}, "
        "matching the reference pictures in identity, outfit and environment.")
    start_f = SIZE_EN.get(parsed["start_framing"], "a medium close-up framing (chest-up)")
    end_f = SIZE_EN.get(parsed["end_framing"], start_f)
    start_a = ANGLE_EN.get(parsed["start_angle"], ANGLE_EN["正面"])
    end_a = ANGLE_EN.get(parsed["end_angle"], start_a)
    lines.append(f"Camera: {_move_en(parsed)}")
    if parsed["move"] in {"dolly_in", "dolly_out"} and start_f != end_f:
        lines.append(f"Framing: starts {start_f} {start_a}, and the shot ends exactly "
                     f"at {end_f} {end_a}; the framing stays between the two extremes.")
    else:
        lines.append(f"Framing: starts {start_f} {start_a} and ends {end_f} {end_a}.")
    lines.append(f"Performance: {_performance_en(parsed)}")
    lines.append(f"Audio: {_AUDIO_EN[mode]}")
    lines.append(f"Constraints: {_CONSTRAINTS}")
    return "\n".join(lines)
