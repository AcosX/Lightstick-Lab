# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lightstick-control contributors
import base64
import binascii
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lightstick_demo.recording import ClientRecording, ClientRecordingError
from lightstick_demo.transports import BaseTransport, TransportStatus


def valid_header() -> bytes:
    return struct.pack(
        "<4sHHHHIII BB6sQ",
        b"LSR1",
        1,
        40,
        16,
        0,
        433_920_000,
        203_000,
        4_800,
        2,
        0,
        bytes(6),
        0,
    )


class RecordingTransport(BaseTransport):
    kind = "test"

    def __init__(self) -> None:
        self.requests = []
        self.block = b"0123456789ABCDEF"

    def connect(self) -> TransportStatus:
        return TransportStatus(self.kind, True, "test")

    def disconnect(self) -> TransportStatus:
        return TransportStatus(self.kind, False, "closed")

    def request(self, command, args=None, timeout=8.0):
        self.requests.append((command, args, timeout))
        if command == "START_CLIENT_RECORDING":
            return {"session_id": "client-test", "header_base64": base64.b64encode(valid_header()).decode("ascii")}
        if command == "STOP_CLIENT_RECORDING":
            return {"status": "stopped"}
        if command == "READ_CLIENT_RECORDING" and args["ack_sequence"] == 0:
            return {
                "active": False,
                "end_of_stream": False,
                "sequence_start": 0,
                "sequence_end": 1,
                "offset_bytes": 40,
                "next_offset_bytes": 56,
                "records_base64": base64.b64encode(self.block).decode("ascii"),
                "data_crc32": binascii.crc32(self.block) & 0xFFFFFFFF,
            }
        if command == "READ_CLIENT_RECORDING":
            return {
                "active": False,
                "end_of_stream": True,
                "sequence_start": 1,
                "sequence_end": 1,
                "offset_bytes": 56,
                "next_offset_bytes": 56,
                "records_base64": "",
            }
        raise AssertionError(command)


