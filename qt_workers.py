# -*- coding: utf-8 -*-
from __future__ import annotations

import traceback
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, Signal, Slot
from subtitle_tool_core import CancelledError


class WorkerSignals(QObject):
    result = Signal(object)
    error = Signal(str)
    message = Signal(str)
    progress = Signal(int, str)
    state = Signal(str, str, str)
    finished = Signal()


class FunctionWorker(QRunnable):
    """Run a callable outside the UI thread and expose Qt-safe callbacks."""

    def __init__(self, function: Callable[[WorkerSignals], Any]) -> None:
        super().__init__()
        self.function = function
        self.signals = WorkerSignals()

    @Slot()
    def run(self) -> None:
        try:
            result = self.function(self.signals)
        except CancelledError:
            # Cancellation is a normal terminal state, not a task failure.
            pass
        except Exception as exc:
            detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
            self.signals.error.emit(detail)
        else:
            self.signals.result.emit(result)
        finally:
            self.signals.finished.emit()
