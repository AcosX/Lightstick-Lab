# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lightstick-control contributors
import unittest

from lightstick_demo.ui import (
    TX_POLL_MIN_TIMEOUT_SECONDS,
    d8_rgb_from_hex,
    d8_slots_for_zones,
    d8_state_after_tx,
    d8_tx_completion_text,
    d8_tx_commands,
    d8_update_slots,
    network_status_is_connected,
    palette_hex,
    profile_tx_command,
    recording_has_valid_data,
    recording_warning_text,
    recover_recording_session,
    select_preferred_ble_device,
    select_preferred_serial_port,
    tx_poll_timeout_seconds,
    wait_for_network_connection,
    wait_for_tx_result,
)
from lightstick_demo.protocol import (
    DEFAULT_D8_PHASES,
    MAX_PULSE_DURATIONS,
    MAX_PULSE_SEQUENCE_US,
    build_d8_frames_from_slots,
    build_d8_transaction,
    d8_word,
)
from lightstick_demo.transports import (
    BaseTransport,
    TransportError,
    TransportStatus,
    TransportTimeoutError,
)


class SequenceTransport(BaseTransport):
    kind = "test"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def connect(self):
        return TransportStatus(self.kind, True, "test")

    def disconnect(self):
        return TransportStatus(self.kind, False, "test")

    def request(self, command, args=None, timeout=8.0):
        self.calls.append((command, args, timeout))
        response = self.responses.pop(0) if self.responses else {"wifi": {"connected": False, "ip": "0.0.0.0"}}
        if isinstance(response, Exception):
            raise response
        return response


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def sleep(self, seconds):
        self.now += seconds


class TxDeadlineTests(unittest.TestCase):
    def test_deadline_covers_maximum_firmware_pulse_request(self) -> None:
        timeout = tx_poll_timeout_seconds(
            {"durations_us": [1_000_000], "repeat": 10, "gap_us": 1_000_000}
        )
        self.assertGreaterEqual(timeout, TX_POLL_MIN_TIMEOUT_SECONDS)
        self.assertGreaterEqual(timeout, 19.0 + 12.0)

    def test_deadline_has_minimum_for_short_request(self) -> None:
        self.assertEqual(
            tx_poll_timeout_seconds({"durations_us": [250], "repeat": 1, "gap_us": 20_000}),
            TX_POLL_MIN_TIMEOUT_SECONDS,
        )


class NetworkPollingTests(unittest.TestCase):
    def test_connecting_then_connected(self):
        transport = SequenceTransport([
            {"wifi": {"connected": False, "ip": "0.0.0.0"}},
            {"wifi": {"connected": True, "ip": "192.0.2.164"}},
        ])
        clock = FakeClock()
        result = wait_for_network_connection(transport, timeout=5, poll_interval=1, clock=lambda: clock.now, sleep=clock.sleep)
        self.assertTrue(network_status_is_connected(result))
        self.assertEqual(len(transport.calls), 2)

    def test_timeout_when_ip_never_becomes_valid(self):
        transport = SequenceTransport([])
        clock = FakeClock()
        with self.assertRaisesRegex(TransportError, "超时"):
            wait_for_network_connection(transport, timeout=2, poll_interval=1, clock=lambda: clock.now, sleep=clock.sleep)
        self.assertGreaterEqual(len(transport.calls), 2)


