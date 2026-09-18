# -*- coding: utf-8 -*-
"""声学分析与长音频分段（纯 numpy 实现）。

流程：
1. 20ms 能量包络（dBFS）+ 过零率；
2. 自动估算"安静/活跃"阈值（分位数自适应，无需人工调参）；
3. 生成候选切点：
   - 持续低人声区 / 拖音衰减落点 / 疑似换气口 / 局部能量低谷；
   - ASR 分句边界（若提供了语音识别结果，可选）；
   - 识别词界回退、时长强制回退（保证 100% 覆盖）；
4. 动态规划在"切点质量 + 段长接近目标"之间全局择优；
5. 输出分段表 + 诊断数据（供前端画波形和切点）。

ASR 缺失时自动降级为纯声学分段，本模块永远不依赖网络和模型下载。
"""
import math

import numpy as np

HOP_SECONDS = 0.02
MIN_SEGMENT_SECONDS = 3.0


# ---------------------------------------------------------------- 基础特征

def _mono(x):
    x = np.asarray(x, dtype=np.float64)
    return x if x.ndim == 1 else x.mean(axis=1)


def envelope(audio, sr, hop_seconds=HOP_SECONDS):
    """20ms 窗 RMS 能量（dBFS），返回 (starts_sample, db)。"""
    mono = _mono(audio)
    if not len(mono) or not np.isfinite(mono).all():
        raise ValueError("音频为空或包含无效数据。")
    hop = max(1, round(sr * hop_seconds))
    n_blocks = int(math.ceil(len(mono) / hop))
    pad = n_blocks * hop - len(mono)
    if pad:
        mono = np.pad(mono, (0, pad))
    power = (mono.reshape(n_blocks, hop) ** 2).mean(axis=1)
    db = 10.0 * np.log10(np.maximum(power, 1e-12))
    smooth = np.convolve(np.pad(db, (2, 2), mode="edge"), np.ones(5) / 5.0, mode="valid")
    starts = np.arange(0, n_blocks * hop, hop)[:n_blocks]
    return starts, smooth[:n_blocks]


def zero_crossing(audio, starts, sr):
    """每个能量块起点附近 40ms 窗口的过零率（用于识别气口）。"""
    mono = _mono(audio)
    signs = mono >= 0
    changes = np.r_[0, signs[1:] != signs[:-1]].astype(np.int32)
    prefix = np.r_[0, np.cumsum(changes)]
    width = max(1, round(sr * 0.04))
    ends = np.minimum(len(mono), starts + width)
    return (prefix[ends] - prefix[starts]) / np.maximum(1, ends - starts)


def waveform_peaks(audio, bins=1200):
    """归一化波形峰值序列（前端画波形用）。"""
    mono = np.abs(_mono(audio))
    if not len(mono):
        return []
    edges = np.linspace(0, len(mono), min(bins, len(mono)) + 1, dtype=int)
    peaks = [float(np.max(mono[edges[i]:edges[i + 1]])) for i in range(len(edges) - 1)]
    scale = np.percentile(peaks, 99) if peaks else 1.0
    return [round(min(1.0, v / max(scale, 1e-9)), 4) for v in peaks]


# ---------------------------------------------------------------- 分段主体

def _sections(lines, starts, active, sr, duration, minimum=2.0):
    """检测无人声区段（前奏/间奏/尾奏）。有 ASR 时结合文本，无 ASR 时纯能量。"""
    recognized = np.zeros(len(starts), dtype=bool)
    for line in lines:
        recognized |= (starts / sr < line["end"]) & ((starts + 0.02 * sr) / sr > line["start"])
    quiet_text = ~recognized & ~active
    sections, i = [], 0
    while i < len(quiet_text):
        if not quiet_text[i]:
            i += 1
            continue
        j = i + 1
        while j < len(quiet_text) and quiet_text[j]:
            j += 1
        a = starts[i] / sr
        b = min((starts[j - 1] / sr) + 0.02, duration)
        if b - a >= minimum:
            if a <= 0.25:
                kind = "intro"
            elif b >= duration - 0.25:
                kind = "outro"
            else:
                kind = "interlude"
            sections.append({"start": round(a, 3), "end": round(b, 3), "kind": kind,
                             "confidence": "medium", "reason": "无识别文字且能量活动较低"
                             if lines else "能量活动较低"})
        i = j
    return sections


