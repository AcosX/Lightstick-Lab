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

from .protocol import (
    D8_ZONES,
    PulseSequence,
    build_d8_transaction,
    tx_pulses_args,
)
from .state import StateStore
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


def _validated_d8_slots(slots: Sequence[int]) -> tuple[int, ...]:
    values = tuple(slots)
    if len(values) != len(D8_ZONES):
        raise ValueError(f"D8 本地状态必须包含 {len(D8_ZONES)} 个槽")
    if any(not isinstance(value, int) or not 0 <= value <= 0xFFFF for value in values):
        raise ValueError("D8 本地槽值必须为 0000..FFFF")
    return values


def _validated_d8_selection(zones: Sequence[str]) -> set[str]:
    selected = {str(zone).strip().upper() for zone in zones}
    invalid = sorted(selected.difference(D8_ZONES))
    if invalid:
        raise ValueError(f"D8 仅支持组别 A-I，不支持: {'、'.join(invalid)}")
    if not selected:
        raise ValueError("D8 至少选择一个组别 A-I")
    return selected


def d8_update_slots(
    previous_slots: Sequence[int],
    zones: Sequence[str],
    word: int,
) -> tuple[int, ...]:
    """Return a candidate state, changing only the selected D8 slots."""

    previous = _validated_d8_slots(previous_slots)
    selected = _validated_d8_selection(zones)
    if not isinstance(word, int) or not 0 <= word <= 0xFFFF:
        raise ValueError("D8 control word 必须为 0000..FFFF")
    return tuple(word if zone in selected else previous[index] for index, zone in enumerate(D8_ZONES))


def d8_slots_for_zones(zones: Sequence[str], word: int) -> tuple[int, ...]:
    """Place one logical D8 word in each selected A-I position."""

    return d8_update_slots((0,) * len(D8_ZONES), zones, word)


def d8_state_after_tx(
    previous_slots: Sequence[int],
    candidate_slots: Sequence[int],
    success: bool,
) -> tuple[int, ...]:
    """Commit a D8 candidate only when its TX transaction completed."""

    previous = _validated_d8_slots(previous_slots)
    candidate = _validated_d8_slots(candidate_slots)
    return candidate if success else previous


def d8_tx_command(
    pulses: PulseSequence,
    frequency_hz: int,
    power_dbm: int,
    gap_us: int,
) -> tuple[str, dict[str, object]]:
    """Return the single bridge command used for one D8 transaction."""

    return (
        "TX_PULSES",
        tx_pulses_args(
            pulses,
            frequency_hz,
            power_dbm,
            repeat=1,
            gap_us=gap_us,
        ),
    )


def d8_tx_commands(
    pulses: PulseSequence,
    frequency_hz: int,
    power_dbm: int,
    gap_us: int,
) -> list[tuple[str, dict[str, object]]]:
    """Return the one-command bridge request for a complete D8 transaction."""

    return [d8_tx_command(pulses, frequency_hz, power_dbm, gap_us)]


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


@dataclass(frozen=True)
class PersistentD8TxResult:
    """One successful D8 TX and the state transition committed with it."""

    previous_slots: tuple[int, ...]
    candidate_slots: tuple[int, ...]
    results: list[dict[str, Any]]

    @property
    def state_changed(self) -> bool:
        return self.previous_slots != self.candidate_slots


def execute_persistent_d8_tx(
    store: StateStore,
    transport: BaseTransport,
    zones: Sequence[str],
    word: int,
    frequency_hz: int,
    power_dbm: int,
    gap_us: int,
    *,
    lock_held: bool = False,
) -> PersistentD8TxResult:
    """Transmit D8 from the latest shared shadow and commit only on success.

    The process lock spans the read, bridge transaction, and atomic state
    write.  This prevents a GUI and CLI process from losing each other's
    unselected-zone updates.  ``lock_held`` is for callers such as the CLI
    that already protect a larger discovery/state transaction.
    """

    context = nullcontext(store) if lock_held else store.locked()
    with context:
        state = store.load_unlocked()
        previous = _validated_d8_slots(state["d8_slots"])
        candidate = d8_update_slots(previous, zones, word)
        pulses = build_d8_transaction(candidate)
        commands = d8_tx_commands(pulses, frequency_hz, power_dbm, gap_us)
        results = Controller(transport).execute_commands(commands)
        state["d8_slots"] = list(candidate)
        store.save_unlocked(state)
        return PersistentD8TxResult(previous, candidate, results)
