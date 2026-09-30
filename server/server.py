import os
import re
import sys
from pathlib import Path
from typing import Optional, List
import json
import shutil
import threading
from urllib.parse import unquote
import zipfile
from typing import Any, Dict

from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse
from pydantic import BaseModel, Field
import youtube_uploader

import jobs

# Ensure parent directory is on python path to import existing modules
CURRENT_DIR = Path(__file__).resolve().parent
ROOT_DIR = CURRENT_DIR.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from config import (
    TEMP_DIR,
    OUTPUT_DIR,
    GEMINI_API_KEY,
    ANTHROPIC_API_KEY,
    DEFAULT_MIN_DURATION,
    DEFAULT_MAX_DURATION,
)
from downloader import (
    extract_video_id,
    get_video_info,
    get_transcript,
    format_transcript_for_prompt,
    download_video,
)
from models import ViralMoment, TranscriptSegment
from viral_detector import detect_viral_moments, format_seconds
from video_processor import cut_clip_916, generate_srt

app = FastAPI(title="ViralReel AI Studio")

# Disable browser caching in development to ensure latest JS and CSS are always loaded
@app.middleware("http")
async def add_no_cache_headers(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static") or request.url.path == "/":
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

STATIC_DIR = CURRENT_DIR / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

# Mount generated reels for direct browser streaming and download
app.mount("/output", StaticFiles(directory=str(OUTPUT_DIR)), name="output")
# Mount per-batch clip folders (mp4 + .srt + caption text) for download
CLIPS_DIR = OUTPUT_DIR / "clips"
CLIPS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/clips", StaticFiles(directory=str(CLIPS_DIR)), name="clips")
# Mount static UI assets
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.api_route("/favicon.ico", methods=["GET", "HEAD"], include_in_schema=False)
async def favicon():
    fav_svg = STATIC_DIR / "favicon.svg"
    if fav_svg.exists():
        return FileResponse(fav_svg, media_type="image/svg+xml")
    return FileResponse(STATIC_DIR / "favicon.png", media_type="image/png")

class AnalyzeRequest(BaseModel):
    url: str
    count: int = Field(default=3, ge=1, le=10)
    engine: str = "auto"
    min_duration: float = DEFAULT_MIN_DURATION
    max_duration: float = DEFAULT_MAX_DURATION


class ClipSpec(BaseModel):
    """One clip the user wants cut out of an already analyzed video."""
    start_time: float
    end_time: float
    title: str = ""
    caption: str = ""
    timeline: str = ""
    viral_score: int = 0
    key_quote: str = ""
    reason: str = ""

    def to_moment(self) -> ViralMoment:
        return ViralMoment(
            title=self.title, caption=self.caption, timeline=self.timeline,
            start_time=float(self.start_time), end_time=float(self.end_time),
            duration=round(float(self.end_time) - float(self.start_time), 2),
            viral_score=self.viral_score, key_quote=self.key_quote, reason=self.reason,
        )


class CutRequest(BaseModel):
    url: str
    video_id: str = ""
    batch_id: str = ""
    clips: List[ClipSpec]


class RenderRequest(BaseModel):
    url: str
    video_id: str
    moment: ViralMoment
    with_subtitles: bool = False
    with_banner: bool = False

@app.get("/")
def get_index():
    index_file = STATIC_DIR / "index.html"
    if not index_file.exists():
        raise HTTPException(status_code=404, detail="Frontend index.html not found.")
    return FileResponse(index_file)

@app.get("/api/status")
def get_status():
    has_gemini = bool(GEMINI_API_KEY)
    has_anthropic = bool(ANTHROPIC_API_KEY)
    default_engine = "gemini" if has_gemini else ("anthropic" if has_anthropic else "none")
    return {
        "gemini_connected": has_gemini,
        "anthropic_connected": has_anthropic,
        "default_engine": default_engine,
    }

# ---------------------------------------------------------------------------
# Job polling + analysis background worker
# ---------------------------------------------------------------------------

@app.get("/api/job/{job_id}")
def get_job(job_id: str):
    """Poll progress of an analyze / cut job."""
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found or expired.")
    return job


_ANALYSIS_CACHE: Dict[str, Dict[str, Any]] = {}


def _run_analysis(job_id: str, req: AnalyzeRequest):
    try:
        jobs.update(job_id, stage="metadata", message="Fetching video info...", percent=5)
        video_id = extract_video_id(req.url)
        info = get_video_info(req.url)

        jobs.update(job_id, stage="transcript", message="Downloading captions / transcript...", percent=12)
        segments = get_transcript(video_id)

        jobs.update(job_id, stage="ai", message="AI is scanning for the best moments...", percent=18)
        analysis = detect_viral_moments(
            title=info.get("title", "Untitled"),
            uploader=info.get("uploader", "Unknown"),
            duration=info.get("duration", 0),
            segments=segments,
            count=req.count,
            min_duration=req.min_duration,
            max_duration=req.max_duration,
            engine=req.engine,
            progress_cb=jobs.make_cb(job_id, base=18, span=72),
        )

        payload = {
            "video_id": video_id,
            "video_info": info,
            "viral_moments": [m.model_dump() for m in analysis.viral_moments],
            "summary": analysis.video_summary,
            "transcript_segment_count": len(segments),
        }
        _ANALYSIS_CACHE[video_id] = payload
        if len(_ANALYSIS_CACHE) > 12:
            for k in list(_ANALYSIS_CACHE)[:2]:
                _ANALYSIS_CACHE.pop(k, None)
        jobs.finish(job_id, payload)
    except Exception as e:
        jobs.fail(job_id, str(e))


@app.post("/api/analyze")
def analyze_video(req: AnalyzeRequest):
    """Start AI moment detection. Returns a job id to poll at /api/job/{id}."""
    try:
        video_id = extract_video_id(req.url)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid YouTube URL: {str(e)}")
    if not (GEMINI_API_KEY or ANTHROPIC_API_KEY):
        raise HTTPException(status_code=400, detail="No API key configured. Add GEMINI_API_KEY to your .env file.")
    if req.min_duration > req.max_duration:
        raise HTTPException(status_code=400, detail="Min seconds must be smaller than max seconds.")

    cached = _ANALYSIS_CACHE.get(video_id)
    if cached and len(cached.get("viral_moments", [])) >= req.count:
        return {"job_id": None, "cached": True, "result": cached}

    job_id = jobs.create_job("analyze", {"video_id": video_id})
    threading.Thread(target=_run_analysis, args=(job_id, req), daemon=True).start()
    return {"job_id": job_id, "cached": False}


# ---------------------------------------------------------------------------
# Cutting: unedited 9:16 clip + .srt sidecar + title/caption package
# ---------------------------------------------------------------------------

def _safe_name(text: str, limit: int = 45) -> str:
    text = re.sub(r"[^\w\s-]", "", text or "").strip()
    text = re.sub(r"[-\s]+", "_", text)
    return text[:limit] or "clip"


def _batch_dir(batch_id: str) -> Path:
    d = CLIPS_DIR / batch_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ensure_source(url: str, video_id: str, job_id: str) -> Path:
    source = TEMP_DIR / f"{video_id}.mp4"
    if source.exists() and source.stat().st_size > 1_000_000:
        return source
    jobs.update(job_id, message="Downloading source video (max resolution)...", percent=6)
    try:
        download_video(url, source)
    except Exception as e:
        raise RuntimeError(f"Video download failed: {e}")
    if not source.exists():
        raise RuntimeError("Video download produced no file.")
    return source


def _ensure_segments(video_id: str) -> List[TranscriptSegment]:
    try:
        return get_transcript(video_id)
    except Exception:
        return []


def _write_clip_package_files(clip_dir: Path, index: int, spec: ClipSpec, srt_path: Path,
                              video_filename: str, info: Dict[str, Any]) -> Dict[str, Any]:
    """Write title.txt / caption.txt next to the mp4 + srt so everything is downloadable."""
    timeline = spec.timeline or f"{format_seconds(spec.start_time)} - {format_seconds(spec.end_time)}"
    title_txt = clip_dir / f"{index:02d}_title.txt"
    caption_txt = clip_dir / f"{index:02d}_caption.txt"
    title_txt.write_text(f"{spec.title}\n({timeline})\n", encoding="utf-8")
    caption_txt.write_text(f"{spec.caption or spec.title}\n", encoding="utf-8")
    files = {
        "video": video_filename,
        "srt": srt_path.name if srt_path else None,
        "title_file": title_txt.name,
        "caption_file": caption_txt.name,
    }
    return {
        "index": index,
        "title": spec.title,
        "caption": spec.caption,
        "timeline": timeline,
        "start_time": spec.start_time,
        "end_time": spec.end_time,
        "duration": round(spec.end_time - spec.start_time, 2),
        "srt_text": srt_path.read_text(encoding="utf-8") if srt_path and srt_path.exists() else "",
        "files": files,
    }


def _run_cut(job_id: str, req: CutRequest):
    try:
        video_id = req.video_id or extract_video_id(req.url)
        batch_id = req.batch_id or video_id
        clip_dir = _batch_dir(batch_id)

        source = _ensure_source(req.url, video_id, job_id)
        segments = _ensure_segments(video_id)
        info = {"id": video_id}
        try:
            info = get_video_info(req.url)
        except Exception:
            pass

        total = max(1, len(req.clips))
        delivered = []
        for i, spec in enumerate(req.clips, start=1):
            jobs.update(job_id, message=f"Cutting clip {i}/{total} ({format_seconds(spec.start_time)} - {format_seconds(spec.end_time)})...",
                        percent=10 + (80 * (i - 1) / total))
            stem = f"{i:02d}_{_safe_name(spec.title)}_{int(spec.start_time)}-{int(spec.end_time)}s"
            out_video = clip_dir / f"{stem}.mp4"
            moment = spec.to_moment()
            try:
                cut_clip_916(source, moment.start_time, moment.end_time, out_video)
            except Exception as e:
                delivered.append({"index": i, "error": str(e)[:300]})
                continue

            srt_path = None
            if segments:
                srt_path = clip_dir / f"{stem}.srt"
                try:
                    generate_srt(moment, segments, srt_path)
                except Exception as e:
                    print(f"[warn] srt generation failed for clip {i}: {e}")
                    srt_path = None

            rel_dir = f"clips/{batch_id}"
            entry = _write_clip_package_files(clip_dir, i, spec, srt_path, out_video.name, info)
            entry["url"] = f"/{rel_dir}/{out_video.name}"
            if srt_path:
                entry["srt_url"] = f"/{rel_dir}/{srt_path.name}"
            entry["size_mb"] = round(out_video.stat().st_size / (1024 * 1024), 2)
            entry["dir_url"] = f"/{rel_dir}/"
            delivered.append(entry)

        manifest = {
            "batch_id": batch_id,
            "video_id": video_id,
            "video_title": info.get("title", ""),
            "clip_count": len([d for d in delivered if "url" in d]),
            "clips": delivered,
        }
        (clip_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

        jobs.update(job_id, message="Packaging downloads...", percent=95)
        zip_name = f"{batch_id}_clips.zip"
        # Fresh ZIP per batch (rebuilt on every cut so it always contains the latest clips)
        zip_path = CLIPS_DIR / zip_name
        if zip_path.exists():
            try:
                zip_path.unlink()
            except Exception:
                pass
        zip_url = None
        try:
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as zf:
                for f in sorted(clip_dir.iterdir()):
                    if f.is_file() and f.name != "manifest.json":
                        zf.write(f, arcname=f"{batch_id}/{f.name}")
            zip_url = f"/api/download/{batch_id}"
        except Exception as e:
            print(f"[warn] zip creation failed: {e}")

        jobs.finish(job_id, {"manifest": manifest, "zip_url": zip_url, "zip_name": zip_name if zip_url else None})
    except Exception as e:
        jobs.fail(job_id, str(e))


@app.post("/api/cut")
def cut_clips(req: CutRequest):
    """Cut the requested clips: unedited 9:16 mp4 + .srt + title + caption each."""
    if not req.clips:
        raise HTTPException(status_code=400, detail="No clips requested.")
    if len(req.clips) > 10:
        raise HTTPException(status_code=400, detail="Maximum 10 clips per batch.")
    try:
        video_id = req.video_id or extract_video_id(req.url)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid YouTube URL: {str(e)}")
    for c in req.clips:
        if c.end_time <= c.start_time:
            raise HTTPException(status_code=400, detail=f"Clip '{c.title}' has end time before start time.")
        if c.start_time < 0:
            raise HTTPException(status_code=400, detail="Start time cannot be negative.")

    batch_id = re.sub(r"[^\w-]", "_", req.batch_id or f"{video_id}_{jobs.create_job('batch')}")[:40]
    job_id = jobs.create_job("cut", {"video_id": video_id, "batch_id": batch_id})
    req.batch_id = batch_id
    req.video_id = video_id
    threading.Thread(target=_run_cut, args=(job_id, req), daemon=True).start()
    return {"job_id": job_id, "batch_id": batch_id}


@app.get("/api/batch/{batch_id}")
def get_batch(batch_id: str):
    safe = re.sub(r"[^\w-]", "", batch_id)
    manifest = CLIPS_DIR / safe / "manifest.json"
    if not manifest.exists():
        raise HTTPException(status_code=404, detail="Batch not found.")
    return json.loads(manifest.read_text(encoding="utf-8"))


def _safe_batch_dir(batch_id: str) -> Path:
    safe = re.sub(r"[^\w-]", "", batch_id or "")
    d = CLIPS_DIR / safe
    if not safe or not d.is_dir():
        raise HTTPException(status_code=404, detail="Batch not found.")
    return d


@app.get("/api/download/clip")
def download_single_clip(
    url: str = Query(..., description="Server-relative clip url, e.g. /clips/batch/file.mp4"),
    name: str = Query("", description="Optional download filename override"),
):
    """Force-download one file from the clips folder (proper Content-Disposition)."""
    raw = url.split("?")[0].split("/")[-1]
    decoded = unquote(raw)
    candidates = list(CLIPS_DIR.rglob(decoded))
    if not candidates:
        safe = re.sub(r"[^\w.\-]", "_", raw)
        candidates = list(CLIPS_DIR.rglob(safe))
    if not candidates:
        raise HTTPException(status_code=404, detail="File not found on server.")
    path = candidates[0]
    suffix = path.suffix.lower()
    media = {".mp4": "video/mp4"}.get(suffix) or (
        "text/plain; charset=utf-8" if suffix in (".srt", ".txt") else "application/octet-stream"
    )
    filename = re.sub(r"[^\w.\- ]", "", name).strip() or path.name
    return FileResponse(str(path), media_type=media, filename=filename)


@app.get("/api/download/{batch_id}")
def download_batch_zip(batch_id: str):
    """Download ALL assets of a batch as one ZIP (rebuilt on the fly if missing/stale)."""
    clip_dir = _safe_batch_dir(batch_id)
    zip_path = CLIPS_DIR / f"{clip_dir.name}_clips.zip"
    newest_asset = max((f.stat().st_mtime for f in clip_dir.iterdir() if f.is_file()), default=0)
    if not zip_path.exists() or zip_path.stat().st_mtime < newest_asset:
        try:
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as zf:
                for f in sorted(clip_dir.iterdir()):
                    if f.is_file() and f.name != "manifest.json":
                        zf.write(f, arcname=f"{clip_dir.name}/{f.name}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"ZIP packaging failed: {e}")
    if not zip_path.exists() or zip_path.stat().st_size == 0:
        raise HTTPException(status_code=404, detail="No files to download in this batch yet.")
    return FileResponse(str(zip_path), media_type="application/zip",
                        filename=f"{clip_dir.name}_clips.zip")


@app.get("/api/subtitles/{video_id}")
def subtitles_for_range(video_id: str, start: float, end: float):
    """SRT text for any arbitrary range (used by the per-clip 'copy SRT' button)."""
    safe_id = re.sub(r"[^0-9A-Za-z_-]", "", video_id)
    segments = _ensure_segments(safe_id)
    if not segments:
        raise HTTPException(status_code=400, detail="No transcript available for this video.")
    spec = ClipSpec(start_time=start, end_time=end)
    tmp = TEMP_DIR / f"srt_{safe_id}_{int(start)}_{int(end)}.srt"
    generate_srt(spec.to_moment(), segments, tmp)
    text = tmp.read_text(encoding="utf-8")
    try:
        tmp.unlink()
    except Exception:
        pass
    return {"srt": text, "timeline": f"{format_seconds(start)} - {format_seconds(end)}"}


# ===================================================
# YOUTUBE STUDIO DIRECT UPLOAD ENDPOINTS
# ===================================================

class UploadShortRequest(BaseModel):
    filename: str
    title: str
    description: Optional[str] = ""
    privacy: str = "public"

class ClientSecretsRequest(BaseModel):
    content: str

@app.get("/api/youtube/status")
def get_youtube_status():
    is_auth = youtube_uploader.is_authenticated()
    channel = youtube_uploader.get_channel_info() if is_auth else None
    return {
        "has_client_secrets": youtube_uploader.has_client_secrets(),
        "is_authenticated": is_auth,
        "channel": channel,
    }

@app.post("/api/youtube/secrets")
def upload_client_secrets(req: ClientSecretsRequest):
    try:
        youtube_uploader.save_client_secrets(req.content)
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/youtube/auth-url")
def get_auth_url():
    try:
        redirect_uri = "http://localhost:8000/api/youtube/callback"
        url = youtube_uploader.get_auth_url(redirect_uri)
        return {"auth_url": url}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/youtube/callback")
def youtube_callback(code: str):
    try:
        redirect_uri = "http://localhost:8000/api/youtube/callback"
        youtube_uploader.exchange_code_for_tokens(code, redirect_uri)
        return RedirectResponse(url="/?youtube=connected")
    except Exception as e:
        return RedirectResponse(url=f"/?youtube=error&detail={str(e)}")

@app.post("/api/youtube/disconnect")
def disconnect_youtube():
    youtube_uploader.disconnect_youtube()
    return {"success": True}

@app.post("/api/youtube/upload")
def upload_to_youtube(req: UploadShortRequest):
    video_path = OUTPUT_DIR / req.filename
    if not video_path.exists():
        raise HTTPException(status_code=404, detail=f"Reel file {req.filename} not found.")

    try:
        result = youtube_uploader.upload_short(
            file_path=video_path,
            title=req.title,
            description=req.description,
            privacy_status=req.privacy,
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"YouTube upload failed: {str(e)}")

class SuggestCopyRequest(BaseModel):
    raw_title: str
    hook: Optional[str] = ""
    quote: Optional[str] = ""

@app.post("/api/youtube/suggest-copy")
def suggest_copy(req: SuggestCopyRequest):
    """Generate high-CTR viral titles and descriptions using Gemini."""
    clean_title = re.sub(r"^reel_[^_]+_", "", req.raw_title)
    clean_title = re.sub(r"^s\d+_e\d+_", "", clean_title)
    clean_title = re.sub(r"^score\d+_", "", clean_title)
    clean_title = re.sub(r"\.mp4$", "", clean_title).replace("_", " ").strip()

    prompt = f"""You are a master YouTube Shorts viral strategist and copywriter.
Generate high-CTR, scroll-stopping title and description for a YouTube Short.

Context:
Topic/Moment: {clean_title}
Hook: {req.hook or clean_title}
Key Quote: {req.quote or ""}

Requirements:
1. `catchy_title`: Irresistible curiosity gap, emotional hook, under 70 chars, ending with #Shorts. Use 1 or 2 relevant emojis (🤯, 💀, 🔥, 👀).
2. `alternative_titles`: 2 alternative punchy viral titles under 70 chars ending with #Shorts.
3. `catchy_description`: Engaging 3-line hook, controversial or intriguing question to spark comments (e.g. "Drop your thoughts below 👇"), and 5-7 targeted trending hashtags.

Return ONLY a JSON object:
{{
  "catchy_title": "...",
  "alternative_titles": ["...", "..."],
  "catchy_description": "..."
}}"""

    try:
        from google import genai
        client = genai.Client(api_key=GEMINI_API_KEY)
        resp = client.chats.create(model="gemini-3.8-flash").send_message(prompt)
        text = resp.text
        text = re.sub(r'^```json\s*', '', text.strip(), flags=re.MULTILINE)
        text = re.sub(r'```$', '', text.strip(), flags=re.MULTILINE)
        return json.loads(text)
    except Exception:
        base = clean_title.title()
        return {
            "catchy_title": f"{base} 🤯 #Shorts",
            "alternative_titles": [
                f"The Moment Everything Changed... 💀 #Shorts",
                f"You Won't Believe This Happened! 🔥 #Shorts"
            ],
            "catchy_description": f"Watch closely till the end... 👀\n\nWhat do you think about this? Drop your thoughts below! 👇\n\n#Shorts #Viral #Trending #EpicMoments #Clips"
        }
