// ComfyUI-EasyLongVideo 前端面板
// 功能：参数中文标签 / 参数预设 / 分段审核（试听/编辑/合并/拆分/连播）/
//       历史项目 / 顺序生成控制 / 节点实时进度
// [v2] 使用 window.comfyAPI 全局（ComfyUI 官方新写法），不再依赖 ESM import，
//      对动态加载/缓存损坏免疫。
//      注意结构：实例在 window.comfyAPI.api.api / .app.app（两层）。
const _comfyGlobal = window.comfyAPI || {};
const app = _comfyGlobal.app?.app ?? null;
const api = _comfyGlobal.api?.api ?? null;

const NODE_TYPE = "EasyLVUnified";
let stylesInjected = false;
let panel = null; // { root, projectId, plan, polling, playlist }

// 参数中文标签（仅改显示，不影响保存的工作流数据）
const CN_LABELS = {
    mode: "模式",
    target_seconds: "目标段长（秒）",
    max_seconds: "最长段长（秒）",
    fps: "帧率 FPS",
    frame_align: "帧数对齐",
    asr_mode: "语音识别",
    asr_model: "识别模型",
    asr_device: "识别设备",
    camera_activity: "镜头活跃度",
    widest_framing: "最远允许景别",
    project_id: "项目编号",
    segment_index: "分段编号",
};

// 参数预设（H3 推荐组合）
const PRESETS = {
    "H3 唱歌": { mode: "singing", target_seconds: 11.0, max_seconds: 15.0,
        fps: "24", frame_align: "h3", asr_mode: "auto", asr_model: "auto",
        asr_device: "auto", camera_activity: "auto", widest_framing: "medium shot" },
    "H3 口播": { mode: "speaking", target_seconds: 12.0, max_seconds: 15.0,
        fps: "24", frame_align: "h3", asr_mode: "auto", asr_model: "auto",
        asr_device: "auto", camera_activity: "steady", widest_framing: "medium close-up" },
};

function injectStyles() {
    if (stylesInjected) return;
    stylesInjected = true;
    const style = document.createElement("style");
    style.textContent = `
    .elv-overlay { position: fixed; inset: 0; background: rgba(0,0,0,.55); z-index: 99999;
        display: flex; align-items: center; justify-content: center; }
    .elv-panel { background: #1e1e22; color: #ddd; border-radius: 12px; width: min(1080px, 94vw);
        max-height: 92vh; display: flex; flex-direction: column; box-shadow: 0 12px 48px rgba(0,0,0,.6); }
    .elv-head { padding: 14px 18px; display: flex; align-items: center; gap: 10px;
        border-bottom: 1px solid #333; flex-wrap: wrap; }
    .elv-head h3 { margin: 0; font-size: 16px; color: #fff; flex: 1; min-width: 180px; }
    .elv-badge { font-size: 12px; padding: 2px 10px; border-radius: 999px; background: #2c2c31; }
    .elv-badge.ok { background: #1d4030; color: #6fe3a1; }
    .elv-badge.warn { background: #4a3a17; color: #f0c674; }
    .elv-badge.err { background: #4a1f1f; color: #ff8f8f; }
    .elv-body { overflow-y: auto; padding: 14px 18px; flex: 1; }
    .elv-wave { width: 100%; height: 90px; background: #161618; border-radius: 8px;
        margin-bottom: 4px; display: block; }
    .elv-fullaudio { width: 100%; margin: 2px 0 8px; height: 34px; }
    .elv-hint { font-size: 12px; color: #888; margin: 4px 0 12px; }
    .elv-seg { border: 1px solid #333; border-radius: 10px; padding: 10px 12px;
        margin-bottom: 10px; background: #232327; }
    .elv-seg.playing { border-color: #d6a95a; background: #2a2620; }
    .elv-seg.running { border-color: #5a8dd6; }
    .elv-seg.done { border-color: #2e6e4e; }
    .elv-seg.failed { border-color: #a05050; }
    .elv-seg-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
    .elv-seg-title { font-weight: 600; color: #fff; }
    .elv-time { color: #9ab; font-size: 12px; }
    .elv-seg-head audio { height: 30px; max-width: 300px; }
    .elv-spacer { flex: 1; }
    .elv-text { font-size: 12px; color: #9aa; margin: 6px 0; white-space: pre-wrap; }
    .elv-brief { width: 100%; box-sizing: border-box; background: #191a1d; color: #cde;
        border: 1px solid #3a3a40; border-radius: 8px; font-size: 12px; padding: 8px;
        min-height: 64px; resize: vertical; font-family: inherit; }
    .elv-warn { color: #f0c674; font-size: 12px; margin-top: 4px; }
    .elv-foot { padding: 12px 18px; border-top: 1px solid #333; display: flex;
        gap: 8px; flex-wrap: wrap; align-items: center; }
    .elv-foot .note { font-size: 12px; color: #f0c674; flex-basis: 100%; }
    .elv-btn { background: #2f2f36; color: #eee; border: 1px solid #444; border-radius: 8px;
        padding: 6px 14px; cursor: pointer; font-size: 13px; }
    .elv-btn:hover { background: #3a3a44; }
    .elv-btn.primary { background: #2563a8; border-color: #3178c6; }
    .elv-btn.primary:hover { background: #2d72c0; }
    .elv-btn.danger { background: #7a3030; border-color: #a04040; }
    .elv-btn:disabled { opacity: .45; cursor: not-allowed; }
    .elv-select { background: #232327; color: #ddd; border: 1px solid #444;
        border-radius: 6px; padding: 5px 8px; font-size: 12px; max-width: 340px; }
    .elv-mini { background: #191a1d; color: #ddd; border: 1px solid #3a3a40;
        border-radius: 6px; padding: 5px 8px; font-size: 12px; width: 90px; }
    .elv-close { background: transparent; border: none; color: #999; font-size: 20px;
        cursor: pointer; line-height: 1; }
    .elv-errorbox { background: #2a1616; border: 1px solid #6a2a2a; border-radius: 8px;
        padding: 8px 10px; margin: 6px 0; }
    .elv-errorbox-head { display: flex; align-items: center; gap: 8px; }
    .elv-errorbox-head .elv-btn { padding: 2px 10px; font-size: 12px; }
    .elv-errorbox pre { max-height: 170px; overflow-y: auto; white-space: pre-wrap;
        word-break: break-all; font-size: 11px; color: #ffb0b0; margin: 6px 0 0;
        line-height: 1.5; scrollbar-width: thin; }
    `;
    document.head.appendChild(style);
}

async function apiGet(url) {
    const res = await fetch(url);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || res.statusText);
    return data;
}
async function apiPost(url, body) {
    const res = await fetch(url, { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body || {}) });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || res.statusText);
    return data;
}

