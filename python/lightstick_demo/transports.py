# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lightstick-control contributors
"""USB serial, BLE GATT, and Wi-Fi HTTP transports for the ESP32 bridge."""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, ClassVar, Iterable


class TransportError(RuntimeError):
    """A connection or bridge request failed."""


class TransportTimeoutError(TransportError):
    """A request exceeded its per-call transport deadline.

    Idempotent commands such as GET_TX_RESULT may safely retry on this error;
    it is distinct from an error the device actually reported.
    """


@dataclass
class TransportStatus:
    kind: str
    connected: bool = False
    detail: str = "未连接"


# Firmware 0.5.2 exposes one stable GATT service. Keep these values in the
# client so scanning is the only BLE selection the user needs to make.
BLE_DEVICE_NAME = "Lightstick N16R8"
BLE_SERVICE_UUID = "8f7a0001-4c53-4331-9638-53334e313652"
BLE_COMMAND_UUID = "8f7a0002-4c53-4331-9638-53334e313652"
BLE_RESPONSE_UUID = "8f7a0003-4c53-4331-9638-53334e313652"
BLE_NOTIFY_CHUNK_BYTES = 180
SERIAL_TX_COMPLETE_CACHE_SIZE = 64


def _serial_port_text(port: dict[str, Any]) -> str:
    return " ".join(str(value or "") for value in port.values()).lower()


def serial_port_preference_key(port: dict[str, Any]) -> tuple[int, str]:
    """Rank likely ESP32 USB ports consistently on macOS, Windows and Linux."""

    device = str(port.get("device") or "").strip()
    lowered = device.lower()
    metadata = _serial_port_text(port)
    if "bluetooth" in metadata:
        return (100, lowered)
    if lowered.startswith("/dev/cu.usbserial"):
        return (0, lowered)
    if lowered.startswith("/dev/cu.usbmodem"):
        return (1, lowered)
    if lowered.startswith("/dev/ttyacm"):
        return (2, lowered)
    if lowered.startswith("/dev/ttyusb"):
        return (3, lowered)
    if lowered.startswith("com") and (port.get("vid") or "usb" in metadata):
        return (4, lowered)
    if lowered.startswith("com"):
        return (5, lowered)
    return (10, lowered)


