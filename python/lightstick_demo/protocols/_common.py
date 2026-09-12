# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lightstick-control contributors
"""Pure protocol builders for the verified lightstick commands.

The functions in this module do not open a device or transmit anything.  They
only construct bytes and the 250 us OOK pulse representation described in the
protocol note.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

CHECKSUM_SEED = 0x96
DEFAULT_BIT_US = 250
AIR_PREFIX_BITS = "11111111000011110"
DEFAULT_D8_PHASES = (2, 1, 0, 2, 1, 0)
D8_SLOT_COUNT = 9
D8_ZONES = tuple(chr(ord("A") + index) for index in range(D8_SLOT_COUNT))
D8_FRAME_BYTES = 21
D8_FRAME_START_INTERVAL_US = 131_100
D8_FRAME_DURATION_US = (len(AIR_PREFIX_BITS) + (D8_FRAME_BYTES * 8 * 3)) * DEFAULT_BIT_US
DEFAULT_D8_GAP_US = D8_FRAME_START_INTERVAL_US - D8_FRAME_DURATION_US
MAX_PULSE_DURATIONS = 2048
MAX_PULSE_SEQUENCE_US = 1_000_000
DEFAULT_FREQUENCY_HZ = 433_920_000
DEFAULT_POWER_DBM = -20
MAX_TX_REPEAT = 10

STATE_NAMES = {
    0x00: "暗",
    0x01: "常亮",
    0x02: "慢闪",
    0x03: "中闪",
    0x04: "快闪",
    0x05: "Fade in",
    0x06: "Fade out",
    0x0B: "保持状态",
}

COLOR_NAMES = {
    0x00: "红",
    0x01: "绿",
    0x02: "蓝",
    0x03: "粉",
    0x04: "白",
    0x05: "黄",
    0x06: "浅蓝",
    0x07: "浅绿",
    0x08: "紫",
    0x09: "橙",
    0x0A: "浅粉",
    0x0B: "较深蓝",
    0x0C: "浅黄",
    0x0D: "较深绿",
    0x0E: "浅红",
    0x0F: "更亮白",
    0xAA: "保持颜色",
}


@dataclass(frozen=True)
class PulseSequence:
    """A level followed by alternating high/low durations in microseconds."""

    start_level: int
    durations_us: tuple[int, ...]

    @property
    def total_us(self) -> int:
        return sum(self.durations_us)

    def as_dict(self) -> dict[str, object]:
        return {
            "start_level": self.start_level,
            "durations_us": list(self.durations_us),
        }


def validate_byte(value: int, label: str = "byte") -> int:
    if not isinstance(value, int) or not 0 <= value <= 0xFF:
        raise ValueError(f"{label} must be an integer in 0..255")
    return value


def validate_nibble(value: int, label: str = "nibble") -> int:
    if not isinstance(value, int) or not 0 <= value <= 0x0F:
        raise ValueError(f"{label} must be an integer in 0..15")
    return value


def checksum(data: Iterable[int]) -> int:
    """Return the documented additive checksum for preceding frame bytes."""

    values = [validate_byte(value) for value in data]
    return (CHECKSUM_SEED + sum(values)) & 0xFF


def with_checksum(data: Iterable[int]) -> bytes:
    values = bytes(validate_byte(value) for value in data)
    return values + bytes([checksum(values)])


def hex_bytes(data: Iterable[int] | bytes | bytearray) -> str:
    return " ".join(f"{value:02X}" for value in data)


def parse_hex(text: str) -> bytes:
    cleaned = "".join(text.split()).replace(":", "")
    if len(cleaned) % 2 or any(char not in "0123456789abcdefABCDEF" for char in cleaned):
        raise ValueError("十六进制数据必须由完整字节组成")
    return bytes.fromhex(cleaned)


def zone_mask(zones: Iterable[str]) -> tuple[int, int]:
    """Encode selected A-P zones into the two documented mask bytes."""

    mask1 = 0
    mask2 = 0
    for zone in zones:
        name = str(zone).strip().upper()
        if len(name) != 1 or not "A" <= name <= "P":
            raise ValueError("区域必须是 A-P")
        index = ord(name) - ord("A")
        if index < 8:
            mask1 |= 1 << index
        else:
            mask2 |= 1 << (index - 8)
    return mask1, mask2


def build_partition_frame(
    mask1: int,
    mask2: int,
    x: int = 0xFF,
    state: int = 0x01,
    color: int = 0x00,
) -> bytes:
    """Build ``00 M1 M2 X S C K``."""

    for value, label in ((mask1, "M1"), (mask2, "M2"), (x, "X"), (state, "state")):
        validate_byte(value, label)
    if color != 0xAA:
        validate_nibble(color, "color")
    return with_checksum((0x00, mask1, mask2, x, state, color))


def build_c0_frame(colors_a_to_j: Sequence[int]) -> bytes:
    """Build ``C0 AB CD EF GH IJ K``.

    The verified format exposes A-J only.  K-P are intentionally absent rather
    than guessed from the two-byte A-P mask used by command 00.
    """

    if len(colors_a_to_j) != 10:
        raise ValueError("C0 requires exactly 10 colors for A-J; K-P are unsupported")
    values = [validate_nibble(value, f"color {index}") for index, value in enumerate(colors_a_to_j)]
    packed = [(values[index] << 4) | values[index + 1] for index in range(0, 10, 2)]
    return with_checksum((0xC0, *packed))


def build_a6_frame(color: int = 0x07) -> bytes:
    """Build the verified short pulse command ``A6 C K``."""

    validate_nibble(color, "A6 color")
    return with_checksum((0xA6, color))


def build_da_frame(
    mask1: int,
    mask2: int,
    x1: int = 0xFF,
    x2: int = 0x01,
    x3: int = 0x00,
) -> bytes:
    """Build the physical-button unlock command.

    The command has been verified on the current stick: after transmitting
    the DA frame from this software, the physical buttons were confirmed to
    unlock again. Other batches of sticks may still require independent
    validation.
    """

    for value, label in ((mask1, "M1"), (mask2, "M2"), (x1, "X1"), (x2, "X2"), (x3, "X3")):
        validate_byte(value, label)
    return with_checksum((0xDA, mask1, mask2, x1, x2, x3))


def rol16(value: int, amount: int) -> int:
    value &= 0xFFFF
    amount %= 16
    return ((value << amount) | (value >> (16 - amount))) & 0xFFFF


def d8_word(red: int, green: int, blue: int, function: int = 0) -> int:
    """Return the logical D8 control word ``FFFF RRRR GGGG BBBB``."""

    for value, label in ((red, "red"), (green, "green"), (blue, "blue"), (function, "function")):
        validate_nibble(value, label)
    if function > 3:
        raise ValueError("D8 canonical function must be 0..3")
    return (function << 12) | (red << 8) | (green << 4) | blue


def build_d8_frame(
    red: int,
    green: int,
    blue: int,
    function: int = 0,
    phase: int = 0,
    slots: Sequence[int] | None = None,
) -> bytes:
    """Build one canonical 21-byte ``D8`` frame.

    ``W = FFFF RRRR GGGG BBBB`` uses the canonical function nibble ``F``.
    The nine 2-byte positions after the type byte are position-based zone
    slots (tentatively ``A..I``); each logical word is stored as
    ``A = ROL16(W, 2)`` big-endian.  When ``slots`` is supplied it must contain
    exactly ``D8_SLOT_COUNT`` raw logical words (slot 0 is the current zone-A
    candidate).  When ``slots`` is None the observed single-active-slot shape
    is used: slot 0 carries the requested colour/function word and slots 1..8
    are zero.  The mapping from those positions to A..I remains a hypothesis.
    """

    for value, label in ((red, "red"), (green, "green"), (blue, "blue"), (function, "function")):
        validate_nibble(value, label)
    validate_byte(phase, "phase")
    if slots is None:
        slots = (d8_word(red, green, blue, function),) + (0,) * (D8_SLOT_COUNT - 1)
    return build_d8_frame_from_slots(slots, phase)


def _validate_d8_slots(slots: Sequence[int]) -> tuple[int, ...]:
    if len(slots) != D8_SLOT_COUNT:
        raise ValueError(f"D8 requires exactly {D8_SLOT_COUNT} slot words")
    values = tuple(slots)
    for slot in values:
        if not isinstance(slot, int) or not 0 <= slot <= 0xFFFF:
            raise ValueError("D8 slot words must be integers in 0..65535")
    return values


def build_d8_frame_from_slots(slots: Sequence[int], phase: int = 0) -> bytes:
    """Build one D8 frame from nine raw logical 16-bit slot words.

    The slots are position based (``slot 0`` is the current A-zone candidate)
    and are rotated left by two bits before being written big-endian on air.
    ``phase`` is an independent byte and is included in the checksum.
    """

    validate_byte(phase, "phase")
    values = _validate_d8_slots(slots)
    encoded = bytearray([0xD8])
    for slot in values:
        word = rol16(slot, 2)
        encoded.append((word >> 8) & 0xFF)
        encoded.append(word & 0xFF)
    encoded.append(phase)
    data = bytes(encoded)
    frame = with_checksum(data)
    if len(frame) != D8_FRAME_BYTES:
        raise AssertionError(f"D8 frame must contain exactly {D8_FRAME_BYTES} bytes")
    return frame


def build_d8_frames_from_slots(
    slots: Sequence[int],
    phases: Sequence[int] = DEFAULT_D8_PHASES,
) -> tuple[bytes, ...]:
    """Build independently checksummed D8 frames for the requested phases."""

    values = _validate_d8_slots(slots)
    return tuple(build_d8_frame_from_slots(values, phase) for phase in phases)


def build_d8_frames(
    red: int,
    green: int,
    blue: int,
    function: int = 0,
    phases: Sequence[int] = DEFAULT_D8_PHASES,
    zones: Sequence[str] | None = None,
) -> tuple[bytes, ...]:
    """Build the D8 phase frames for the requested zones (A..I).

    ``zones`` selects which slots receive the colour/function word; every
    unselected slot stays ``0x0000`` (off).  When ``zones`` is None only zone
    ``A`` (slot 0) is written, preserving the observed single-active-slot
    shape under the current A..I working hypothesis.
    """

    if zones is None:
        zones = ("A",)
    selected = {str(zone).strip().upper() for zone in zones}
    invalid = sorted(zone for zone in selected if zone not in set(D8_ZONES))
    if invalid:
        raise ValueError(f"D8 仅支持组别 A-I，不支持: {'、'.join(invalid)}")
    word = d8_word(red, green, blue, function)
    slots = tuple(word if zone in selected else 0 for zone in D8_ZONES)
    return build_d8_frames_from_slots(slots, phases)


def encode_air_pulses(payload: bytes, bit_us: int = DEFAULT_BIT_US) -> PulseSequence:
    """Encode bytes using the verified raw prefix and ``01+d`` bit coding."""

    if not payload:
        raise ValueError("payload must contain at least one byte")
    if not isinstance(bit_us, int) or bit_us < 50:
        raise ValueError("bit_us must be at least 50")
    payload_bits = "".join(f"{value:08b}" for value in payload)
    raw_bits = AIR_PREFIX_BITS + "".join(f"01{bit}" for bit in payload_bits)
    levels = [1 if bit == "1" else 0 for bit in raw_bits]
    durations: list[int] = []
    current = levels[0]
    run = 0
    for level in levels:
        if level == current:
            run += 1
        else:
            durations.append(run * bit_us)
            current = level
            run = 1
    durations.append(run * bit_us)
    return PulseSequence(start_level=levels[0], durations_us=tuple(durations))


def combine_pulse_sequences(
    sequences: Sequence[PulseSequence],
    gap_us: int = DEFAULT_D8_GAP_US,
) -> PulseSequence:
    """Concatenate frame pulse sequences into one continuous OOK transaction.

    A ``gap_us`` low-level period is inserted between consecutive sequences so
    the burst matches the field-observed frame cadence (~131 ms start to start
    for a 21-byte D8 frame at 250 us per bit).  Every sequence must start at
    level 1 (the verified air prefix starts high).
    """

    if not sequences:
        raise ValueError("sequences must contain at least one PulseSequence")
    if not isinstance(gap_us, int) or gap_us <= 0:
        raise ValueError("gap_us must be a positive integer")
    merged: list[int] = []
    for index, sequence in enumerate(sequences):
        if sequence.start_level != 1:
            raise ValueError("combined frame sequences must start at level 1")
        if not sequence.durations_us:
            raise ValueError("combined frame sequences must contain durations")
        if any(duration <= 0 for duration in sequence.durations_us):
            raise ValueError("combined frame sequences must contain positive durations")
        if index:
            # The encoded frame ends either high or low.  Extend an existing
            # low run when possible; otherwise append a new low run before
            # the next frame's high preamble.
            previous_ends_high = len(merged) % 2 == 1
            if previous_ends_high:
                merged.append(gap_us)
            else:
                merged[-1] += gap_us
        merged.extend(sequence.durations_us)
    if len(merged) > MAX_PULSE_DURATIONS:
        raise ValueError(f"combined pulse sequence requires at most {MAX_PULSE_DURATIONS} durations")
    total_us = sum(merged)
    if total_us > MAX_PULSE_SEQUENCE_US:
        raise ValueError(f"combined pulse sequence cannot exceed {MAX_PULSE_SEQUENCE_US // 1000} ms")
    return PulseSequence(start_level=1, durations_us=tuple(merged))


def build_d8_transaction(
    slots: Sequence[int],
    phases: Sequence[int] = DEFAULT_D8_PHASES,
    bit_us: int = DEFAULT_BIT_US,
    frame_interval_us: int = D8_FRAME_START_INTERVAL_US,
) -> PulseSequence:
    """Build one continuous OOK transaction containing all D8 phase frames.

    Each phase remains a complete, independently checksummed 21-byte frame.
    The default low gap targets the observed 131.1ms frame start interval.
    """

    frames = build_d8_frames_from_slots(slots, phases)
    pulses = tuple(encode_air_pulses(frame, bit_us) for frame in frames)
    if not pulses:
        raise ValueError("D8 transaction requires at least one phase")
    if not isinstance(frame_interval_us, int) or frame_interval_us <= 0:
        raise ValueError("frame_interval_us must be a positive integer")
    gap_us = frame_interval_us - pulses[0].total_us
    if gap_us <= 0:
        raise ValueError("frame_interval_us must exceed the encoded D8 frame duration")
    if any(pulse.total_us != pulses[0].total_us for pulse in pulses):
        raise ValueError("D8 phase frames must have equal encoded durations")
    return combine_pulse_sequences(pulses, gap_us=gap_us)


def build_d8_pulse_sequence(
    slots: Sequence[int],
    phases: Sequence[int] = DEFAULT_D8_PHASES,
    bit_us: int = DEFAULT_BIT_US,
    frame_interval_us: int = D8_FRAME_START_INTERVAL_US,
) -> PulseSequence:
    """Compatibility alias for :func:`build_d8_transaction`."""

    return build_d8_transaction(slots, phases, bit_us, frame_interval_us)


def tx_pulses_args(
    pulses: PulseSequence,
    frequency_hz: int = DEFAULT_FREQUENCY_HZ,
    power_dbm: int = DEFAULT_POWER_DBM,
    repeat: int = 1,
    gap_us: int = 20_000,
) -> dict[str, object]:
    """Create the ESP32 ``TX_PULSES`` argument object."""

    if pulses.start_level not in (0, 1):
        raise ValueError("start_level must be 0 or 1")
    if not pulses.durations_us or any(duration <= 0 for duration in pulses.durations_us):
        raise ValueError("durations_us must contain positive durations")
    if len(pulses.durations_us) > MAX_PULSE_DURATIONS:
        raise ValueError(f"durations_us requires at most {MAX_PULSE_DURATIONS} entries")
    if pulses.total_us > MAX_PULSE_SEQUENCE_US:
        raise ValueError(f"one pulse sequence cannot exceed {MAX_PULSE_SEQUENCE_US // 1000} ms")
    if not 1 <= repeat <= MAX_TX_REPEAT:
        raise ValueError(f"repeat must be in 1..{MAX_TX_REPEAT}")
    if gap_us < 0:
        raise ValueError("gap_us must not be negative")
    return {
        "durations_us": list(pulses.durations_us),
        "start_level": pulses.start_level,
        "frequency_hz": int(frequency_hz),
        "power_dbm": int(power_dbm),
        "repeat": int(repeat),
        "gap_us": int(gap_us),
    }


def tx_packet_args(
    data: bytes,
    frequency_hz: int = DEFAULT_FREQUENCY_HZ,
    power_dbm: int = DEFAULT_POWER_DBM,
    repeat: int = 1,
    gap_us: int = 20_000,
) -> dict[str, object]:
    """Create the ESP32 ``TX_PACKET`` argument object.

    Packet mode is deliberately separate from the air protocol builders: it is
    a generic bridge operation and does not add a checksum or OOK preamble.
    """

    if not isinstance(data, (bytes, bytearray)) or not 1 <= len(data) <= 61:
        raise ValueError("data must contain 1..61 bytes")
    if not 1 <= repeat <= MAX_TX_REPEAT:
        raise ValueError(f"repeat must be in 1..{MAX_TX_REPEAT}")
    if gap_us < 0:
        raise ValueError("gap_us must not be negative")
    return {
        "data_hex": hex_bytes(data),
        "frequency_hz": int(frequency_hz),
        "power_dbm": int(power_dbm),
        "repeat": int(repeat),
        "gap_us": int(gap_us),
    }
