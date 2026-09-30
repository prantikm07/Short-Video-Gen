#!/usr/bin/env python3
"""
ViralReel AI - End-to-end pipeline (CLI)

1. Paste a YouTube link
2. Choose min/max clip length in seconds
3. Choose how many reels (up to 10)

The app finds the hooks + best parts and delivers, for every reel:
  * a hooking title
  * a ready-to-post social caption
  * the timeline of that part (e.g. 03:25 - 04:10)
  * the subtitles of that part as a .srt file
  * the part itself, cut as an unedited 9:16 high quality video (no burned text)

Usage:
    python server/pipeline.py --url "https://youtu.be/VIDEO_ID" --count 5 --min 45 --max 70
    python server/pipeline.py --url "..." --dry-run          # analysis only, no download
    python server/pipeline.py                                # interactive mode
"""

import sys
import argparse
from pathlib import Path
from typing import List, Optional

CURRENT_DIR = Path(__file__).resolve().parent
ROOT_DIR = CURRENT_DIR.parent
for path_str in [str(CURRENT_DIR), str(ROOT_DIR)]:
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.markdown import Markdown
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn

from config import TEMP_DIR, OUTPUT_DIR, DEFAULT_MIN_DURATION, DEFAULT_MAX_DURATION
from downloader import extract_video_id, get_video_info, get_transcript, download_video
from models import TranscriptSegment, ViralMoment
from viral_detector import detect_viral_moments, format_seconds
from video_processor import cut_clip_916, generate_srt

console = Console()


def _clip_stem(index: int, moment: ViralMoment) -> str:
    import re
    safe = re.sub(r"[^\w\s-]", "", moment.title or "").strip()
    safe = re.sub(r"[-\s]+", "_", safe)[:45] or "clip"
    return f"{index:02d}_{safe}_{int(moment.start_time)}-{int(moment.end_time)}s"


def deliver_clips(video_url: str, moments: List[ViralMoment], segments: List[TranscriptSegment],
                  batch_dir: Path, keep_source: bool = False) -> List[dict]:
    """Cut every moment into an unedited 9:16 mp4 + write its .srt / title / caption files."""
    batch_dir.mkdir(parents=True, exist_ok=True)
    video_id = extract_video_id(video_url)
    source_path = TEMP_DIR / f"{video_id}.mp4"

    with Progress(SpinnerColumn(), TextColumn("[bold blue]{task.description}"),
                  BarColumn(), console=console, transient=False) as progress:
        task = progress.add_task("Downloading source video (max resolution)...", total=None)
        if not (source_path.exists() and source_path.stat().st_size > 1_000_000):
            download_video(video_url, source_path)
        progress.update(task, description="Source ready.")

        delivered: List[dict] = []
        for i, m in enumerate(moments, start=1):
            stem = _clip_stem(i, m)
            out_video = batch_dir / f"{stem}.mp4"
            progress.update(task, description=f"Cutting reel {i}/{len(moments)} ({m.timeline})...")
            try:
                cut_clip_916(source_path, m.start_time, m.end_time, out_video)
            except Exception as e:
                console.print(f"[bold red]✗ Reel {i} failed:[/] {e}")
                continue

            srt_path = None
            if segments:
                srt_path = batch_dir / f"{stem}.srt"
                try:
                    generate_srt(m, segments, srt_path)
                except Exception as e:
                    console.print(f"[yellow]⚠ SRT failed for reel {i}: {e}[/yellow]")
                    srt_path = None

            (batch_dir / f"{stem}_title.txt").write_text(f"{m.title}\n({m.timeline})\n", encoding="utf-8")
            (batch_dir / f"{stem}_caption.txt").write_text(f"{m.caption}\n", encoding="utf-8")

            size_mb = round(out_video.stat().st_size / (1024 * 1024), 2)
            delivered.append({"index": i, "moment": m, "video": out_video, "srt": srt_path, "size_mb": size_mb})
            console.print(f"[bold green]✓ Reel {i}[/] {out_video.name}  ({size_mb} MB, {m.duration:.0f}s)")

    if not keep_source and source_path.exists():
        try:
            source_path.unlink()
        except Exception:
            pass
    return delivered


def print_package(delivered: List[dict], batch_dir: Path):
    table = Table(title="Your clips", show_lines=True)
    table.add_column("#", style="cyan", width=3)
    table.add_column("Title", style="bold white", overflow="fold")
    table.add_column("Timeline", style="green", width=16)
    table.add_column("Files", style="magenta", overflow="fold")
    for d in delivered:
        m: ViralMoment = d["moment"]
        files = Path(d["video"]).name
        if d.get("srt"):
            files += f"\n{Path(d['srt']).name}"
        table.add_row(str(d["index"]), m.title, m.timeline, files)
    console.print(table)
    for d in delivered:
        m: ViralMoment = d["moment"]
        console.print(Panel.fit(
            f"[bold]{m.title}[/]\n[dim]Caption:[/] {m.caption}",
            title=f"Reel {d['index']} · {m.timeline} · 🔥{m.viral_score}",
            border_style="red",
        ))
    console.print(f"\n[bold green]All files saved in:[/] {batch_dir}\n")


