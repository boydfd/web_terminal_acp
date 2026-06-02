from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from itertools import count
from uuid import UUID

from app.services.runtime.protocol import AgentMessage, TerminalPayload, encode_agent_message

logger = logging.getLogger(__name__)
INPUT_PRIORITY_TERMINAL_CHUNK_BUDGET = 16

EncodedMessageSender = Callable[[str], Awaitable[None]]


class OutboundWriterClosed(RuntimeError):
    """Raised when an outbound writer is used after it has been closed."""


async def _send_encoded(
    send: EncodedMessageSender,
    message: AgentMessage,
    *,
    slow_send_warn_seconds: float,
) -> None:
    started_at = time.perf_counter()
    await send(encode_agent_message(message))
    elapsed = time.perf_counter() - started_at
    if elapsed >= slow_send_warn_seconds:
        logger.warning(
            "client-agent outbound send was slow",
            extra={
                "message_type": message.type,
                "client_id": str(message.client_id),
                "window_id": str(message.window_id) if message.window_id is not None else None,
                "request_id": message.request_id,
                "elapsed_seconds": round(elapsed, 3),
            },
        )


def _control_message_priority(message: AgentMessage) -> int:
    if message.type.startswith("terminal_") or message.type in {
        "create_window_result",
        "kill_window_result",
    }:
        return 0
    if message.type == "git_worktree_result":
        return 3
    return 1


class ControlMessageWriter:
    def __init__(
        self,
        send: EncodedMessageSender,
        *,
        slow_send_warn_seconds: float = 1.0,
    ) -> None:
        self._send = send
        self._slow_send_warn_seconds = slow_send_warn_seconds
        self._queue: asyncio.PriorityQueue[tuple[int, int, AgentMessage]] = asyncio.PriorityQueue()
        self._sequence = count()
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    def start(self) -> None:
        if self._closed:
            raise OutboundWriterClosed("outbound writer is closed")
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def send(self, message: AgentMessage) -> None:
        if self._closed:
            raise OutboundWriterClosed("outbound writer is closed")
        await self._queue.put((_control_message_priority(message), next(self._sequence), message))

    async def drain(self) -> None:
        await self._queue.join()
        self._raise_task_error()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        while True:
            _priority, _sequence, message = await self._queue.get()
            try:
                await _send_encoded(
                    self._send,
                    message,
                    slow_send_warn_seconds=self._slow_send_warn_seconds,
                )
            finally:
                self._queue.task_done()

    def _raise_task_error(self) -> None:
        if self._task is None or not self._task.done() or self._task.cancelled():
            return
        exception = self._task.exception()
        if exception is not None:
            raise exception


