# -*- coding: utf-8 -*-
"""镜头规划与中文镜头简报生成（纯 Python 规则，零依赖）。

原则（与参考实现一致）：
- 唱歌模式：按每段相对能量（low/medium/high）安排运镜，相邻段避免同类运镜、
  横移方向尽量交替、推拉在景别边界自动转为横移、绕轴角避免直接左右翻转；
- 口播模式：全程固定机位，沿用参考图构图，保持人物位置与裁切稳定；
- 所有运镜均为"物理运镜"描述，适配 H3/Wan/Hunyuan 等常见视频模型提示词习惯。
"""
import copy
import hashlib
import json
from pathlib import Path

SIZE_ORDER = ["close-up", "medium close-up", "medium shot", "full shot"]
ANGLES = ["front", "front three-quarter right", "front three-quarter left"]

VALID_MOVES = ["truck_left", "truck_right", "arc_left", "arc_right",
               "dolly_in", "dolly_out", "micro_reframe"]

# 各能量档位的默认运镜池（micro=呼吸式微调, truck=横移, arc=弧线, dolly=推拉）
MOVE_POOL = {
    "low": ["micro_reframe", "arc_left", "arc_right"],
    "medium": ["truck_left", "truck_right", "arc_left", "arc_right",
               "micro_reframe", "dolly_in", "dolly_out"],
    "high": ["truck_left", "truck_right", "dolly_in", "dolly_out"],
}

PERFORMANCE_TEXT = {
    "restrained": "克制的音乐表演，以小幅头部、肩部和空闲手动作回应音乐",
    "natural": "自然且受控的音乐表演，头部、肩部和空闲手动作适中",
    "energetic": "投入而受控的音乐表演，头部、肩部和空闲手动作清晰可见",
}

# ---------------------------------------------------------------- 用户规则
# 规则文件保存在 ComfyUI 用户目录（EasyLongVideo/camera_rules.json），
# 面板「运镜规则：查看与修改」可编辑；未保存/解析失败时使用默认值。


def default_rules():
    return {
        "schema": 1,
        "singing": {
            "move_pool": copy.deepcopy(MOVE_POOL),
            "allowed_angles": list(ANGLES),
            "no_adjacent_same_family": True,
            "alternate_lateral_direction": True,
            "avoid_direct_axis_cross": True,
        },
        "performance_text": dict(PERFORMANCE_TEXT),
    }


def validate_rules(payload):
    """校验用户提交的规则，非法字段回退默认值，返回干净规则。"""
    rules = default_rules()
    if not isinstance(payload, dict):
        return rules
    singing = payload.get("singing") or {}
    pool = singing.get("move_pool") or {}
    if isinstance(pool, dict):
        for band in ("low", "medium", "high"):
            items = pool.get(band)
            if isinstance(items, list):
                cleaned = [m for m in items if m in VALID_MOVES]
                if cleaned:
                    rules["singing"]["move_pool"][band] = cleaned
    angles = singing.get("allowed_angles")
    if isinstance(angles, list):
        cleaned = [a for a in angles if a in ANGLES]
        if cleaned:
            rules["singing"]["allowed_angles"] = cleaned
    for flag in ("no_adjacent_same_family", "alternate_lateral_direction",
                 "avoid_direct_axis_cross"):
        if flag in singing:
            rules["singing"][flag] = bool(singing[flag])
    perf = payload.get("performance_text")
    if isinstance(perf, dict):
        for key in ("restrained", "natural", "energetic"):
            value = perf.get(key)
            if isinstance(value, str) and value.strip():
                rules["performance_text"][key] = value.strip()[:300]
    return rules


def load_rules(path):
    """从磁盘读取用户规则；文件缺失或损坏时静默回退默认。"""
    p = Path(path)
    if not p.is_file():
        return default_rules()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return validate_rules(data)
    except (OSError, json.JSONDecodeError):
        return default_rules()


def save_rules(path, payload):
    rules = validate_rules(payload)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rules, ensure_ascii=False, indent=2), encoding="utf-8")
    return rules