def sorted_serial_ports(ports: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return usable serial ports in automatic-connection preference order."""

    usable = [
        port
        for port in ports
        if str(port.get("device") or "").strip() and "bluetooth" not in _serial_port_text(port)
    ]
    return sorted(usable, key=serial_port_preference_key)


class BaseTransport:
    kind = "未知"

    def connect(self) -> TransportStatus:
        raise NotImplementedError

    def disconnect(self) -> TransportStatus:
        raise NotImplementedError

    def request(self, command: str, args: dict[str, Any] | None = None, timeout: float = 8.0) -> Any:
        raise NotImplementedError


class SerialTransport(BaseTransport):
    kind = "USB Serial"

    def __init__(self, port: str, baudrate: int = 921600) -> None:
        self.port_name = port
        self.baudrate = baudrate
        self._serial: Any = None
        self._lock = threading.Lock()
        self._rx_buffer = bytearray()
        self._tx_complete_cache: dict[str, dict[str, Any]] = {}

    @staticmethod
    def list_ports() -> list[dict[str, Any]]:
        try:
            from serial.tools import list_ports
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise TransportError("缺少 pyserial，请先安装 requirements.txt") from exc
        return [
            {
                "device": port.device,
                "description": port.description or "",
                "hwid": port.hwid or "",
                "vid": port.vid,
                "pid": port.pid,
                "serial_number": port.serial_number or "",
                "manufacturer": port.manufacturer or "",
                "product": port.product or "",
                "interface": port.interface or "",
                "location": port.location or "",
            }
            for port in list_ports.comports()
        ]

    def connect(self) -> TransportStatus:
        try:
            import serial
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise TransportError("缺少 pyserial，请先安装 requirements.txt") from exc
        if not self.port_name:
            raise TransportError("请选择串口路径")
        with self._lock:
            self._serial = serial.Serial(self.port_name, self.baudrate, timeout=0.2, write_timeout=2)
            self._rx_buffer.clear()
            self._tx_complete_cache.clear()
        return TransportStatus(self.kind, True, f"{self.port_name} @ {self.baudrate}")

    def disconnect(self) -> TransportStatus:
        with self._lock:
            if self._serial is not None:
                self._serial.close()
            self._serial = None
            self._rx_buffer.clear()
            self._tx_complete_cache.clear()
        return TransportStatus(self.kind, False, "已断开")

    def _cache_tx_complete(self, message: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
        if message.get("event") != "TX_COMPLETE":
            return None
        request_id = str(message.get("id") or "")
        if not request_id:
            return None
        if message.get("ok"):
            raw_result = message.get("result")
            result = dict(raw_result) if isinstance(raw_result, dict) else {}
            result.setdefault("status", "success")
            result.setdefault("request_id", request_id)
        else:
            result = {
                "status": "error",
                "request_id": request_id,
                "error": str(message.get("error") or "TX failed"),
            }
        # Reinsert an updated entry at the newest position. Dict insertion
        # order gives us a small FIFO cache without another synchronization
        # primitive; callers already hold ``_lock``.
        self._tx_complete_cache.pop(request_id, None)
        self._tx_complete_cache[request_id] = result
        while len(self._tx_complete_cache) > SERIAL_TX_COMPLETE_CACHE_SIZE:
            del self._tx_complete_cache[next(iter(self._tx_complete_cache))]
        return request_id, result

    def _next_buffered_message(self) -> dict[str, Any] | None:
        while b"\n" in self._rx_buffer:
            line, _, remainder = self._rx_buffer.partition(b"\n")
            self._rx_buffer[:] = remainder
            line = line.rstrip(b"\r")
            if not line:
                continue
            try:
                message = json.loads(line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict):
                return message
        return None

    def request(self, command: str, args: dict[str, Any] | None = None, timeout: float = 8.0) -> Any:
        if timeout <= 0:
            raise TransportError("timeout 必须大于 0")
        request_args = args or {}
        request_id = f"mac-{uuid.uuid4().hex[:12]}"
        line = (
            json.dumps({"id": request_id, "cmd": command, "args": request_args}, ensure_ascii=False)
            + "\n"
        ).encode()
        target_tx_id = (
            str(request_args.get("request_id") or "")
            if command.upper() == "GET_TX_RESULT"
            else ""
        )
        with self._lock:
            if self._serial is None or not self._serial.is_open:
                raise TransportError("USB serial 未连接")
            if target_tx_id and target_tx_id in self._tx_complete_cache:
                return dict(self._tx_complete_cache[target_tx_id])
            deadline = time.monotonic() + timeout
            self._serial.write(line)
            self._serial.flush()
            while True:
                message = self._next_buffered_message()
                if message is not None:
                    completed = self._cache_tx_complete(message)
                    if completed is not None:
                        completed_id, completed_result = completed
                        if target_tx_id and completed_id == target_tx_id:
                            return dict(completed_result)
                        # Even when its id matches the synchronous TX_PULSES
                        # request, completion is out-of-band. Keep waiting for
                        # the accepted/pending ACK for that command.
                        continue
                    if message.get("id") != request_id:
                        continue
                    if not message.get("ok"):
                        raise TransportError(str(message.get("error") or "设备命令失败"))
                    return message.get("result")
                if time.monotonic() >= deadline:
                    break
                raw = self._serial.readline()
                if raw:
                    self._rx_buffer.extend(raw)
        raise TransportTimeoutError(f"{command} 超时 ({timeout:.1f}s)")


class HttpTransport(BaseTransport):
    kind = "Wi-Fi HTTP"

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.session: Any = None
        self._lock = threading.Lock()

    def connect(self) -> TransportStatus:
        if not self.base_url.startswith(("http://", "https://")):
            raise TransportError("HTTP 地址应以 http:// 或 https:// 开头")
        try:
            import requests

            self.session = requests.Session()
            response = self.session.get(f"{self.base_url}/health", timeout=5)
            response.raise_for_status()
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise TransportError("缺少 requests，请先安装 requirements.txt") from exc
        except Exception as exc:
            self.session = None
            raise TransportError(f"HTTP 连接失败: {exc}") from exc
        return TransportStatus(self.kind, True, self.base_url)

    def disconnect(self) -> TransportStatus:
        with self._lock:
            if self.session is not None:
                self.session.close()
            self.session = None
        return TransportStatus(self.kind, False, "已断开")

    def request(self, command: str, args: dict[str, Any] | None = None, timeout: float = 8.0) -> Any:
        with self._lock:
            if self.session is None:
                raise TransportError("Wi-Fi HTTP 未连接")
            request_id = f"mac-{uuid.uuid4().hex[:12]}"
            try:
                response = self.session.post(
                    f"{self.base_url}/api/command",
                    json={"id": request_id, "cmd": command, "args": args or {}},
                    timeout=timeout,
                )
                response.raise_for_status()
                body = response.json()
            except Exception as exc:
                try:
                    from requests.exceptions import Timeout as RequestsTimeout
                except ImportError:
                    RequestsTimeout = None
                if RequestsTimeout is not None and isinstance(exc, RequestsTimeout):
                    raise TransportTimeoutError(f"HTTP 请求超时 ({timeout:.1f}s): {exc}") from exc
                raise TransportError(f"HTTP 请求失败: {exc}") from exc
        if not isinstance(body, dict):
            raise TransportError("HTTP 响应必须是 JSON object")
        if body.get("id") != request_id:
            raise TransportError("HTTP 响应 id 与请求不匹配")
        if body.get("ok") is False:
            raise TransportError(str(body.get("error") or "设备命令失败"))
        return body.get("result", body)

    def get_json(self, path: str, timeout: float = 5.0) -> Any:
        with self._lock:
            if self.session is None:
                raise TransportError("Wi-Fi HTTP 未连接")
            try:
                response = self.session.get(f"{self.base_url}{path}", timeout=timeout)
                response.raise_for_status()
                return response.json()
            except Exception as exc:
                raise TransportError(f"HTTP GET 失败: {exc}") from exc


class BleTransport(BaseTransport):
    kind = "BLE GATT"

    _bleak: ClassVar[Any] = None

    def __init__(
        self,
        address: str,
        write_uuid: str = BLE_COMMAND_UUID,
        notify_uuid: str = BLE_RESPONSE_UUID,
        service_uuid: str = BLE_SERVICE_UUID,
    ) -> None:
        self.address = address.strip()
        self.service_uuid = service_uuid.strip()
        self.write_uuid = write_uuid.strip()
        self.notify_uuid = notify_uuid.strip()
        self.connected = False
        self._lock = threading.Lock()

    @classmethod
    def scan(cls, timeout: float = 5.0) -> list[dict[str, str]]:
        try:
            from bleak import BleakScanner
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise TransportError("缺少 bleak，请先安装 requirements.txt") from exc

        async def discover() -> list[dict[str, str]]:
            devices = await BleakScanner.discover(timeout=timeout)
            return [{"name": device.name or "(unnamed)", "address": device.address} for device in devices]

        return asyncio.run(discover())

    def connect(self) -> TransportStatus:
        if not self.address:
            raise TransportError("请输入 BLE 地址")
        if not self.write_uuid:
            raise TransportError("请输入 BLE 写入 characteristic UUID")
        if not self.notify_uuid:
            raise TransportError("请输入 BLE 通知 characteristic UUID")

        async def open_client() -> None:
            from bleak import BleakClient

            async with BleakClient(self.address) as client:
                if not client.is_connected:
                    raise TransportError("BLE GATT 连接未建立")

        try:
            asyncio.run(open_client())
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise TransportError("缺少 bleak，请先安装 requirements.txt") from exc
        except Exception as exc:
            raise TransportError(f"BLE 连接失败: {exc}") from exc
        self.connected = True
        return TransportStatus(self.kind, True, self.address)

    def disconnect(self) -> TransportStatus:
        self.connected = False
        return TransportStatus(self.kind, False, "已断开")

    def request(self, command: str, args: dict[str, Any] | None = None, timeout: float = 8.0) -> Any:
        if not self.connected:
            raise TransportError("BLE GATT 未连接")
        if not self.write_uuid:
            raise TransportError("未配置 BLE 写入 UUID")
        if not self.notify_uuid:
            raise TransportError("未配置 BLE 通知 UUID")
        request_id = f"mac-{uuid.uuid4().hex[:12]}"
        payload = (
            json.dumps(
                {"id": request_id, "cmd": command, "args": args or {}},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")

        async def exchange() -> Any:
            from bleak import BleakClient

            response_buffer = bytearray()
            response_event = asyncio.Event()
            response_message: dict[str, Any] | None = None

            def on_notify(_: int, data: bytearray) -> None:
                nonlocal response_message
                response_buffer.extend(data)
                while b"\n" in response_buffer:
                    line, _, remainder = response_buffer.partition(b"\n")
                    response_buffer[:] = remainder
                    try:
                        message = json.loads(line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if message.get("id") == request_id:
                        response_message = message
                        response_event.set()

            async with BleakClient(self.address) as client:
                await client.start_notify(self.notify_uuid, on_notify)
                try:
                    for offset in range(0, len(payload), BLE_NOTIFY_CHUNK_BYTES):
                        await client.write_gatt_char(
                            self.write_uuid,
                            payload[offset : offset + BLE_NOTIFY_CHUNK_BYTES],
                            response=True,
                        )
                    await asyncio.wait_for(response_event.wait(), timeout=timeout)
                finally:
                    await client.stop_notify(self.notify_uuid)
            if response_message is None:
                raise TransportError("BLE 响应 id 与请求不匹配")
            if not response_message.get("ok"):
                raise TransportError(str(response_message.get("error") or "BLE 命令失败"))
            return response_message.get("result")

        try:
            # Each command opens its own GATT session. Serializing exchanges
            # keeps response chunks from two commands from interleaving.
            with self._lock:
                return asyncio.run(exchange())
        except asyncio.TimeoutError as exc:
            raise TransportTimeoutError(f"BLE 请求超时 ({timeout:.1f}s)") from exc
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise TransportError("缺少 bleak，请先安装 requirements.txt") from exc
        except TransportError:
            raise
        except Exception as exc:
            raise TransportError(f"BLE 请求失败: {exc}") from exc
