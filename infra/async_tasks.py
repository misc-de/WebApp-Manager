from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from gi.repository import GLib


class AsyncTaskRunner:
    def run(self, func: Callable[[], Any], callback: Callable[[Any], Any] | None = None, error_callback: Callable[[Exception], Any] | None = None) -> threading.Thread:
        def worker():
            try:
                result = func()
            except Exception as error:  # noqa: BLE001
                if error_callback is not None:
                    GLib.idle_add(error_callback, error)
                return
            if callback is not None:
                GLib.idle_add(callback, result)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        return thread