const fmt = (s) => `${Math.floor(s / 60)}:${(s % 60).toFixed(1).padStart(4, "0")}`;
const esc = (t) => String(t ?? "").replace(/[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

// ---------------------------------------------------------------- 波形

function drawWave(canvas, analysis, plan, dragCut) {
    if (!canvas || !analysis?.waveform) return;
    const ctx = canvas.getContext("2d");
    const w = canvas.width = canvas.clientWidth * devicePixelRatio;
    const h = canvas.height = (canvas.clientHeight || 110) * devicePixelRatio;
    ctx.clearRect(0, 0, w, h);
    const dur = analysis.duration || plan.duration || 1;
    const dual = !!analysis.waveform.vocals;
    const half = dual ? h / 2 : h;
    // 记录切点时间（秒），供拖拽命中
    canvas._cutTimes = (plan.segments || []).slice(1)
        .map((r) => r.start_sample / plan.sample_rate);

    function drawTrack(peaks, color, y0, hh, alpha) {
        ctx.fillStyle = color;
        ctx.globalAlpha = alpha;
        const bw = Math.max(1, w / peaks.length - 0.5);
        peaks.forEach((p, i) => {
            const bh = Math.max(1, p * hh * 0.92);
            ctx.fillRect((i / peaks.length) * w, y0 + (hh - bh) / 2, bw, bh);
        });
        ctx.globalAlpha = 1;
    }
    // 上：原曲（蓝）；下：人声（绿）
    drawTrack(analysis.waveform.original || [], "#3d5a80", 0, half, 0.95);
    if (dual) drawTrack(analysis.waveform.vocals, "#2e7d5b", half, h - half, 0.95);

    // 切点线 + 拖拽手柄 + 段标签
    (plan.segments || []).forEach((row, i) => {
        const x0 = (row.start_sample / plan.sample_rate) / dur * w;
        const dragging = dragCut && dragCut.boundary === i;
        if (i > 0) {
            const px = dragging ? (dragCut.t / dur) * w : x0;
            ctx.fillStyle = dragging ? "#ffd54a" : "#e05656";
            ctx.fillRect(px - devicePixelRatio, 0, Math.max(2, 2 * devicePixelRatio), h);
            // 手柄圆点
            ctx.beginPath();
            ctx.arc(px, 9 * devicePixelRatio, 6 * devicePixelRatio, 0, Math.PI * 2);
            ctx.fillStyle = dragging ? "#ffd54a" : "#ff8080";
            ctx.fill();
            ctx.strokeStyle = "#222"; ctx.stroke();
            if (dragging) {
                ctx.font = `${12 * devicePixelRatio}px sans-serif`;
                ctx.fillStyle = "#ffd54a";
                const tt = `${dragCut.t.toFixed(2)}s（松开保存）`;
                const tx = Math.min(px + 10 * devicePixelRatio, w - 140 * devicePixelRatio);
                ctx.fillText(tt, tx, 14 * devicePixelRatio);
            }
        }
        if (i > 0 || true) {
            const label = `第${i + 1}段`;
            ctx.font = `${11 * devicePixelRatio}px sans-serif`;
            const tw = ctx.measureText(label).width;
            const lx = Math.min(x0 + 4 * devicePixelRatio, w - tw - 4);
            const ly = dragging && i > 0 ? 24 * devicePixelRatio : 4;
            ctx.fillStyle = "rgba(20,20,24,0.75)";
            ctx.fillRect(lx - 3, ly, tw + 6, 15 * devicePixelRatio);
            ctx.fillStyle = "#ffd98a";
            ctx.fillText(label, lx, ly + 11 * devicePixelRatio);
        }
    });
    if (dual) {
        ctx.font = `${10 * devicePixelRatio}px sans-serif`;
        ctx.fillStyle = "#7fa8d9"; ctx.fillText("原曲", 6, half - 6);
        ctx.fillStyle = "#6fd0a0"; ctx.fillText("人声", 6, h - 6);
    }
}

// ---------------------------------------------------------------- 面板

function statusBadge(plan) {
    const map = { draft: ["draft", "warn"], running: ["生成中…", ""],
        pausing: ["暂停中…", ""], stopping: ["停止中…", ""],
        paused: ["已暂停", "warn"], stopped: ["已停止", "warn"],
        merging: ["合成中…", ""], completed: ["已完成", "ok"],
        completed_no_ffmpeg: ["完成（无 ffmpeg）", "warn"], failed: ["失败", "err"] };
    const [text, cls] = map[plan.run_status] || [plan.run_status, ""];
    return `<span class="elv-badge ${cls}">${text}</span>`;
}

function segClass(row) {
    const st = row.job?.status;
    if (st === "completed") return "done";
    if (st === "failed") return "failed";
    if (st === "queued" || st === "running") return "running";
    return "";
}

function renderSegments(container, plan) {
    const keep = container.dataset.playIndex || "";
    container.innerHTML = "";
    plan.segments.forEach((row, i) => {
        const sr = plan.sample_rate;
        const a = row.start_sample / sr, b = row.end_sample / sr;
        const job = row.job || {};
        const div = document.createElement("div");
        div.className = `elv-seg ${segClass(row)} ${String(i) === keep ? "playing" : ""}`;
        div.dataset.index = i;
        div.innerHTML = `
            <div class="elv-seg-head">
                <span class="elv-seg-title">第 ${i + 1} 段</span>
                <span class="elv-time">${fmt(a)} → ${fmt(b)}（${(b - a).toFixed(1)}s · ${row.generation_frames}帧 · ${esc(row.boundary_kind)}）</span>
                <audio controls preload="none" data-i="${i}" data-track="source" src="/elv/project/${plan.id}/audio?index=${i}"></audio>
                <button class="elv-btn" data-act="track" data-i="${i}" style="padding:2px 8px;font-size:11px" title="切换试听分离后的人声">🎤 听人声</button>
                <span class="elv-spacer"></span>
                <span class="elv-spacer"></span>
                <span class="elv-badge ${job.status === "completed" ? "ok" : job.status === "failed" ? "err" : ""}">${esc(job.status || "pending")}</span>
                <button class="elv-btn" data-act="cue" data-i="${i}" title="试听切点前后各2秒">◎ 试听切点</button>
                <button class="elv-btn" data-act="resegsep" data-i="${i}" ${row.resep_status === "running" ? "disabled" : ""} title="只对本段重新分离人声（±5秒上下文）">🎤 重分离本段${row.resep_status === "running" ? "中…" : ""}</button>
                <button class="elv-btn" data-act="mute" data-i="${i}" style="padding:2px 8px;font-size:11px;${row.mute_vocals ? "background:#5a2a2a;border-color:#a04040;color:#ffb0b0" : ""}" title="标记本段无人声：人声驱动输出归零，人物强制不开口">${row.mute_vocals ? "🔇 已静音驱动" : "🔇 本段无人声"}</button>
                <button class="elv-btn" data-act="redo" data-i="${i}" ${plan.run_status === "running" ? "disabled" : ""}>重做本段</button>
                <button class="elv-btn" data-act="merge" data-i="${i}" ${i >= plan.segments.length - 1 ? "disabled" : ""}>并入下一段</button>
                <button class="elv-btn" data-act="split" data-i="${i}">✂ 拆分</button>
                <button class="elv-btn" data-act="versions" data-i="${i}" ${row.resep_status === "running" ? "disabled" : ""}>↺ 版本管理${row.takes?.length ? ` (${row.takes.length})` : ""}</button>
            </div>
            ${job.status === "completed" && job.video ? `
            <div style="margin:8px 0 4px">
                <div class="elv-hint" style="margin:0 0 4px">当前分段结果：</div>
                <video controls preload="metadata" style="width:100%;max-height:280px;border-radius:8px;background:#111"
                    src="/elv/project/${plan.id}/segment/${i}/video?v=${encodeURIComponent(job.completed_at || job.video || "")}"></video>
            </div>` : ""}
            ${row.text ? `<div class="elv-text">${esc(row.text)}</div>` : ""}
            <div class="elv-seg-head" style="margin-bottom:6px">
                <label style="font-size:12px;color:#99a">本段镜头简报（可直接编辑，失焦自动保存）：</label>
                ${row.brief_default ? `<button class="elv-btn" data-act="resetbrief" data-i="${i}" style="padding:2px 10px;font-size:12px" ${row.brief_edited ? "" : "disabled"}>↺ 恢复默认简报</button>` : ""}
            </div>
            <textarea class="elv-brief" data-i="${i}">${esc(row.brief)}</textarea>
            ${(row.warnings || []).map((wn) => `<div class="elv-warn">⚠ ${esc(wn)}</div>`).join("")}
            ${row.resep_error ? `<div class="elv-warn">✖ 本段重分离失败：${esc(row.resep_error)}</div>` : ""}
            ${job.error ? `<div class="elv-warn">✖ ${esc(job.error)}</div>` : ""}`;
        container.appendChild(div);
    });
}

// 拆分弹窗：从候选切点中选择（或手输秒数）
async function openSplitDialog(panel, index) {
    const plan = panel.plan;
    const row = plan.segments[index];
    const sr = plan.sample_rate;
    const a = row.start_sample / sr + 0.5, b = row.end_sample / sr - 0.5;
    if (b - a < 1.0) { alert("这一段太短，无法拆分。"); return; }
    let candidates = [];
    try {
        const analysis = await apiGet(`/elv/project/${panel.projectId}/analysis`);
        candidates = (analysis.candidates || [])
            .filter((c) => a <= c.time <= b && !c.protected)
            .sort((x, y) => x.time - y.time).slice(0, 60);
    } catch (err) { /* 没有诊断数据也允许手输 */ }
    const overlay = document.createElement("div");
    overlay.className = "elv-overlay";
    overlay.innerHTML = `
    <div class="elv-panel" style="width:min(560px,92vw)">
        <div class="elv-head"><h3>✂ 拆分第 ${index + 1} 段</h3>
            <button class="elv-close">✕</button></div>
        <div class="elv-body">
            <div class="elv-hint">范围 ${fmt(a)} ~ ${fmt(b)}。优先选择候选切点（置信度高、
                位于人声低谷），确认后两侧将各成为独立分段。</div>
            ${candidates.length ? `<select class="elv-select" id="elv-split-sel" style="width:100%">
                ${candidates.map((c) => `<option value="${c.time}">${fmt(c.time)}s · ${esc(c.kind)} · 置信 ${c.confidence}</option>`).join("")}
            </select>` : `<div class="elv-warn">该段范围内没有保存的候选切点，请手动输入时间。</div>`}
            <div style="display:flex;gap:8px;align-items:center;margin-top:10px">
                <label style="font-size:12px">或手动输入（秒）：</label>
                <input class="elv-mini" id="elv-split-manual" type="number"
                    min="${a.toFixed(2)}" max="${b.toFixed(2)}" step="0.1"
                    placeholder="${((a + b) / 2).toFixed(1)}">
            </div>
        </div>
        <div class="elv-foot">
            <button class="elv-btn primary" id="elv-split-ok">确认拆分</button>
            <span class="elv-spacer"></span>
            <span class="elv-hint">拆分后需要重新「保存并确认」。</span>
        </div>
    </div>`;
    document.body.appendChild(overlay);
    overlay.querySelector(".elv-close").onclick = () => overlay.remove();
    overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) overlay.remove(); });
    overlay.querySelector("#elv-split-ok").onclick = async () => {
        const manual = overlay.querySelector("#elv-split-manual").value;
        const sel = overlay.querySelector("#elv-split-sel");
        const at = manual ? parseFloat(manual) : (sel ? parseFloat(sel.value) : NaN);
        if (!isFinite(at) || at < a || at > b) { alert("请输入范围内的有效时间。"); return; }
        try {
            panel.plan = await apiPost(`/elv/project/${panel.projectId}/edit`,
                { revision: panel.plan.revision, operations: [{ op: "split", index, at }] });
            overlay.remove();
            refresh(panel);
        } catch (err) { alert(err.message); }
    };
}

