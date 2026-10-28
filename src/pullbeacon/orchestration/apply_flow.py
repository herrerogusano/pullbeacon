"""`@pullbeacon apply` flow (implemented in phase 06)."""

from __future__ import annotations

from pullbeacon.domain.errors import ApplyRejected
from pullbeacon.domain.models import RunResult
from pullbeacon.orchestration.events import Task
from pullbeacon.orchestration.flows import Services


def run_apply(services: Services, task: Task, *, dry_run: bool) -> RunResult:
    raise ApplyRejected("apply is not available in this build")