def run_pipeline(url: str, count: int = 3, engine: str = "auto", output_dir: Path = OUTPUT_DIR,
                 dry_run: bool = False, min_duration: float = DEFAULT_MIN_DURATION,
                 max_duration: float = DEFAULT_MAX_DURATION, keep_source: bool = False) -> List[dict]:
    console.print("\n[bold cyan]🎬 VIRALREEL AI — YT → unedited 9:16 clips + titles, captions & SRT[/bold cyan]\n")

    try:
        video_id = extract_video_id(url)
    except Exception as e:
        console.print(f"[bold red]✗ Invalid YouTube URL:[/] {e}")
        return []

    with console.status("[bold green]Fetching video metadata...", spinner="dots"):
        try:
            info = get_video_info(url)
        except Exception as e:
            console.print(f"[bold red]✗ Could not fetch video info:[/] {e}")
            return []

    duration = info.get("duration", 0)
    console.print(Panel(
        f"[bold]{info.get('title', 'Untitled')}[/]\n"
        f"Channel: {info.get('uploader', 'Unknown')} | Length: {format_seconds(duration)} | ID: {video_id}",
        title="Video Found", border_style="blue",
    ))

    with console.status("[bold green]Downloading captions / transcript...", spinner="dots"):
        try:
            segments = get_transcript(video_id)
        except Exception as e:
            console.print(f"[bold red]✗ {e}[/]")
            return []
    console.print(f"[dim]Transcript: {len(segments)} segments.[/dim]")

    with Progress(SpinnerColumn(), TextColumn("[bold blue]{task.description}"), console=console) as progress:
        task = progress.add_task("AI scanning for hooks & best parts...", total=None)
        cb = lambda msg: progress.update(task, description=msg)
        try:
            analysis = detect_viral_moments(
                title=info.get("title", "Untitled"), uploader=info.get("uploader", "Unknown"),
                duration=duration, segments=segments, count=count, min_duration=min_duration,
                max_duration=max_duration, engine=engine, progress_cb=cb,
            )
        except Exception as e:
            console.print(f"[bold red]✗ AI analysis failed:[/] {e}")
            return []

    moments = analysis.viral_moments
    console.print(f"\n[bold yellow]✨ {len(moments)} clips selected ({min_duration:.0f}-{max_duration:.0f}s each)[/bold yellow]\n")

    batch_dir = Path(output_dir) / f"{video_id}_clips"
    if dry_run:
        for i, m in enumerate(moments, start=1):
            console.print(Panel.fit(
                f"[bold]{m.title}[/]\n{m.caption}\n[dim]{m.timeline} · {m.duration:.0f}s · 🔥{m.viral_score}[/dim]",
                title=f"Dry run · Reel {i}", border_style="cyan"))
        return []

    delivered = deliver_clips(url, moments, segments, batch_dir, keep_source=keep_source)
    if delivered:
        print_package(delivered, batch_dir)
    return delivered


def main():
    parser = argparse.ArgumentParser(description="YouTube → unedited 9:16 clips with title, caption, timeline & SRT")
    parser.add_argument("-u", "--url", type=str, help="YouTube video URL")
    parser.add_argument("-c", "--count", type=int, default=3, help="Number of clips to deliver (1-10)")
    parser.add_argument("-e", "--engine", choices=["auto", "gemini", "anthropic"], default="auto")
    parser.add_argument("-o", "--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--dry-run", action="store_true", help="Analyze only, do not download/cut")
    parser.add_argument("--min-duration", type=float, default=DEFAULT_MIN_DURATION, help="Min clip length in seconds")
    parser.add_argument("--max-duration", type=float, default=DEFAULT_MAX_DURATION, help="Max clip length in seconds")
    parser.add_argument("--keep-source", action="store_true", help="Keep the downloaded source video")
    args = parser.parse_args()

    url = args.url
    if not url:
        url = console.input("[bold cyan]Paste your YouTube video link: [/]").strip()
    if not url:
        console.print("[bold red]Error: No YouTube URL provided![/]")
        sys.exit(1)

    try:
        count = args.count if args.count else int(console.input("[bold cyan]How many clips? (1-10) [/]").strip() or "3")
    except ValueError:
        count = 3
    count = max(1, min(count, 10))

    try:
        min_dur = args.min_duration
        max_dur = args.max_duration
        if min_dur > max_dur:
            console.print("[yellow]Min > Max — swapping them.[/yellow]")
            min_dur, max_dur = max_dur, min_dur
        run_pipeline(url=url, count=count, engine=args.engine, output_dir=args.output_dir,
                     dry_run=args.dry_run, min_duration=min_dur, max_duration=max_dur,
                     keep_source=args.keep_source)
    except KeyboardInterrupt:
        console.print("\n[bold yellow]Cancelled by user.[/]")
        sys.exit(0)
    except Exception as exc:
        console.print(f"\n[bold red]✗ Pipeline crashed:[/] {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