// 连播：依次播放每段音频
// 版本管理弹窗：预览当前版本与历史归档，可恢复指定版本
async function openVersionsDialog(panel, index) {
    const plan = panel.plan;
    const row = plan.segments[index];
    const takes = row.takes || [];
    const versions = [];
    if (row.job?.status === "completed" && row.job?.video) {
        versions.push({ label: "当前版本", take: null, ts: row.job.completed_at,
                        video: row.job.video });
    }
    [...takes].reverse().forEach((t, ridx) => {
        versions.push({ label: `历史版本 ${takes.length - ridx}`,
                        take: takes.indexOf(t), ts: t.archived_at, video: t.video });
    });
    const overlay = document.createElement("div");
    overlay.className = "elv-overlay";
    overlay.innerHTML = `
    <div class="elv-panel" style="width:min(720px,92vw)">
        <div class="elv-head"><h3>🗂 第 ${index + 1} 段 · 版本管理</h3>
            <button class="elv-close">✕</button></div>
        <div class="elv-body">
            ${versions.map((v, vi) => `
            <div style="border:1px solid #3a3a40;border-radius:8px;padding:8px;margin-bottom:10px">
                <div class="elv-seg-head" style="margin-bottom:6px">
                    <b>${esc(v.label)}</b>
                    ${v.ts ? `<span class="elv-time">${new Date(v.ts * 1000).toLocaleString()}</span>` : ""}
                    <span class="elv-spacer"></span>
                    ${v.take !== null && v.take !== undefined
                        ? `<button class="elv-btn" data-restore="${v.take}">↺ 恢复此版本</button>` : ""}
                </div>
                <video controls preload="metadata" style="width:100%;max-height:240px;border-radius:8px;background:#111"
                    src="/elv/project/${plan.id}/segment/${index}/${v.take !== null && v.take !== undefined ? `take/${v.take}/` : ""}video?v=${vi}"></video>
            </div>`).join("") || `<div class="elv-hint">暂无历史版本。</div>`}
        </div>
    </div>`;
    document.body.appendChild(overlay);
    overlay.querySelector(".elv-close").onclick = () => overlay.remove();
    overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) overlay.remove(); });
    overlay.querySelectorAll("[data-restore]").forEach((btn) => {
        btn.onclick = async () => {
            try {
                panel.plan = await apiPost(`/elv/project/${panel.projectId}/restore`,
                    { index, take: parseInt(btn.dataset.restore) });
                overlay.remove();
                panel.noteEl.textContent = "版本已恢复（当前版本自动归档），点「重做本段」可再次生成。";
                refresh(panel);
            } catch (err) { alert(err.message); }
        };
    });
}

function startPlaylist(panel) {
    stopPlaylist(panel);
    const player = new Audio();
    panel.playlist = { player, index: 0 };
    player.onended = () => {
        const list = panel.plan?.segments || [];
        const next = panel.playlist.index + 1;
        if (next < list.length) playSegment(panel, next);
        else stopPlaylist(panel);
    };
    playSegment(panel, 0);
}
function playSegment(panel, index) {
    if (!panel.playlist) return;
    panel.playlist.index = index;
    const vocals = panel.playVocalsChk?.checked ? "&vocals=1" : "";
    panel.playlist.player.src = `/elv/project/${panel.projectId}/audio?index=${index}${vocals}`;
    panel.playlist.player.play().catch(() => {});
    panel.listEl.dataset.playIndex = String(index);
    renderSegments(panel.listEl, panel.plan);
    bindSegmentEvents(panel);
    const card = panel.listEl.querySelector(`[data-index="${index}"]`);
    card?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    if (panel.playBtn) {
        panel.playBtn.textContent = `⏹ 连播中 (${index + 1}/${panel.plan.segments.length})`;
    }
}
function stopPlaylist(panel) {
    if (panel.playlist) {
        panel.playlist.player.pause();
        panel.playlist = null;
    }
    panel.listEl.dataset.playIndex = "";
    if (panel.playBtn) panel.playBtn.textContent = "▶ 连播全部分段";
    if (panel.plan) { renderSegments(panel.listEl, panel.plan); bindSegmentEvents(panel); }
}

const row_mute_state = (panel, i) => !!(panel.plan?.segments?.[i]?.mute_vocals);

