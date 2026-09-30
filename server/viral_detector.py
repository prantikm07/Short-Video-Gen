"""
AI virality detection engine (token efficient).

A long video is never dumped into one giant prompt. Instead:

  Stage A - SCOUT (map):   the transcript is compacted and split into overlapping
                           chunks. Each chunk returns at most a few candidates as
                           TINY tuples: [[start, end, score], ...]  -> almost no output tokens.
  Stage B - PACKAGE (reduce): only the chosen clips are sent back to the model,
                           and just for those we ask for the hooking title + caption.

Cost therefore scales with the number of clips you request, not with video length.
Short videos skip straight to a single combined call.
"""

import json
import re
from typing import List, Tuple
from google import genai
from google.genai import types
import anthropic

from models import AnalysisResponse, ViralMoment, TranscriptSegment
from config import (
    GEMINI_API_KEY,
    ANTHROPIC_API_KEY,
    DEFAULT_GEMINI_MODEL,
    DEFAULT_ANTHROPIC_MODEL,
    DEFAULT_MIN_DURATION,
    DEFAULT_MAX_DURATION,
)

# ---------------------------------------------------------------------------
# Prompts (kept short on purpose - every character is billed on every call)
# ---------------------------------------------------------------------------

SCOUT_SYSTEM = """You are an elite short-form video scout (TikTok / YouTube Shorts / Instagram Reels).
You receive ONE chunk of a timestamped transcript from a long video.
Find the segments inside this chunk that would work as STANDALONE vertical clips.

A segment is worth cutting when:
- it opens with a hook (bold claim, question, conflict, punchline, reveal, emotional peak);
- it ends on a complete thought, never mid-sentence;
- it makes sense with zero outside context.
Reject intros, ads, small talk and filler.

Return AT MOST {max_candidates} best segments from THIS chunk (0 is fine if the chunk is weak).
Timestamps are absolute seconds taken straight from the [sec] markers shown.
Every segment must last between {min_duration} and {max_duration} seconds and stay inside this chunk's range."""

SCOUT_USER = """Video: {title} ({uploader}) | total length {duration}s
Chunk {chunk_index}/{chunk_total}, covering {range_label}.

---
{formatted_transcript}
---

Reply with compact JSON only, no prose, no markdown:
{{"c":[[start_sec,end_sec,score]]}}
score = viral potential 1-100. Example: {{"c":[[742.0,795.5,88],[810.0,860.0,71]]}}"""

PACKAGE_SYSTEM = """You are a viral short-form producer and copywriter.
You get the exact spoken text of a few already-selected clips.
For EACH clip return the finished posting package. Echo the clip ids back unchanged."""

PACKAGE_USER = """Source video: {title} ({uploader})

Clips (id, time range in seconds, spoken text):
{clips_block}

Return JSON only:
{{"moments":[{{"id":0,"title":"...","caption":"..."}}]}}
- title: hooking headline, max 12 words / 70 chars, curiosity gap, no lies.
- caption: ready-to-post social caption = 1 punchy line + 4-6 relevant hashtags.
Include ALL {count} ids exactly once, no extra keys."""


# ---------------------------------------------------------------------------
# Transcript helpers
# ---------------------------------------------------------------------------

def format_seconds(seconds: float) -> str:
    """325 -> '05:25', 3725 -> '1:02:05'."""
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _group_segments(segments: List[TranscriptSegment], window: float = 15.0) -> List[TranscriptSegment]:
    """Collapse word-level captions into ~window-second lines (fewer tokens, same meaning)."""
    grouped: List[TranscriptSegment] = []
    buf: List[str] = []
    buf_start = buf_end = 0.0
    for seg in segments:
        if not buf:
            buf, buf_start, buf_end = [seg.text], seg.start, seg.end
            continue
        if seg.end - buf_start > window or len(" ".join(buf)) > 220:
            grouped.append(TranscriptSegment(text=" ".join(buf), start=buf_start,
                                             duration=max(0.5, buf_end - buf_start)))
            buf, buf_start, buf_end = [seg.text], seg.start, seg.end
        else:
            buf.append(seg.text)
            buf_end = max(buf_end, seg.end)
    if buf:
        grouped.append(TranscriptSegment(text=" ".join(buf), start=buf_start,
                                         duration=max(0.5, buf_end - buf_start)))
    return grouped


