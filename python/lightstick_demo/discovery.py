# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lightstick-control contributors
"""Automatic USB, Wi-Fi, and BLE bridge discovery for headless control."""

from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from .transports import (
    BLE_DEVICE_NAME,
    BaseTransport,
    BleTransport,
    HttpTransport,
    SerialTransport,
    TransportError,
    sorted_serial_ports,
)


DISCOVERY_PORT = 4210
DISCOVERY_REQUEST = b"LIGHTSTICK_DISCOVER"
MDNS_URL = "http://lightstick-n16r8.local"
VALID_TRANSPORTS = ("usb", "wifi", "ble")
BLE_BRIDGE_NAMES = (BLE_DEVICE_NAME, "LIGHTSTICK_BRIDGE")


class DiscoveryError(TransportError):
    def __init__(self, message: str, attempts: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.attempts = attempts


class IdentityError(TransportError):
    """A reachable endpoint is not the required bridge hardware."""


@dataclass(frozen=True)
class Candidate:
    transport: str
    endpoint: str
    source: str


@dataclass
class DiscoveryResult:
    transport: BaseTransport
    kind: str
    endpoint: str
    info: dict[str, Any]
    attempts: list[dict[str, Any]]

    @property
    def cache_entry(self) -> dict[str, Any]:
        return {"transport": self.kind, "endpoint": self.endpoint}


def validate_bridge_info(info: Any) -> dict[str, Any]:
    if not isinstance(info, dict):
        raise IdentityError("GET_INFO 返回格式无效")
    mismatches: list[str] = []
    if info.get("firmware") != "lightstick-bridge":
        mismatches.append("firmware")
    if info.get("hardware_profile") != "ESP32-S3-N16R8":
        mismatches.append("hardware_profile")
    if info.get("cc1101_found") is not True:
        mismatches.append("cc1101_found")
    if mismatches:
        raise IdentityError("桥接器身份校验失败: " + ", ".join(mismatches))
    return info


def is_target_ble_device(device: dict[str, Any]) -> bool:
    """Match only the firmware's explicit BLE names, case/separator agnostic."""

    name = str(device.get("name") or "").strip()
    if not name:
        return False
    normalized = "".join(character for character in name.casefold() if character.isalnum())
    expected = {
        "".join(character for character in target.casefold() if character.isalnum())
        for target in BLE_BRIDGE_NAMES
    }
    return normalized in expected


def discover_wifi_urls(timeout: float = 0.8) -> list[str]:
    """Broadcast the firmware discovery message and collect unique HTTP URLs."""

    urls: list[str] = []
    seen: set[str] = set()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(max(0.05, timeout))
        sock.bind(("", 0))
        sock.sendto(DISCOVERY_REQUEST, ("255.255.255.255", DISCOVERY_PORT))
        while True:
            try:
                payload, address = sock.recvfrom(4096)
            except socket.timeout:
                break
            try:
                message = json.loads(payload.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError):
                continue
            if not isinstance(message, dict):
                continue
            ip = str(message.get("ip") or address[0] or "").strip()
            if not ip or ip == "0.0.0.0":
                continue
            try:
                port = int(message.get("http_port") or 80)
            except (TypeError, ValueError):
                continue
            url = f"http://{ip}" if port == 80 else f"http://{ip}:{port}"
            if url not in seen:
                seen.add(url)
                urls.append(url)
    return urls


class AutoDiscovery:
    """Validate candidates in cache, USB, Wi-Fi, BLE order."""

    def __init__(
        self,
        *,
        list_serial_ports: Callable[[], list[dict[str, Any]]] = SerialTransport.list_ports,
        scan_ble: Callable[..., list[dict[str, str]]] = BleTransport.scan,
        discover_wifi: Callable[..., list[str]] = discover_wifi_urls,
        transport_factory: Callable[[Candidate], BaseTransport] | None = None,
    ) -> None:
        self._list_serial_ports = list_serial_ports
        self._scan_ble = scan_ble
        self._discover_wifi = discover_wifi
        self._transport_factory = transport_factory or self._default_transport_factory

    @staticmethod
    def _default_transport_factory(candidate: Candidate) -> BaseTransport:
        if candidate.transport == "usb":
            return SerialTransport(candidate.endpoint, 921600)
        if candidate.transport == "wifi":
            return HttpTransport(candidate.endpoint)
        if candidate.transport == "ble":
            return BleTransport(candidate.endpoint)
        raise TransportError(f"不支持的传输: {candidate.transport}")

    @staticmethod
    def _failure(candidate: Candidate, exc: Exception) -> dict[str, Any]:
        return {
            "transport": candidate.transport,
            "endpoint": candidate.endpoint,
            "source": candidate.source,
            "ok": False,
            "error": {
                "type": "identity" if isinstance(exc, IdentityError) else "transport",
                "message": str(exc),
            },
        }

    @staticmethod
    def _scan_failure(kind: str, exc: Exception) -> dict[str, Any]:
        return {
            "transport": kind,
            "endpoint": "",
            "source": "scan",
            "ok": False,
            "error": {"type": "discovery", "message": str(exc)},
        }

    def _try_candidate(
        self,
        candidate: Candidate,
        attempts: list[dict[str, Any]],
    ) -> DiscoveryResult | None:
        transport: BaseTransport | None = None
        try:
            transport = self._transport_factory(candidate)
            transport.connect()
            info = validate_bridge_info(transport.request("GET_INFO", {}, timeout=5))
        except Exception as exc:
            attempts.append(self._failure(candidate, exc))
            if transport is not None:
                try:
                    transport.disconnect()
                except Exception:
                    pass
            return None
        attempts.append(
            {
                "transport": candidate.transport,
                "endpoint": candidate.endpoint,
                "source": candidate.source,
                "ok": True,
            }
        )
        return DiscoveryResult(
            transport=transport,
            kind=candidate.transport,
            endpoint=candidate.endpoint,
            info=info,
            attempts=attempts,
        )

    def _stage_candidates(
        self,
        kind: str,
        attempts: list[dict[str, Any]],
    ) -> Iterable[Candidate]:
        if kind == "usb":
            try:
                ports = sorted_serial_ports(self._list_serial_ports())
            except Exception as exc:
                attempts.append(self._scan_failure(kind, exc))
                return ()
            return tuple(
                Candidate("usb", str(port.get("device") or ""), "scan")
                for port in ports
                if str(port.get("device") or "").strip()
            )
        if kind == "wifi":
            try:
                urls = self._discover_wifi(timeout=0.8)
            except Exception as exc:
                attempts.append(self._scan_failure(kind, exc))
                urls = []
            candidates = [Candidate("wifi", url.rstrip("/"), "udp") for url in urls]
            candidates.append(Candidate("wifi", MDNS_URL, "mdns"))
            return tuple(candidates)
        if kind == "ble":
            try:
                devices = self._scan_ble(timeout=5.0)
            except Exception as exc:
                attempts.append(self._scan_failure(kind, exc))
                return ()
            return tuple(
                Candidate("ble", str(device.get("address") or ""), "scan")
                for device in devices
                if str(device.get("address") or "").strip() and is_target_ble_device(device)
            )
        return ()

    def connect(
        self,
        requested: str = "auto",
        cached: dict[str, Any] | None = None,
    ) -> DiscoveryResult:
        if requested not in {"auto", *VALID_TRANSPORTS}:
            raise ValueError(f"不支持的传输: {requested}")
        attempts: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()

        if isinstance(cached, dict):
            cached_kind = str(cached.get("transport") or "").lower()
            cached_endpoint = str(cached.get("endpoint") or "").strip().rstrip("/")
            if (
                cached_kind in VALID_TRANSPORTS
                and cached_endpoint
                and (requested == "auto" or requested == cached_kind)
            ):
                candidate = Candidate(cached_kind, cached_endpoint, "cache")
                seen.add((candidate.transport, candidate.endpoint))
                result = self._try_candidate(candidate, attempts)
                if result is not None:
                    return result

        stages = VALID_TRANSPORTS if requested == "auto" else (requested,)
        for kind in stages:
            for candidate in self._stage_candidates(kind, attempts):
                key = (candidate.transport, candidate.endpoint)
                if key in seen:
                    continue
                seen.add(key)
                result = self._try_candidate(candidate, attempts)
                if result is not None:
                    return result

        raise DiscoveryError("未找到通过身份校验的 Lightstick 桥接器", attempts)