function bindSegmentEvents(panel) {
    panel.listEl.querySelectorAll(".elv-btn[data-act]").forEach((btn) => {
        btn.onclick = async () => {
            const i = +btn.dataset.i;
            try {
                if (btn.dataset.act === "merge") {
                    panel.plan = await apiPost(`/elv/project/${panel.projectId}/edit`,
                        { revision: panel.plan.revision,
                          operations: [{ op: "merge_next", index: i }] });
                    refresh(panel);
                } else if (btn.dataset.act === "redo") {
                    const promptPayload = await collectPromptPayload(panel);
                    await apiPost(`/elv/project/${panel.projectId}/retry`,
                        { index: i, ...promptPayload });
                    refresh(panel);
                } else if (btn.dataset.act === "split") {
                    openSplitDialog(panel, i);
                } else if (btn.dataset.act === "resetbrief") {
                    try {
                        panel.plan = await apiPost(`/elv/project/${panel.projectId}/edit`,
                            { revision: panel.plan.revision,
                              operations: [{ op: "reset_brief", index: i }] });
                        refresh(panel);
                    } catch (err) { alert(err.message); }
                } else if (btn.dataset.act === "restore") {
                    if (!confirm("将该段恢复到上一个版本？当前版本会归档保存。")) return;
                    try {
                        panel.plan = await apiPost(`/elv/project/${panel.projectId}/restore`,
                            { index: i });
                        refresh(panel);
                    } catch (err) { alert(err.message); }
                } else if (btn.dataset.act === "mute") {
                    const toMute = !row_mute_state(panel, i);
                    try {
                        await apiPost(`/elv/project/${panel.projectId}/segment/${i}/mute`,
                            { mute: toMute });
                        panel.noteEl.textContent = toMute
                            ? `第 ${i + 1} 段已标记「无人声」，点「重做本段」后人物将在此段保持不开口。`
                            : `第 ${i + 1} 段已取消静音标记，点「重做本段」恢复人声驱动。`;
                        refresh(panel);
                    } catch (err) { alert(err.message); }
                } else if (btn.dataset.act === "track") {
                    // 切换试听：原声(含伴奏) ↔ 分离人声
                    const audio = btn.closest(".elv-seg-head").querySelector("audio");
                    const toVocals = audio.dataset.track !== "vocals";
                    audio.dataset.track = toVocals ? "vocals" : "source";
                    audio.src = `/elv/project/${panel.projectId}/audio?index=${i}` +
                        (toVocals ? "&vocals=1" : "");
                    audio.play().catch(() => {});
                    btn.textContent = toVocals ? "🎧 听原曲" : "🎤 听人声";
                } else if (btn.dataset.act === "versions") {
                    openVersionsDialog(panel, i);
                } else if (btn.dataset.act === "resegsep") {
                    try {
                        await apiPost(`/elv/project/${panel.projectId}/segment/${i}/re-separate`, {});
                        panel.noteEl.textContent =
                            `第 ${i + 1} 段人声重分离已启动（几秒完成），完成后重做本段即可用新人声。`;
                        refresh(panel);
                    } catch (err) { alert(err.message); }
                } else if (btn.dataset.act === "cue") {
                    // 试听切点前后各 2 秒（切点质量精听）
                    const plan = panel.plan;
                    const row = plan.segments[i];
                    const sr = plan.sample_rate;
                    const cut = row.start_sample / sr;
                    const t0 = Math.max(0, cut - 2), t1 = Math.min(plan.duration, cut + 2);
                    try { panel.cuePlayer?.pause(); } catch (e) {}
                    panel.cuePlayer = new Audio(
                        `/elv/project/${panel.projectId}/audio?t0=${t0.toFixed(3)}&t1=${t1.toFixed(3)}`);
                    panel.cuePlayer.play().catch(() => {});
                }
            } catch (err) { alert(err.message); }
        };
    });
    panel.listEl.querySelectorAll(".elv-brief").forEach((ta) => {
        ta.onchange = async () => {
            try {
                panel.plan = await apiPost(`/elv/project/${panel.projectId}/edit`,
                    { revision: panel.plan.revision,
                      operations: [{ op: "brief", index: +ta.dataset.i, brief: ta.value }] });
                refresh(panel);
            } catch (err) { alert(err.message); }
        };
    });
}

// 快照当前画布（API prompt），定位 loader 与视频输出节点
async function collectPromptPayload(panel) {
    const { workflow, output } = await app.graphToPrompt();
    let loaderId = null;
    let videoId = null;
    const videoNodes = [];
    for (const [id, node] of Object.entries(output || {})) {
        if (node.class_type === NODE_TYPE) loaderId = id;
        const ct = String(node.class_type || "");
        if (/video|gif|save/i.test(ct)) videoNodes.push(id);
    }
    if (panel.videoSelect && panel.videoSelect.value) videoId = panel.videoSelect.value;
    if (!loaderId) throw new Error("画布中未找到 EasyLVUnified 节点。");
    if (!videoId) throw new Error("请选择视频输出节点（如 VHS Video Combine）。");
    // 节点 id → 类型映射：供执行进度显示"正在执行哪个节点"
    try {
        const wfMap = {};
        for (const n of (workflow.nodes || [])) wfMap[String(n.id)] = n.type;
        window._elvWorkflowMap = wfMap;
    } catch (err) { /* 显示降级 */ }
    return { prompt: output, workflow, loader_id: loaderId, video_id: videoId,
             client_id: api.clientId || "" };
}

