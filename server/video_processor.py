import sys
import subprocess
import re
from pathlib import Path
from typing import List, Optional
from models import ViralMoment, TranscriptSegment
from config import TARGET_WIDTH, TARGET_HEIGHT, OUTPUT_DIR, TEMP_DIR

def format_ass_timestamp(seconds: float) -> str:
    """Format seconds into ASS timestamp format: H:MM:SS.cs"""
    if seconds < 0:
        seconds = 0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int((seconds - int(seconds)) * 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"

def split_text_into_chunks(text: str, max_words: int = 5) -> List[str]:
    """Break long sentences into short dynamic subtitle bursts (TikTok style)."""
    words = text.split()
    if not words:
        return []
    chunks = []
    for i in range(0, len(words), max_words):
        chunks.append(" ".join(words[i:i + max_words]))
    return chunks

def generate_ass_subtitles(
    moment: ViralMoment,
    all_segments: List[TranscriptSegment],
    output_ass_path: Path,
    include_hook_banner: bool = True,
    hook_duration: float = 6.0,
) -> Path:
    """
    Generate styled ASS subtitle file for the specific clip segment.
    Features:
    - Bold, high-contrast captions placed in the vertical video safe zone.
    - Optional eye-catching Hook Banner at top.
    """
    start_offset = moment.start_time
    end_offset = moment.end_time

    # Filter segments that fall within this reel
    relevant_segments: List[TranscriptSegment] = []
    for seg in all_segments:
        # Check overlap
        if seg.end > start_offset and seg.start < end_offset:
            # Clip bounds
            c_start = max(seg.start, start_offset) - start_offset
            c_end = min(seg.end, end_offset) - start_offset
            if c_end > c_start:
                relevant_segments.append(TranscriptSegment(
                    text=seg.text,
                    start=c_start,
                    duration=c_end - c_start
                ))

    # ASS Header with high-readability TikTok styles
    # Colors in ASS are &HAABBGGRR:
    # &H0000FFFF = Bright Yellow
    # &H00FFFFFF = Pure White
    # &H80000000 = Semi-transparent Black Box/Shadow
    ass_header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {TARGET_WIDTH}
PlayResY: {TARGET_HEIGHT}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: HookBanner,Arial,52,&H0000FFFF,&H000000FF,&H00000000,&HA0000000,-1,0,0,0,100,100,1,0,1,5,3,8,60,60,200,1
Style: Captions,Arial,66,&H00FFFFFF,&H000000FF,&H00000000,&HA0000000,-1,0,0,0,100,100,0,0,1,6,3,2,60,60,340,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    dialogues = []

    # 1. Top Hook Banner
    if include_hook_banner and moment.hook:
        clean_hook = moment.hook.upper().strip()
        banner_end = min(hook_duration, moment.duration)
        dialogues.append(
            f"Dialogue: 1,{format_ass_timestamp(0)},{format_ass_timestamp(banner_end)},HookBanner,,0,0,0,,{clean_hook}"
        )

    # 2. Synchronized Captions
    for seg in relevant_segments:
        # Split segment into smaller chunks for fast-paced reading
        chunks = split_text_into_chunks(seg.text, max_words=5)
        if not chunks:
            continue
        chunk_duration = seg.duration / len(chunks)

        for i, chunk in enumerate(chunks):
            chunk_start = seg.start + (i * chunk_duration)
            chunk_end = chunk_start + chunk_duration
            dialogues.append(
                f"Dialogue: 0,{format_ass_timestamp(chunk_start)},{format_ass_timestamp(chunk_end)},Captions,,0,0,0,,{chunk.upper()}"
            )

    full_ass = ass_header + "\n".join(dialogues) + "\n"
    output_ass_path.write_text(full_ass, encoding='utf-8')
    return output_ass_path


def get_video_duration(video_path: Path) -> float:
    """Retrieve total duration of video in seconds using ffprobe."""
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(video_path)
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return float(res.stdout.strip())
    except Exception:
        return 0.0

def get_video_dimensions(video_path: Path) -> tuple[int, int]:
    """Retrieve width and height of video using ffprobe."""
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=s=x:p=0",
            str(video_path)
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        w, h = res.stdout.strip().split("x")
        return int(w), int(h)
    except Exception:
        return 1920, 1080

def format_srt_timestamp(seconds: float) -> str:
    """Seconds -> SRT timestamp 'HH:MM:SS,mmm'."""
    if seconds < 0:
        seconds = 0.0
    total_ms = int(round(seconds * 1000))
    h, rem = divmod(total_ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def generate_srt(
    moment: ViralMoment,
    all_segments: List[TranscriptSegment],
    output_srt_path: Path,
    max_chars: int = 42,
) -> Path:
    """
    Plain .srt subtitle file for ONE clip (timings relative to the clip start).
    Nothing is burned into the video - this is a sidecar file you can upload or
    edit yourself.
    """
    start_offset, end_offset = moment.start_time, moment.end_time
    blocks: List[str] = []
    index = 1

    for seg in all_segments:
        if not (seg.end > start_offset and seg.start < end_offset):
            continue
        c_start = max(seg.start, start_offset) - start_offset
        c_end = min(seg.end, end_offset) - start_offset
        if c_end <= c_start:
            continue

        words = seg.text.split()
        if not words:
            continue

        # split long caption lines into readable sub-lines with proportional timing
        chunks: List[str] = []
        cur: List[str] = []
        for w in words:
            cur.append(w)
            if len(" ".join(cur)) >= max_chars:
                chunks.append(" ".join(cur))
                cur = []
        if cur:
            chunks.append(" ".join(cur))

        per = (c_end - c_start) / len(chunks)
        for i, chunk in enumerate(chunks):
            t0 = c_start + i * per
            t1 = (c_end if i == len(chunks) - 1 else t0 + per)
            if t1 - t0 < 0.2:
                t1 = t0 + 0.2
            blocks.append(f"{index}\n{format_srt_timestamp(t0)} --> {format_srt_timestamp(t1)}\n{chunk}\n")
            index += 1

    output_srt_path.parent.mkdir(parents=True, exist_ok=True)
    output_srt_path.write_text("\n".join(blocks), encoding="utf-8")
    return output_srt_path


def render_reel(
    source_video_path: Path,
    moment: ViralMoment,
    output_file: Path,
    all_segments: Optional[List[TranscriptSegment]] = None,
    with_subtitles: bool = False,
    with_hook_banner: bool = False,
) -> Path:
    """
    Render a 9:16 vertical video reel keeping the FULL WIDTH of the video
    with clean black bars on top and bottom.
    External subtitles and banners are disabled by default.
    """
    output_file.parent.mkdir(parents=True, exist_ok=True)

    # Validate timestamps against source video duration
    source_dur = get_video_duration(source_video_path)
    if source_dur > 0:
        if moment.start_time >= source_dur:
            raise ValueError(
                f"Requested start time ({moment.start_time:.1f}s) exceeds total video length ({source_dur:.1f}s). "
                f"Please choose a timestamp before {source_dur:.1f}s."
            )
        if moment.end_time > source_dur:
            moment.end_time = source_dur
            moment.duration = moment.end_time - moment.start_time

    # Generate ASS subtitles only if explicitly requested
    ass_path = None
    if with_subtitles and all_segments:
        ass_path = TEMP_DIR / f"sub_{output_file.stem}.ass"
        generate_ass_subtitles(
            moment=moment,
            all_segments=all_segments,
            output_ass_path=ass_path,
            include_hook_banner=with_hook_banner,
        )

    # Preserve FULL WIDTH: scale to fit 9:16 canvas and pad with black bars on top and bottom
    # Standardize to 1080x1920 for maximum encoding speed and Shorts/Reels spec compliance
    canvas_w, canvas_h = 1080, 1920

    filter_parts = [
        f"scale={canvas_w}:{canvas_h}:force_original_aspect_ratio=decrease:flags=bicubic",
        f"pad={canvas_w}:{canvas_h}:(ow-iw)/2:(oh-ih)/2:black"
    ]

    # Add ASS subtitles only if explicitly enabled
    if ass_path and ass_path.exists():
        escaped_ass = ass_path.as_posix().replace(":", "\\:").replace("'", "\\'")
        filter_parts.append(f"ass='{escaped_ass}'")

    vf_chain = ",".join(filter_parts)

    temp_render_path = output_file.with_suffix(".rendering.mp4")

    # Hardware acceleration check for macOS (Apple Silicon / VideoToolbox)
    is_macos = sys.platform == "darwin"
    if is_macos:
        vcodec_opts = [
            "-c:v", "h264_videotoolbox",
            "-b:v", "8M",
            "-maxrate", "12M",
            "-bufsize", "16M",
            "-allow_sw", "1"
        ]
    else:
        vcodec_opts = [
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "20"
        ]

    cmd = [
        "ffmpeg",
        "-y",
        "-ss", str(moment.start_time),
        "-to", str(moment.end_time),
        "-i", str(source_video_path),
        "-vf", vf_chain,
        *vcodec_opts,
        "-c:a", "aac",
        "-b:a", "192k",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(temp_render_path),
    ]

    process = subprocess.run(cmd, capture_output=True, text=True)
    if process.returncode != 0:
        if temp_render_path.exists():
            temp_render_path.unlink()
        raise RuntimeError(f"FFmpeg failed while rendering {output_file.name}:\n{process.stderr}")

    if temp_render_path.exists():
        temp_render_path.replace(output_file)

    # Clean up temp ass
    if ass_path and ass_path.exists():
        try:
            ass_path.unlink()
        except Exception:
            pass

    return output_file


def cut_clip_916(
    source_video_path: Path,
    start_time: float,
    end_time: float,
    output_file: Path,
    canvas_w: int = TARGET_WIDTH,
    canvas_h: int = TARGET_HEIGHT,
) -> Path:
    """
    Deliver ONE clip as an unedited 9:16 vertical video.

    "Unedited" = the picture is exactly what the source has: full width kept,
    scaled onto a 1080x1920 canvas with black bars top/bottom. No subtitles,
    no banners, no text overlays - you edit it yourself.

    Fast path: if the source is already <=1080 wide and <=1920 tall (i.e. a true
    vertical video) the stream is copied with NO re-encode at all, so quality is
    bit-for-bit identical to the source. Otherwise a high-quality single re-encode
    (crf 17, slow preset) keeps the result crisp.
    """
    output_file.parent.mkdir(parents=True, exist_ok=True)
    duration = end_time - start_time
    if duration <= 0:
        raise ValueError(f"Invalid clip range: {start_time} -> {end_time}")

    src_dur = get_video_duration(source_video_path)
    if src_dur > 0:
        if start_time >= src_dur:
            raise ValueError(
                f"Start time ({start_time:.1f}s) exceeds video length ({src_dur:.1f}s)."
            )
        if end_time > src_dur:
            duration = src_dur - start_time

    w, h = get_video_dimensions(source_video_path)
    already_vertical_fit = (w <= canvas_w and h <= canvas_h) or (h >= w and abs(w / h - canvas_w / canvas_h) < 0.01)

    temp_render_path = output_file.with_suffix(".rendering.mp4")
    base = [
        "ffmpeg", "-y",
        "-ss", f"{start_time:.3f}",
        "-i", str(source_video_path),
        "-t", f"{duration:.3f}",
        "-map", "0:v:0", "-map", "0:a?",
    ]

    try:
        if already_vertical_fit:
            # Zero quality loss: stream copy, keyframe-snapped cut.
            cmd = base + ["-c", "copy", "-avoid_negative_ts", "make_zero", str(temp_render_path)]
        else:
            vf = (
                f"scale={canvas_w}:{canvas_h}:force_original_aspect_ratio=decrease:flags=lanczos,"
                f"pad={canvas_w}:{canvas_h}:(ow-iw)/2:(oh-ih)/2:black"
            )
            cmd = base + [
                "-vf", vf,
                "-c:v", "libx264", "-preset", "slow", "-crf", "17",
                "-c:a", "aac", "-b:a", "192k",
                "-pix_fmt", "yuv420p",
                "-movflags", "+faststart",
                str(temp_render_path),
            ]
        process = subprocess.run(cmd, capture_output=True, text=True)
        ok = process.returncode == 0 and temp_render_path.exists() and temp_render_path.stat().st_size > 50_000
        if not ok and not already_vertical_fit:
            raise RuntimeError(f"FFmpeg cut failed:\n{process.stderr[-1200:]}")

        if not ok:
            # stream copy produced junk (rare codec mismatch) -> fall back to re-encode
            vf = (
                f"scale={canvas_w}:{canvas_h}:force_original_aspect_ratio=decrease:flags=lanczos,"
                f"pad={canvas_w}:{canvas_h}:(ow-iw)/2:(oh-ih)/2:black"
            )
            cmd = base + [
                "-vf", vf,
                "-c:v", "libx264", "-preset", "slow", "-crf", "17",
                "-c:a", "aac", "-b:a", "192k",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                str(temp_render_path),
            ]
            process = subprocess.run(cmd, capture_output=True, text=True)
            if process.returncode != 0 or not temp_render_path.exists():
                raise RuntimeError(f"FFmpeg cut failed:\n{process.stderr[-1200:]}")

        temp_render_path.replace(output_file)
        return output_file
    finally:
        if temp_render_path.exists():
            try:
                temp_render_path.unlink()
            except Exception:
                pass
