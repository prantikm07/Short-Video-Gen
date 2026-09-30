// ===================================================
// VIRALREEL AI — CLIENT-SIDE JAVASCRIPT
// Flow: paste link -> pick reel count + min/max seconds
//       -> AI detects best moments (background job polling)
//       -> cut unedited 9:16 clips + .srt + title + caption
// All downloads go through /api/download/* so the browser
// always saves the file instead of opening it in a tab.
// ===================================================

let currentVideoInfo = null;
let currentMoments = [];
let loadingInterval = null;
let currentModalReel = null;

function dlClipUrl(url, filename) {
  return `/api/download/clip?url=${encodeURIComponent(url)}&name=${encodeURIComponent(filename || "")}`;
}

function downloadViaApi(url, filename) {
  const a = document.createElement("a");
  a.href = dlClipUrl(url, filename);
  a.download = filename || url.split("/").pop();
  document.body.appendChild(a);
  a.click();
  a.remove();
}
window.downloadViaApi = downloadViaApi;

document.addEventListener("DOMContentLoaded", () => {
  checkApiStatus();

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeVideoModal();
  });

  loadBatches();
});

// Toast notification helper
function showToast(message, type = "success") {
  const container = document.getElementById("toast-container");
  if (!container) return;
  const toast = document.createElement("div");
  toast.className = `toast toast-${type}`;
  toast.innerHTML = `<span>${type === "success" ? "\u2713" : "\u26a0\ufe0f"}</span> <span>${message}</span>`;
  container.appendChild(toast);
  setTimeout(() => {
    toast.style.opacity = "0";
    setTimeout(() => toast.remove(), 300);
  }, 4200);
}

