"""The network thread: one asyncio loop for the backend and pi-panel-ctl calls.

GTK owns the main thread, so everything that waits on a socket lives here,
the same way pi_panel_varlink.AppService keeps its server on a thread.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from typing import Any, Coroutine


class NetThread:
    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="pi-panel-music-net", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def submit(self, coro: Coroutine[Any, Any, Any]) -> concurrent.futures.Future:
        """Run |coro| on the loop. Cancelling the future cancels it."""
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def stop(self, timeout: float = 2.0) -> None:
        async def cancel_all() -> None:
            tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        if not self._thread.is_alive():
            return
        try:
            self.submit(cancel_all()).result(timeout)
        except (concurrent.futures.TimeoutError, concurrent.futures.CancelledError):
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout)
