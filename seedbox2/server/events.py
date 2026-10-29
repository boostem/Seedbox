"""The event stream: any thread publishes `{type, data}` events; each `/api/events` client drains its own queue.

Ported from seedbox/events.py (Seedbox 1), so the frontend's EventSource handling carries over, with two changes
(#39): events published with a `key` (progress) replace the client's older unread event with that key instead of
queueing behind it, and `terminal` events (a job finishing) are never dropped when a slow client's queue is full.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable

log = logging.getLogger(__name__)

HEARTBEAT_S = 15.0
QUEUE_SIZE = 1000  # per client; on overflow the oldest non-terminal events go (a stalled client can't eat memory)


class _Item:
    """One encoded event in a client's queue. `key`: newer events with the same key replace it while it waits;
    `terminal`: never dropped on overflow."""

    __slots__ = ("key", "line", "terminal")

    def __init__(self, line: str | None, key: str | None = None, terminal: bool = False) -> None:
        self.line, self.key, self.terminal = line, key, terminal


_CLOSE = _Item(None, terminal=True)  # ends a stream


class _Subscriber:
    """One client: its event loop and its queue of encoded events. `push` and `get` run on that loop."""

    def __init__(self, loop: asyncio.AbstractEventLoop, maxsize: int) -> None:
        self.loop = loop
        self.maxsize = maxsize
        self.items: deque[_Item] = deque()
        self.waiting: dict[str, _Item] = {}  # coalescing key -> its queued item
        self.ready = asyncio.Event()
        self.dropped = 0

    def push(self, item: _Item) -> None:
        if item.key is not None:
            queued = self.waiting.get(item.key)
            if queued is not None:  # the client hasn't read the older one yet: it only needs the newest
                queued.line = item.line
                return
            item = _Item(item.line, item.key, item.terminal)  # each client coalesces into its own copy
            self.waiting[item.key] = item
        self.items.append(item)
        if len(self.items) > self.maxsize:
            self._drop_oldest()
        self.ready.set()

    def _drop_oldest(self) -> None:
        for i, old in enumerate(self.items):
            if not old.terminal:
                del self.items[i]
                if old.key is not None:
                    self.waiting.pop(old.key, None)
                self.dropped += 1
                return

    async def get(self) -> _Item:
        while not self.items:
            self.ready.clear()
            await self.ready.wait()
        item = self.items.popleft()
        if item.key is not None:
            self.waiting.pop(item.key, None)
        return item


class EventBus:
    """Fan-out of `{type, data}` events to every connected `/api/events` client."""

    def __init__(self, queue_size: int = QUEUE_SIZE, heartbeat_s: float = HEARTBEAT_S) -> None:
        self.queue_size = queue_size
        self.heartbeat_s = heartbeat_s
        self._subs: set[_Subscriber] = set()
        self._lock = threading.Lock()
        self._closed = False

    def publish(self, type_: str, data: object = None, *, key: str | None = None, terminal: bool = False) -> None:
        """Send one event to every client. Safe from any thread; never blocks, never raises.

        `key`: a client that hasn't read the last event with this key yet gets this one in its place (progress).
        `terminal`: kept even when the client's queue overflows (a job's final state).
        """
        try:
            line = json.dumps({"type": type_, "data": data}, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as e:
            log.warning("dropping unencodable %s event: %s", type_, e)
            return
        self._broadcast(_Item(line, key, terminal))

    def _broadcast(self, item: _Item) -> None:
        with self._lock:
            subs = list(self._subs)
        for sub in subs:
            try:
                sub.loop.call_soon_threadsafe(sub.push, item)
            except RuntimeError:  # that client's loop is gone
                self._discard(sub)

    def _discard(self, sub: _Subscriber) -> None:
        with self._lock:
            self._subs.discard(sub)

    @property
    def clients(self) -> int:
        with self._lock:
            return len(self._subs)

    def close(self) -> None:
        """End every open stream (server shutdown); later subscribers get an immediately-closed stream."""
        with self._lock:
            self._closed = True
        self._broadcast(_CLOSE)

    async def stream(self, until_disconnected: Callable[[], Awaitable[None]] | None = None) -> AsyncIterator[str]:
        """SSE body: `data: <json>` per event, a comment line every `heartbeat_s`.

        `until_disconnected` returns when the client goes away; the stream then ends and unsubscribes at once.
        """
        sub = _Subscriber(asyncio.get_running_loop(), self.queue_size)
        with self._lock:
            closed = self._closed
            if not closed:
                self._subs.add(sub)
        if closed:
            return
        watcher = asyncio.ensure_future(until_disconnected()) if until_disconnected is not None else None
        getter: asyncio.Future | None = None
        try:
            yield "retry: 2000\n: connected\n\n"
            while True:
                getter = asyncio.ensure_future(sub.get())
                waiting = {getter} if watcher is None else {getter, watcher}
                done, _ = await asyncio.wait(waiting, timeout=self.heartbeat_s, return_when=asyncio.FIRST_COMPLETED)
                if getter not in done:
                    getter.cancel()
                    if watcher is not None and watcher in done:
                        break
                    yield ": heartbeat\n\n"
                    continue
                item = getter.result()
                if item is _CLOSE:
                    break
                yield f"data: {item.line}\n\n"
        finally:  # also reached when the response task is cancelled mid-wait (client gone, shutdown)
            for task in (getter, watcher):
                if task is not None and not task.done():
                    task.cancel()
            self._discard(sub)