async function refresh(panel) {
    try {
        const plan = await apiGet(`/elv/project/${panel.projectId}`);
        panel.plan = plan;
        const sepStatus = plan.separation_status === "running" ? "分离中…"
            : plan.separation_status === "failed" ? "分离失败"
            : plan.separation_status === "done" ? "已完成重分离" : "";
        panel.statusEl.innerHTML = statusBadge(plan) +
            (plan.separation ? ` <span class="elv-badge">人声:${esc(plan.separation)}${sepStatus ? " · " + sepStatus : ""}</span>` : "");
        if (panel.resSepBtn) {
            panel.resSepBtn.style.display = plan.mode === "singing" ? "inline-block" : "none";
            panel.resSepBtn.disabled = plan.separation_status === "running";
        }
        const sepErr = panel.overlay.querySelector("#elv-warns");
        if (sepErr && plan.separation_error) {
            sepErr.style.display = "block";
            sepErr.innerHTML = "⚠ 人声分离失败：" + esc(plan.separation_error);
        }
        // 错误详情：固定高度可滚动框 + 复制按钮（不再撑爆面板）
        const errBox = panel.overlay.querySelector("#elv-errorbox");
        if (errBox) {
            if (plan.error) {
                errBox.style.display = "block";
                errBox.querySelector("#elv-error-text").textContent = plan.error;
                errBox.querySelector("#elv-error-copy").onclick = async () => {
                    try {
                        await navigator.clipboard.writeText(plan.error || "");
                        errBox.querySelector("#elv-error-copy").textContent = "✓ 已复制";
                        setTimeout(() => {
                            const b = errBox.querySelector("#elv-error-copy");
                            if (b) b.textContent = "📋 复制全部错误";
                        }, 1500);
                    } catch (err) { alert("复制失败：" + err.message); }
                };
            } else {
                errBox.style.display = "none";
            }
        }
        const warnEl = panel.overlay.querySelector("#elv-warns");
        if (warnEl) {
            const warns = plan.warnings || [];
            warnEl.style.display = warns.length ? "block" : "none";
            warnEl.innerHTML = warns.map((wn) => `⚠ ${esc(wn)}`).join("<br>");
        }
        // H3 接线指引（唱歌模式尤其重要：口型必须用人声驱动）
        const wire = panel.overlay.querySelector("#elv-wire");
        if (wire) {
            if (plan.mode === "singing") {
                wire.style.display = "block";
                wire.innerHTML = "⚠ <b>唱歌模式接线提醒</b>：把 <b>vocals_padded（人声）</b>接到 H3 的音频输入" +
                    "（驱动口型与动作，避免人物跟着鼓点贝斯乱开口）；" +
                    "成片音轨（VHS 的 audio 输入）用 <b>original_audio_padded（原曲）</b>，保留伴奏。";
            } else {
                wire.style.display = "none";
            }
        }
        renderSegments(panel.listEl, plan);
        bindSegmentEvents(panel);
        // 桌面通知：状态变为失败时提醒一次
        if (panel._lastStatus && panel._lastStatus !== "failed" && plan.run_status === "failed") {
            elvNotify("⚠️ 长视频生成失败", plan.error || "请打开面板查看详情。");
        }
        panel._lastStatus = plan.run_status;
        const running = ["running", "pausing", "stopping", "merging"].includes(plan.run_status);
        // 诊断统计行（实时段状态 + 打开时缓存的识别/声学统计）
        const done = plan.segments.filter((r) => r.job?.status === "completed").length;
        const lowConf = plan.segments.filter((r) => (r.boundary_confidence ?? 1) < 0.5).length;
        const a = panel.analysis;
        const stats = [];
        if (a) {
            if (a.phrases?.length) stats.push(`${a.phrases.length} 句识别短语`);
            if (a.sections?.length) stats.push(`${a.sections.length} 段疑似无人声区`);
            if (a.protected_words?.length) stats.push(`${a.protected_words.length} 个受保护词`);
        }
        stats.push(`${done}/${plan.segments.length} 段已生成`);
        if (lowConf) stats.push(`⚠ ${lowConf} 个低置信切点（请试听）`);
        if (plan.separation) stats.push(`人声:${plan.separation}`);
        const diagEl = panel.overlay.querySelector("#elv-diag");
        if (diagEl) diagEl.innerHTML = `<span class="elv-hint" style="margin:0">诊断：${stats.map(esc).join(" · ")}</span>`;
        panel.btnRun.disabled = running;
        panel.btnApprove.disabled = running;
        panel.btnPause.disabled = !running;
        panel.btnStop.disabled = !running;
        panel.finalEl.innerHTML = plan.final_video
            ? `<video controls style="width:100%;border-radius:8px" src="/elv/project/${plan.id}/final"></video>` : "";
        if (panel.retryInput && document.activeElement !== panel.retryInput) {
            panel.retryInput.value = plan.auto_retry ?? 0;
        }
        // 导出选项同步
        if (panel.exportPrefix && document.activeElement !== panel.exportPrefix) {
            panel.exportPrefix.value = plan.export_prefix ?? "";
        }
        if (panel.exportDl) panel.exportDl.checked = !!plan.export_to_downloads;
        // 间奏批量标记按钮显示条件
        if (panel.muteInterludesBtn) {
            panel.muteInterludesBtn.style.display =
                (plan.mode === "singing" && panel.analysis?.sections?.length)
                    ? "inline-block" : "none";
        }
        // 间奏静音灵敏度（唱歌模式显示）
        const gateWrap = panel.overlay.querySelector("#elv-gate-wrap");
        if (gateWrap) {
            gateWrap.style.display = plan.mode === "singing" ? "inline" : "none";
            if (document.activeElement !== panel.gateInput) {
                panel.gateInput.value = plan.vocals_gate_thr_scale ?? 0.22;
                panel.gateVal.textContent = "×" + (+panel.gateInput.value).toFixed(2);
            }
        }
        if (!running && panel.polling) { clearInterval(panel.polling); panel.polling = null; }
    } catch (err) { /* 静默轮询错误 */ }
}

async function fillHistory(panel) {
    try {
        const projects = await apiGet("/elv/projects");
        const options = projects.map((p) =>
            `<option value="${p.id}" ${p.id === panel.projectId ? "selected" : ""}>${new Date(p.created * 1000).toLocaleString()} · ${p.mode === "speaking" ? "口播" : "唱歌"} · ${p.count}段 · ${fmt(p.duration)}</option>`);
        panel.historyEl.innerHTML =
            `<option value="" disabled>选择历史项目…</option>` + options.join("");
    } catch (err) { panel.historyEl.innerHTML = ""; }
}