_ZH_SIZE = {
    "close-up": "近景（头部与肩部）",
    "medium close-up": "中近景（胸部以上）",
    "medium shot": "中景（腰部以上）",
    "full shot": "全身景（头顶至双脚完整入画）",
}
_ZH_ANGLE = {
    "front": "正面",
    "front three-quarter left": "正面左前侧约30度",
    "front three-quarter right": "正面右前侧约30度",
}


def _family(value):
    if value.startswith("truck_"):
        return "lateral"
    if value.startswith("arc_"):
        return "arc"
    if value in {"dolly_in", "dolly_out"}:
        return "dolly"
    if value == "micro_reframe":
        return "micro"
    return value


def energy_bands(rows):
    """把每段的相对能量分为 low/medium/high（分位数三分）。"""
    values = [float(r.get("energy_db", -20)) for r in rows]
    if len(values) < 2 or max(values) - min(values) < 3:
        return ["medium"] * len(rows)
    low, high = np_quantile(values, 0.33), np_quantile(values, 0.67)
    return ["low" if v <= low else ("high" if v >= high else "medium") for v in values]


def np_quantile(values, q):
    import numpy as np
    return float(np.quantile(np.asarray(values, dtype=float), q))


def _pick_movement(band, activity, states, seed_key, index, singing_rules=None):
    """确定性（哈希种子）选择运镜，并施加连续性约束（可由用户规则覆盖）。"""
    sr = singing_rules or default_rules()["singing"]
    resolved = activity if activity != "auto" else {
        "low": "moderate", "medium": "moderate", "high": "dynamic"}[band]
    if resolved == "steady":
        resolved = "moderate"
    base_pool = sr["move_pool"]["high"] if (resolved == "dynamic" and band == "high") \
        else sr["move_pool"][band]
    pool = list(base_pool)
    if resolved == "dynamic" and "micro_reframe" in pool:
        pool.remove("micro_reframe")

    if states:
        if sr.get("no_adjacent_same_family", True):
            prev_family = _family(states[-1].get("move_type", ""))
            filtered = [m for m in pool if _family(m) != prev_family]
            if filtered:
                pool = filtered
        # 横移方向交替
        if sr.get("alternate_lateral_direction", True):
            last_lateral = next((s for s in reversed(states)
                                 if _family(s.get("move_type", "")) == "lateral"), None)
            if last_lateral and last_lateral.get("move_direction") in {"left", "right"}:
                opposite = "truck_right" if last_lateral["move_direction"] == "left" else "truck_left"
                pool = [m for m in pool if _family(m) != "lateral" or m == opposite]
    if not pool:
        pool = ["truck_left", "truck_right"]
    key = f"{seed_key}|{index}|{band}|{resolved}"
    return min(pool, key=lambda m: hashlib.sha256(
        f"{key}|{m}".encode("utf-8")).hexdigest()), resolved


def _move_description(move, band, activity, framing, sizes, resolved):
    """返回 (end_framing, family, direction, 中文运镜描述)。"""
    position = sizes.index(framing)
    dynamic = resolved == "dynamic"
    if move == "dolly_in" and position < len(sizes) - 1:
        end = sizes[position + 1]
        return end, "dolly in", "forward", (
            f"摄影机平稳前移，从{_ZH_SIZE[framing]}推进至{_ZH_SIZE[end]}，"
            "取景范围逐步收紧，人物身体比例与服装保持一致，背景产生自然视差")
    if move == "dolly_out" and position > 0:
        end = sizes[position - 1]
        return end, "dolly out", "backward", (
            f"摄影机平稳后移，从{_ZH_SIZE[framing]}拉远至{_ZH_SIZE[end]}，"
            "取景范围逐步扩大，人物身体比例与服装保持一致，逐步露出原有场景")
    if move == "micro_reframe":
        pace = "极轻微" if band == "low" else "克制的"
        return framing, "micro", "", (
            f"摄影机做{pace}呼吸式构图微调，人物保持清晰可读，场景空间关系连贯")
    if move in {"arc_left", "arc_right"}:
        side = "左侧" if move == "arc_left" else "右侧"
        return framing, "arc", "left" if move == "arc_left" else "right", (
            f"摄影机实体向{side}环绕人物约20度，人物可随运镜自然偏离中心，"
            "保持完整的当前景别裁切，背景产生清晰但克制的局部视差；不是原地摇镜")
    # truck
    direction = "left" if move == "truck_left" else "right"
    side = "左侧" if direction == "left" else "右侧"
    if dynamic:
        return framing, "lateral", direction, (
            f"摄影机沿水平轨道向{side}持续平稳横移约一个人物身宽的距离，"
            "人物可随运镜自然偏离中心，保持完整的当前景别裁切，"
            "背景地标与人物产生清晰、持续的横向视差，开场到结尾的构图明显改变")
    return framing, "lateral", direction, (
        f"摄影机向{side}短距离平稳横移，人物可随运镜自然偏离中心，"
        "保持完整的当前景别裁切，背景产生短暂视差")