class BulkUploadWriter:
    def __init__(
        self,
        send: EncodedMessageSender,
        *,
        terminal_output_maxsize: int = 2000,
        ai_event_maxsize: int = 2000,
        status_event_maxsize: int = 2000,
        terminal_burst: int = 64,
        terminal_chunk_bytes: int = 8192,
        slow_send_warn_seconds: float = 1.0,
    ) -> None:
        if terminal_burst < 1:
            raise ValueError("terminal_burst must be at least 1")
        if terminal_chunk_bytes < 1:
            raise ValueError("terminal_chunk_bytes must be at least 1")
        self._send = send
        self._terminal_burst = terminal_burst
        self._terminal_chunk_bytes = terminal_chunk_bytes
        self._slow_send_warn_seconds = slow_send_warn_seconds
        self._terminal_output_maxsize = terminal_output_maxsize
        self._terminal_output_queued_count = 0
        self._terminal_output_unfinished_count = 0
        self._terminal_output_queues: dict[UUID, deque[AgentMessage]] = {}
        self._terminal_output_windows: deque[UUID] = deque()
        self._priority_terminal_windows: dict[UUID, int] = {}
        self._priority_terminal_order: deque[UUID] = deque()
        self._terminal_output_condition = asyncio.Condition()
        self._ai_event_queue: asyncio.Queue[AgentMessage] = asyncio.Queue(maxsize=ai_event_maxsize)
        self._status_event_queue: asyncio.Queue[AgentMessage] = asyncio.Queue(maxsize=status_event_maxsize)
        self._not_empty = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    def start(self) -> None:
        if self._closed:
            raise OutboundWriterClosed("outbound writer is closed")
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def send_terminal_output(self, message: AgentMessage) -> None:
        if message.type not in {"terminal_output", "aux_terminal_output"}:
            raise ValueError("BulkUploadWriter.send_terminal_output requires terminal output messages")
        if message.window_id is None:
            raise ValueError("terminal_output messages require window_id")
        for chunk in self._split_terminal_output(message):
            await self._enqueue_terminal_output(chunk)

    async def prioritize_terminal_window(self, window_id: UUID) -> None:
        async with self._terminal_output_condition:
            self._priority_terminal_windows[window_id] = INPUT_PRIORITY_TERMINAL_CHUNK_BUDGET
            self._terminal_output_condition.notify_all()
        self._not_empty.set()

    def _split_terminal_output(self, message: AgentMessage) -> list[AgentMessage]:
        payload = TerminalPayload.model_validate(message.payload)
        data = payload.to_bytes()
        if len(data) <= self._terminal_chunk_bytes:
            return [message]
        metadata = {
            key: value
            for key, value in message.payload.items()
            if key not in {"window_id", "data"}
        }
        return [
            message.model_copy(
                update={
                    "payload": {
                        **TerminalPayload.from_bytes(
                        payload.window_id,
                        data[index : index + self._terminal_chunk_bytes],
                        ).model_dump(mode="json"),
                        **metadata,
                    }
                }
            )
            for index in range(0, len(data), self._terminal_chunk_bytes)
        ]

    async def _enqueue_terminal_output(self, message: AgentMessage) -> None:
        async with self._terminal_output_condition:
            while (
                self._terminal_output_maxsize > 0
                and self._terminal_output_queued_count >= self._terminal_output_maxsize
            ):
                if self._closed:
                    raise OutboundWriterClosed("outbound writer is closed")
                await self._terminal_output_condition.wait()
            if self._closed:
                raise OutboundWriterClosed("outbound writer is closed")
            queue = self._terminal_output_queues.get(message.window_id)
            if queue is None:
                queue = deque()
                self._terminal_output_queues[message.window_id] = queue
                self._terminal_output_windows.append(message.window_id)
            if message.window_id in self._priority_terminal_windows:
                message.payload["input_priority"] = True
                self._priority_terminal_order.append(message.window_id)
            queue.append(message)
            self._terminal_output_queued_count += 1
            self._terminal_output_unfinished_count += 1
        self._not_empty.set()

    async def send_ai_event(self, message: AgentMessage) -> None:
        if message.type not in {"ai_event", "agent_work_presence"}:
            raise ValueError(
                "BulkUploadWriter.send_ai_event requires ai_event or agent_work_presence messages"
            )
        if self._closed:
            raise OutboundWriterClosed("outbound writer is closed")
        if message.type == "agent_work_presence":
            await self._status_event_queue.put(message)
        else:
            await self._ai_event_queue.put(message)
        self._not_empty.set()

    async def drain(self) -> None:
        while True:
            self._raise_task_error()
            async with self._terminal_output_condition:
                terminal_output_done = self._terminal_output_unfinished_count == 0
            if terminal_output_done and self._status_event_queue.empty() and self._ai_event_queue.empty():
                await self._status_event_queue.join()
                await self._ai_event_queue.join()
                self._raise_task_error()
                return
            await asyncio.sleep(0)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._not_empty.set()
        async with self._terminal_output_condition:
            self._terminal_output_condition.notify_all()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        terminal_messages_since_ai_event = 0
        while True:
            queue_name, message = await self._next_message(terminal_messages_since_ai_event)
            try:
                await _send_encoded(
                    self._send,
                    message,
                    slow_send_warn_seconds=self._slow_send_warn_seconds,
                )
            finally:
                if queue_name == "terminal_output":
                    async with self._terminal_output_condition:
                        self._terminal_output_unfinished_count -= 1
                        self._terminal_output_condition.notify_all()
                elif queue_name == "status_event":
                    self._status_event_queue.task_done()
                else:
                    self._ai_event_queue.task_done()

            if queue_name == "terminal_output":
                terminal_messages_since_ai_event += 1
            else:
                terminal_messages_since_ai_event = 0

    async def _next_message(self, terminal_messages_since_ai_event: int) -> tuple[str, AgentMessage]:
        while True:
            self._not_empty.clear()
            if (
                terminal_messages_since_ai_event >= self._terminal_burst
                and (not self._status_event_queue.empty() or not self._ai_event_queue.empty())
            ):
                return self._pop_non_terminal_message()
            terminal_output = await self._pop_terminal_output()
            if terminal_output is not None:
                return "terminal_output", terminal_output
            if not self._status_event_queue.empty():
                return "status_event", self._status_event_queue.get_nowait()
            if not self._ai_event_queue.empty():
                return "ai_event", self._ai_event_queue.get_nowait()
            await self._not_empty.wait()

    def _pop_non_terminal_message(self) -> tuple[str, AgentMessage]:
        if not self._status_event_queue.empty():
            return "status_event", self._status_event_queue.get_nowait()
        return "ai_event", self._ai_event_queue.get_nowait()

    async def _pop_terminal_output(self) -> AgentMessage | None:
        async with self._terminal_output_condition:
            while self._priority_terminal_order:
                window_id = self._priority_terminal_order.popleft()
                if window_id not in self._priority_terminal_windows:
                    continue
                message = self._pop_terminal_output_for_window(
                    window_id,
                    priority_only=True,
                    requeue=False,
                )
                if message is not None:
                    message.payload["input_priority"] = True
                    remaining = self._priority_terminal_windows[window_id] - 1
                    if remaining > 0:
                        self._priority_terminal_windows[window_id] = remaining
                    else:
                        self._priority_terminal_windows.pop(window_id, None)
                    return message
            while self._terminal_output_windows:
                window_id = self._terminal_output_windows.popleft()
                message = self._pop_terminal_output_for_window(window_id)
                if message is not None:
                    return message
            return None

    def _pop_terminal_output_for_window(
        self,
        window_id: UUID,
        *,
        priority_only: bool = False,
        requeue: bool = True,
    ) -> AgentMessage | None:
        queue = self._terminal_output_queues.get(window_id)
        if not queue:
            self._terminal_output_queues.pop(window_id, None)
            return None
        if priority_only:
            message = _pop_first_input_priority_message(queue)
            if message is None:
                return None
        else:
            message = queue.popleft()
        self._terminal_output_queued_count -= 1
        self._terminal_output_condition.notify_all()
        if queue and requeue:
            self._terminal_output_windows.append(window_id)
        elif not queue:
            self._terminal_output_queues.pop(window_id, None)
        return message

    def _raise_task_error(self) -> None:
        if self._task is None or not self._task.done() or self._task.cancelled():
            return
        exception = self._task.exception()
        if exception is not None:
            raise exception


def _pop_first_input_priority_message(queue: deque[AgentMessage]) -> AgentMessage | None:
    for index, message in enumerate(queue):
        if message.payload.get("input_priority") is True:
            del queue[index]
            return message
    return None
