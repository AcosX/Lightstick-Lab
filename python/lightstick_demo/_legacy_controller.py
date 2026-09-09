"""Compatibility helpers; new frontends use Engine."""
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, Sequence
from .protocol import D8_ZONES, PulseSequence, build_d8_transaction, tx_pulses_args
from .state import StateStore
from .transports import BaseTransport
from .controller import Controller

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
        state['protocol_states']['protocol_d8'] = {'slots': list(candidate)}
        store.save_unlocked(state)
        return PersistentD8TxResult(previous, candidate, results)
