# -*- coding: utf-8 -*-
"""ABC 记谱法解析（可选增强，零依赖）。

用途（融合 YuE2/ABC 工具链思路）：
1. 从 ABC 谱提取**段落结构**（Intro/Interlude/Outro/Verse/Chorus/Bridge）——
   这是最权威的"间奏/前奏"判定来源，比纯声学检测更准；
2. 提取每段**旋律特征**（音符密度、音高范围、拍号）——注入镜头简报的
   "音乐上下文"行，让 H3 的表演/运镜更贴合音乐；
3. 把旋律行转成简谱数字（1-7）供展示（简单渲染，供人工参考）。

ABC 是公开文本格式；本模块只解析常见子集，兼容手写/工具产出的谱面。
"""
import re
from collections import OrderedDict

HEADER_RE = re.compile(r"^([A-Z]):\s*(.*)$")
NOTE_RE = re.compile(r"([_^=]*)([A-Ga-g])([,']*)(\d*)([/]?)(\d*)")
_STRUCT_KINDS = {
    "intro": "前奏", "interlude": "间奏", "outro": "尾奏",
    "verse": "主歌", "chorus": "副歌", "bridge": "桥段",
    "prechorus": "预备副歌", "solo": "独奏", "ending": "尾段",
}


def _kind_of(label):
    text = (label or "").strip().lower()
    for key, zh in _STRUCT_KINDS.items():
        if key in text:
            return key
    if any(w in text for w in ("唱", "歌")):
        return "verse"
    return "section"


def _pitch_of(note_token):
    """ABC 音符 → 相对音高（0=中音 C 前的 A... 简化：按字母+八度偏移）。"""
    m = NOTE_RE.fullmatch(note_token)
    if not m:
        return None
    letter = m.group(2)
    octave = m.group(3).count("'") - m.group(3).count(",")
    base = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}[letter.upper()]
    return base + 12 * octave


def _melody_features(line):
    """一行旋律的特征：音符数、平均音高、音高跨度、休止符数。"""
    notes = [n for n in re.findall(r"[_^=]*[A-Ga-g][,']*", line)]
    pitches = [p for p in (_pitch_of(n) for n in notes) if p is not None]
    rests = line.count("z") + line.count("Z")
    return {
        "note_count": len(notes),
        "mean_pitch": round(sum(pitches) / len(pitches), 2) if pitches else None,
        "pitch_span": (max(pitches) - min(pitches)) if pitches else 0,
        "rests": rests,
    }


def parse_abc(text):
    """解析 ABC 文本 → {headers, bars, structure, bpm, melody_sections}。

    structure: [{kind, label, bar_start, bar_end, features, zh}]，bar 号基于
    全谱累计小节。时间映射由调用方按"小节比例 × 音频总时长"完成。
    """
    if not text:
        return None
    text = str(text).replace("\r\n", "\n").replace("\r", "\n")
    headers = OrderedDict()
    bars = []          # 每个小节一个 dict
    current_label = None
    structure = []
    bar_counter = 0

    def flush(bar_text, label):
        nonlocal bar_counter
        bar_counter += 1
        bars.append({"label": label, "text": bar_text,
                     "features": _melody_features(bar_text)})

    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("%"):
            # 注释可兼作段落标签（% intro / % interlude / % outro ...）
            label = line[1:].strip()
            if label and not label.startswith((":", "-", "!", "?")) \
                    and not label.lower().startswith(("abc", "transcribed", "generated", "note")):
                current_label = label
            continue
        if line.startswith("P:"):
            # 段落标签（P:Verse 等）：先于通用 header 处理，标记后续小节归属
            current_label = line[2:].strip() or current_label
            continue
        m = HEADER_RE.match(line)
        if m and m.group(1) in "XYZTMKLQWC":
            key, val = m.group(1), m.group(2)
            headers[key] = val
            if key == "Q":
                q = re.search(r"(\d+)", val)
                if q and "bpm" not in headers:
                    headers["bpm"] = int(q.group(1))
            continue
        # 旋律/谱行：按 | 分隔小节，仅剥小节线记号，保留音符数字
        for p in [s.strip() for s in line.split("|") if s.strip()]:
            p_clean = re.sub(r"[\[\]:]+", "", p).strip()
            if p_clean:
                flush(p_clean, current_label)

    # 聚合 structure：相同 label 连续合并
    for i, bar in enumerate(bars):
        kind = _kind_of(bar["label"])
        if structure and structure[-1]["label"] == bar["label"]:
            structure[-1]["bar_end"] = i + 1
            structure[-1]["features"]["note_count"] += bar["features"]["note_count"]
        else:
            structure.append({"label": bar["label"] or "",
                              "kind": kind, "zh": _STRUCT_KINDS.get(kind, "乐段"),
                              "bar_start": i + 1, "bar_end": i + 1,
                              "features": dict(bar["features"])})

    if not bars:
        return None
    return {
        "headers": dict(headers),
        "bar_count": len(bars),
        "bpm": headers.get("bpm"),
        "structure": structure,
    }


def melody_to_jianpu(line, key="C"):
    """把 ABC 旋律行转成简谱数字（1-7，附高音点/低音点标记）。

    仅作人工参考渲染；音高映射基于 ABC 字母相对中音 C 的偏移。
    """
    result = []
    for token in re.findall(r"[_^=]*[A-Ga-g][,']*|z+", line):
        if token.startswith("z"):
            result.append("0")
            continue
        p = _pitch_of(token)
        if p is None:
            continue
        degree = (p - 0) % 12  # 简谱按 C 大调映射（近似）
        base = [1, 1, 2, 2, 3, 4, 4, 5, 5, 6, 6, 7][degree]
        octave = (p - 0) // 12 - 1  # 相对 C4=1
        s = str(base)
        if octave > 0:
            s = s + "•" * min(octave, 2)
        elif octave < 0:
            s = "•" * min(-octave, 2) + s
        result.append(s)
    return " ".join(result) if result else ""
