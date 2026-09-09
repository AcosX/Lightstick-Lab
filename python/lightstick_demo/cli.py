# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lightstick-control contributors
"""Headless command-line control for Lightstick Lab."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from typing import Any, Sequence

from .controller import (
    TxOutcomeUncertainError,
    d8_tx_commands,
    d8_update_slots,
    execute_persistent_d8_tx,
)
from .discovery import AutoDiscovery, DiscoveryError
from .protocol import (
    DEFAULT_FREQUENCY_HZ,
    DEFAULT_POWER_DBM,
    D8_ZONES,
    build_d8_frames_from_slots,
    build_d8_transaction,
    d8_word,
    hex_bytes,
)
from .state import StateError, StateStore
from .engine import Engine
from .model import LogicalUpdate, RadioSettings
from .protocols.registry import ProtocolRegistry
from .transports import TransportError


EXIT_USAGE = 2
EXIT_STATE = 3
EXIT_DISCOVERY = 4
EXIT_TX_FAILED = 5
EXIT_TX_UNCERTAIN = 6
EXIT_INTERNAL = 7

EFFECT_FUNCTIONS = {"solid": 0, "slow": 1, "medium": 2, "fast": 3}


class CliUsageError(ValueError):
    pass


class CliArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CliUsageError(message)


def build_parser() -> argparse.ArgumentParser:
    parser = CliArgumentParser(prog="lightstickctl", description="Control a lightstick through the ESP32 bridge")
    parser.add_argument(
        "--zone",
        action="append",
        required=True,
        metavar="A[,B]|all",
        help="D8 zone A-I; repeat or separate zones with commas",
    )
    parser.add_argument("--protocol", default="protocol_d8", help="Protocol plugin ID")
    parser.add_argument("--rgb", required=True, metavar="RRGGBB", help="six-digit RGB colour")
    parser.add_argument("--effect", choices=('solid','slow','medium','fast','off','fade_in','fade_out','hold'), default="solid")
    parser.add_argument("--transport", choices=("auto", "usb", "wifi", "ble"), default="auto")
    parser.add_argument(
        "--frequency",
        "--frequency-hz",
        dest="frequency_hz",
        type=int,
        default=DEFAULT_FREQUENCY_HZ,
    )
    parser.add_argument(
        "--power",
        "--power-dbm",
        dest="power_dbm",
        type=int,
        default=DEFAULT_POWER_DBM,
    )
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def parse_zones(values: Sequence[str]) -> tuple[str, ...]:
    selected: set[str] = set()
    for value in values:
        for part in str(value).split(","):
            zone = part.strip().upper()
            if not zone:
                raise CliUsageError("--zone 不能包含空值")
            if zone == "ALL":
                selected.update(D8_ZONES)
            elif zone in D8_ZONES:
                selected.add(zone)
            else:
                raise CliUsageError(f"D8 仅支持组别 A-I，不支持: {zone}")
    if not selected:
        raise CliUsageError("至少需要一个 --zone")
    return tuple(zone for zone in D8_ZONES if zone in selected)


def parse_rgb(value: str) -> tuple[str, tuple[int, int, int]]:
    text = str(value).strip().removeprefix("#").upper()
    if not re.fullmatch(r"[0-9A-F]{6}", text):
        raise CliUsageError("--rgb 必须为 6 位十六进制 RRGGBB")
    channels = tuple(int(text[index : index + 2], 16) for index in range(0, 6, 2))
    nibbles = tuple(min(15, (channel + 8) // 17) for channel in channels)
    return text, nibbles  # type: ignore[return-value]


def validate_radio_args(frequency_hz: int, power_dbm: int) -> None:
    in_band = any(
        low <= frequency_hz <= high
        for low, high in ((300_000_000, 348_000_000), (378_000_000, 464_000_000), (779_000_000, 928_000_000))
    )
    if not in_band:
        raise CliUsageError("--frequency 必须位于 CC1101 支持的频段")
    if not -30 <= power_dbm <= 10:
        raise CliUsageError("--power 必须为 -30..10 dBm")


@dataclass(frozen=True)
class PreparedCommand:
    zones: tuple[str, ...]
    rgb: str
    effect: str
    word: int
    slots: tuple[int, ...]
    frames_hex: tuple[str, ...]
    commands: list[tuple[str, dict[str, Any]]]
    pulse_count: int
    duration_us: int


def prepare_command(args: argparse.Namespace, previous_slots: Sequence[int]) -> PreparedCommand:
    zones = parse_zones(args.zone)
    rgb_text, (red, green, blue) = parse_rgb(args.rgb)
    validate_radio_args(args.frequency_hz, args.power_dbm)
    function = EFFECT_FUNCTIONS[args.effect]
    word = d8_word(red, green, blue, function)
    slots = d8_update_slots(previous_slots, zones, word)
    frames = build_d8_frames_from_slots(slots)
    pulses = build_d8_transaction(slots)
    commands = d8_tx_commands(pulses, args.frequency_hz, args.power_dbm, 20_000)
    return PreparedCommand(
        zones=zones,
        rgb=rgb_text,
        effect=args.effect,
        word=word,
        slots=slots,
        frames_hex=tuple(hex_bytes(frame) for frame in frames),
        commands=[(command, dict(command_args)) for command, command_args in commands],
        pulse_count=len(pulses.durations_us),
        duration_us=pulses.total_us,
    )


def _base_output(args: argparse.Namespace, prepared: PreparedCommand) -> dict[str, Any]:
    return {
        "ok": True,
        "family": "D8",
        "zones": list(prepared.zones),
        "rgb": prepared.rgb,
        "effect": prepared.effect,
        "frequency_hz": args.frequency_hz,
        "power_dbm": args.power_dbm,
        "target_ack": False,
    }


def execute(
    args: argparse.Namespace,
    *,
    store: StateStore | None = None,
    discovery: AutoDiscovery | None = None,
) -> dict[str, Any]:
    store = store or StateStore()
    engine = Engine(store=store, discovery=discovery, protocol_id=args.protocol)
    if args.protocol not in engine.registry.plugins:
        raise CliUsageError('Unknown protocol: ' + args.protocol)
    rgb_text, rgb = parse_rgb(args.rgb)
    validate_radio_args(args.frequency_hz, args.power_dbm)
    zones = set()
    for item in args.zone:
        for zone in item.split(','):
            zone = zone.strip().upper()
            zones.update(engine.protocol.capabilities.zones if zone == 'ALL' else (zone,))
    update = LogicalUpdate(tuple(sorted(zones)), rgb, args.effect)
    radio = RadioSettings(args.frequency_hz, args.power_dbm)
    try:
        plan = engine.preview(update, radio)
    except ValueError as exc:
        raise CliUsageError(str(exc)) from exc
    output = {'ok': True, 'protocol': engine.protocol.id, 'family': engine.protocol.display_name.split()[0],
              'zones': sorted(zones), 'rgb': rgb_text, 'effect': args.effect,
              'frequency_hz': args.frequency_hz, 'power_dbm': args.power_dbm, 'target_ack': False}
    if args.dry_run:
        output.update(dry_run=True, frames_hex=[hex_bytes(frame) for frame in plan.frames],
                      transaction={'command': plan.commands[0][0], 'frames': len(plan.frames),
                                   'pulse_durations': sum(len(a.get('durations_us', ())) for _, a in plan.commands),
                                   'duration_us': sum(sum(a.get('durations_us', ())) for _, a in plan.commands)})
        return output
    try:
        with store.locked():
            found = engine.connect(args.transport, lock_held=True)
            results = engine.execute_update(update, radio, lock_held=True)
        result = results[-1]
        output.update(dry_run=False, transport=found.kind, endpoint=found.endpoint,
                      request_id=str(result.get('request_id') or ''), status=str(result.get('status') or 'success'),
                      attempts=found.attempts)
        return output
    finally:
        engine.stop()


def _error_payload(code: str, message: str, **details: Any) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    error.update(details)
    return {"ok": False, "error": error}


def _emit(payload: dict[str, Any], json_output: bool, *, error: bool = False) -> None:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return
    stream = sys.stderr if error else sys.stdout
    if payload.get("ok"):
        if payload.get("dry_run"):
            print(
                f"{payload['family']} dry-run: zones={','.join(payload['zones'])} rgb={payload['rgb']} "
                f"effect={payload['effect']}",
                file=stream,
            )
        else:
            print(
                f"TX success via {payload['transport']} ({payload['endpoint']}), "
                f"request_id={payload.get('request_id') or '-'}; target ACK unavailable",
                file=stream,
            )
    else:
        error_body = payload.get("error") or {}
        print(f"{error_body.get('code', 'error')}: {error_body.get('message', '')}", file=stream)


def main(argv: Sequence[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    json_output = "--json" in values
    try:
        args = build_parser().parse_args(values)
        payload = execute(args)
    except CliUsageError as exc:
        payload = _error_payload("usage", str(exc))
        _emit(payload, json_output, error=True)
        return EXIT_USAGE
    except StateError as exc:
        payload = _error_payload("state_error", str(exc))
        _emit(payload, json_output, error=True)
        return EXIT_STATE
    except DiscoveryError as exc:
        payload = _error_payload("discovery_failed", str(exc), attempts=exc.attempts)
        _emit(payload, json_output, error=True)
        return EXIT_DISCOVERY
    except TxOutcomeUncertainError as exc:
        payload = _error_payload("tx_outcome_uncertain", str(exc), target_ack=False)
        _emit(payload, json_output, error=True)
        return EXIT_TX_UNCERTAIN
    except TransportError as exc:
        payload = _error_payload("tx_failed", str(exc), target_ack=False)
        _emit(payload, json_output, error=True)
        return EXIT_TX_FAILED
    except Exception as exc:  # pragma: no cover - final defensive boundary
        payload = _error_payload("internal_error", str(exc))
        _emit(payload, json_output, error=True)
        return EXIT_INTERNAL
    _emit(payload, args.json_output)
    return 0