function openPanel(projectId) {
    injectStyles();
    closePanel();
    const overlay = document.createElement("div");
    overlay.className = "elv-overlay";
    overlay.innerHTML = `
    <div class="elv-panel">
        <div class="elv-head">
            <h3>🎬 长视频分段审核</h3>
            <select class="elv-select" id="elv-history" style="max-width:300px"></select>
            <span id="elv-status"></span>
            <button class="elv-close" title="关闭">✕</button>
        </div>
        <div class="elv-body">
            <div class="elv-hint" id="elv-wire" style="display:none;color:#f0c674;background:#2a2416;border:1px solid #4a3a17;border-radius:8px;padding:8px 10px;margin-bottom:8px"></div>
            <div class="elv-warn" id="elv-warns" style="display:none;background:#2a1f16;border:1px solid #4a3517;border-radius:8px;padding:8px 10px;margin-bottom:8px"></div>
            <div class="elv-errorbox" id="elv-errorbox" style="display:none">
                <div class="elv-errorbox-head">
                    <span class="elv-badge err">失败详情</span>
                    <span class="elv-hint" style="margin:0">错误摘要见上方；完整信息可滚动查看</span>
                    <span class="elv-spacer"></span>
                    <button class="elv-btn" id="elv-error-copy">📋 复制全部错误</button>
                </div>
                <pre id="elv-error-text"></pre>
            </div>
            <canvas class="elv-wave" id="elv-wave" height="110"></canvas>
            <div class="elv-hint" id="elv-wavelabel"></div>
            <div id="elv-diag" style="margin:2px 0 6px"></div>
            <div style="display:flex;gap:8px;align-items:center;margin-bottom:6px">
                <audio class="elv-fullaudio" controls preload="none" data-track="source"
                    src="/elv/project/${projectId}/audio" title="整曲试听" style="flex:1"></audio>
                <button class="elv-btn" id="elv-full-track" style="padding:4px 10px;font-size:12px" title="切换整曲试听音轨">🎤 听人声版</button>
            </div>
            <div class="elv-hint">红色竖线为切点；每段可试听，或点「▶ 连播」按顺序试听全部段。
                「◎ 试听切点」播放切点前后各2秒，用于精听切点质量。修改简报后自动保存；
                调整切点后需要重新「保存并确认」。</div>
            <div id="elv-list"></div>
            <div id="elv-final"></div>
        </div>
        <div class="elv-foot">
            <button class="elv-btn primary" id="elv-approve">✓ 保存并确认</button>
            <button class="elv-btn primary" id="elv-run">▶ 开始顺序生成</button>
            <button class="elv-btn" id="elv-pause">⏸ 暂停</button>
            <button class="elv-btn danger" id="elv-stop">⏹ 停止</button>
            <button class="elv-btn" id="elv-assemble">🎞 仅重新合成</button>
            <button class="elv-btn" id="elv-reseparate" style="display:none">🎤 重新分离人声</button>
            <button class="elv-btn" id="elv-playall">▶ 连播全部分段</button>
            <label style="font-size:12px;color:#99a" title="连播时使用分离后的人声轨（检查各段泄漏）"><input type="checkbox" id="elv-play-vocals"> 人声连播</label>
            <button class="elv-btn" id="elv-reveal">📂 成片位置</button>
            <input class="elv-mini" id="elv-export-prefix" placeholder="成片文件名前缀" style="width:110px" title="成片文件命名前缀（留空用时间戳）">
            <label style="font-size:12px;color:#99a" title="合成完成后自动复制一份到系统下载文件夹"><input type="checkbox" id="elv-export-dl"> 到下载</label>
            <span class="elv-spacer"></span>
            <label style="font-size:12px;color:#99a;display:none" id="elv-gate-wrap">间奏静音灵敏度
                <input type="range" id="elv-gate" min="0.05" max="0.6" step="0.01" style="width:90px;vertical-align:middle">
                <span id="elv-gate-val" style="color:#ddd"></span></label>
            <button class="elv-btn" id="elv-mute-interludes" style="display:none" title="批量标记分析检测出的无人声区段落（可单独取消）">🔇 标记间奏段</button>
            <button class="elv-btn" id="elv-ref-note" title="为所有段落简报追加四视图参考图画面约束（防止人物变成四视图拼图）">📌 追加参考图约束</button>
            <label style="font-size:12px;color:#99a">失败自动重试
                <input class="elv-mini" id="elv-retry" type="number" min="0" max="9" value="0" style="width:52px"> 次</label>
            <label style="font-size:12px;color:#99a">视频输出节点：</label>
            <select class="elv-select" id="elv-video"></select>
            <div class="note" id="elv-note"></div>
        </div>
    </div>`;
    document.body.appendChild(overlay);
    overlay.querySelector(".elv-close").onclick = () => { stopPlaylist(panel); closePanel(); };
    overlay.addEventListener("mousedown", (e) => {
        if (e.target === overlay) { stopPlaylist(panel); closePanel(); }
    });

    panel = { projectId, overlay, plan: null, polling: null, playlist: null,
        statusEl: overlay.querySelector("#elv-status"),
        listEl: overlay.querySelector("#elv-list"),
        finalEl: overlay.querySelector("#elv-final"),
        noteEl: overlay.querySelector("#elv-note"),
        historyEl: overlay.querySelector("#elv-history"),
        videoSelect: overlay.querySelector("#elv-video"),
        retryInput: overlay.querySelector("#elv-retry"),
        gateInput: overlay.querySelector("#elv-gate"),
        gateVal: overlay.querySelector("#elv-gate-val"),
        muteInterludesBtn: overlay.querySelector("#elv-mute-interludes"),
        playVocalsChk: overlay.querySelector("#elv-play-vocals"),
        exportPrefix: overlay.querySelector("#elv-export-prefix"),
        exportDl: overlay.querySelector("#elv-export-dl"),
        playBtn: overlay.querySelector("#elv-playall"),
        btnApprove: overlay.querySelector("#elv-approve"),
        btnRun: overlay.querySelector("#elv-run"),
        btnPause: overlay.querySelector("#elv-pause"),
        btnStop: overlay.querySelector("#elv-stop") };

    panel.historyEl.onchange = () => {
        if (panel.historyEl.value) openPanel(panel.historyEl.value);
    };

    // 波形切点拖拽：mousedown 命中切点手柄 → 移动 → mouseup 保存
    const waveCanvas = overlay.querySelector("#elv-wave");
    let drag = null;
    const cutFromX = (clientX) => {
        const rect = waveCanvas.getBoundingClientRect();
        return Math.max(0, Math.min(panel.plan.duration,
            ((clientX - rect.left) / rect.width) * panel.plan.duration));
    };
    waveCanvas.addEventListener("mousedown", (e) => {
        if (!panel.plan || !panel.plan.approved === undefined) { /* 允许 draft 也可拖 */ }
        if (!panel.plan) return;
        const rect = waveCanvas.getBoundingClientRect();
        const t = cutFromX(e.clientX);
        const tol = (12 / rect.width) * panel.plan.duration; // 12px 命中容差
        let best = null;
        (waveCanvas._cutTimes || []).forEach((ct, i) => {
            const d = Math.abs(ct - t);
            if (d < tol && (!best || d < best.d)) best = { boundary: i + 1, t: ct, d };
        });
        if (best) {
            drag = best;
            e.preventDefault();
            panel.noteEl.textContent = "拖动切点中…松开保存（两侧分段保持至少 3 秒）";
        }
    });
    waveCanvas.addEventListener("mousemove", (e) => {
        if (!drag) return;
        drag.t = cutFromX(e.clientX);
        drawWave(waveCanvas, panel.analysis, panel.plan, drag);
    });
    window.addEventListener("mouseup", () => {
        if (!drag) return;
        const d = drag;
        drag = null;
        if (!panel.plan) return;
        const segs = panel.plan.segments;
        const sr = panel.plan.sample_rate;
        const left = segs[d.boundary - 1], right = segs[d.boundary];
        const lo = (left.start_sample + 3 * sr) / sr;
        const hi = (right.end_sample - 3 * sr) / sr;
        const t = Math.round(Math.max(lo, Math.min(hi, d.t)) * 1000) / 1000;
        apiPost(`/elv/project/${panel.projectId}/edit`, {
            revision: panel.plan.revision,
            operations: [{ op: "move_boundary", boundary: d.boundary, at: t }],
        }).then((p) => {
            panel.plan = p;
            panel.noteEl.textContent =
                `切点已移至 ${t.toFixed(2)}s。已自动取消确认状态，请重新「保存并确认」。`;
            refresh(panel);
        }).catch((err) => { alert(err.message); refresh(panel); });
    });
    panel.retryInput.onchange = async () => {
        try {
            await apiPost(`/elv/project/${panel.projectId}/settings`,
                { auto_retry: +panel.retryInput.value || 0 });
            panel.noteEl.textContent = "已保存：失败自动重试 " +
                (+panel.retryInput.value || 0) + " 次。";
        } catch (err) { alert(err.message); }
    };
    // 间奏静音灵敏度（越小越严格：间奏压得越干净，太紧会误伤轻声演唱）
    panel.gateInput.onchange = async () => {
        try {
            const v = +panel.gateInput.value || 0.22;
            await apiPost(`/elv/project/${panel.projectId}/settings`,
                { vocals_gate_thr_scale: v });
            panel.gateVal.textContent = "×" + v.toFixed(2);
            panel.noteEl.textContent =
                "门限已保存（×" + v.toFixed(2) + "）。对某段生效请「重做本段」" +
                "（配合「🎤 重分离本段」效果更佳）。";
        } catch (err) { alert(err.message); }
    };
    // 一键批量标记间奏段（分析检测的无人声区）
    panel.muteInterludesBtn.onclick = async () => {
        if (!confirm("将分析检测出的「无人声区」段落批量标记为 🔇 静音驱动？\n" +
                "（标记段的人物将不开口；可单独取消某段的标记）")) return;
        try {
            const res = await apiPost(`/elv/project/${panel.projectId}/mute-interludes`,
                { mute: true });
            panel.noteEl.textContent = res.marked.length
                ? `已标记 ${res.marked.length} 个间奏段：第 ${res.marked.map((x) => x + 1).join("、")} 段。重做对应段后生效。`
                : "没有检测到落在无人声区内的分段。";
            refresh(panel);
        } catch (err) { alert(err.message); }
    };
    // 成片导出选项
    panel.exportPrefix.onchange = async () => {
        try {
            await apiPost(`/elv/project/${panel.projectId}/settings`,
                { export_prefix: panel.exportPrefix.value });
            panel.noteEl.textContent = "成片文件名前缀已保存。";
        } catch (err) { alert(err.message); }
    };
    panel.exportDl.onchange = async () => {
        try {
            await apiPost(`/elv/project/${panel.projectId}/settings`,
                { export_to_downloads: panel.exportDl.checked });
            panel.noteEl.textContent = panel.exportDl.checked
                ? "合成后将自动复制一份到系统下载文件夹。"
                : "已关闭自动复制到下载文件夹。";
        } catch (err) { alert(err.message); }
    };
    // 桌面通知（生成完成/失败提醒）
    try {
        if ("Notification" in window && Notification.permission === "default") {
            Notification.requestPermission();
        }
    } catch (err) { /* 忽略 */ }
    // 一键为所有段落追加四视图参考图约束
    overlay.querySelector("#elv-ref-note").onclick = async () => {
        const note = "画面约束：参考图为人物多视角设定图（四视图），仅用于锁定人物长相、发型、" +
            "服装与身份一致性；视频画面始终是同一位人物在真实场景中的连续实拍镜头，" +
            "画面中自始至终只出现这一个人物；严禁重现参考图的拼接版式、白底设定图样式，" +
            "严禁同时出现多个人物或多个分身。";
        if (!confirm("为所有段落的镜头简报追加参考图画面约束？\n（已有该约束的段落会自动跳过）")) return;
        try {
            panel.plan = await apiPost(`/elv/project/${panel.projectId}/edit`,
                { revision: panel.plan.revision,
                  operations: [{ op: "append_note", text: note }] });
            panel.noteEl.textContent = "已为全部段落追加参考图约束，请重新「保存并确认」后生效。";
            refresh(panel);
        } catch (err) { alert(err.message); }
    };
    panel.playBtn.onclick = () =>
        panel.playlist ? stopPlaylist(panel) : startPlaylist(panel);
    // 整曲试听音轨切换
    const fullTrackBtn = overlay.querySelector("#elv-full-track");
    fullTrackBtn.onclick = () => {
        const audio = overlay.querySelector(".elv-fullaudio");
        const toVocals = audio.dataset.track !== "vocals";
        audio.dataset.track = toVocals ? "vocals" : "source";
        audio.src = `/elv/project/${panel.projectId}/audio` + (toVocals ? "?vocals=1" : "");
        audio.play().catch(() => {});
        fullTrackBtn.textContent = toVocals ? "🎧 听原曲" : "🎤 听人声版";
    };
        panel.resSepBtn = overlay.querySelector("#elv-reseparate");
        panel.resSepBtn.onclick = async () => {
            if (!confirm("重新分离人声？切点保持不变，段音频输出将自动更新\n" +
                    "（如需按新人声重算切点，请之后点「🔄 重新分析并分段」）。")) return;
            try {
                await apiPost(`/elv/project/${panel.projectId}/re-separate`, {});
                panel.noteEl.textContent = "人声分离已启动（约 1~2 分钟），面板状态会显示进度。";
                refresh(panel);
            } catch (err) { alert(err.message); }
        };
    overlay.querySelector("#elv-reveal").onclick = () =>
        apiPost(`/elv/project/${panel.projectId}/reveal-final`, {})
            .catch((e) => alert(e.message));

    panel.btnApprove.onclick = async () => {
        try {
            panel.plan = await apiPost(`/elv/project/${panel.projectId}/approve`,
                { revision: panel.plan.revision });
            panel.noteEl.textContent = "已确认分段方案，可以开始顺序生成。";
            refresh(panel);
        } catch (err) { alert(err.message); }
    };
    panel.btnRun.onclick = async () => {
        try {
            const payload = await collectPromptPayload(panel);
            await apiPost(`/elv/project/${panel.projectId}/run`, payload);
            panel.noteEl.textContent = "顺序生成已启动；可随时暂停，关闭面板不影响运行。";
            startPolling(panel);
            refresh(panel);
        } catch (err) { alert(err.message); }
    };
    panel.btnPause.onclick = () => apiPost(`/elv/project/${panel.projectId}/pause`)
        .then(() => refresh(panel)).catch((e) => alert(e.message));
    panel.btnStop.onclick = () => apiPost(`/elv/project/${panel.projectId}/stop`)
        .then(() => refresh(panel)).catch((e) => alert(e.message));
    overlay.querySelector("#elv-assemble").onclick = () =>
        apiPost(`/elv/project/${panel.projectId}/assemble`)
            .then(() => refresh(panel)).catch((e) => alert(e.message));

    // 输出节点下拉：从画布收集
    app.graphToPrompt().then(({ output }) => {
        const options = [];
        for (const [id, node] of Object.entries(output || {})) {
            options.push(`<option value="${id}">${id}: ${esc(node.class_type)}</option>`);
        }
        panel.videoSelect.innerHTML = options.join("");
        const prefer = [...panel.videoSelect.options]
            .find((o) => /videocombine/i.test(o.text));
        if (prefer) panel.videoSelect.value = prefer.value;
    });

    fillHistory(panel);
    startPolling(panel);
    refresh(panel);
}

