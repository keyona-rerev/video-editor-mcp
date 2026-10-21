"""In-memory background jobs. One render runs at a time."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

_RETAIN_SECONDS = 24 * 3600


@dataclass
class Job:
    id: str
    kind: str
    status: str = "queued"  # queued, running, done, error
    stage: str = "queued"
    progress: float = 0.0
    result: Optional[dict] = None
    error: Optional[str] = None
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)

    def set(self, stage: str, progress: float) -> None:
        self.stage = stage
        self.progress = max(0.0, min(1.0, progress))
        self.updated = time.time()

    def to_dict(self) -> dict:
        return {
            "job_id": self.id,
            "kind": self.kind,
            "status": self.status,
            "stage": self.stage,
            "progress": round(self.progress, 3),
            "result": self.result,
            "error": self.error,
            "seconds_running": round(time.time() - self.created, 1),
        }


_jobs: dict[str, Job] = {}
_guard = threading.Lock()
_one_at_a_time = threading.Semaphore(1)


def _prune() -> None:
    cutoff = time.time() - _RETAIN_SECONDS
    with _guard:
        for job_id in [j for j, job in _jobs.items() if job.updated < cutoff]:
            del _jobs[job_id]


def submit(kind: str, work: Callable[[Job], dict]) -> Job:
    """Start work in a thread. Return the job at once."""
    job = Job(id=uuid.uuid4().hex[:12], kind=kind)
    with _guard:
        _jobs[job.id] = job

    def run() -> None:
        with _one_at_a_time:
            job.status = "running"
            job.set("starting", 0.0)
            try:
                job.result = work(job)
                job.status = "done"
                job.set("done", 1.0)
            except Exception as exc:  # noqa: BLE001 - report every failure
                job.status = "error"
                job.error = f"{type(exc).__name__}: {exc}"
                job.updated = time.time()

    threading.Thread(target=run, name=f"job-{job.id}", daemon=True).start()
    _prune()
    return job


def get(job_id: str) -> Optional[Job]:
    with _guard:
        return _jobs.get(job_id)


def recent(limit: int = 10) -> list[Job]:
    with _guard:
        return sorted(_jobs.values(), key=lambda j: j.created, reverse=True)[:limit]
