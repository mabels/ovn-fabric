"""The ovn-fabric runtime.

Independent of the generator: a small async job tree with lifecycle and
event hooks. See `runtime.service` for the DSL.

    python3 -m runtime
"""

from __future__ import annotations

from .service import (
    Job,
    JobState,
    JobStatus,
    LogEvent,
    LogLevel,
    ProcessStatus,
    capture_cmd,
    run_cmd,
)

__all__ = [
    "Job",
    "JobState",
    "JobStatus",
    "LogEvent",
    "LogLevel",
    "ProcessStatus",
    "capture_cmd",
    "run_cmd",
]
