# -*- coding: utf-8 -*-
"""端到端冒烟测试：mock folder_paths / torch，模拟 ComfyUI 环境，
完整走一遍 分析 → 确认 → 按段加载 流程。

运行：python tests/smoke_test.py
"""
import os
import sys
import tempfile
import types
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SR = 22050
DURATION = 47.0

# ------------------------------------------------ mock torch（最小 shim）
class FakeTensor:
    def __init__(self, arr):
        self.arr = np.asarray(arr)

    def __getitem__(self, item):
        return FakeTensor(self.arr[item])

    @property
    def ndim(self):
        return self.arr.ndim

    @property
    def shape(self):
        return self.arr.shape

    def detach(self):
        return self

    def cpu(self):
        return self

    def float(self):
        return self

    def numpy(self):
        return self.arr

    def unsqueeze(self, axis):
        return FakeTensor(np.expand_dims(self.arr, axis))

    @property
    def T(self):
        return FakeTensor(self.arr.T)


fake_torch = types.ModuleType("torch")
fake_torch.from_numpy = FakeTensor
sys.modules["torch"] = fake_torch

# ------------------------------------------------ mock folder_paths
tmp_root = tempfile.mkdtemp(prefix="elv_smoke_")
fp = types.ModuleType("folder_paths")
fp.get_output_directory = lambda: os.path.join(tmp_root, "output")
fp.base_path = tmp_root
fp.models_dir = os.path.join(tmp_root, "models")
sys.modules["folder_paths"] = fp

# ------------------------------------------------ 合成"口播"音频：短句+气口
rng = np.random.default_rng(3)
audio_1d = np.zeros(int(DURATION * SR), dtype=np.float32)
t_cursor = 0.0
while t_cursor < DURATION - 3.0:
    dur = 1.6 + 1.2 * rng.random()
    n = int(dur * SR)
    t = np.arange(n) / SR
    tone = 0.5 * np.sin(2 * np.pi * (150 + 40 * rng.random()) * t)
    env = np.minimum(1, np.minimum(t / 0.04, (dur - t) / 0.1)).clip(0, 1)
    start = int(t_cursor * SR)
    end = min(len(audio_1d), start + n)
    audio_1d[start:end] += (tone * env)[:end - start].astype(np.float32)
    t_cursor += dur + 0.3 + 0.4 * rng.random()

audio_dict = {"waveform": FakeTensor(audio_1d[None, None, :]), "sample_rate": SR}

# ------------------------------------------------ 运行
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "easylongvideo", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
_pkg = importlib.util.module_from_spec(_spec)
sys.modules["easylongvideo"] = _pkg
_spec.loader.exec_module(_pkg)

from easylongvideo.lv_store import (read_plan, write_plan, fingerprint,  # noqa: E402
                                    projects_root)
from easylongvideo.nodes import EasyLVUnified  # noqa: E402

node = EasyLVUnified()
result = node.run(audio=audio_dict, mode="speaking", target_seconds=8.0,
                  max_seconds=12.0, fps="24", frame_align="h3", asr_mode="off",
                  asr_model="auto", asr_device="cpu", camera_activity="auto",
                  widest_framing="medium shot", project_id="", segment_index=0)
project_id = result["ui"]["elv_project"][0]
plan = read_plan(projects_root(), project_id)
count = len(plan["segments"])
assert count >= 3, f"分段数量异常：{count}"
assert plan["duration"] == DURATION
print(f"[1] 分析 OK：{count} 段，总时长 {plan['duration']:.1f}s，警告 {plan['warnings']}")

# 覆盖完整性
cursor = 0
for row in plan["segments"]:
    assert row["start_sample"] == cursor
    cursor = row["end_sample"]
assert cursor == int(DURATION * SR)
print("[2] 覆盖完整性 OK")

# 未确认时再次运行一体化节点：与参考实现一致，应视为"重新分析"（新建项目）
result2 = node.run(audio=audio_dict, mode="speaking", target_seconds=8.0,
                   max_seconds=12.0, fps="24", frame_align="h3", asr_mode="off",
                   asr_model="auto", asr_device="cpu", camera_activity="auto",
                   widest_framing="medium shot", project_id=project_id, segment_index=0)
new_id = result2["ui"]["elv_project"][0]
assert new_id and new_id != project_id, "未确认时应重新分析产生新项目"
plan2 = read_plan(projects_root(), new_id)
assert not plan2["approved"]
print(f"[3] 未确认→重新分析 OK：新项目 {new_id[:8]}…")
project_id = new_id  # 后续流程使用新项目
plan = read_plan(projects_root(), project_id)

# 确认后加载第 0 段
plan["approved"] = True
plan["approved_fingerprint"] = fingerprint(plan)
write_plan(projects_root(), plan)
loaded = node.run(audio=audio_dict, mode="speaking", target_seconds=8.0,
                  max_seconds=12.0, fps="24", frame_align="h3", asr_mode="off",
                  asr_model="auto", asr_device="cpu", camera_activity="auto",
                  widest_framing="medium shot", project_id=project_id, segment_index=0)
src = loaded["result"][0]["waveform"].arr
n_samples = src.shape[-1]
row = plan["segments"][0]
expected = (row["generation_frames"] * SR + 23) // 24
assert n_samples == expected, f"补齐采样数不符：{n_samples} != {expected}"
assert (expected - (row["end_sample"] - row["start_sample"])) < SR  # 补齐不超过 1 秒
brief = loaded["result"][2]
assert "固定机位" in brief and "模式：口播" in brief
assert loaded["result"][3] == row["generation_frames"]
assert loaded["result"][4].endswith("seg_0000")
print(f"[4] 按段加载 OK：第 0 段 {n_samples} 采样（{n_samples/SR:.2f}s），帧数 {row['generation_frames']}")
print(f"    简报示例：{brief.splitlines()[1][:60]}…")

# 每一段都能加载
for i in range(count):
    out = node.run(audio=audio_dict, mode="speaking", target_seconds=8.0,
                   max_seconds=12.0, fps="24", frame_align="h3", asr_mode="off",
                   asr_model="auto", asr_device="cpu", camera_activity="auto",
                   widest_framing="medium shot", project_id=project_id, segment_index=i)
    assert out["result"][0]["waveform"].arr.size > 0
print(f"[5] 全部 {count} 段加载 OK")

# project 列表
import lv_store  # noqa: E402
listing = lv_store.list_projects(projects_root())
assert listing and listing[0]["id"] == project_id
print(f"[6] 项目列表 OK：{len(listing)} 个项目，状态 {listing[0]['status']}")

print("\n✅ 冒烟测试全部通过（临时目录：", tmp_root, "）")