function formatTime(seconds) {
  const s = Math.max(0, Math.round(Number(seconds) || 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  return `${h ? h + ":" : ""}${String(m).padStart(h ? 2 : 1, "0")}:${sec < 10 ? "0" : ""}${sec}`;
}

function escapeHtml(str) {
  if (str === null || str === undefined) return "";
  return String(str).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

async function copyText(text, label) {
  try {
    await navigator.clipboard.writeText(text || "");
    showToast(`${label} copied to clipboard!`);
  } catch (err) {
    showToast("Clipboard access denied by the browser.", "error");
  }
}

// 1. Check API key status
async function checkApiStatus() {
  try {
    const res = await fetch("/api/status");
    const data = await res.json();
    [["gemini-status-badge", data.gemini_connected], ["anthropic-status-badge", data.anthropic_connected]].forEach(([id, ok]) => {
      const badge = document.getElementById(id);
      if (!badge) return;
      const dot = badge.querySelector(".status-dot");
      if (ok) {
        badge.classList.add("status-active");
        if (dot) dot.style.backgroundColor = "var(--accent-green)";
      } else if (dot) {
        dot.style.backgroundColor = "var(--text-dim)";
      }
    });
  } catch (err) {
    console.warn("Could not check API status:", err);
  }
}

// 2. Clipboard paste helper
async function handlePasteClipboard() {
  try {
    const text = await navigator.clipboard.readText();
    if (text) {
      document.getElementById("youtube-url-input").value = text.trim();
      showToast("Pasted link from clipboard!");
    }
  } catch (err) {
    showToast("Clipboard access denied. Please paste manually.", "error");
  }
}

// 3. Analyze video -> detect the best moments (background job + polling)
async function handleAnalyze() {
  const url = document.getElementById("youtube-url-input").value.trim();
  if (!url) {
    showToast("Please enter a valid YouTube video URL", "error");
    return;
  }

  const engine = document.getElementById("engine-select").value;
  const count = parseInt(document.getElementById("clips-count-select").value, 10);
  const minDuration = parseFloat(document.getElementById("min-duration-input").value) || 20;
  const maxDuration = parseFloat(document.getElementById("max-duration-input").value) || 60;
  if (minDuration > maxDuration) {
    showToast("Min seconds must be smaller than max seconds.", "error");
    return;
  }

  const loadingSection = document.getElementById("loading-section");
  const analyzeBtn = document.getElementById("analyze-btn");
  const momentsSection = document.getElementById("moments-section");
  const overviewSection = document.getElementById("video-overview-section");
  const progressFill = document.getElementById("progress-bar-fill");
  const loadingTitle = document.getElementById("loading-title");
  const loadingDesc = document.getElementById("loading-desc");

  loadingSection.classList.remove("hidden");
  momentsSection.classList.add("hidden");
  overviewSection.classList.add("hidden");
  analyzeBtn.disabled = true;
  if (progressFill) progressFill.style.width = "3%";
  loadingTitle.textContent = "Scanning for the best moments...";
  loadingDesc.textContent = "Fetching video metadata and captions.";
  loadingSection.scrollIntoView({ behavior: "smooth" });

  try {
    const res = await fetch("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, count, engine, min_duration: minDuration, max_duration: maxDuration })
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Virality analysis failed.");

    let result = null;
    if (data.cached && data.result) {
      result = data.result;
    } else {
      result = await pollJob(data.job_id, (job) => {
        if (progressFill) progressFill.style.width = `${Math.max(3, job.percent || 0)}%`;
        if (job.message) loadingDesc.textContent = job.message;
      });
    }

    currentVideoInfo = result.video_info || {};
    if (!currentVideoInfo.webpage_url) currentVideoInfo.webpage_url = url;
    if (!currentVideoInfo.id) currentVideoInfo.id = result.video_id;
    currentMoments = result.viral_moments || [];

    document.getElementById("video-thumb").src = currentVideoInfo.thumbnail || "";
    document.getElementById("video-title").textContent = currentVideoInfo.title || "Video";
    document.getElementById("video-channel").textContent = currentVideoInfo.uploader || "";
    document.getElementById("video-duration").textContent = formatTime(currentVideoInfo.duration);
    document.getElementById("video-transcript-words").textContent = `${result.transcript_segment_count || 0} transcript segments`;
    document.getElementById("video-summary").textContent = result.summary || "Best moments detected.";
    overviewSection.classList.remove("hidden");

    renderMomentsList(currentMoments);
    momentsSection.classList.remove("hidden");
    momentsSection.scrollIntoView({ behavior: "smooth" });
    showToast(`Found ${currentMoments.length} clip-ready moments! Now hit "Cut All Reels".`);
  } catch (err) {
    showToast(err.message, "error");
  } finally {
    clearInterval(loadingInterval);
    loadingSection.classList.add("hidden");
    analyzeBtn.disabled = false;
  }
}

// Poll a background job until done/error
async function pollJob(jobId, onTick) {
  if (!jobId) throw new Error("No job id returned by the server.");
  const started = Date.now();
  while (true) {
    await new Promise(r => setTimeout(r, 1500));
    const res = await fetch(`/api/job/${jobId}`);
    if (res.status === 404) throw new Error("Job expired or was lost (server restarted?). Try again.");
    if (!res.ok) throw new Error("Job polling failed.");
    const job = await res.json();
    if (onTick) { try { onTick(job); } catch (e) {} }
    if (job.status === "done") return job.result;
    if (job.status === "error") throw new Error(job.error || "Background job failed.");
    if (Date.now() - started > 45 * 60 * 1000) throw new Error("Job timed out after 45 minutes.");
  }
}

// 4. Render detected moment cards (title, caption, timeline, srt preview, cut button)
function renderMomentsList(moments) {
  const grid = document.getElementById("moments-grid");
  grid.innerHTML = "";

  moments.forEach((m, idx) => {
    const card = document.createElement("article");
    card.className = "moment-card glass-panel";
    card.id = `moment-card-${idx}`;

    card.innerHTML = `
      <div class="moment-top-row">
        <span class="rank-badge">REEL #${idx + 1}</span>
        <div class="score-badge" title="Viral potential score">
          <span>\ud83d\udd25</span><span>${m.viral_score}/100</span>
        </div>
        <span class="timeline-pill" title="Timeline of this part in the source video">\u23f1 ${escapeHtml(m.timeline || (formatTime(m.start_time) + " - " + formatTime(m.end_time)))}</span>
      </div>

      <h3 class="moment-title">${escapeHtml(m.title)}</h3>

      <div class="hook-banner-preview">
        <span class="hook-preview-label">CAPTION TO POST</span>
        <p class="caption-text">${escapeHtml(m.caption)}</p>
      </div>

      <div class="time-adjuster-box">
        <div class="time-inputs">
          <label class="control-label">START</label>
          <input type="number" id="start-time-${idx}" class="time-input-field" value="${Number(m.start_time).toFixed(1)}" step="0.5" onchange="updateDuration(${idx})" />
          <label class="control-label">END</label>
          <input type="number" id="end-time-${idx}" class="time-input-field" value="${Number(m.end_time).toFixed(1)}" step="0.5" onchange="updateDuration(${idx})" />
        </div>
        <span id="duration-badge-${idx}" class="duration-pill">${Number(m.duration).toFixed(1)}s</span>
      </div>

      <details class="srt-details">
        <summary>\ud83d\udcac Subtitles (.srt) for this part</summary>
        <pre id="srt-preview-${idx}" class="srt-preview">Loading subtitles...</pre>
        <div class="srt-actions">
          <button type="button" class="secondary-btn tiny-btn" onclick="copySrtOf(${idx})">\ud83d\udccb Copy SRT</button>
          <button type="button" class="secondary-btn tiny-btn" onclick="downloadSrtOf(${idx})">\u2b07 Download .srt</button>
        </div>
      </details>

      <button id="cut-btn-${idx}" class="render-btn" onclick="handleCutSingle(${idx})">
        <span class="btn-icon">\u2702\ufe0f</span>
        <span class="btn-text">Cut This Clip (9:16)</span>
      </button>
    `;

    grid.appendChild(card);

    // Fetch the SRT for this range once (server caches the transcript per video)
    fetchSrtFor(idx, Number(m.start_time), Number(m.end_time));
  });
}

async function fetchSrtFor(idx, start, end) {
  const vid = (currentVideoInfo && currentVideoInfo.id) || "";
  const pre = document.getElementById(`srt-preview-${idx}`);
  try {
    const res = await fetch(`/api/subtitles/${encodeURIComponent(vid)}?start=${start}&end=${end}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "no transcript");
    currentMoments[idx].srt = data.srt || "";
    if (pre) pre.textContent = data.srt ? data.srt.slice(0, 700) + (data.srt.length > 700 ? "\n\u2026" : "") : "(no speech detected in this range)";
  } catch (err) {
    if (pre) pre.textContent = "Subtitles unavailable for this range (no captions on the source video).";
  }
}

function updateDuration(idx) {
  const start = parseFloat(document.getElementById(`start-time-${idx}`).value) || 0;
  const end = parseFloat(document.getElementById(`end-time-${idx}`).value) || 0;
  document.getElementById(`duration-badge-${idx}`).textContent = `${Math.max(0, end - start).toFixed(1)}s`;
}

function buildClipSpec(idx) {
  const m = currentMoments[idx];
  const start = parseFloat(document.getElementById(`start-time-${idx}`).value);
  const end = parseFloat(document.getElementById(`end-time-${idx}`).value);
  if (!(end > start)) {
    showToast(`Clip ${idx + 1}: end time must be after start time.`, "error");
    return null;
  }
  return {
    start_time: start,
    end_time: end,
    duration: Math.round((end - start) * 10) / 10,
    title: m.title || "",
    caption: m.caption || "",
    timeline: m.timeline || `${formatTime(start)} - ${formatTime(end)}`,
    viral_score: m.viral_score || 0,
    key_quote: m.key_quote || "",
    reason: m.reason || ""
  };
}

// 5. Cut one clip (unedited 9:16 mp4 + .srt + title + caption package)
async function handleCutSingle(idx) {
  const spec = buildClipSpec(idx);
  if (!spec) return;
  await runCutJob([spec], `Cutting Reel #${idx + 1}...`);
}

// 6. Cut all detected reels in one batch
async function handleCutAll() {
  if (!currentMoments.length) {
    showToast("Detect the moments first!", "error");
    return;
  }
  const specs = [];
  for (let i = 0; i < currentMoments.length; i++) {
    const s = buildClipSpec(i);
    if (s) specs.push(s);
  }
  if (!specs.length) return;
  await runCutJob(specs, `Cutting ${specs.length} clips...`);
}

async function runCutJob(clips, startLabel) {
  const btnAll = document.getElementById("render-all-btn");
  const progress = document.getElementById("cut-progress");
  const bar = document.getElementById("cut-progress-bar-fill");
  const title = document.getElementById("cut-loading-title");
  const desc = document.getElementById("cut-loading-desc");

  btnAll.disabled = true;
  btnAll.textContent = "\u23f3 Cutting...";
  progress.classList.remove("hidden");
  title.textContent = startLabel;
  desc.textContent = "Downloading the source video at max quality (cached between batches).";
  bar.style.width = "2%";
  progress.scrollIntoView({ behavior: "smooth" });

  try {
    const res = await fetch("/api/cut", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: currentVideoInfo.webpage_url,
        video_id: currentVideoInfo.id || "",
        clips
      })
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Cut request failed.");

    const result = await pollJob(data.job_id, (job) => {
      bar.style.width = `${Math.max(2, job.percent || 0)}%`;
      if (job.message) desc.textContent = job.message;
    });

    showToast(`\u2705 ${result.manifest.clip_count} clip package(s) ready! Downloads below.`);
    renderResults(result);
    loadBatches();
  } catch (err) {
    showToast(err.message, "error");
  } finally {
    progress.classList.add("hidden");
    btnAll.disabled = false;
    btnAll.textContent = "\u2702\ufe0f Cut All Reels (9:16)";
  }
}

function renderResults(result) {
  const manifest = result.manifest;
  (manifest.clips || []).forEach(c => {
    if (c.error) {
      showToast(`Clip ${c.index} failed: ${c.error}`, "error");
      return;
    }
    const idx = c.index - 1;
    const btn = document.getElementById(`cut-btn-${idx}`);
    if (btn) {
      btn.classList.add("done");
      btn.querySelector(".btn-text").textContent = "\u2705 Clip Ready \u2014 Preview";
      btn.onclick = () => openClipModal(c);
    }
  });

  let box = document.getElementById("batch-results");
  if (!box) {
    box = document.createElement("div");
    box.id = "batch-results";
    box.className = "batch-results glass-panel";
    document.getElementById("moments-grid").after(box);
  }
  box.innerHTML = `
    <div class="section-header">
      <div>
        <h2 class="section-title">\ud83d\udce6 Latest Batch: ${escapeHtml(manifest.video_title || manifest.batch_id)}</h2>
        <p class="section-subtitle">Unedited 9:16 clips with .srt + title + caption sidecar files.</p>
      </div>
      ${result.zip_url ? `<a class="primary-btn zip-btn" href="${result.zip_url}" download>\u2b07\ufe0f Download All as ZIP</a>` : ""}
    </div>
  `;
  const list = document.createElement("div");
  list.className = "results-list";
  (manifest.clips || []).filter(c => !c.error).forEach(c => {
    const row = document.createElement("div");
    row.className = "result-row";
    row.innerHTML = `
      <span class="result-timeline">\u23f1 ${escapeHtml(c.timeline)}</span>
      <span class="result-title" title="${escapeHtml(c.title)}">${escapeHtml(c.title)}</span>
      <span class="result-size">${c.size_mb || "?"} MB</span>
      <button type="button" class="tiny-link" onclick="downloadViaApi('${c.url}')">\u2b07 mp4</button>
      ${c.srt_url ? `<button type="button" class="tiny-link" onclick="downloadViaApi('${c.srt_url}')">\u2b07 srt</button>` : ""}
    `;
    const preview = document.createElement("button");
    preview.className = "secondary-btn tiny-btn";
    preview.textContent = "Preview";
    preview.onclick = () => openClipModal(c);
    row.appendChild(preview);
    list.appendChild(row);
  });
  box.appendChild(list);
  box.scrollIntoView({ behavior: "smooth" });
}

// 7. Clip packages gallery (scans the served /clips/ directory listing)
async function scanClipsDir(path) {
  const res = await fetch(path);
  if (!res.ok) return [];
  const html = await res.text();
  const doc = new DOMParser().parseFromString(html, "text/html");
  return Array.from(doc.querySelectorAll("a"))
    .map(a => decodeURIComponent(a.getAttribute("href")))
    .filter(h => h && !h.startsWith("?") && !h.startsWith("/"));
}

async function loadBatches() {
  const grid = document.getElementById("gallery-grid");
  const empty = document.getElementById("gallery-empty");
  try {
    const entries = await scanClipsDir("/clips/");
    const dirs = entries.filter(e => e.endsWith("/") && !e.includes("_clips.zip")).map(e => e.replace(/\/$/, ""));
    const zips = entries.filter(e => e.endsWith("_clips.zip"));

    const batches = [];
    for (const dir of dirs.slice(-12).reverse()) {
      try {
        const r = await fetch(`/api/batch/${encodeURIComponent(dir)}`);
        if (r.ok) batches.push(await r.json());
      } catch (e) { /* skip broken batch */ }
    }
    for (const z of zips.slice(-12).reverse()) {
      const dir = z.replace(/_clips\.zip$/, "");
      if (batches.some(b => b.batch_id === dir)) continue;
      batches.push({ batch_id: dir, zip_only: true, zip_url: `/clips/${z}`, zip_name: z });
    }

    if (!batches.length) {
      grid.innerHTML = "";
      empty.classList.remove("hidden");
      return;
    }
    empty.classList.add("hidden");
    grid.innerHTML = "";

    batches.forEach(b => {
      const card = document.createElement("div");
      card.className = "gallery-card glass-panel";
      if (b.zip_only) {
        card.innerHTML = `
          <div class="gallery-card-title" title="${escapeHtml(b.batch_id)}">${escapeHtml(b.batch_id)}</div>
          <div class="gallery-card-meta"><span>ZIP package</span><span>9:16 Vertical</span></div>
          <div class="gallery-actions">
            <button type="button" class="download-reel-btn" onclick="window.location.href='${b.zip_url}'">\u2b07 Save ZIP</button>
          </div>`;
      } else {
        const first = (b.clips || []).find(c => c.url);
        card.innerHTML = `
          <div class="gallery-preview-wrapper">
            ${first ? `<video class="gallery-video-preview" src="${first.url}#t=0.5" preload="metadata" muted playsinline loop></video>
            <div class="preview-play-overlay">\u25b6</div>` : ""}
          </div>
          <div class="gallery-card-title" title="${escapeHtml(b.video_title || b.batch_id)}">${escapeHtml(b.video_title || b.batch_id)}</div>
          <div class="gallery-card-meta">
            <span>${(b.clips || []).filter(c => !c.error).length} clips</span>
            <span>9:16 Unedited</span>
          </div>
          <div class="gallery-actions">
            ${first ? `<button type="button" class="preview-reel-btn" onclick='openBatchFirst(${JSON.stringify(first.url)})'>\u25b6 Preview</button>` : ""}
            <button type="button" class="download-reel-btn" onclick="window.location.href='/api/download/${encodeURIComponent(b.batch_id)}'">\u2b07 Download All (ZIP)</button>
          </div>`;
      }
      grid.appendChild(card);
    });
  } catch (err) {
    console.warn("Could not load clip packages:", err);
  }
}

function openBatchFirst(url) {
  openClipModal({ url, title: "Clip", caption: "", timeline: "", srt_text: "" });
}
window.openBatchFirst = openBatchFirst;

// 8. Clip modal — preview + every deliverable in one place
function openClipModal(clip) {
  if (!clip || !clip.url) return;
  currentModalReel = clip;
  const modal = document.getElementById("video-modal");
  const player = document.getElementById("modal-video-player");

  player.src = clip.url;
  player.load();
  player.play().catch(() => {});

  document.getElementById("modal-reel-title").textContent = clip.title || "Clip";
  document.getElementById("modal-caption-text").textContent = clip.caption || "";
  const scoreBadge = document.getElementById("modal-score-badge");
  scoreBadge.textContent = clip.viral_score ? `${clip.viral_score}/100` : "";
  scoreBadge.style.display = clip.viral_score ? "" : "none";
  document.getElementById("modal-duration-badge").textContent = clip.duration ? `${Number(clip.duration).toFixed(1)}s` : "";
  document.getElementById("modal-timeline-badge").textContent = clip.timeline ? `\u23f1 ${clip.timeline}` : "";

  const dl = document.getElementById("modal-download-btn");
  dl.onclick = (e) => { e.preventDefault(); downloadViaApi(clip.url); };

  const srtBtn = document.getElementById("modal-srt-btn");
  const srtName = (clip.url.split("/").pop() || "clip.mp4").replace(/\.mp4$/i, "") + ".srt";
  if (clip.srt_url || (clip.files && clip.files.srt)) {
    const srtUrl = clip.srt_url || clip.url.replace(/[^/]+$/, encodeURIComponent(clip.files.srt));
    srtBtn.onclick = (e) => { e.preventDefault(); downloadViaApi(srtUrl, clip.files ? clip.files.srt : srtName); };
    srtBtn.style.display = "";
  } else if (clip.srt_text) {
    const blob = new Blob([clip.srt_text], { type: "text/plain;charset=utf-8" });
    srtBtn.onclick = (e) => { e.preventDefault(); window.location.href = URL.createObjectURL(blob); };
    srtBtn.download = srtName;
    srtBtn.style.display = "";
  } else {
    srtBtn.style.display = "none";
  }

  modal.classList.remove("hidden");
  document.body.style.overflow = "hidden";
}
window.openClipModal = openClipModal;

function closeVideoModal() {
  const modal = document.getElementById("video-modal");
  const player = document.getElementById("modal-video-player");
  if (player) { player.pause(); player.src = ""; }
  if (modal) modal.classList.add("hidden");
  document.body.style.overflow = "";
}
window.closeVideoModal = closeVideoModal;

function copyModalTitle() {
  copyText(currentModalReel ? currentModalReel.title : "", "Title");
}
window.copyModalTitle = copyModalTitle;

function copyModalCaption() {
  copyText(currentModalReel ? currentModalReel.caption : "", "Caption");
}
window.copyModalCaption = copyModalCaption;

function copyModalSrt() {
  copyText(currentModalReel ? (currentModalReel.srt_text || "") : "", "SRT subtitles");
}
window.copyModalSrt = copyModalSrt;

function copySrtOf(idx) {
  copyText(currentMoments[idx] ? (currentMoments[idx].srt || "") : "", "SRT subtitles");
}
window.copySrtOf = copySrtOf;

function downloadSrtOf(idx) {
  const m = currentMoments[idx];
  if (!m || !m.srt) { showToast("No subtitles available for this clip yet.", "error"); return; }
  const blob = new Blob([m.srt], { type: "text/plain;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `reel_${idx + 1}_${(m.title || "clip").slice(0, 30)}.srt`;
  a.click();
  URL.revokeObjectURL(a.href);
}
window.downloadSrtOf = downloadSrtOf;
