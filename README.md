# ComfyUI-EasyLongVideo

零依赖的长音频分段生成插件：导入一条完整音频（唱歌 / 口播），自动分析并
切分成段、为每段生成中文镜头简报；在面板中试听与微调后，自动逐段调用你的
视频生成工作流，最后拼接并配回原音频，输出完整成片。

灵感来自 [ComfyUI-H3-LongVideo](https://github.com/Zzz1phyrf/ComfyUI-H3-LongVideo)，
针对"普通电脑、不动环境"重新实现。

## 与原版的核心差异：适配性优先

| 痛点（原版） | 本插件的做法 |
| --- | --- |
| 强制依赖 `faster-whisper==1.2.1`，装不上就不能用 | **完全可选**：装了自动启用（切点更准），没装自动降级为纯声学分段，功能不缺失 |
| 隐式依赖 `soundfile`（requirements 未写全） | 不需要。读写 WAV 用 Python 标准库 `wave` |
| 唱歌模式强制下载 334MB 人声分离模型且吃 GPU | 不内置分离。有分离人声（如 UVR 产出）就接到 `vocals` 输入提升精度，没有就用原曲分析 |
| ASR 模型固定 large-v3-turbo（约 1.6GB） | 默认 `small`（约 460MB，CPU 也能跑），可选 tiny/base/medium/large-v3-turbo |
| ffmpeg/ffprobe 必须在 PATH | 四级自动探测：PATH → ComfyUI 根目录 → `ELV_FFMPEG` 环境变量 → imageio-ffmpeg（ComfyUI 官方依赖，基本必有）。实在没有也不崩，输出各段素材 + 手动合成说明 |
| 绑定 VHS_VideoCombine + 4 个外部节点 | 只需要本插件 1 个节点 + 任意能把视频存成 mp4 的输出节点（VHS / 其他均可） |
| 24fps 与帧数硬校验 | fps 可选 16/24/25/30，分辨率不一致自动缩放统一 |
| 仅面向 MiniMax H3 | 帧对齐可选 `h3`（5+17k）或 `none`；镜头简报是纯文本，H3 / Wan / Hunyuan 等工作流都能接 |

**安装后不需要 pip 安装任何东西，不下载任何必选模型，开箱即用。**

## 安装

1. 把 `ComfyUI-EasyLongVideo` 整个文件夹放入：

   ```
   ComfyUI/custom_nodes/ComfyUI-EasyLongVideo
   ```

2. 重启 ComfyUI。完成——没有第 2.5 步。

> 可选增强：想让唱歌/口播的切点更准，可在 ComfyUI 的 Python 环境中执行
> `pip install faster-whisper`，重启即可自动启用（首次识别会下载所选模型到
> `ComfyUI/models/EasyLongVideo/asr/`，失败不影响主流程）。

## 使用流程

### 1. 搭最小工作流

```
[Load Audio] ──audio──▶ [长视频 · 音频分析与顺序生成 (EasyLVUnified)]
                                                        │
              original_audio_padded / vocals_padded / segment_brief /
              generation_frames / filename_prefix
                        │
          segment_brief ──▶ [长视频 · 简报转视频提示词 (EasyLVBriefToPrompt)]
          （纯规则把中文简报转成英文结构化提示词，零依赖、无需 LLM；
            reference_notes 可选输入填参考图职责说明）
                        │
                        ▼
              [你的视频生成节点] ──▶ [视频输出节点（如 VHS Video Combine）]
```

节点顶部有 **⚙ 参数预设** 下拉（H3 唱歌 / H3 口播 / 自定义），
参数显示为中文标签；`reference_notes` 里填参考图职责（如
"Picture 1 is the performer together with the scene"）效果更好。

- `segment_brief` 是中文镜头简报，可直接交给提示词扩写节点（LLM/小助手），
  或原样拼进视频提示词；
- `generation_frames` 是按 `fps` 和帧对齐规则算好的帧数，接到视频节点即可；
- `filename_prefix` 建议接到视频输出节点的 `filename_prefix`，成片归档更整齐；
- 有已分离的人声（同一版本、未裁切）可以接到 `vocals` 可选输入，分析更准。

### 2. 分析与确认

节点上提供三个按钮（与原版习惯一致）：

- **⚙ 运镜规则：查看与修改**：编辑唱歌模式的运镜池、机位角、连续性约束与
  表演文案（JSON），保存到 ComfyUI 用户目录，**重新分析后生效**；
- **🔄 重新分析并分段**：清空项目编号，点击 ComfyUI「运行」即按当前参数
  重新分析；
- **📋 打开分段与生成控制**：打开分段审核面板。

唱歌模式分析时**自动分离人声**（内置 HTDemucs，权重约 319MB 首次自动下载）：
切点分析、口型驱动都用干净人声，成片音轨仍用原曲。口播模式自动跳过。

首次运行节点会分析音频，并自动弹出**分段审核面板**：

- 顶部波形图带切点红线（切点来自能量谷/气口/ASR 分句）；
- 每段卡片：试听（**原声 / 分离人声一键切换**）、查看识别文本、编辑镜头简报
  （自动保存 + 恢复默认）、「✂ 拆分」「并入下一段」「🎤 重分离本段」
  「🔇 本段无人声（强制静音驱动）」「重做本段」；
- **波形切点拖拽**：按住波形上的圆形手柄左右滑动调整切点，松手保存
  （两侧分段自动保持 ≥3 秒）；
- 顶部整曲播放器（含人声版切换）+ 「▶ 连播全部分段」；
- 左上角**历史项目下拉**可切换打开任何旧项目；
- 底部：**失败自动重试**次数、**间奏静音灵敏度**滑块（唱歌模式）、
  「📌 追加参考图约束」（四视图参考图防穿帮）、「📂 成片位置」；
- 生成完成的段直接在卡片内**视频预览**，支持「↺ 恢复上一版」版本管理（保留最近 5 版）；
- 底部「保存并确认」后即可生成。

**节点上的实时进度**：顺序生成时节点会依次显示当前执行的节点名
（PromptExpand → H3 采样百分比 → VHS）、自动重试与完成状态。

**间奏人物开口问题**：分层解决——内置人声分离切断伴奏干扰、
间奏静音门限压制分离泄漏、🔇 开关对确认无演唱的段强制静音驱动。

### 3. 顺序生成与合成

- 点击「开始顺序生成」：插件把当前画布快照为模板，逐段注入
  `project_id / segment_index` 自动提交 ComfyUI 队列；
- 支持暂停（当前段完成后停）/ 停止 / 单段重做；
- 全部完成后自动拼接各段并配回原音频，输出到：

  ```
  ComfyUI/output/EasyLongVideo/final_videos/
  ```

- 没有 ffmpeg 时不会报错崩溃：各段视频与 `work/concat.txt` 手动合成说明
  会保留在项目目录，装好 ffmpeg 后点「仅重新合成」即可。

### 4. 手动逐段（不用自动控制器也可以）

面板确认分段后，把节点上的 `segment_index` 改成 0,1,2…逐次 Queue，
每段手动下载输出即可；「仅重新合成」仍可用来最后拼片。

## 节点参数说明

| 参数 | 说明 |
| --- | --- |
| `mode` | `singing` 唱歌（安排运镜）/ `speaking` 口播（固定机位） |
| `target_seconds` / `max_seconds` | 每段目标/最长时长（秒） |
| `fps` | 输出视频帧率：16 / 24 / 25 / 30 |
| `frame_align` | `h3`：帧数对齐 5+17k 且 ≥124（H3/AnimateDiff 类约束）；`none`：自由 |
| `asr_mode` | `auto`：装了 faster-whisper 就用；`off`：纯声学分段 |
| `asr_model` | `auto`=small；可改 tiny/base/medium/large-v3-turbo |
| `asr_device` | `auto` / `cuda` / `cpu`（CPU 自动用 int8 提速） |
| `camera_activity` | 唱歌运镜强度：auto（按能量）/ moderate / dynamic / steady |
| `widest_framing` | 唱歌允许的最远景别（决定推拉可用的景别范围） |
| `vocals`（可选输入） | 已分离的人声，与原声同采样率同长度 |

## 输出目录

```
ComfyUI/output/EasyLongVideo/
├── projects/<项目id>/
│   ├── state/plan.json          分段方案与生成状态
│   ├── state/analysis.json      波形与切点诊断
│   ├── audio/source.wav         原始音频（PCM16）
│   ├── audio/vocals.wav         人声音频
│   ├── takes/  work/  cache/    各段产物与合成缓存
│   └── work/如何手动合成.txt    （仅当找不到 ffmpeg）
└── final_videos/                最终成片
```

## 测试

```bash
# 核心单元测试（任意带 numpy 的 Python 即可，无需 ComfyUI）
python -m unittest discover -s tests -p "test_*.py" -v

# 端到端冒烟测试（mock ComfyUI 环境，完整走 分析→确认→按段加载）
python tests/smoke_test.py
```

## 已知边界（诚实说明）

- 未内置人声分离：带伴奏的唱歌建议自行用 UVR 等工具分离后接入 `vocals`，
  否则切点质量会下降（面板会标注低置信切点，务必试听）；
- 未做节拍对齐（原版也仅作次级择优）：切点以"人声安全"为最高优先；
- 段与段之间是剪辑衔接，不追求无缝长镜头；
- 唱歌识别可能有错字/切点偏差，生成前请在面板试听；
- 顺序生成依赖画布快照：生成中途大幅改画布后，请用「重做本段」刷新快照。

## 文件结构

```
ComfyUI-EasyLongVideo/
├── __init__.py           入口：节点注册 + 路由注册（带降级保护）
├── nodes.py              ComfyUI 节点 EasyLVUnified
├── lv_audio.py           零依赖 WAV 读写 / AUDIO 张量转换
├── lv_segment.py         声学分析 + 动态规划分段（纯 numpy）
├── lv_camera.py          镜头规划 + 中文简报
├── lv_asr.py             可选 ASR（检测→子进程→降级）
├── lv_asr_worker.py      ASR 子进程脚本
├── lv_store.py           项目持久化（原子写、路径防越界）
├── lv_ffmpeg.py          ffmpeg 四级探测 + 拼接合成
├── lv_controller.py      顺序生成控制器（快照/注入/轮询/合成）
├── routes.py             /elv/* HTTP API
├── web/js/easy_long_video.js  分段审核面板
├── examples/             接入示例说明
└── tests/                单元测试 + 冒烟测试
```

## 许可证

MIT
