# -*- coding: utf-8 -*-
"""核心逻辑单元测试（不依赖 ComfyUI，任何带 numpy 的 Python 均可运行）。

运行：python -m unittest discover -s tests -p "test_*.py" -v
"""
import math
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lv_audio import read_wav, wav_bytes, write_wav  # noqa: E402
from lv_camera import (camera_sequence, default_rules, segment_brief,  # noqa: E402
                       load_rules, save_rules, validate_rules)
from lv_prompt import parse_brief, to_prompt  # noqa: E402
from lv_segment import align_frames, envelope, segmentation  # noqa: E402

SR = 22050


def make_song(total_seconds=40.0, phrase=2.5, seed=7):
    """合成一段模拟人声：短句（正弦+颤音）+ 句间静音。"""
    rng = np.random.default_rng(seed)
    total = int(total_seconds * SR)
    audio = np.zeros(total, dtype=np.float32)
    t_cursor = 0.0
    while t_cursor < total_seconds - phrase:
        # 一句"人声"
        dur = phrase * (0.7 + 0.3 * rng.random())
        n = int(dur * SR)
        t = np.arange(n) / SR
        f0 = 220 + 60 * rng.random()
        tone = 0.6 * np.sin(2 * np.pi * f0 * t) * (1 + 0.15 * np.sin(2 * np.pi * 5 * t))
        env = np.minimum(1, np.minimum(t / 0.05, (dur - t) / 0.15)).clip(0, 1)
        start = int(t_cursor * SR)
        end = min(total, start + n)
        audio[start:end] += (tone * env)[:end - start].astype(np.float32)
        t_cursor += dur + 0.35 + 0.5 * rng.random()  # 句间气口
    return audio


class TestWavIO(unittest.TestCase):
    def test_roundtrip(self):
        audio = make_song(3.0)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.wav"
            write_wav(path, audio, SR)
            back, sr = read_wav(path)
            self.assertEqual(sr, SR)
            self.assertEqual(len(back), len(audio))
            # 16-bit 量化误差内一致
            self.assertLess(float(np.abs(back[:, 0] - audio).max()), 1e-3)

    def test_wav_bytes(self):
        audio = make_song(2.0)
        data = wav_bytes(audio, SR)
        self.assertGreater(len(data), 44)

    def test_read_range(self):
        audio = make_song(5.0)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "b.wav"
            write_wav(path, audio, SR)
            part, _ = read_wav(path, start=SR, stop=2 * SR)
            self.assertEqual(len(part), SR)


class TestSegmentation(unittest.TestCase):
    def _check_rows(self, rows, audio):
        cursor = 0
        for row in rows:
            self.assertEqual(row["start_sample"], cursor, "分段必须首尾相接")
            cursor = row["end_sample"]
        self.assertEqual(cursor, len(audio), "分段必须覆盖整条音频")
        for row in rows:
            dur = (row["end_sample"] - row["start_sample"]) / SR
            self.assertGreater(dur, 1.0)
            self.assertLessEqual(dur, 15.0 + 1e-6)

    def test_singing_like_audio(self):
        audio = make_song(60.0)
        rows = segmentation(audio, SR, None, maximum=15, target=11)
        self._check_rows(rows, audio)
        self.assertGreaterEqual(len(rows), 2)

    def test_silence(self):
        audio = np.zeros(SR * 30, dtype=np.float32)
        rows = segmentation(audio, SR, None, maximum=15, target=10)
        self._check_rows(rows, audio)  # 静音也要 100% 覆盖

    def test_with_transcript(self):
        audio = make_song(50.0)
        transcript = {"segments": [
            {"start": i * 5.0, "end": i * 5.0 + 4.0, "text": f"第{i}句"}
            for i in range(9)],
            "words": [{"start": i * 5.0 + j * 0.8, "end": i * 5.0 + j * 0.8 + 0.6,
                       "word": f"词{i}{j}"} for i in range(9) for j in range(4)]}
        rows, analysis = segmentation(audio, SR, transcript, maximum=15, target=10,
                                      return_analysis=True)
        self._check_rows(rows, audio)
        self.assertTrue(analysis["waveform"]["original"])
        self.assertTrue(analysis["phrases"])

    def test_envelope_shape(self):
        audio = make_song(10.0)
        starts, db = envelope(audio, SR)
        self.assertEqual(len(starts), len(db))
        self.assertTrue(np.isfinite(db).all())

    def test_align_frames(self):
        # 5.2s * 24fps = 125 帧 → 向上对齐到 5 + 17*8 = 141
        self.assertEqual(align_frames(5.2, 24, "h3"), 141)
        # 验证 h3 对齐：帧数满足 5+17k 且 >= need
        for seconds in (5.0, 7.3, 10.0, 11.4, 15.0):
            f = align_frames(seconds, 24, "h3")
            self.assertGreaterEqual(f, math.ceil(seconds * 24))
            self.assertGreaterEqual(f, 124)
            self.assertEqual((f - 5) % 17, 0)
        # none 模式为自由帧数
        self.assertEqual(align_frames(10.0, 24, "none"), 240)