def _build_candidates(audio, sr, starts, db, lines, words, quiet_thr, active_thr,
                      target, total):
    """生成候选切点集合：sample -> {db, reason, penalty, kind, confidence, warnings...}"""
    zcr = zero_crossing(audio, starts, sr)
    zcr_high = float(np.percentile(zcr, 75))
    candidates = {}

    def word_overlap(t):
        return [w["word"] for w in words if w["start"] + 0.02 < t < w["end"] - 0.02]

    def quiet_span(i):
        a = b = i
        while a > 0 and db[a - 1] <= quiet_thr:
            a -= 1
        while b + 1 < len(db) and db[b + 1] <= quiet_thr:
            b += 1
        return a, b, (b - a + 1) * HOP_SECONDS

    def add(sample, reason, penalty, warnings=(), kind="valley", conf=0.5):
        sample = int(np.clip(sample, 1, total - 1))
        i = min(len(db) - 1, int(np.searchsorted(starts, sample)))
        overlap = word_overlap(sample / sr)
        item = {"sample": sample, "db": float(db[i]), "reason": reason,
                "penalty": float(penalty), "warnings": list(warnings),
                "kind": kind, "confidence": conf,
                "overlap": bool(overlap), "protected": list(overlap)}
        if overlap:
            item["warnings"].append("切点落在识别词内：" + "/".join(overlap))
            item["penalty"] += 20
            item["confidence"] = min(item["confidence"], 0.15)
        old = candidates.get(sample)
        if old is None or item["penalty"] < old["penalty"]:
            candidates[sample] = item

    # ---- 1. 能量局部极小（核心来源） ----
    for i in range(1, len(db) - 1):
        window = db[max(0, i - 15):i + 16]
        if db[i] <= db[i - 1] and db[i] < db[i + 1] and db[i] <= window.min():
            _, _, span = quiet_span(i)
            prev_mean = float(np.mean(db[max(0, i - 10):i])) if i else db[i]
            next_mean = float(np.mean(db[i + 1:min(len(db), i + 6)])) if i + 1 < len(db) else db[i]
            if span >= 0.08:
                add(starts[i], "持续低人声区", 4, kind="quiet", conf=0.65)
            elif prev_mean - db[i] >= 6 and next_mean <= quiet_thr + 4:
                add(starts[i], "拖音衰减后的能量落点", 3, kind="drag_end", conf=0.72)
            elif zcr[i] >= zcr_high and db[i] < active_thr:
                add(starts[i], "疑似换气口附近的低能量区", 3.5,
                    ("气口为声学估计，请试听",), "breath", 0.62)
            else:
                add(starts[i], "人声局部低谷（未确认句尾）", 7,
                    ("可能位于句中，请试听",), "valley", 0.4)

    # ---- 2. ASR 分句边界（可用时显著提高质量） ----
    for first, second in zip(lines, lines[1:]):
        center = (float(first["end"]) + float(second["start"])) / 2
        idx = [i for i, s in enumerate(starts)
               if center - 0.75 <= s / sr <= center + 0.75 and not word_overlap(s / sr)]
        if idx:
            i = min(idx, key=lambda n: db[n])
            if db[i] <= quiet_thr and quiet_span(i)[2] >= 0.06:
                add(starts[i], "分句间隙的持续低能量区", 0, kind="phrase_gap", conf=0.92)
            elif db[i] < active_thr:
                add(starts[i], "分句附近的能量低谷", 3, ("低谷较短，请试听确认",),
                    "phrase_valley", 0.64)
        point = round(center * sr)
        if 0.02 < center < total / sr - 0.02 and not word_overlap(center):
            add(point, "ASR 分句边界", 5, ("分句处无明显低谷，请检查拖音",),
                "phrase_boundary", 0.55)

    # ---- 3. 分句区间内择优升级 ----
    if lines:
        phrase_ranges = [(min(a["end"], b["start"]) - 0.75, max(a["end"], b["start"]) + 0.75)
                         for a, b in zip(lines, lines[1:])]
        for lo, hi in phrase_ranges:
            options = []
            for point, c in candidates.items():
                if not lo <= point / sr <= hi or c["db"] > quiet_thr or c["overlap"]:
                    continue
                i = int(np.searchsorted(starts, point))
                if quiet_span(i)[2] >= 0.08:
                    options.append(c)
            if options:
                chosen = min(options, key=lambda c: c["db"])
                chosen.update(reason="分句间隙的持续低能量区", penalty=0.0,
                              kind="phrase_gap", confidence=0.92)
                chosen["warnings"] = []

    # ---- 4. 无人声区段边界 ----
    active = db > active_thr
    for section in _sections(lines, starts, active, sr, total / sr):
        for t, edge in ((section["start"], "开始"), (section["end"], "结束")):
            if 0.05 < t < total / sr - 0.05 and not word_overlap(t):
                add(round(t * sr), f"疑似{section['kind']} {edge}边界", 2,
                    ("无人声段为声学估计，请试听",), "section", 0.75)

    # ---- 5. 词界回退（仅当段内没有更好候选时才会被选中） ----
    for w in words:
        point = round(float(w["end"]) * sr)
        if 0 < point < total and point not in candidates:
            add(point, "识别词界回退", 12, ("词界回退，可能截断拖音",),
                "word_fallback", 0.28)

    # ---- 6. 时长强制回退（保证任何音频都能 100% 覆盖） ----
    for t in np.arange(target, total / sr, target):
        point = round(t * sr)
        if point not in candidates:
            i = min(len(db) - 1, int(np.searchsorted(starts, point)))
            warns = ["无可靠切点，按时长强制回退，务必试听"]
            if i < len(db) and db[i] > active_thr:
                warns.append("强制切点可能落在持续唱音区")
            add(point, "时长强制回退", 40, tuple(warns), "forced", 0.08)

    candidates[0] = {"sample": 0, "db": -120.0, "penalty": 0.0, "warnings": [],
                     "reason": "音频开始", "kind": "endpoint", "confidence": 1.0,
                     "overlap": False, "protected": []}
    candidates[total] = {"sample": total, "db": -120.0, "penalty": 0.0, "warnings": [],
                         "reason": "音频结束", "kind": "endpoint", "confidence": 1.0,
                         "overlap": False, "protected": []}
    return candidates, active