class TxResultPollingTests(unittest.TestCase):
    def test_tx_poll_retries_a_single_transport_timeout(self) -> None:
        transport = SequenceTransport([
            TransportTimeoutError("serial frame dropped"),
            {"status": "success", "sequences_sent": 1},
        ])
        result = wait_for_tx_result(transport, "req-1", timeout=30)
        self.assertEqual(result["status"], "success")
        self.assertEqual([call[0] for call in transport.calls], ["GET_TX_RESULT", "GET_TX_RESULT"])

    def test_tx_poll_waits_for_pending_then_success(self) -> None:
        clock = FakeClock()
        transport = SequenceTransport([
            {"status": "pending"},
            {"status": "pending"},
            {"status": "success", "sequences_sent": 1},
        ])
        result = wait_for_tx_result(
            transport, "req-1", timeout=30, clock=lambda: clock.now, sleep=clock.sleep
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(len(transport.calls), 3)

    def test_tx_poll_does_not_retry_a_device_error(self) -> None:
        transport = SequenceTransport([
            TransportError("TX result not found; request_id may be expired"),
        ])
        with self.assertRaisesRegex(TransportError, "TX result not found"):
            wait_for_tx_result(transport, "req-1", timeout=30)
        self.assertEqual(len(transport.calls), 1)

    def test_tx_poll_aborts_after_total_deadline_of_timeouts(self) -> None:
        class AlwaysTimeoutTransport(SequenceTransport):
            def request(self, command, args=None, timeout=8.0):
                self.calls.append((command, args, timeout))
                raise TransportTimeoutError("timeout")

        transport = AlwaysTimeoutTransport([])
        clock = FakeClock()
        with self.assertRaisesRegex(TransportError, "等待超时"):
            wait_for_tx_result(
                transport, "req-1", timeout=2, clock=lambda: clock.now, sleep=clock.sleep
            )
        self.assertGreaterEqual(len(transport.calls), 2)

    def test_ble_scan_prefers_firmware_device_name(self):
        device = select_preferred_ble_device([
            {"name": "other", "address": "A"},
            {"name": "Lightstick N16R8", "address": "B"},
        ])
        self.assertEqual(device["address"], "B")

    def test_ble_scan_does_not_fall_back_to_unnamed_or_unrelated_device(self):
        self.assertIsNone(select_preferred_ble_device([
            {"name": None, "address": "A"},
            {"name": "Other", "address": "B"},
        ]))


class CompactUiLogicTests(unittest.TestCase):
    def test_d8_hex_maps_to_canonical_nibbles(self):
        self.assertEqual(d8_rgb_from_hex("#FF0000"), (15, 0, 0))
        self.assertEqual(d8_rgb_from_hex("66CCFF"), (6, 12, 15))
        with self.assertRaisesRegex(ValueError, "#RRGGBB"):
            d8_rgb_from_hex("#12FG00")

    def test_d8_palette_mapping_has_the_known_light_blue(self):
        self.assertEqual(palette_hex(0), "#FF0000")
        self.assertEqual(palette_hex(6), "#66CCFF")
        self.assertEqual(d8_rgb_from_hex(palette_hex(6)), (6, 12, 15))
        with self.assertRaisesRegex(ValueError, "0..15"):
            palette_hex(16)

    def test_d8_selected_zones_fill_position_based_slots(self):
        word = d8_word(6, 12, 15, 0)
        self.assertEqual(
            d8_slots_for_zones(("A", "B"), word),
            (word, word, 0, 0, 0, 0, 0, 0, 0),
        )
        self.assertEqual(
            d8_slots_for_zones(("I",), word),
            (0, 0, 0, 0, 0, 0, 0, 0, word),
        )
        with self.assertRaisesRegex(ValueError, "A-I"):
            d8_slots_for_zones(("J",), word)
        with self.assertRaisesRegex(ValueError, "至少选择"):
            d8_slots_for_zones((), word)

    def test_d8_update_preserves_unselected_slots_and_supports_off(self):
        red = d8_word(15, 0, 0, 0)
        blue = d8_word(0, 0, 15, 0)
        previous = (red, 0, 0, 0, 0, 0, 0, 0, 0)
        candidate = d8_update_slots(previous, ("B",), blue)
        self.assertEqual(candidate, (red, blue, 0, 0, 0, 0, 0, 0, 0))
        self.assertEqual(
            d8_update_slots(candidate, ("B",), 0),
            (red, 0, 0, 0, 0, 0, 0, 0, 0),
        )

    def test_d8_state_commits_only_after_success(self):
        red = d8_word(15, 0, 0, 0)
        candidate = (0, red, 0, 0, 0, 0, 0, 0, 0)
        previous = (red, 0, 0, 0, 0, 0, 0, 0, 0)
        self.assertEqual(d8_state_after_tx(previous, candidate, False), previous)
        self.assertEqual(d8_state_after_tx(previous, candidate, True), candidate)

    def test_d8_completion_does_not_claim_a_target_ack(self):
        self.assertEqual(d8_tx_completion_text(True), "TX 完成：6 帧 / 1 事务（棒端无 ACK）")
        self.assertIn("与本地记录状态相同", d8_tx_completion_text(False))
        self.assertIn("棒端无 ACK", d8_tx_completion_text(False))

    def test_d8_ui_transaction_is_one_request_with_six_phase_frames(self):
        word = d8_word(6, 12, 15, 0)
        slots = d8_slots_for_zones(("A", "B"), word)
        frames = build_d8_frames_from_slots(slots, DEFAULT_D8_PHASES)
        self.assertEqual(len(frames), 6)
        self.assertEqual(tuple(frame[-2] for frame in frames), DEFAULT_D8_PHASES)
        self.assertEqual(tuple(frame[1:3] for frame in frames), (frames[0][1:3],) * 6)
        self.assertEqual(tuple(frame[3:5] for frame in frames), (frames[0][3:5],) * 6)
        transaction = build_d8_transaction(slots)
        commands = d8_tx_commands(transaction, 433920000, -20, 20000)
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0][0], "TX_PULSES")
        args = commands[0][1]
        self.assertEqual(args["repeat"], 1)
        self.assertIn(len(args["durations_us"]), (2039, 2040))
        self.assertLessEqual(len(args["durations_us"]), MAX_PULSE_DURATIONS)
        self.assertLessEqual(transaction.total_us, MAX_PULSE_SEQUENCE_US)

    def test_profile_auto_selects_packet_or_pulse_command(self):
        self.assertEqual(
            profile_tx_command({"name": "packet", "data_hex": "00 FF"}),
            ("TX_PACKET", {"profile": "packet"}),
        )
        self.assertEqual(
            profile_tx_command({"name": "pulse", "durations_us": [250, 500]}),
            ("TX_PULSES", {"profile": "pulse"}),
        )
        with self.assertRaisesRegex(ValueError, "data_hex"):
            profile_tx_command({"name": "empty"})

    def test_record_warning_clears_only_after_32_records(self):
        self.assertFalse(recording_has_valid_data({"records": 31}))
        self.assertTrue(recording_has_valid_data({"records": 32}))
        self.assertEqual(recording_warning_text(False), "警告：截止目前未接收到有效数据。")
        self.assertEqual(recording_warning_text(True), "")

    def test_serial_scan_skips_bluetooth_for_usb_serial(self):
        selected = select_preferred_serial_port([
            {"device": "/dev/cu.Bluetooth-Incoming-Port"},
            {"device": "/dev/cu.usbserial-1420"},
        ])
        self.assertEqual(selected["device"], "/dev/cu.usbserial-1420")

    def test_serial_scan_returns_none_when_only_bluetooth_is_present(self):
        selected = select_preferred_serial_port([
            {
                "device": "/dev/cu.Bluetooth-Incoming-Port",
                "description": "Bluetooth-Incoming-Port",
            },
        ])
        self.assertIsNone(selected)

    def test_serial_scan_falls_back_to_non_bluetooth_nonstandard_device(self):
        selected = select_preferred_serial_port([
            {
                "device": "/dev/cu.Bluetooth-Modem",
                "description": "Bluetooth virtual serial port",
            },
            {
                "device": "/dev/cu.lightstick-bridge",
                "description": "FTDI USB Serial",
            },
        ])
        self.assertEqual(selected["device"], "/dev/cu.lightstick-bridge")


class RecordingRecoveryTests(unittest.TestCase):
    def test_recovery_stops_then_drains_preserved_session(self):
        calls = []

        class Recorder:
            def stop(self):
                calls.append("stop")
                return {"status": "stopped"}

            def drain(self, report=None):
                calls.append("drain")
                return {"records": 2, "bytes": 88, "path": "capture.lsr"}

        result = recover_recording_session(Recorder())
        self.assertEqual(calls, ["stop", "drain"])
        self.assertEqual(result["records"], 2)

    def test_discarded_session_is_not_drained_again(self):
        calls = []

        class Recorder:
            def stop(self):
                calls.append("stop")
                return {"discarded": True, "records": 1}

            def drain(self, report=None):
                calls.append("drain")
                raise AssertionError("discarded session should not be drained")

        result = recover_recording_session(Recorder())
        self.assertEqual(calls, ["stop"])
        self.assertTrue(result["discarded"])


if __name__ == "__main__":
    unittest.main()