def _start_composition(index, band, sizes, previous_end, states, seed_key,
                       angles=None, avoid_axis_cross=True):
    """选择本段开场的景别与角度：倾向与能量匹配、避免与上段结尾完全重复。"""
    angles = list(angles or ANGLES)
    target_size = (sizes[0] if band == "high"
                   else sizes[-1] if band == "low"
                   else sizes[len(sizes) // 2])
    if index == 0:
        return target_size, angles[0]
    candidates = []
    prev_angle = previous_end["angle"]
    prev_side = "right" if "right" in prev_angle else ("left" if "left" in prev_angle else "front")
    for framing in sizes:
        for angle in angles:
            if framing == previous_end["framing"] and angle == prev_angle:
                continue
            side = "right" if "right" in angle else ("left" if "left" in angle else "front")
            if avoid_axis_cross and prev_side != "front" and side != "front" and side != prev_side:
                continue  # 避免直接左右翻转（跨轴线）
            score = 0.0
            if framing != target_size:
                score += 1.5
            changes = int(framing != previous_end["framing"]) + int(angle != prev_angle)
            if changes == 1:
                score += 0.25
            if states and angle == states[-1].get("start_angle"):
                score += 0.3
            tie = hashlib.sha256(
                f"{seed_key}|comp|{index}|{framing}|{angle}".encode("utf-8")).hexdigest()
            candidates.append((score, tie, framing, angle))
    _, _, framing, angle = min(candidates)
    return framing, angle


def _cut_relationship(previous_end, framing, angle, prev_state, family):
    size_changed = framing != previous_end["framing"]
    angle_changed = angle != previous_end["angle"]
    if size_changed and angle_changed:
        strategy, reason, risk = "shot-size plus angle cut", "景别与机位同时变化", "low"
    elif size_changed:
        strategy, reason, risk = "shot-size cut", "仅景别变化，观察轴保持可读", "medium"
    elif angle_changed:
        strategy, reason, risk = "30-degree angle cut", "仅机位角变化", "review"
    else:
        strategy, reason, risk = "matched-action cut", "构图保持，依赖可见动作衔接", "review"
    if prev_state and prev_state.get("move_family") == family == "lateral" and \
            prev_state.get("move_direction"):
        reason += "；切点两侧屏幕方向的运动保持兼容"
    return strategy, reason, risk


def camera_sequence(mode, rows, activity="auto", widest="medium close-up", seed="elv",
                    rules=None):
    """为所有分段规划镜头状态序列，返回与 rows 等长的 state 列表。

    rules: 可选的用户自定义规则（validate_rules 产出的 dict 或原始 JSON dict）；
           缺省时使用内置默认规则。
    """
    singing_rules = validate_rules(rules) if rules else default_rules()
    if mode == "speaking":
        return [{
            "start_framing": "medium close-up", "end_framing": "medium close-up",
            "start_angle": "front", "end_angle": "front",
            "move_text": "固定机位，人物与背景构图保持稳定",
            "move_family": "steady", "move_direction": "", "move_type": "steady",
            "entry_cut": "opening" if i == 0 else "locked-camera continuity cut",
            "entry_risk": "low",
            "entry_motion": "静止", "exit_motion": "静止",
            "band": "medium",
            "performance": "自然口播，口型跟随音频，保留自然眨眼、呼吸和克制的小幅动作",
        } for i in range(len(rows))]

    sr = singing_rules["singing"]
    sizes = SIZE_ORDER[:SIZE_ORDER.index(widest) + 1]
    if widest == "medium close-up":
        sizes = ["medium close-up"]
    bands = energy_bands(rows)
    states = []
    previous_end = {"framing": "medium close-up", "angle": "front"}
    for index, row in enumerate(rows):
        band = bands[index]
        framing, angle = _start_composition(
            index, band, sizes, previous_end, states, seed,
            angles=sr["allowed_angles"], avoid_axis_cross=sr.get("avoid_direct_axis_cross", True))
        move, resolved = _pick_movement(band, activity, states, seed, index, sr)
        # 推拉越界时降级为横移
        position = sizes.index(framing)
        if move == "dolly_in" and position >= len(sizes) - 1:
            move = "truck_right"
        if move == "dolly_out" and position <= 0:
            move = "truck_left"
        ending, family, direction, move_text = _move_description(
            move, band, activity, framing, sizes, resolved)
        if family == "arc":
            order = [a for a in ["front three-quarter left", "front",
                                 "front three-quarter right"] if a in sr["allowed_angles"]]
            order = order or list(sr["allowed_angles"])
            pos = order.index(angle) if angle in order else 0
            step = -1 if direction == "left" else 1
            ending_angle = order[max(0, min(len(order) - 1, pos + step))]
        else:
            ending_angle = angle
        if index == 0:
            strategy, risk = "opening", "low"
        else:
            strategy, _, risk = _cut_relationship(
                previous_end, framing, angle, states[-1] if states else None, family)
        perf_pref = {"low": "restrained", "medium": "natural", "high": "energetic"}[band]
        states.append({
            "start_framing": framing, "end_framing": ending,
            "start_angle": angle, "end_angle": ending_angle,
            "move_text": move_text, "move_family": family,
            "move_direction": direction, "move_type": move,
            "entry_cut": strategy, "entry_risk": risk,
            "entry_motion": ("静止" if family in {"steady", "micro"}
                             else "运镜已自然进行"),
            "exit_motion": ("静止" if family in {"steady", "micro"} else "运镜保持进行"),
            "band": band,
            "performance": singing_rules["performance_text"][perf_pref],
            "activity": resolved,
        })
        previous_end = {"framing": ending, "angle": ending_angle}
    return states


def segment_brief(mode, state):
    """生成单段中文镜头简报（与参考实现的文本协议兼容）。"""
    if mode == "speaking":
        camera = ("沿用参考画面的原有构图，保持人物位置、人物尺度、身体可见范围、"
                  "头顶留白和裁切边界；全程固定机位单一连续镜头，"
                  "背景透视和构图跨段保持一致")
        performance = state["performance"]
    else:
        opening = (f"{_ZH_SIZE[state['start_framing']]}"
                   f"{_ZH_ANGLE.get(state['start_angle'], state['start_angle'])}开场，"
                   "依表演朝向留出空间")
        ending_text = (f"{_ZH_SIZE[state['end_framing']]}"
                       f"{_ZH_ANGLE.get(state['end_angle'], state['end_angle'])}")
        if state["move_family"] in {"dolly in", "dolly out"}:
            camera = (f"{opening}；{state['move_text']}；整个片段只完成上述景别变化，"
                      f"最后一帧恰好到达{ending_text}")
        elif state["move_family"] == "steady":
            camera = f"{opening}；{state['move_text']}；结束于{ending_text}，静止结束"
        else:
            camera = (f"{opening}；{state['move_text']}；结束于{ending_text}，"
                      f"{state['exit_motion']}至片段结束")
        performance = state["performance"]
    return (
        f"模式：{'口播' if mode == 'speaking' else '唱歌'}\n"
        f"镜头方案：{camera}\n"
        f"表演节奏：{performance}\n"
    )
