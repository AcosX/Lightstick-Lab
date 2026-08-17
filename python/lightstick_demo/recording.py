# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lightstick-control contributors
"""Acknowledged client recording reader for the ESP32 ``LSR1`` API."""

from __future__ import annotations

import base64
import binascii
import json
import os
import struct
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from .transports import BaseTransport

LSR1_HEADER_BYTES = 40
LSR1_RECORD_BYTES = 16
FAILED_START_CLEANUP_TIMEOUT_SECONDS = 30.0


def _first(result: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in result:
            return result[key]
    return default


class ClientRecordingError(RuntimeError):
    pass


class ClientRecording:
    def __init__(self, transport: BaseTransport, output_path: Path, name: str, max_records: int = 512) -> None:
        self.transport = transport
        self.output_path = output_path
        self.name = name
        self.max_records = max(1, min(512, max_records))
        self.session_id: str | None = None
        self.ack_sequence = 0
        self.bytes_written = 0
        self.last_status: dict[str, Any] = {}
        self._temp_path: Path | None = None

    def start(self, args: dict[str, Any] | None = None) -> dict[str, Any]:
        result = self.transport.request(
            "START_CLIENT_RECORDING",
            {"name": self.name, "utc_epoch_ms": int(time.time() * 1000), **(args or {})},
            timeout=12,
        )
        if not isinstance(result, dict):
            raise ClientRecordingError("START_CLIENT_RECORDING 返回格式无效")
        session_id = _first(result, "session_id", "sessionId")
        if not session_id:
            raise ClientRecordingError("响应没有 session_id")
        self.session_id = str(session_id)
        self.ack_sequence = 0
        self.bytes_written = 0
        self.last_status = {}
        try:
            header = _first(result, "header_base64", "headerBase64")
            if header:
                try:
                    header_bytes = base64.b64decode(header, validate=True)
                except (ValueError, binascii.Error) as exc:
                    raise ClientRecordingError(f"LSR1 header 解码失败: {exc}") from exc
                self._validate_header(header_bytes)
            else:
                raise ClientRecordingError("响应没有 LSR1 header")
            self._temp_path = None
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{self.output_path.name}.",
                suffix=".part",
                dir=self.output_path.parent,
                delete=False,
            )
            self._temp_path = Path(temporary.name)
            try:
                temporary.write(header_bytes)
                temporary.flush()
                os.fsync(temporary.fileno())
            finally:
                temporary.close()
            self.bytes_written = len(header_bytes)
            return result
        except Exception as exc:
            self._cleanup_temp()
            stop_error = self._stop_after_failed_start()
            if stop_error is None:
                self.session_id = None
            elif hasattr(exc, "add_note"):
                exc.add_note(f"client recording 清理失败: {stop_error}")
            raise

    def _stop_after_failed_start(self) -> Exception | None:
        if not self.session_id:
            return None
        try:
            self.discard()
        except Exception as exc:  # Preserve the session for caller recovery.
            return ClientRecordingError(f"清理 client recording 数据失败: {exc}")
        return None

    def _cleanup_request(self, command: str, args: dict[str, Any], deadline: float) -> Any:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ClientRecordingError("client recording 清理超时")
        return self.transport.request(command, args, timeout=min(12.0, max(0.001, remaining)))

    def _discard_after_failed_start(self, session_id: str, deadline: float) -> None:
        while True:
            result = self._cleanup_request(
                "READ_CLIENT_RECORDING",
                {
                    "session_id": session_id,
                    "ack_sequence": self.ack_sequence,
                    "max_records": self.max_records,
                },
                deadline,
            )
            if not isinstance(result, dict):
                raise ClientRecordingError("READ_CLIENT_RECORDING 返回格式无效")
            self.last_status = result
            data, sequence_end = self._validate_discard_block(result, session_id)
            self.ack_sequence = sequence_end
            active = bool(result.get("active", False))
            end_of_stream = bool(_first(result, "end_of_stream", "endOfStream", default=False))
            # A data-bearing response is pending until the next request carries
            # its sequence_end acknowledgement, even if a simulator sets EOS.
            if not data and not active and end_of_stream:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ClientRecordingError("client recording 清理超时")
            if not data:
                time.sleep(min(0.05, remaining))

    def _validate_discard_block(self, result: dict[str, Any], session_id: str) -> tuple[bytes, int]:
        response_session = _first(result, "session_id", "sessionId")
        if response_session is not None and str(response_session) != session_id:
            raise ClientRecordingError("READ_CLIENT_RECORDING session_id 不匹配")
        data = self._decode_block(result)
        record_count_raw = _first(result, "record_count", "recordCount")
        if record_count_raw is None:
            record_count = len(data) // LSR1_RECORD_BYTES
        else:
            try:
                record_count = int(record_count_raw)
            except (TypeError, ValueError) as exc:
                raise ClientRecordingError("record_count 无效") from exc
        if record_count < 0 or len(data) != record_count * LSR1_RECORD_BYTES:
            raise ClientRecordingError("record block record_count 与数据长度不匹配")

        try:
            sequence_start = int(_first(result, "sequence_start", "sequenceStart", default=self.ack_sequence))
            sequence_end = int(_first(result, "sequence_end", "sequenceEnd", default=self.ack_sequence))
        except (TypeError, ValueError) as exc:
            raise ClientRecordingError("record block sequence 无效") from exc
        if sequence_start != self.ack_sequence:
            raise ClientRecordingError(
                f"record block sequence_start 不连续: {sequence_start} != {self.ack_sequence}"
            )
        if sequence_end != sequence_start + record_count:
            raise ClientRecordingError("record block sequence_end 与 record_count 不匹配")

        expected_offset = LSR1_HEADER_BYTES + self.ack_sequence * LSR1_RECORD_BYTES
        offset = _first(result, "offset_bytes", "offsetBytes")
        if offset is not None and int(offset) != expected_offset:
            raise ClientRecordingError(f"record offset 不连续: {offset} != {expected_offset}")
        next_offset = _first(result, "next_offset_bytes", "nextOffsetBytes")
        expected_next_offset = LSR1_HEADER_BYTES + sequence_end * LSR1_RECORD_BYTES
        if next_offset is not None and int(next_offset) != expected_next_offset:
            raise ClientRecordingError(
                f"record next_offset_bytes 不匹配: {next_offset} != {expected_next_offset}"
            )
        return data, sequence_end

    def _cleanup_temp(self) -> None:
        if self._temp_path is None:
            return
        try:
            self._temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        finally:
            self._temp_path = None

    def _has_usable_temp(self) -> bool:
        return self._temp_path is not None and self._temp_path.is_file()

    def discard(self) -> dict[str, Any]:
        """Stop and acknowledge a preserved session whose local sink is unusable."""
        if not self.session_id:
            raise ClientRecordingError("没有可丢弃的 recording session")
        if self._has_usable_temp():
            raise ClientRecordingError("当前 recording 仍有可用的本地临时文件")

        session_id = self.session_id
        deadline = time.monotonic() + FAILED_START_CLEANUP_TIMEOUT_SECONDS
        stop_error: Exception | None = None
        try:
            self._cleanup_request(
                "STOP_CLIENT_RECORDING",
                {"session_id": session_id},
                deadline,
            )
        except Exception as exc:  # Best effort: still try to drain unread records.
            stop_error = exc

        try:
            self._discard_after_failed_start(session_id, deadline)
        except Exception as exc:
            detail = f"client recording 丢弃失败: {exc}"
            if stop_error is not None:
                detail = f"STOP_CLIENT_RECORDING 失败: {stop_error}; {detail}"
            raise ClientRecordingError(detail) from exc

        discarded_records = self.ack_sequence
        last_status = self.last_status
        self._cleanup_temp()
        self.session_id = None
        self.ack_sequence = 0
        self.bytes_written = 0
        return {
            "discarded": True,
            "session_id": session_id,
            "records": discarded_records,
            "bytes": 0,
            "last": last_status,
        }

    def stop(self) -> dict[str, Any]:
        if not self.session_id:
            raise ClientRecordingError("没有活动的 recording session")
        if not self._has_usable_temp():
            return self.discard()
        return self.transport.request("STOP_CLIENT_RECORDING", {"session_id": self.session_id}, timeout=12)

    def drain(self, report: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
        if not self.session_id:
            raise ClientRecordingError("没有活动的 recording session")
        total_records = 0
        while True:
            result = self.transport.request(
                "READ_CLIENT_RECORDING",
                {"session_id": self.session_id, "ack_sequence": self.ack_sequence, "max_records": self.max_records},
                timeout=15,
            )
            if not isinstance(result, dict):
                raise ClientRecordingError("READ_CLIENT_RECORDING 返回格式无效")
            self.last_status = result
            data = self._decode_block(result)
            sequence_end = int(_first(result, "sequence_end", "sequenceEnd", default=self.ack_sequence) or self.ack_sequence)
            self._verify_offsets(result, len(data))
            if data:
                if sequence_end <= self.ack_sequence:
                    raise ClientRecordingError("record block sequence_end 没有前进")
                self._append_durable(data)
                total_records += len(data) // LSR1_RECORD_BYTES
                self.ack_sequence = sequence_end
            elif sequence_end > self.ack_sequence:
                # An empty block must not advance an acknowledgement without bytes.
                raise ClientRecordingError("设备返回了无数据但 sequence_end 前进的块")
            if report:
                report({"records": total_records, "bytes": self.bytes_written, "active": bool(result.get("active")), "eos": bool(_first(result, "end_of_stream", "endOfStream", default=False))})
            active = bool(result.get("active", False))
            end_of_stream = bool(_first(result, "end_of_stream", "endOfStream", default=False))
            if not active and end_of_stream:
                self._finalize()
                self.session_id = None
                return {"records": total_records, "bytes": self.bytes_written, "path": str(self.output_path), "last": result}
            if not data:
                time.sleep(0.05)

    def _decode_block(self, result: dict[str, Any]) -> bytes:
        encoded = _first(result, "records_base64", "data_base64", "recordsBase64", "dataBase64", default="")
        if not encoded:
            return b""
        try:
            data = base64.b64decode(str(encoded), validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ClientRecordingError(f"record block 解码失败: {exc}") from exc
        expected_crc = _first(result, "data_crc32", "dataCrc32")
        if expected_crc is not None:
            try:
                expected = int(str(expected_crc), 16) if isinstance(expected_crc, str) and expected_crc.lower().startswith("0x") else int(expected_crc)
            except (TypeError, ValueError) as exc:
                raise ClientRecordingError("record block CRC32 无效") from exc
            actual = binascii.crc32(data) & 0xFFFFFFFF
            if actual != expected:
                raise ClientRecordingError(f"record block CRC32 不匹配: {actual:08X} != {expected:08X}")
        if len(data) % LSR1_RECORD_BYTES:
            raise ClientRecordingError("record block 不是 16-byte LSR1 record 的整数倍")
        return data

    def _verify_offsets(self, result: dict[str, Any], data_length: int) -> None:
        offset = _first(result, "offset_bytes", "offsetBytes")
        next_offset = _first(result, "next_offset_bytes", "nextOffsetBytes")
        if offset is not None and int(offset) != self.bytes_written:
            raise ClientRecordingError(f"record offset 不连续: {offset} != {self.bytes_written}")
        if next_offset is not None and int(next_offset) != self.bytes_written + data_length:
            raise ClientRecordingError("record next_offset_bytes 不匹配")

    def _append_durable(self, data: bytes) -> None:
        if self._temp_path is None:
            raise ClientRecordingError("recording 临时文件尚未建立")
        with self._temp_path.open("ab") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        self.bytes_written += len(data)

    @staticmethod
    def _validate_header(header: bytes) -> None:
        if len(header) != LSR1_HEADER_BYTES:
            raise ClientRecordingError(f"LSR1 header 必须是 {LSR1_HEADER_BYTES} bytes，实际 {len(header)}")
        magic, version, header_size, record_size, reserved16, frequency, bandwidth, rate_millikbaud, modulation, start_level, reserved, _utc_epoch_ms = struct.unpack(
            "<4sHHHHIII BB6sQ", header
        )
        if magic != b"LSR1":
            raise ClientRecordingError("LSR1 header magic 无效")
        if version != 1:
            raise ClientRecordingError(f"LSR1 header version 必须为 1，实际 {version}")
        if header_size != LSR1_HEADER_BYTES:
            raise ClientRecordingError(f"LSR1 header_size 必须为 {LSR1_HEADER_BYTES}，实际 {header_size}")
        if record_size != LSR1_RECORD_BYTES:
            raise ClientRecordingError(f"LSR1 record_size 必须为 {LSR1_RECORD_BYTES}，实际 {record_size}")
        if reserved16 != 0 or reserved != bytes(6):
            raise ClientRecordingError("LSR1 header 保留字段必须为零")
        if not (
            300_000_000 <= frequency <= 348_000_000
            or 378_000_000 <= frequency <= 464_000_000
            or 779_000_000 <= frequency <= 928_000_000
        ):
            raise ClientRecordingError("LSR1 frequency_hz 不在 CC1101 支持频段")
        if not 58_000 <= bandwidth <= 812_500:
            raise ClientRecordingError("LSR1 rx_bandwidth_hz 超出范围")
        if not 600 <= rate_millikbaud <= 500_000:
            raise ClientRecordingError("LSR1 data_rate_kbaud 超出范围")
        if modulation > 4:
            raise ClientRecordingError("LSR1 modulation 超出范围")
        if start_level > 1:
            raise ClientRecordingError("LSR1 start_level 必须为 0 或 1")

    def _finalize(self) -> None:
        if self._temp_path is None:
            raise ClientRecordingError("recording 临时文件尚未建立")
        if self.bytes_written < LSR1_HEADER_BYTES or (self.bytes_written - LSR1_HEADER_BYTES) % LSR1_RECORD_BYTES:
            raise ClientRecordingError("LSR1 文件长度不符合 header + record 对齐")
        with self._temp_path.open("r+b") as handle:
            os.fsync(handle.fileno())
        os.replace(self._temp_path, self.output_path)
        self._temp_path = None
        try:
            directory_fd = os.open(self.output_path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