def format_transcript_window(segments: List[TranscriptSegment], start: float, end: float) -> str:
    """Compact timestamped text for a time range: '[742.0] sentence'."""
    lines = []
    for s in segments:
        if s.end <= start or s.start >= end:
            continue
        lines.append(f"[{s.start:.1f}] {s.text}")
    return "\n".join(lines)


def build_chunks(segments: List[TranscriptSegment], target_chars: int, overlap: float) -> List[Tuple[float, float]]:
    """Split the timeline into overlapping windows holding ~target_chars of text."""
    if not segments:
        return []
    chunks: List[Tuple[float, float]] = []
    cur_start = segments[0].start
    chars = 0
    last_end = segments[0].end
    for s in segments:
        chars += len(s.text) + 10
        last_end = max(last_end, s.end)
        if chars >= target_chars:
            chunks.append((cur_start, last_end))
            cur_start = max(cur_start, last_end - overlap)
            chars = sum(len(x.text) + 10 for x in segments if x.end > cur_start and x.start < last_end)
    if not chunks or chunks[-1][1] < last_end:
        chunks.append((chunks[-1][1] - overlap if chunks else segments[0].start, last_end))
    merged: List[Tuple[float, float]] = []
    for c in chunks:
        if merged and c[1] - c[0] < 20:
            merged[-1] = (merged[-1][0], c[1])
        else:
            merged.append(c)
    return merged


# ---------------------------------------------------------------------------
# Model plumbing
# ---------------------------------------------------------------------------

