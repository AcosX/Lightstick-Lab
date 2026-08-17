# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lightstick-control contributors
"""Small background worker bridge that keeps Tk callbacks responsive."""

from __future__ import annotations

import threading
import traceback
from dataclasses import dataclass
from queue import Queue
from typing import Any, Callable


@dataclass
class WorkerEvent:
    kind: str
    task: str
    value: Any = None
    error: str | None = None


class TaskRunner:
    def __init__(self) -> None:
        self.events: Queue[WorkerEvent] = Queue()

    def submit(self, task: str, operation: Callable[[], Any]) -> None:
        def run() -> None:
            try:
                result = operation()
            except Exception as exc:  # pragma: no cover - exercised by UI error paths
                self.events.put(WorkerEvent("error", task, error=f"{exc}\n{traceback.format_exc(limit=2)}"))
            else:
                self.events.put(WorkerEvent("result", task, value=result))

        threading.Thread(target=run, name=f"lightstick-{task}", daemon=True).start()

    def submit_progress(
        self,
        task: str,
        operation: Callable[[Callable[[Any], None]], Any],
    ) -> None:
        def report(value: Any) -> None:
            self.events.put(WorkerEvent("progress", task, value=value))

        def run() -> None:
            try:
                result = operation(report)
            except Exception as exc:  # pragma: no cover - exercised by UI error paths
                self.events.put(WorkerEvent("error", task, error=f"{exc}\n{traceback.format_exc(limit=2)}"))
            else:
                self.events.put(WorkerEvent("result", task, value=result))

        threading.Thread(target=run, name=f"lightstick-{task}", daemon=True).start()
