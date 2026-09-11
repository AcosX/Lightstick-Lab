# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lightstick-control contributors
import unittest

from lightstick_demo.protocol import (
    DEFAULT_D8_PHASES,
    DEFAULT_D8_GAP_US,
    D8_FRAME_DURATION_US,
    D8_FRAME_START_INTERVAL_US,
    MAX_PULSE_DURATIONS,
    MAX_PULSE_SEQUENCE_US,
    PulseSequence,
    build_a6_frame,
    build_c0_frame,
    build_d8_frame,
    build_d8_frames,
    build_d8_frame_from_slots,
    build_d8_frames_from_slots,
    build_d8_transaction,
    build_da_frame,
    build_partition_frame,
    combine_pulse_sequences,
    encode_air_pulses,
    hex_bytes,
    parse_hex,
    tx_packet_args,
    tx_pulses_args,
)


class ProtocolTests(unittest.TestCase):
    def test_legacy_api_reexports_shared_waveform_implementation(self):
        from lightstick_demo import protocol
        from lightstick_demo.protocols import _common
        for name in ('PulseSequence', 'build_d8_transaction', 'build_partition_frame', 'encode_air_pulses'):
            self.assertIs(getattr(protocol, name), getattr(_common, name))
        namespace = {}
        exec('from lightstick_demo.protocol import *', namespace)
        self.assertIs(namespace['PulseSequence'], _common.PulseSequence)

    def test_verified_frames_and_checksums(self) -> None:
        self.assertEqual(hex_bytes(build_partition_frame(0xFF, 0xFF, 0xFF, 1, 0)), "00 FF FF FF 01 00 94")
        self.assertEqual(hex_bytes(build_partition_frame(0xFF, 0xFF, 0xFF, 1, 1)), "00 FF FF FF 01 01 95")
        self.assertEqual(hex_bytes(build_c0_frame(range(10))), "C0 01 23 45 67 89 AF")
        self.assertEqual(hex_bytes(build_a6_frame(0x07)), "A6 07 43")
        self.assertEqual(hex_bytes(build_da_frame(0xFF, 0xFF, 0xFF, 1, 0)), "DA FF FF FF 01 00 6E")

    def test_d8_known_frame_and_phase_sequence(self) -> None:
        frame = build_d8_frame(6, 12, 15, 0, 2)
        self.assertEqual(len(frame), 21)
        self.assertEqual(hex_bytes(frame), "D8 1B 3C 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 02 C7")
        frames = build_d8_frames(6, 12, 15)
        self.assertEqual(tuple(frame[-2] for frame in frames), DEFAULT_D8_PHASES)
        self.assertEqual(len({frame[-1] for frame in frames}), 3)

    def test_d8_arbitrary_nine_slots_are_rotated_and_checksummed(self) -> None:
        slots = (0x1234, 0xABCD, 0x0001, 0, 0, 0, 0, 0, 0)
        frame = build_d8_frame_from_slots(slots, phase=2)
        self.assertEqual(
            hex_bytes(frame),
            "D8 48 D0 AF 36 00 04 00 00 00 00 00 00 00 00 00 00 00 00 02 71",
        )
        frames = build_d8_frames_from_slots(slots, phases=(2, 1, 0))
        self.assertEqual(tuple(frame[-2] for frame in frames), (2, 1, 0))
        self.assertEqual(
            tuple(frame[-1] for frame in frames),
            tuple((0x96 + sum(frame[:-1])) & 0xFF for frame in frames),
        )

    def test_d8_phase_frames_are_one_continuous_transaction(self) -> None:
        slots = (0x06CF,) + (0,) * 8
        pulse = build_d8_transaction(slots)
        self.assertEqual(D8_FRAME_DURATION_US, 130_250)
        self.assertEqual(DEFAULT_D8_GAP_US, 850)
        self.assertEqual(D8_FRAME_START_INTERVAL_US, D8_FRAME_DURATION_US + DEFAULT_D8_GAP_US)
        self.assertEqual(pulse.start_level, 1)
        self.assertEqual(pulse.total_us, 6 * D8_FRAME_DURATION_US + 5 * DEFAULT_D8_GAP_US)
        self.assertLessEqual(len(pulse.durations_us), MAX_PULSE_DURATIONS)
        self.assertLessEqual(pulse.total_us, MAX_PULSE_SEQUENCE_US)
        self.assertEqual(len(pulse.durations_us), 2039)
        self.assertEqual(
            tx_pulses_args(pulse)["durations_us"],
            list(pulse.durations_us),
        )

    def test_combine_pulses_keeps_inter_frame_gap_low(self) -> None:
        ending_low = combine_pulse_sequences(
            (PulseSequence(1, (100, 200)), PulseSequence(1, (300,))),
            gap_us=850,
        )
        self.assertEqual(ending_low.durations_us, (100, 1_050, 300))

        ending_high = combine_pulse_sequences(
            (PulseSequence(1, (100,)), PulseSequence(1, (300,))),
            gap_us=850,
        )
        self.assertEqual(ending_high.durations_us, (100, 850, 300))

    def test_combined_pulse_limits_are_enforced(self) -> None:
        too_many = PulseSequence(1, (10,) * (MAX_PULSE_DURATIONS + 1))
        with self.assertRaisesRegex(ValueError, "2048"):
            combine_pulse_sequences((too_many,), gap_us=850)

        too_long = PulseSequence(1, (MAX_PULSE_SEQUENCE_US,))
        with self.assertRaisesRegex(ValueError, "1000 ms"):
            combine_pulse_sequences((too_long, PulseSequence(1, (10,))), gap_us=850)

    def test_ook_encoding_merges_prefix_and_uses_250us(self) -> None:
        pulse = encode_air_pulses(build_partition_frame(0xFF, 0xFF, 0xFF, 1, 0))
        self.assertEqual(pulse.start_level, 1)
        self.assertEqual(len(pulse.durations_us), 116)
        self.assertEqual(pulse.total_us, 46_250)
        self.assertEqual(pulse.durations_us[:4], (2000, 1000, 1000, 500))
        self.assertEqual(set(pulse.durations_us), {250, 500, 1000, 2000})

    def test_bridge_argument_shapes(self) -> None:
        pulse = encode_air_pulses(build_a6_frame())
        self.assertEqual(
            tx_pulses_args(pulse),
            {
                "durations_us": list(pulse.durations_us),
                "start_level": 1,
                "frequency_hz": 433_920_000,
                "power_dbm": -20,
                "repeat": 1,
                "gap_us": 20_000,
            },
        )
        self.assertEqual(
            tx_packet_args(bytes.fromhex("00 FF 01")),
            {"data_hex": "00 FF 01", "frequency_hz": 433_920_000, "power_dbm": -20, "repeat": 1, "gap_us": 20_000},
        )

    def test_validation_and_hex_parser(self) -> None:
        self.assertEqual(parse_hex("00:ff 01"), b"\x00\xff\x01")
        with self.assertRaises(ValueError):
            build_c0_frame(range(9))
        with self.assertRaises(ValueError):
            build_d8_frame(0, 0, 0, function=4)
        with self.assertRaises(ValueError):
            tx_packet_args(b"")

    def test_repeat_bound_matches_firmware_contract(self) -> None:
        self.assertEqual(tx_packet_args(b"\x00", repeat=10)["repeat"], 10)
        with self.assertRaisesRegex(ValueError, "1..10"):
            tx_packet_args(b"\x00", repeat=11)


if __name__ == "__main__":
    unittest.main()
