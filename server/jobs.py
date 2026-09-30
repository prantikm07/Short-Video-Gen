"""Tiny in-memory job/progress registry used by the web studio."""
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

_LOCK = threading.Lock()
_JOBS: Dict[str, Dict[str, Any]] = {}
_MAX_JOBS = 60


def create_job(kind: str, meta: Optional[dict] = None) -> str:
    job_id = uuid.uuid4().hex[:12]
    with _LOCK:
        if len(_JOBS) >= _MAX_JOBS:  # keep the registry small
            for old_id in sorted(_JOBS, key=lambda k: _JOBS[k].get("created", 0))[:10]:
                if _JOBS[old_id].get("status") in ("done", "error"):
                    _JOBS.pop(old_id, None)
        _JOBS[job_id] = {
            "id": job_id,
            "kind": kind,
            "status": "running",      # running | done | error
            "stage": "queued",
            "message": "Starting...",
            "percent": 0.0,
            "result": None,
            "error": None,
            "meta": meta or {},
            "created": time.time(),
            "updated": time.time(),
        }
    return job_id


def update(job_id: str, *, stage: Optional[str] = None, message: Optional[str] = None,
           percent: Optional[float] = None) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        if stage:
            job["stage"] = stage
        if message:
            job["message"] = message
        if percent is not None:
            job["percent"] = max(0.0, min(100.0, float(percent)))
        job["updated"] = time.time()


def finish(job_id: str, result: Any) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        job.update(status="done", stage="done", message="Finished", percent=100.0,
                   result=result, updated=time.time())


def fail(job_id: str, error: str) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        job.update(status="error", stage="error", message="Failed", error=str(error)[:500],
                   updated=time.time())


def get(job_id: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        job = _JOBS.get(job_id)
        return dict(job) if job else None


def make_cb(job_id: str, base: float = 0.0, span: float = 100.0):
    """Return a callback that maps free-form AI progress messages onto the job bar."""
    def cb(msg: str):
        pct = base
        text = str(msg)
        if "part " in text.lower() and "/" in text:
            digits = [p for p in text.replace(",", " ").split() if "/" in p]
            try:
                a, b = digits[0].split("/")[:2]
                pct = base + span * (int(a) / max(1, int(b))) * 0.85
            except Exception:
                pass
        elif "titles" in text.lower() or "captions" in text.lower():
            pct = base + span * 0.9
        update(job_id, message=text[:180], percent=pct)
    return cb


def cleanup_stale(max_age_seconds: int = 3 * 3600) -> List[str]:
    now = time.time()
    removed = []
    with _LOCK:
        for jid, job in list(_JOBS.items()):
            if now - job.get("created", 0) > max_age_seconds:
                _JOBS.pop(jid, None)
                removed.append(jid)
    return removed