function startPolling(panel) {
    if (panel.polling) clearInterval(panel.polling);
    panel.polling = setInterval(() => refresh(panel), 2000);
}

function closePanel() {
    if (panel) {
        if (panel.polling) clearInterval(panel.polling);
        panel.overlay.remove();
        panel = null;
    }
}

// ---------------------------------------------------------------- 运镜规则编辑

async function openRulesDialog() {
    injectStyles();
    let rules;
    try {
        rules = await apiGet("/elv/rules");
    } catch (err) { alert("读取运镜规则失败：" + err.message); return; }
    const overlay = document.createElement("div");
    overlay.className = "elv-overlay";
    overlay.innerHTML = `
    <div class="elv-panel" style="width:min(760px,92vw)">
        <div class="elv-head">
            <h3>⚙ 运镜规则（唱歌模式）</h3>
            <button class="elv-close" title="关闭">✕</button>
        </div>
        <div class="elv-body">
            <div class="elv-hint">修改后点击保存；<b>重新分析并分段</b>后生效（已确认的项目不会自动变化）。
                各字段含义：move_pool=各能量档可用运镜池；allowed_angles=可用机位角；
                no_adjacent_same_family=相邻段不同类运镜；alternate_lateral_direction=横移方向交替；
                avoid_direct_axis_cross=避免直接跨轴线；performance_text=三档表演节奏文案。
                可用运镜值：${["truck_left", "truck_right", "arc_left", "arc_right", "dolly_in", "dolly_out", "micro_reframe"].join(" / ")}</div>
            <textarea class="elv-brief" id="elv-rules-text" style="min-height:320px;font-family:Consolas,monospace">${esc(JSON.stringify(rules, null, 2))}</textarea>
            <div class="elv-warn" id="elv-rules-err"></div>
        </div>
        <div class="elv-foot">
            <button class="elv-btn primary" id="elv-rules-save">💾 保存规则</button>
            <button class="elv-btn" id="elv-rules-reset">↺ 恢复默认</button>
            <span class="elv-spacer"></span>
            <span class="elv-hint">保存位置：ComfyUI 用户目录/EasyLongVideo/camera_rules.json</span>
        </div>
    </div>`;
    document.body.appendChild(overlay);
    overlay.querySelector(".elv-close").onclick = () => overlay.remove();
    overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) overlay.remove(); });
    overlay.querySelector("#elv-rules-save").onclick = async () => {
        try {
            const parsed = JSON.parse(overlay.querySelector("#elv-rules-text").value);
            rules = await apiPost("/elv/rules", parsed);
            overlay.querySelector("#elv-rules-text").value = JSON.stringify(rules, null, 2);
            overlay.querySelector("#elv-rules-err").textContent =
                "✓ 已保存。点击节点上的「重新分析并分段」后生效。";
        } catch (err) {
            overlay.querySelector("#elv-rules-err").textContent = "✖ " + err.message;
        }
    };
    overlay.querySelector("#elv-rules-reset").onclick = async () => {
        try {
            rules = await apiPost("/elv/rules/reset", {});
            overlay.querySelector("#elv-rules-text").value = JSON.stringify(rules, null, 2);
            overlay.querySelector("#elv-rules-err").textContent = "✓ 已恢复默认（尚未保存前可继续编辑）。";
        } catch (err) {
            overlay.querySelector("#elv-rules-err").textContent = "✖ " + err.message;
        }
    };
}

