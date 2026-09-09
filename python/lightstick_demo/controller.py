# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lightstick-control contributors
"""GUI-independent bridge transmission controller.

The controller owns the point where a bridge transmission becomes committed:
once a TX command has been submitted, it only polls that same request.  It
never falls back to another transport and never submits the waveform twice.
"""

from __future__ import annotations

import time
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from .transports import BaseTransport, TransportError, TransportTimeoutError


TX_POLL_MIN_TIMEOUT_SECONDS = 30.0
TX_POLL_TRANSPORT_MARGIN_SECONDS = 12.0


class TxOutcomeUncertainError(TransportTimeoutError):
    """A TX may have run, but its final bridge-side outcome is unknown."""


class TxSubmissionUncertainError(TxOutcomeUncertainError):
    """The bridge may have received a TX command whose ACK was not observed."""


def tx_poll_timeout_seconds(args: dict[str, Any]) -> float:
    """Return a deadline that covers the firmware's bounded pulse TX."""

    durations_us = sum(max(0, int(value)) for value in args.get("durations_us", ()))
    repeat = max(1, int(args.get("repeat", 1)))
    gap_us = max(0, int(args.get("gap_us", 0)))
    expected_seconds = (
        (durations_us * repeat) + (gap_us * max(0, repeat - 1))
    ) / 1_000_000.0
    return max(TX_POLL_MIN_TIMEOUT_SECONDS, expected_seconds + TX_POLL_TRANSPORT_MARGIN_SECONDS)


def wait_for_tx_result(
    transport: BaseTransport,
    request_id: str,
    timeout: float,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Poll the idempotent ``GET_TX_RESULT`` command to a final result."""

    if timeout <= 0:
        raise TransportError("TX 结果等待超时必须大于 0")
    deadline = clock() + timeout
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            raise TransportTimeoutError("TX 结果等待超时，已发送 ABORT")
        try:
            result = transport.request(
                "GET_TX_RESULT",
                {"request_id": request_id},
                timeout=min(3.0, remaining),
            )
        except TransportTimeoutError:
            remaining = deadline - clock()
            if remaining <= 0:
                raise TransportTimeoutError("TX 结果等待超时，已发送 ABORT")
            sleep(min(0.05, max(0.01, remaining)))
            continue
        if not isinstance(result, dict):
            raise TransportError("GET_TX_RESULT 返回格式无效")
        status = str(result.get("status") or "").lower()
        if status in {"success", "sent", "completed"}:
            return result
        if status in {"error", "failed", "aborted"}:
            raise TransportError(str(result.get("error") or f"TX {status}"))
        if status != "pending":
            raise TransportError(f"未知 TX result status: {status or '(empty)'}")
        sleep(min(0.05, max(0.01, deadline - clock())))


class Controller:
    """Execute bridge TX commands against one already-validated transport."""

    def __init__(self, transport: BaseTransport) -> None:
        self.transport = transport

    def _abort(self) -> None:
        try:
            self.transport.request("ABORT", {}, timeout=5)
        except Exception:
            pass

    def execute_commands(
        self,
        commands: Sequence[tuple[str, dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        """Submit every command once and wait for its bridge-side completion."""

        results: list[dict[str, Any]] = []
        try:
            for command, args in commands:
                try:
                    result = self.transport.request(command, args, timeout=12)
                except TransportTimeoutError as exc:
                    if command.upper() in {"TX_PULSES", "TX_PACKET"}:
                        raise TxSubmissionUncertainError(
                            f"{command} 提交后未收到响应；为避免重复发射，未重试"
                        ) from exc
                    raise
                if not isinstance(result, dict):
                    raise TransportError(f"{command} 返回格式无效")
                if command.upper() == "TX_PULSES":
                    request_id = str(result.get("request_id") or "")
                    if not request_id:
                        raise TransportError("TX_PULSES 未返回 request_id")
                    try:
                        result = wait_for_tx_result(
                            self.transport,
                            request_id,
                            tx_poll_timeout_seconds(args),
                        )
                    except TransportTimeoutError as exc:
                        raise TxOutcomeUncertainError(
                            f"TX_PULSES {request_id} 已提交，但未收到最终结果；为避免重复发射，未重试"
                        ) from exc
                elif str(result.get("status") or "").lower() not in {
                    "sent",
                    "success",
                    "completed",
                }:
                    raise TransportError("TX_PACKET 未返回成功结果")
                results.append(result)
            return results
        except Exception:
            self._abort()
            raise



def __getattr__(name):
    from . import _legacy_controller
    if hasattr(_legacy_controller, name):
        return getattr(_legacy_controller, name)
    raise AttributeError(name)