def _dynamic_program(points, candidates, sr, target, maximum, total, quiet_db, loud_db):
    """在候选切点上做全局 DP：段长接近目标 + 切点质量好。"""
    costs, prev = {points[0]: 0.0}, {}
    for j, end in enumerate(points[1:], 1):
        costs[end] = math.inf
        for start in reversed(points[:j]):
            duration = (end - start) / sr
            if duration > maximum:
                break
            if duration < MIN_SEGMENT_SECONDS and end != total:
                continue
            c = candidates[end]
            if end == total:
                level = 0.0
            else:
                level = float(np.clip((c["db"] - quiet_db) / max(loud_db - quiet_db, 1e-6),
                                      0.0, 1.0))
            score = (costs.get(start, math.inf) + 0.5
                     + ((duration - target) / 4.0) ** 2 + c["penalty"] + 2.0 * level)
            if duration < MIN_SEGMENT_SECONDS:
                score += 5.0
            if score < costs[end]:
                costs[end], prev[end] = score, start
    if total not in prev:
        raise ValueError("无法构造完整分段，请调整目标/最长时长后重试。")
    boundaries = [total]
    while boundaries[-1]:
        boundaries.append(prev[boundaries[-1]])
    boundaries.reverse()
    return boundaries


def segmentation(audio, sr, transcript=None, maximum=15.0, target=11.0,
                 mix_audio=None, return_analysis=False):
    """主入口：把音频切成若干段。

    transcript: {"segments": [{start,end,text}], "words": [{start,end,word}]} 或 None
    返回 rows 列表（可选附带 analysis 诊断数据）。
    """
    maximum, target = float(maximum), float(target)
    if not 3.0 <= target <= maximum <= 60.0:
        raise ValueError("参数范围要求：3 ≤ 目标时长 ≤ 最长时长 ≤ 60 秒。")
    total = len(audio)
    starts, db = envelope(audio, sr)
    lines = list((transcript or {}).get("segments", []))
    words = list((transcript or {}).get("words", []))

    noise, voice = np.percentile(db, [15, 85])
    dynamic = max(6.0, float(voice - noise))
    quiet_thr = min(float(np.median(db) - 8), float(noise + 0.35 * dynamic))
    active_thr = float(noise + 0.55 * dynamic)

    candidates, active = _build_candidates(audio, sr, starts, db, lines, words,
                                           quiet_thr, active_thr, target, total)
    points = sorted(candidates)
    levels = [c["db"] for c in candidates.values() if c["kind"] == "phrase_gap"]
    quiet_db, loud_db = np.percentile(levels if levels else db, [10, 90])

    boundaries = _dynamic_program(points, candidates, sr, target, maximum,
                                  total, quiet_db, loud_db)
    selected = set(boundaries)

    rows = []
    for index, (start, end) in enumerate(zip(boundaries, boundaries[1:])):
        related = [line["text"] for line in lines
                   if start / sr <= (line["start"] + line["end"]) / 2 < end / sr]
        mask = (starts >= start) & (starts < end)
        level = float(np.median(db[mask])) if mask.any() else -120.0
        c = candidates[end]
        rows.append({
            "index": index,
            "start_sample": int(start), "end_sample": int(end),
            "text": " / ".join(related),
            "energy_db": round(level, 2),
            "reason": c["reason"], "warnings": list(c["warnings"]),
            "boundary_kind": c["kind"],
            "boundary_confidence": round(float(c["confidence"]), 3),
            "vocal_state": "含识别人声" if related else "人声状态不确定",
        })

    if not return_analysis:
        return rows

    diagnostics = []
    ranked = sorted(candidates.values(),
                    key=lambda c: (c["sample"] not in selected, c["penalty"], c["sample"]))[:600]
    for c in sorted(ranked, key=lambda c: c["sample"]):
        diagnostics.append({"time": round(c["sample"] / sr, 3), "kind": c["kind"],
                            "reason": c["reason"],
                            "confidence": round(float(c["confidence"]), 3),
                            "selected": c["sample"] in selected,
                            "protected": c["overlap"], "warnings": c["warnings"]})
    analysis = {
        "schema": 1,
        "duration": total / sr,
        "waveform": {
            "original": waveform_peaks(audio if mix_audio is None else mix_audio),
            "vocals": waveform_peaks(audio),
        },
        "phrases": [{"start": round(float(s["start"]), 3), "end": round(float(s["end"]), 3),
                     "text": s["text"]} for s in lines],
        "protected_words": [{"start": round(float(w["start"]), 3),
                             "end": round(float(w["end"]), 3), "text": w["word"]}
                            for w in words],
        "sections": _sections(lines, starts, active, sr, total / sr),
        "candidates": diagnostics,
        "thresholds_db": {"quiet": round(quiet_thr, 2), "active": round(active_thr, 2)},
        "limitations": ["气口、拖音和无人声段均为声学估计，生成前请试听确认",
                        "未提供语音识别时为纯声学分段，切点质量可能下降"],
    }
    return rows, analysis


# ---------------------------------------------------------------- 帧数工具

def align_frames(seconds, fps=24, align="h3", edit_frames=None):
    """计算模型生成帧数。

    align="h3"：对齐 H3/AnimateDiff 类约束 frames = 5 + 17k（且 ≥ 124）；
    align="none"：自由帧数 = ceil(seconds * fps)。
    """
    fps = int(fps)
    need = max(1, int(math.ceil(float(seconds) * fps)))
    if edit_frames:
        need = max(need, int(edit_frames))
    if align == "h3":
        need = max(124, need)
        return 5 + 17 * int(math.ceil((need - 5) / 17.0))
    return need


def edit_frames_for(seconds, fps=24):
    return max(1, int(math.ceil(float(seconds) * int(fps))))


def padded_samples(frames, sr, fps=24):
    """给定帧数对应需要补齐到的采样点数（向上取整）。"""
    fps = int(fps)
    return (int(frames) * int(sr) + fps - 1) // fps
