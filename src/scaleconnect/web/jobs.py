"""Runs one send at a time in the background, so a phone that locks its screen loses nothing.

The library holds the caller until every scale is done. The page asks for progress instead.
A preview id can be sent once. A second request for the same id gets the job that is already there.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from scaleconnect.errors import ScaleConnectError

log = logging.getLogger("scaleconnect.web")


class SendBusy(Exception):
    """A different send is running."""


@dataclass
class Job:
    preview_id: str
    scales: tuple[str, ...]
    state: str = "running"  # running, done or error
    change_id: str | None = None
    error: dict[str, Any] | None = None
    started: float = field(default_factory=time.monotonic)
    finished: threading.Event = field(default_factory=threading.Event)


class SendJobs:
    def __init__(self, service: Any) -> None:
        self._service = service
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}

    def get(self, preview_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(preview_id)

    def busy(self) -> bool:
        with self._lock:
            return any(j.state == "running" for j in self._jobs.values())

    def start(
        self,
        preview_id: str,
        scales: tuple[str, ...],
        *,
        acknowledge_warnings: bool,
        acknowledge_drift: list[str],
        requested_by: str,
    ) -> tuple[Job, bool]:
        """(job, started_now). A repeated preview id returns the same job and starts nothing."""
        with self._lock:
            existing = self._jobs.get(preview_id)
            if existing is not None:
                return existing, False
            if any(j.state == "running" for j in self._jobs.values()):
                raise SendBusy()
            job = Job(preview_id, tuple(scales))
            self._jobs[preview_id] = job
            if len(self._jobs) > 100:  # old finished jobs are forgotten. The log still has them
                for key in [k for k, j in self._jobs.items() if j.state != "running"][:-50]:
                    del self._jobs[key]
        thread = threading.Thread(
            target=self._run,
            args=(job, acknowledge_warnings, list(acknowledge_drift), requested_by),
            name=f"send-{preview_id[:8]}",
            daemon=True,
        )
        thread.start()
        return job, True

    def _run(self, job: Job, ack_warnings: bool, ack_drift: list[str], who: str) -> None:
        try:
            record = self._service.apply_change(
                job.preview_id,
                acknowledge_warnings=ack_warnings,
                acknowledge_drift=ack_drift,
                requested_by=who,
            )
            job.change_id = record.change_id
            job.state = "done"
        except ScaleConnectError as exc:
            # The library raises these before it sends anything, so the preview can be tried again.
            job.error = {**exc.to_dict(), "sent": False}
            job.state = "error"
            with self._lock:
                self._jobs.pop(job.preview_id, None)
        except Exception:  # noqa: BLE001 - nothing may escape a thread silently
            log.exception("A send stopped on an unexpected error")
            job.error = {
                "code": "internal",
                "message": "Something went wrong inside the program. Look at the scales, then check the recent changes.",
                "sent": None,
            }
            job.state = "error"  # the job stays, so this preview cannot be sent a second time
        finally:
            job.finished.set()