class TestCamera(unittest.TestCase):
    def _rows(self, n=8):
        return [{"index": i, "start_sample": i * SR * 11, "end_sample": (i + 1) * SR * 11,
                 "energy_db": -20 + (i % 3) * 4, "generation_frames": 264,
                 "edit_frames": 264, "brief": ""} for i in range(n)]

    def test_singing_sequence(self):
        rows = self._rows()
        states = camera_sequence("singing", rows, activity="auto",
                                 widest="medium shot", seed="test")
        self.assertEqual(len(states), len(rows))
        for i in range(1, len(states)):
            # 相邻段不允许同类运镜（同 family 同方向）
            a, b = states[i - 1], states[i]
            self.assertFalse(
                a["move_family"] == b["move_family"] == "lateral"
                and a["move_direction"] == b["move_direction"],
                "相邻两段不允许相同方向的横移")
        for state in states:
            self.assertIn(state["start_framing"],
                          ["close-up", "medium close-up", "medium shot"])
            self.assertIn(state["move_family"], {"lateral", "arc", "dolly in",
                                                 "dolly out", "micro"})

    def test_speaking_is_locked(self):
        rows = self._rows()
        states = camera_sequence("speaking", rows)
        for state in states:
            self.assertEqual(state["move_family"], "steady")
            brief = segment_brief("speaking", state)
            self.assertIn("固定机位", brief)

    def test_brief_format(self):
        rows = self._rows(3)
        states = camera_sequence("singing", rows, widest="full shot", seed="x")
        for state in states:
            brief = segment_brief("singing", state)
            self.assertIn("模式：唱歌", brief)
            self.assertIn("镜头方案：", brief)
            self.assertIn("表演节奏：", brief)

    def test_rules_roundtrip(self):
        # 默认规则 → 校验通过
        rules = validate_rules(default_rules())
        self.assertEqual(rules["singing"]["move_pool"]["high"],
                         ["truck_left", "truck_right", "dolly_in", "dolly_out"])
        # 自定义：low 档只留横移 + 自定义表演文案
        custom = {"singing": {"move_pool": {"low": ["truck_left", " bogus ", "truck_right"]}},
                  "performance_text": {"restrained": "极简表演测试"}}
        rules = validate_rules(custom)
        self.assertEqual(rules["singing"]["move_pool"]["low"], ["truck_left", "truck_right"])
        self.assertEqual(rules["performance_text"]["restrained"], "极简表演测试")
        # 非法运镜被过滤后池为空 → 回退默认池
        rules = validate_rules({"singing": {"move_pool": {"medium": ["bogus"]}}})
        self.assertTrue(rules["singing"]["move_pool"]["medium"])
        # 规则真正影响镜头规划：横移交替关闭后仍能产出序列
        states = camera_sequence("singing", self._rows(6), rules=custom, seed="r1")
        self.assertEqual(len(states), 6)

    def test_rules_file_roundtrip(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "camera_rules.json"
            saved = save_rules(path, {"performance_text": {"natural": "文件版文案"}})
            self.assertEqual(saved["performance_text"]["natural"], "文件版文案")
            loaded = load_rules(path)
            self.assertEqual(loaded["performance_text"]["natural"], "文件版文案")
            self.assertEqual(loaded["singing"]["move_pool"],
                             default_rules()["singing"]["move_pool"])
            # 损坏文件 → 静默回退默认
            path.write_text("{broken json", encoding="utf-8")
            self.assertEqual(load_rules(path), default_rules())
            self.assertEqual(load_rules(Path(tmp) / "不存在.json"), default_rules())


class TestPrompt(unittest.TestCase):
    def _singing_brief(self):
        rows = [{"index": i, "start_sample": i * SR * 11, "end_sample": (i + 1) * SR * 11,
                 "energy_db": -20, "generation_frames": 264, "edit_frames": 264,
                 "brief": ""} for i in range(3)]
        states = camera_sequence("singing", rows, widest="full shot", seed="p")
        return segment_brief("singing", states[1])

    def test_parse_and_convert_singing(self):
        brief = self._singing_brief()
        parsed = parse_brief(brief)
        self.assertEqual(parsed["mode"], "singing")
        self.assertIsNotNone(parsed["move"])
        prompt = to_prompt(brief)
        self.assertIn("Summary:", prompt)
        self.assertIn("Camera:", prompt)
        self.assertIn("singing reference", prompt)
        self.assertIn("add new subtitles", prompt.lower())

    def test_speaking_brief(self):
        rows = [{"index": 0, "start_sample": 0, "end_sample": SR * 12,
                 "energy_db": -20, "generation_frames": 288, "edit_frames": 288,
                 "brief": ""}]
        states = camera_sequence("speaking", rows)
        brief = segment_brief("speaking", states[0])
        prompt = to_prompt(brief)
        self.assertIn("locked-off camera", prompt)
        self.assertIn("speech reference", prompt)
        # 中文模式
        zh = to_prompt(brief, language="chinese")
        self.assertIn("固定机位", zh)
        self.assertIn("口型", zh)

    def test_reference_notes(self):
        brief = self._singing_brief()
        prompt = to_prompt(brief, reference_notes="Picture 1 = performer and scene.")
        self.assertIn("Picture 1 = performer and scene.", prompt)


class TestReseed(unittest.TestCase):
    def _load_controller(self):
        """lv_controller 用相对导入，这里用 fake 包方式加载（顶层无 torch 依赖）。"""
        import importlib
        import types
        root = str(Path(__file__).resolve().parent.parent)
        if "elv_test_pkg" not in sys.modules:
            pkg = types.ModuleType("elv_test_pkg")
            pkg.__path__ = [root]
            sys.modules["elv_test_pkg"] = pkg
        return importlib.import_module("elv_test_pkg.lv_controller")

    def test_reseed_samplers(self):
        ctrl = self._load_controller()
        prompt = {
            "10": {"class_type": "KSampler",
                   "inputs": {"seed": 12345, "steps": 20, "cfg": 6.0,
                              "model": ["1", 0], "positive": ["2", 0]}},
            "20": {"class_type": "KSamplerAdvanced",
                   "inputs": {"noise_seed": 999, "seed": 888, "add_noise": "enabled"}},
            "30": {"class_type": "RandomNoise",
                   "inputs": {"noise_seed": 555}},
            "40": {"class_type": "PromptExpand",
                   "inputs": {"seed": 723165177602862, "source_text": "模式：唱歌"}},
            "50": {"class_type": "EmptyH3Video",
                   "inputs": {"width": 960, "batch_size": 1}},
        }
        before = {nid: dict(prompt[nid]["inputs"]) for nid in prompt}
        ctrl._reseed_samplers(prompt)
        # 采样节点：seed 必须变化且为正整数
        self.assertNotEqual(prompt["10"]["inputs"]["seed"], before["10"]["seed"])
        self.assertNotEqual(prompt["20"]["inputs"]["noise_seed"], 999)
        self.assertNotEqual(prompt["20"]["inputs"]["seed"], 888)
        self.assertNotEqual(prompt["30"]["inputs"]["noise_seed"], 555)
        self.assertIsInstance(prompt["10"]["inputs"]["seed"], int)
        # LLM 扩写节点：seed 必须保持不变（走缓存）
        self.assertEqual(prompt["40"]["inputs"]["seed"], 723165177602862)
        # 非采样节点不触碰；连线输入不动
        self.assertEqual(prompt["50"]["inputs"], before["50"])
        self.assertEqual(prompt["10"]["inputs"]["model"], ["1", 0])
        self.assertEqual(prompt["10"]["inputs"]["steps"], 20)


if __name__ == "__main__":
    unittest.main(verbosity=2)