class RecordingTests(unittest.TestCase):
    def test_durable_ack_sequence_and_offsets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.lsr"
            path.write_bytes(b"old destination")
            transport = RecordingTransport()
            recording = ClientRecording(transport, path, "capture")
            result = recording.start()
            self.assertEqual(result["session_id"], "client-test")
            result = recording.drain()
            self.assertEqual(result["records"], 1)
            self.assertEqual(result["bytes"], 56)
            self.assertEqual(path.stat().st_size, 56)
            self.assertNotEqual(path.read_bytes(), b"old destination")
            self.assertEqual(path.read_bytes()[:40], valid_header())
            reads = [args for command, args, _ in transport.requests if command == "READ_CLIENT_RECORDING"]
            self.assertEqual([args["ack_sequence"] for args in reads], [0, 1])
            self.assertEqual(reads[0]["max_records"], 512)

    def test_header_must_be_full_lsr1_header(self) -> None:
        class BadHeaderTransport(RecordingTransport):
            def request(self, command, args=None, timeout=8.0):
                if command == "START_CLIENT_RECORDING":
                    return {"session_id": "bad", "header_base64": base64.b64encode(b"LSR1").decode("ascii")}
                return super().request(command, args, timeout)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.lsr"
            path.write_bytes(b"keep me")
            with self.assertRaisesRegex(RuntimeError, "40 bytes"):
                ClientRecording(BadHeaderTransport(), path, "bad").start()
            self.assertEqual(path.read_bytes(), b"keep me")

    def test_header_fields_are_validated_before_destination(self) -> None:
        class WrongFieldsTransport(RecordingTransport):
            def request(self, command, args=None, timeout=8.0):
                if command == "START_CLIENT_RECORDING":
                    bad = bytearray(valid_header())
                    bad[4:6] = (2).to_bytes(2, "little")
                    return {"session_id": "bad-fields", "header_base64": base64.b64encode(bad).decode("ascii")}
                return super().request(command, args, timeout)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fields.lsr"
            path.write_bytes(b"keep me")
            with self.assertRaisesRegex(RuntimeError, "version"):
                ClientRecording(WrongFieldsTransport(), path, "fields").start()
            self.assertEqual(path.read_bytes(), b"keep me")

    def test_local_setup_failure_stops_started_session_and_keeps_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.lsr"
            path.write_bytes(b"keep me")
            transport = RecordingTransport()
            recording = ClientRecording(transport, path, "capture")
            with patch("lightstick_demo.recording.os.fsync", side_effect=OSError("fsync failed")):
                with self.assertRaisesRegex(OSError, "fsync failed"):
                    recording.start()
            self.assertEqual(path.read_bytes(), b"keep me")
            self.assertIsNone(recording._temp_path)
            stop_requests = [
                args for command, args, _ in transport.requests if command == "STOP_CLIENT_RECORDING"
            ]
            self.assertEqual(stop_requests, [{"session_id": "client-test"}])
            read_requests = [
                args for command, args, _ in transport.requests if command == "READ_CLIENT_RECORDING"
            ]
            self.assertEqual([args["ack_sequence"] for args in read_requests], [0, 1])
            self.assertIsNone(recording.session_id)

    def test_failed_start_cleanup_discards_data_before_clearing_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.lsr"
            transport = RecordingTransport()
            recording = ClientRecording(transport, path, "capture")
            with patch("lightstick_demo.recording.os.fsync", side_effect=OSError("fsync failed")):
                with self.assertRaisesRegex(OSError, "fsync failed"):
                    recording.start()
            commands = [command for command, _, _ in transport.requests]
            self.assertEqual(
                commands,
                ["START_CLIENT_RECORDING", "STOP_CLIENT_RECORDING", "READ_CLIENT_RECORDING", "READ_CLIENT_RECORDING"],
            )
            self.assertIsNone(recording.session_id)

    def test_failed_start_cleanup_clears_session_when_stop_response_is_lost(self) -> None:
        class StopResponseLostTransport(RecordingTransport):
            def request(self, command, args=None, timeout=8.0):
                if command == "STOP_CLIENT_RECORDING":
                    self.requests.append((command, args, timeout))
                    raise RuntimeError("response lost")
                return super().request(command, args, timeout)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.lsr"
            transport = StopResponseLostTransport()
            recording = ClientRecording(transport, path, "capture")
            with patch("lightstick_demo.recording.os.fsync", side_effect=OSError("fsync failed")):
                with self.assertRaisesRegex(OSError, "fsync failed"):
                    recording.start()
            commands = [command for command, _, _ in transport.requests]
            self.assertEqual(
                commands,
                ["START_CLIENT_RECORDING", "STOP_CLIENT_RECORDING", "READ_CLIENT_RECORDING", "READ_CLIENT_RECORDING"],
            )
            self.assertIsNone(recording.session_id)

    def test_failed_start_cleanup_failure_preserves_session_for_recovery(self) -> None:
        class UnreadableTransport(RecordingTransport):
            def request(self, command, args=None, timeout=8.0):
                if command == "START_CLIENT_RECORDING":
                    return {"session_id": "client-recover", "header_base64": base64.b64encode(b"LSR1").decode("ascii")}
                if command == "READ_CLIENT_RECORDING":
                    raise RuntimeError("read unavailable")
                return super().request(command, args, timeout)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.lsr"
            path.write_bytes(b"keep me")
            recording = ClientRecording(UnreadableTransport(), path, "capture")
            with self.assertRaisesRegex(RuntimeError, "40 bytes") as raised:
                recording.start()
            self.assertEqual(recording.session_id, "client-recover")
            self.assertEqual(path.read_bytes(), b"keep me")
            self.assertTrue(any("清理 client recording 数据失败" in note for note in raised.exception.__notes__))

    def test_preserved_session_can_be_discarded_after_transport_recovers(self) -> None:
        class RecoverableTransport(RecordingTransport):
            def __init__(self) -> None:
                super().__init__()
                self.read_available = False

            def request(self, command, args=None, timeout=8.0):
                if command == "READ_CLIENT_RECORDING" and not self.read_available:
                    self.requests.append((command, args, timeout))
                    raise RuntimeError("read unavailable")
                return super().request(command, args, timeout)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.lsr"
            transport = RecoverableTransport()
            recording = ClientRecording(transport, path, "capture")
            with patch("lightstick_demo.recording.os.fsync", side_effect=OSError("fsync failed")):
                with self.assertRaisesRegex(OSError, "fsync failed"):
                    recording.start()

            self.assertEqual(recording.session_id, "client-test")
            self.assertIsNone(recording._temp_path)
            with self.assertRaisesRegex(ClientRecordingError, "丢弃失败"):
                recording.stop()
            self.assertEqual(recording.session_id, "client-test")
            self.assertIsNone(recording._temp_path)

            transport.read_available = True
            result = recording.stop()
            self.assertTrue(result["discarded"])
            self.assertEqual(result["last"]["end_of_stream"], True)
            self.assertIsNone(recording.session_id)
            self.assertIsNone(recording._temp_path)
            read_requests = [
                args for command, args, _ in transport.requests if command == "READ_CLIENT_RECORDING"
            ]
            self.assertEqual([args["ack_sequence"] for args in read_requests[-2:]], [0, 1])

            recording.start()
            self.assertEqual(recording.session_id, "client-test")
            self.assertIsNotNone(recording._temp_path)
            recording._cleanup_temp()


if __name__ == "__main__":
    unittest.main()