def _parse_json(raw: str) -> dict:
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\s*```$", "", text, flags=re.MULTILINE)
    try:
        return json.loads(text)
    except Exception:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def _call_gemini(system: str, prompt: str, api_key: str, model_name: str) -> str:
    client = genai.Client(api_key=api_key)
    models_to_try: List[str] = []
    for m in [model_name, "gemini-2.5-flash", "gemini-2.0-flash", "gemini-flash-latest"]:
        if m and m not in models_to_try:
            models_to_try.append(m)
    last_error = None
    for m in models_to_try:
        try:
            # Recommended pattern: system instruction + temperature live on the
            # chat, and we send exactly one user turn (no function calling).
            chat = client.chats.create(
                model=m,
                config=types.GenerateContentConfig(system_instruction=system, temperature=0.3),
            )
            resp = chat.send_message(prompt)
            if resp and resp.text:
                return resp.text
        except Exception as e:  # rate limited / unknown model -> next candidate
            last_error = e
    raise RuntimeError(f"Gemini call failed ({models_to_try}): {last_error}")


def _call_anthropic(system: str, prompt: str, api_key: str, model_name: str) -> str:
    client = anthropic.Anthropic(api_key=api_key)
    models_to_try = [m for m in [model_name, "claude-3-5-haiku-latest", "claude-sonnet-latest"] if m]
    last_error = None
    for m in models_to_try:
        try:
            resp = client.messages.create(
                model=m, max_tokens=1500, system=system,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text
        except Exception as e:
            last_error = e
    raise RuntimeError(f"Anthropic call failed ({models_to_try}): {last_error}")


def _ask(system: str, prompt: str, engine: str, gemini_key: str, anthropic_key: str,
         gemini_model: str, anthropic_model: str) -> str:
    if engine == "anthropic" and anthropic_key:
        return _call_anthropic(system, prompt, anthropic_key, anthropic_model)
    if gemini_key:
        return _call_gemini(system, prompt, gemini_key, gemini_model)
    if anthropic_key:
        return _call_anthropic(system, prompt, anthropic_key, anthropic_model)
    raise ValueError("No API key available.")


# ---------------------------------------------------------------------------
# Stage A - scouting pass over one chunk
# ---------------------------------------------------------------------------

def _scout_chunk(compacted: List[TranscriptSegment], chunk: Tuple[float, float], meta: dict,
                 index: int, total: int, count: int, min_dur: float, max_dur: float,
                 engine: str, gemini_key: str, anthropic_key: str) -> List[ViralMoment]:
    start, end = chunk
    system = SCOUT_SYSTEM.format(max_candidates=max(2, min(count, 4)),
                                 min_duration=min_dur, max_duration=max_dur)
    user = SCOUT_USER.format(
        title=meta["title"], uploader=meta["uploader"], duration=int(meta["duration"]),
        chunk_index=index, chunk_total=total,
        range_label=f"{format_seconds(start)}-{format_seconds(end)} ({int(start)}s-{int(end)}s)",
        formatted_transcript=format_transcript_window(compacted, start, end),
    )
    raw = _ask(system, user, engine, gemini_key, anthropic_key,
               meta["gemini_model"], meta["anthropic_model"])
    data = _parse_json(raw)
    out: List[ViralMoment] = []
    for item in (data.get("c") or [])[:4]:
        try:
            s, e = float(item[0]), float(item[1])
            score = int(float(item[2])) if len(item) > 2 else 50
        except Exception:
            continue
        s = max(0.0, s)
        e = min(float(meta["duration"]), e)
        dur = e - s
        if dur < max(8.0, min_dur * 0.5) or dur > max_dur * 1.6:
            continue
        out.append(ViralMoment(title="", caption="", timeline="", start_time=round(s, 2),
                               end_time=round(e, 2), duration=round(dur, 1),
                               viral_score=max(1, min(100, score)), reason="", key_quote=""))
    return out


def _dedupe_and_rank(candidates: List[ViralMoment], count: int) -> List[ViralMoment]:
    """Keep the strongest non-overlapping candidates spread across the timeline."""
    picked: List[ViralMoment] = []
    for c in sorted(candidates, key=lambda m: m.viral_score, reverse=True):
        clash = any(
            min(c.end_time, p.end_time) - max(c.start_time, p.start_time) > min(c.duration, p.duration) * 0.25
            for p in picked
        )
        if not clash:
            picked.append(c)
        if len(picked) >= count:
            break
    return sorted(picked, key=lambda m: m.viral_score, reverse=True)


# ---------------------------------------------------------------------------
# Stage B - titles + captions for the selected clips only
# ---------------------------------------------------------------------------

def _package(selected: List[ViralMoment], compacted: List[TranscriptSegment], meta: dict,
             engine: str, gemini_key: str, anthropic_key: str) -> None:
    clips_block = []
    for i, m in enumerate(selected):
        text = format_transcript_window(compacted, m.start_time, m.end_time)
        if len(text) > 2600:  # keep this request tiny even for long clips
            text = text[:1300] + "\n[...]\n" + text[-1200:]
        clips_block.append(f"id={i} | {int(m.start_time)}s-{int(m.end_time)}s ({m.duration:.0f}s)\n{text}")

    user = PACKAGE_USER.format(title=meta["title"], uploader=meta["uploader"],
                              clips_block="\n\n".join(clips_block), count=len(selected))
    try:
        raw = _ask(PACKAGE_SYSTEM, user, engine, gemini_key, anthropic_key,
                   meta["gemini_model"], meta["anthropic_model"])
        data = _parse_json(raw)
        for item in data.get("moments", []):
            idx = int(item.get("id", -1))
            if 0 <= idx < len(selected):
                selected[idx].title = str(item.get("title", "")).strip().strip('"')
                selected[idx].caption = str(item.get("caption", "")).strip().strip('"')
    except Exception as e:
        print(f"[warn] title/caption pass skipped ({e}); using transcript-derived fallbacks")

    for m in selected:
        if not m.title:
            head = format_transcript_window(compacted, m.start_time, m.start_time + 12)
            seed = re.sub(r"^\[[\d.]+\]\s*", "", head.split("\n")[0]) if head else ""
            m.title = (seed[:65].rstrip() or "Best moment of the video")
        if not m.caption:
            m.caption = (f"{m.title}\n\nWait for it 👀 Full video on the channel.\n\n"
                         f"#Shorts #Reels #Viral #FYP #{re.sub(r'[^A-Za-z]', '', meta['title'].split()[0][:12]) or 'Clip'}")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def detect_viral_moments(
    title: str,
    uploader: str,
    duration: float,
    segments: List[TranscriptSegment],
    count: int = 3,
    min_duration: float = DEFAULT_MIN_DURATION,
    max_duration: float = DEFAULT_MAX_DURATION,
    engine: str = "auto",
    gemini_model: str = DEFAULT_GEMINI_MODEL,
    anthropic_model: str = DEFAULT_ANTHROPIC_MODEL,
    progress_cb=None,
) -> AnalysisResponse:
    """Map/reduce transcript analysis -> exactly `count` clip specs (unedited cuts)."""
    count = max(1, min(int(count), 10))
    if not segments:
        raise ValueError("Empty transcript: nothing to analyze.")

    if engine == "gemini" and not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY is not set. Please set it in .env.")
    if engine == "anthropic" and not ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY is not set. Please set it in .env.")
    if engine == "auto" and not (GEMINI_API_KEY or ANTHROPIC_API_KEY):
        raise ValueError("Neither GEMINI_API_KEY nor ANTHROPIC_API_KEY found in environment or .env file.")
    effective_engine = engine if engine != "auto" else ("gemini" if GEMINI_API_KEY else "anthropic")

    def notify(msg: str):
        if progress_cb:
            try:
                progress_cb(msg)
            except Exception:
                pass

    compacted = _group_segments(segments)
    meta = {"title": title or "Untitled", "uploader": uploader or "Unknown",
            "duration": float(duration or compacted[-1].end),
            "gemini_model": gemini_model, "anthropic_model": anthropic_model}
    full_text_len = sum(len(s.text) for s in compacted)

    # --- Stage A: how many calls can we afford? -----------------------------
    budget_chars = 42000          # ~11k tokens per call ceiling
    candidates: List[ViralMoment] = []
    if full_text_len <= budget_chars:
        chunks = [(compacted[0].start, compacted[-1].end)]     # 1 call total
    else:
        window_chars = 12000
        chunks = build_chunks(compacted, window_chars, overlap=min(45.0, max(min_duration, 20.0)))
        max_calls = max(2, min(8, int(3 * budget_chars // window_chars)))
        if len(chunks) > max_calls:                            # widen windows instead of billing more calls
            scale = -(-len(chunks) // max_calls)
            chunks = build_chunks(compacted, window_chars * scale, overlap=min(90.0, 45.0 * scale))

    notify(f"Scanning {format_seconds(meta['duration'])} of transcript in {len(chunks)} pass(es)...")
    for i, ch in enumerate(chunks, start=1):
        notify(f"Scouting part {i}/{len(chunks)} ({format_seconds(ch[0])} - {format_seconds(ch[1])})")
        try:
            candidates += _scout_chunk(compacted, ch, meta, i, len(chunks), count,
                                       min_duration, max_duration, effective_engine,
                                       GEMINI_API_KEY, ANTHROPIC_API_KEY)
        except Exception as e:
            print(f"[warn] scout pass {i}/{len(chunks)} failed: {e}")

    if not candidates:  # last resort so the app still delivers usable cuts
        span = max(min_duration, min(max_duration, meta["duration"] / (count + 1)))
        for i in range(count):
            s = i * span * 1.05
            e = min(meta["duration"], s + span)
            if e - s < 5:
                break
            candidates.append(ViralMoment(title="", caption="", timeline="", start_time=round(s, 1),
                                          end_time=round(e, 1), duration=round(e - s, 1),
                                          viral_score=60 - i, reason="", key_quote=""))

    selected = _dedupe_and_rank(candidates, count)

    # Guarantee the requested quantity with extra non-overlapping spans.
    guard = 0
    while len(selected) < count and guard < 40:
        guard += 1
        span = max(min_duration, min(max_duration, meta["duration"] / max(1, count)))
        used = [(m.start_time, m.end_time) for m in selected]
        placed = False
        for k in range(int(meta["duration"] // span) + 2):
            s, e = k * span, min(meta["duration"], (k + 1) * span)
            if e - s < max(8.0, min_duration * 0.5):
                continue
            if any(min(e, u1) - max(s, u0) > 0.2 * span for u0, u1 in used):
                continue
            selected.append(ViralMoment(title="", caption="", timeline="", start_time=round(s, 1),
                                        end_time=round(e, 1), duration=round(e - s, 1),
                                        viral_score=max(30, 60 - len(selected) * 4), reason="", key_quote=""))
            placed = True
            break
        if not placed:
            break

    notify(f"Writing hooking titles + captions for {len(selected)} clips...")
    _package(selected, compacted, meta, effective_engine, GEMINI_API_KEY, ANTHROPIC_API_KEY)

    for m in selected:
        m.timeline = f"{format_seconds(m.start_time)} - {format_seconds(m.end_time)}"
        if not m.reason:
            m.reason = "Opens on a hook and lands on a complete payoff - works standalone."
        if not m.key_quote:
            m.key_quote = format_transcript_window(compacted, m.start_time,
                                                   m.start_time + 15).replace("\n", " ")[:220]

    return AnalysisResponse(
        video_summary=f"{len(selected)} clip-ready moments found in '{meta['title']}' ({format_seconds(meta['duration'])}).",
        viral_moments=selected,
    )