// ---------------------------------------------------------------- 节点进度

function findNodeByProject(projectId) {
    let found = null;
    for (const node of Object.values(app.graph._nodes || {})) {
        if (node.type !== NODE_TYPE) continue;
        const widget = node.widgets?.find((w) => w.name === "project_id");
        if (widget?.value === projectId) { found = node; break; }
        if (!found && widget) found = node; // 兜底：第一个 EasyLVUnified
    }
    return found;
}

function setProgress(projectId, text) {
    const node = findNodeByProject(projectId);
    if (!node) return;
    let w = node.widgets?.find((w) => w.name === "elv_progress");
    if (!w) {
        w = node.addWidget("button", text, null, () => {});
        w.name = "elv_progress";
        w.serialize = false;
    }
    w.label = text;
    w.name = "elv_progress";
    if (w.onPropertyChanged) w.onPropertyChanged("label", text);
    app.graph.setDirtyCanvas(true, false);
}

// 节点实时进度（websocket 事件推送；api 不可用时自动降级为面板轮询）
const _elvActivePrompts = {}; // prompt_id -> {project_id, segment_index}
try {
    if (api && typeof api.addEventListener === "function") {
        api.addEventListener("elv-task", ({ detail }) => {
            if (!detail?.project_id) return;
            _elvActivePrompts[detail.prompt_id] = {
                project_id: detail.project_id, index: detail.segment_index };
            setProgress(detail.project_id,
                `🎬 第 ${detail.segment_index + 1} 段已提交，开始执行…`);
        });
        // ComfyUI 原生事件：当前执行的节点 → 实时显示执行链条
        api.addEventListener("executing", ({ detail }) => {
            if (!detail?.prompt_id || !detail.node) return;
            const info = _elvActivePrompts[detail.prompt_id];
            if (!info) return;
            const typeName = (window._elvWorkflowMap || {})[String(detail.node)]
                || ("节点 " + detail.node);
            setProgress(info.project_id,
                `⚙ 第 ${info.index + 1} 段执行中：${typeName}`);
        });
        // 采样进度百分比
        api.addEventListener("progress", ({ detail }) => {
            if (!detail?.prompt_id) return;
            const info = _elvActivePrompts[detail.prompt_id];
            if (!info) return;
            const pct = detail.max ? Math.round((detail.value / detail.max) * 100) : 0;
            setProgress(info.project_id, `🎨 第 ${info.index + 1} 段采样中 ${pct}%`);
        });
        api.addEventListener("elv-segment", ({ detail }) => {
            if (!detail?.project_id) return;
            const { project_id, segment_index, total } = detail;
            setProgress(project_id, `🎬 正在生成第 ${segment_index + 2}/${total} 段…（已完成 ${segment_index + 1}）`);
            if (panel && panel.projectId === project_id) refresh(panel);
        });
        api.addEventListener("elv-retry", ({ detail }) => {
            if (!detail?.project_id) return;
            const { project_id, segment_index, attempt, max_retry, error } = detail;
            setProgress(project_id, `⚠ 第 ${segment_index + 1} 段失败，自动重试 ${attempt}/${max_retry}…`);
        });
        api.addEventListener("elv-final", ({ detail }) => {
            if (!detail?.project_id) return;
            setProgress(detail.project_id, "✅ 全部完成，成片已合成");
            elvNotify("🎬 长视频生成完成", "全部段落已合成成片，打开面板查看。");
            if (panel && panel.projectId === detail.project_id) refresh(panel);
        });
    } else {
        console.warn("[EasyLongVideo] api 实例不可用，进度推送降级为面板轮询。");
    }
} catch (err) {
    console.warn("[EasyLongVideo] 事件监听注册失败（不影响面板）:", err);
}

// 桌面通知（浏览器 Notification，需用户授权一次）
function elvNotify(title, body) {
    try {
        if (!("Notification" in window) || Notification.permission !== "granted") return;
        new Notification(title, { body: (body || "").slice(0, 180) });
    } catch (err) { /* 忽略 */ }
}

// ---------------------------------------------------------------- 扩展注册

async function loadWaveAndMaybeOpen(node, projectId) {
    openPanel(projectId);
    try {
        const analysis = await apiGet(`/elv/project/${projectId}/analysis`);
        const plan = await apiGet(`/elv/project/${projectId}`);
        if (panel) {
            panel.plan = plan;
            panel.analysis = analysis;
            drawWave(panel.overlay.querySelector("#elv-wave"), analysis, plan);
            const wl = panel.overlay.querySelector("#elv-wavelabel");
            if (wl) {
                const dual = !!analysis.waveform?.vocals;
                wl.textContent = dual
                    ? "上轨：原曲（含伴奏） · 下轨：人声（口型驱动用） · 红线：切点 · 黄标：分段"
                    : "单轨波形（未分离人声） · 红线：切点 · 黄标：分段";
            }
            renderSegments(panel.listEl, plan);
            bindSegmentEvents(panel);
        }
    } catch (err) { /* 忽略 */ }
}

app.registerExtension({
    name: "EasyLongVideo.Panel",
    beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData?.name !== NODE_TYPE) return;
        const onExecuted = nodeType.prototype.onExecuted;
        nodeType.prototype.onExecuted = function (message) {
            onExecuted?.apply(this, arguments);
            const ids = message?.elv_project;
            if (ids?.length) {
                loadWaveAndMaybeOpen(this, String(ids[0]));
                const widget = this.widgets?.find((w) => w.name === "project_id");
                if (widget) widget.value = String(ids[0]);
            }
        };
        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            onNodeCreated?.apply(this, arguments);
            const self = this;
            setTimeout(() => {
                // 参数中文名
                try {
                    for (const w of self.widgets || []) {
                        if (CN_LABELS[w.name]) w.label = CN_LABELS[w.name];
                    }
                } catch (err) { console.warn("[EasyLongVideo] 中文标签失败:", err); }
                // 防崩加固：每个控件独立容错，单个失败不影响其余
                try {
                    const preset = self.addWidget("combo", "⚙ 参数预设", "自定义", (v) => {
                        try {
                            if (v === "自定义" || !PRESETS[v]) return;
                            for (const [name, value] of Object.entries(PRESETS[v])) {
                                const w = self.widgets?.find((x) => x.name === name);
                                if (w) { w.value = value; }
                            }
                            app.graph.setDirtyCanvas(true, false);
                        } catch (err) { console.warn("[EasyLongVideo] 预设失败:", err); }
                    }, { values: Object.keys(PRESETS).concat(["自定义"]) });
                    preset.serialize = false;
                } catch (err) { console.warn("[EasyLongVideo] 预设下拉失败:", err); }
                const makeBtn = (label, handler) => {
                    try {
                        const btn = self.addWidget("button", label, null, handler);
                        btn.serialize = false;
                        return btn;
                    } catch (err) {
                        console.warn("[EasyLongVideo] 按钮 " + label + " 添加失败:", err);
                        return null;
                    }
                };
                makeBtn("⚙ 运镜规则：查看与修改", openRulesDialog);
                makeBtn("🔄 重新分析并分段", () => {
                    const widget = self.widgets?.find((w) => w.name === "project_id");
                    if (widget) widget.value = "";
                    alert("已重置项目编号。\n请点击 ComfyUI 的「运行 (Queue)」，将按当前参数重新分析并分段。");
                });
                makeBtn("📋 打开分段与生成控制", () => {
                    const widget = self.widgets?.find((w) => w.name === "project_id");
                    if (widget?.value) loadWaveAndMaybeOpen(self, widget.value);
                    else alert("请先运行一次节点完成音频分析。");
                });
            }, 0);
        };
    },
});

console.log("[EasyLongVideo] 前端扩展已加载");
