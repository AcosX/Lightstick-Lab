"""Protocol-independent control values. RGB channels use the range 0..15."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any

@dataclass(frozen=True)
class ZoneState:
    rgb: tuple[int, int, int] = (0, 0, 0)
    effect: str = 'solid'
    palette: int | None = None

    def __post_init__(self):
        object.__setattr__(self, "rgb", tuple(self.rgb))
        if len(self.rgb) != 3 or any(type(v) is not int or not 0 <= v <= 15 for v in self.rgb):
            raise ValueError('RGB channels must be integers in 0..15')
        if not isinstance(self.effect, str) or not self.effect:
            raise ValueError('effect must be a nonempty string')

@dataclass(frozen=True)
class LogicalUpdate:
    zones: tuple[str, ...]
    rgb: tuple[int, int, int] = (0, 0, 0)
    effect: str = 'solid'
    palette: int | None = None

    def __post_init__(self):
        object.__setattr__(self, "zones", tuple(self.zones))
        object.__setattr__(self, "rgb", tuple(self.rgb))
        if not self.zones or any(not isinstance(z, str) or not z for z in self.zones):
            raise ValueError('At least one named zone is required')
        ZoneState(self.rgb, self.effect, self.palette)

@dataclass(frozen=True)
class LogicalState:
    values: tuple[tuple[str, ZoneState], ...] = ()

    def apply(self, updates):
        values = dict(self.values)
        for update in updates:
            for zone in update.zones:
                values[zone] = ZoneState(update.rgb, update.effect, update.palette)
        return LogicalState(tuple(sorted(values.items())))

@dataclass(frozen=True)
class Capabilities:
    zones: tuple[str, ...]
    effects: tuple[str, ...] = ('solid', 'slow', 'medium', 'fast', 'off')
    color_mode: str = 'rgb'
    palette: tuple[tuple[int, str, str], ...] = ()
    fade: bool = False
    hold: bool = False

    def validate(self, update):
        unknown = set(update.zones).difference(self.zones)
        if unknown:
            raise ValueError('unsupported_zone = ' + ','.join(sorted(unknown)))
        if update.effect not in self.effects:
            raise ValueError('unsupported_effect = ' + update.effect)

@dataclass(frozen=True)
class RadioSettings:
    frequency_hz: int = 433_920_000
    power_dbm: int = -20
    repeat: int = 1
    gap_us: int = 20_000

    def __post_init__(self):
        if not any(a <= self.frequency_hz <= b for a,b in ((300000000,348000000),(378000000,464000000),(779000000,928000000))):
            raise ValueError('Frequency outside CC1101 bands')
        if not -30 <= self.power_dbm <= 10 or not 1 <= self.repeat <= 10 or self.gap_us < 0:
            raise ValueError('Invalid power, repeat or gap')

@dataclass(frozen=True)
class TransmissionPlan:
    commands: tuple[tuple[str, dict[str, Any]], ...]
    next_state: dict[str, Any] = field(default_factory=dict)
    frames: tuple[bytes, ...] = ()

    def __post_init__(self):
        if any(command not in ('TX_PULSES', 'TX_PACKET') for command, _ in self.commands):
            raise ValueError('Plans may only contain bridge TX commands')
