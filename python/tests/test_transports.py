# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lightstick-control contributors
import json
import sys
import types
import unittest
from unittest.mock import patch

from lightstick_demo.transports import (
    BLE_COMMAND_UUID,
    BLE_RESPONSE_UUID,
    BLE_SERVICE_UUID,
    BleTransport,
    HttpTransport,
    SerialTransport,
    TransportError,
    TransportTimeoutError,
)


class FakeSerial:
    is_open = True

    def __init__(self) -> None:
        self.writes = []
        self.responses = []

    def write(self, payload):
        self.writes.append(payload)
        request = json.loads(payload.decode("utf-8"))
        self.responses.append((json.dumps({"id": request["id"], "ok": True, "result": {"echo": request}}) + "\n").encode())

    def flush(self):
        return None

    def readline(self):
        return self.responses.pop(0) if self.responses else b""

    def close(self):
        self.is_open = False


def serial_line(message):
    return (json.dumps(message) + "\n").encode()


class TxCompleteBeforeAckSerial(FakeSerial):
    def __init__(self) -> None:
        super().__init__()
        self.tx_request_id = ""

    def write(self, payload):
        self.writes.append(payload)
        request = json.loads(payload.decode("utf-8"))
        if request["cmd"] != "TX_PULSES":
            raise AssertionError("cached GET_TX_RESULT must not write another request")
        self.tx_request_id = request["id"]
        self.responses.extend(
            [
                serial_line(
                    {
                        "id": self.tx_request_id,
                        "ok": True,
                        "event": "TX_COMPLETE",
                        "result": {
                            "status": "success",
                            "request_id": self.tx_request_id,
                            "sequences_sent": 1,
                        },
                    }
                ),
                serial_line(
                    {
                        "id": self.tx_request_id,
                        "ok": True,
                        "result": {
                            "status": "pending",
                            "accepted": True,
                            "request_id": self.tx_request_id,
                        },
                    }
                ),
            ]
        )


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self.body


class FakeSession:
    def __init__(self):
        self.calls = []

    def get(self, url, timeout):
        self.calls.append(("GET", url, None, timeout))
        return FakeResponse({"ok": True})

    def post(self, url, json, timeout):
        self.calls.append(("POST", url, json, timeout))
        return FakeResponse({"id": json["id"], "ok": True, "result": {"echo": json}})

    def close(self):
        return None


class FakeBleakClient:
    instances = []

    def __init__(self, address):
        self.address = address
        self.is_connected = True
        self.notify_callback = None
        self.writes = []
        self.input_buffer = bytearray()
        self.__class__.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def start_notify(self, uuid, callback):
        self.notify_callback = callback

    async def stop_notify(self, uuid):
        return None

    async def write_gatt_char(self, uuid, payload, response=True):
        self.writes.append((uuid, payload, response))
        self.input_buffer.extend(payload)
        while b"\n" in self.input_buffer:
            line, _, remainder = self.input_buffer.partition(b"\n")
            self.input_buffer[:] = remainder
            request = json.loads(line.decode("utf-8"))
            if self.notify_callback is not None:
                response = (json.dumps({"id": request["id"], "ok": True, "result": {"echo": request}}) + "\n").encode()
                for offset in range(0, len(response), 37):
                    self.notify_callback(0, response[offset : offset + 37])


class FakeBleDevice:
    def __init__(self, name, address):
        self.name = name
        self.address = address


class FakeBleakScanner:
    @staticmethod
    async def discover(timeout):
        del timeout
        return [FakeBleDevice("Other", "AA:AA"), FakeBleDevice("Lightstick N16R8", "BB:BB")]


class TransportTests(unittest.TestCase):
    def test_serial_json_line_request(self) -> None:
        fake = FakeSerial()
        transport = SerialTransport("/dev/cu.test")
        transport._serial = fake
        result = transport.request("GET_STATUS", {"sample": True})
        request = json.loads(fake.writes[0].decode("utf-8"))
        self.assertEqual(request["cmd"], "GET_STATUS")
        self.assertEqual(request["args"], {"sample": True})
        self.assertEqual(result["echo"]["id"], request["id"])

    def test_serial_lock_wait_does_not_consume_request_timeout(self) -> None:
        class FakeClock:
            now = 0.0

        class AdvancingLock:
            def __enter__(self):
                FakeClock.now = 2.0
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

        fake = FakeSerial()
        transport = SerialTransport("/dev/cu.test")
        transport._serial = fake
        transport._lock = AdvancingLock()
        with patch("lightstick_demo.transports.time.monotonic", side_effect=lambda: FakeClock.now):
            result = transport.request("GET_STATUS", timeout=1.0)
        self.assertEqual(result["echo"]["cmd"], "GET_STATUS")

    def test_serial_reassembles_partial_lines_and_drops_bad_complete_lines(self) -> None:
        class SplitSerial(FakeSerial):
            def write(self, payload):
                self.writes.append(payload)
                request = json.loads(payload.decode("utf-8"))
                response = serial_line({"id": request["id"], "ok": True, "result": {"status": "ok"}})
                split = len(response) // 2
                self.responses.extend([b"not-json\n", response[:split], response[split:]])

        transport = SerialTransport("/dev/cu.test")
        transport._serial = SplitSerial()
        self.assertEqual(transport.request("GET_STATUS"), {"status": "ok"})
        self.assertEqual(transport._rx_buffer, bytearray())

    def test_serial_preserves_partial_line_across_requests(self) -> None:
        class FakeClock:
            now = 0.0

        class AcrossRequestSerial(FakeSerial):
            def __init__(self) -> None:
                super().__init__()
                self.first_tail = b""

            def write(self, payload):
                self.writes.append(payload)
                request = json.loads(payload.decode("utf-8"))
                response = serial_line(
                    {"id": request["id"], "ok": True, "result": {"call": len(self.writes)}}
                )
                if len(self.writes) == 1:
                    split = len(response) // 2
                    self.responses.append(response[:split])
                    self.first_tail = response[split:]
                else:
                    self.responses.extend([self.first_tail, response])

            def readline(self):
                if self.responses:
                    return self.responses.pop(0)
                FakeClock.now += 1.0
                return b""

        transport = SerialTransport("/dev/cu.test")
        transport._serial = AcrossRequestSerial()
        with patch("lightstick_demo.transports.time.monotonic", side_effect=lambda: FakeClock.now):
            with self.assertRaises(TransportTimeoutError):
                transport.request("GET_STATUS", timeout=0.5)
            self.assertTrue(transport._rx_buffer)
            self.assertEqual(transport.request("GET_INFO", timeout=0.5), {"call": 2})
        self.assertEqual(transport._rx_buffer, bytearray())

    def test_serial_disconnect_clears_parser_and_completion_state(self) -> None:
        fake = FakeSerial()
        transport = SerialTransport("/dev/cu.test")
        transport._serial = fake
        transport._rx_buffer.extend(b"partial")
        transport._tx_complete_cache["tx-old"] = {
            "status": "success",
            "request_id": "tx-old",
        }
        transport.disconnect()
        self.assertFalse(fake.is_open)
        self.assertEqual(transport._rx_buffer, bytearray())
        self.assertEqual(transport._tx_complete_cache, {})

    def test_serial_tx_complete_does_not_replace_same_id_pending_ack(self) -> None:
        fake = TxCompleteBeforeAckSerial()
        transport = SerialTransport("/dev/cu.test")
        transport._serial = fake
        accepted = transport.request("TX_PULSES", {"durations_us": [250]})
        self.assertEqual(accepted["status"], "pending")
        self.assertEqual(accepted["request_id"], fake.tx_request_id)
        self.assertEqual(transport._tx_complete_cache[fake.tx_request_id]["status"], "success")

    def test_serial_get_tx_result_hits_existing_completion_before_write(self) -> None:
        fake = TxCompleteBeforeAckSerial()
        transport = SerialTransport("/dev/cu.test")
        transport._serial = fake
        transport.request("TX_PULSES", {"durations_us": [250]})
        completed = transport.request("GET_TX_RESULT", {"request_id": fake.tx_request_id})
        self.assertEqual(completed["status"], "success")
        self.assertEqual(completed["request_id"], fake.tx_request_id)
        self.assertEqual(len(fake.writes), 1)

    def test_serial_get_tx_result_accepts_matching_live_completion(self) -> None:
        class CompletionDuringGetSerial(FakeSerial):
            def write(self, payload):
                self.writes.append(payload)
                request = json.loads(payload.decode("utf-8"))
                target = request["args"]["request_id"]
                self.responses.append(
                    serial_line(
                        {
                            "id": target,
                            "ok": True,
                            "event": "TX_COMPLETE",
                            "result": {"status": "success", "request_id": target},
                        }
                    )
                )

        transport = SerialTransport("/dev/cu.test")
        transport._serial = CompletionDuringGetSerial()
        result = transport.request("GET_TX_RESULT", {"request_id": "tx-target"})
        self.assertEqual(result, {"status": "success", "request_id": "tx-target"})

    def test_serial_get_tx_result_converts_matching_error_completion(self) -> None:
        class ErrorCompletionSerial(FakeSerial):
            def write(self, payload):
                self.writes.append(payload)
                request = json.loads(payload.decode("utf-8"))
                self.responses.append(
                    serial_line(
                        {
                            "id": request["args"]["request_id"],
                            "ok": False,
                            "event": "TX_COMPLETE",
                            "error": "radio failed",
                        }
                    )
                )

        transport = SerialTransport("/dev/cu.test")
        transport._serial = ErrorCompletionSerial()
        result = transport.request("GET_TX_RESULT", {"request_id": "tx-error"})
        self.assertEqual(
            result,
            {"status": "error", "request_id": "tx-error", "error": "radio failed"},
        )

    def test_serial_get_tx_result_does_not_mispair_wrong_completion_id(self) -> None:
        class WrongCompletionSerial(FakeSerial):
            def write(self, payload):
                self.writes.append(payload)
                request = json.loads(payload.decode("utf-8"))
                target = request["args"]["request_id"]
                self.responses.extend(
                    [
                        serial_line(
                            {
                                "id": "tx-other",
                                "ok": True,
                                "event": "TX_COMPLETE",
                                "result": {"status": "success", "request_id": "tx-other"},
                            }
                        ),
                        serial_line(
                            {
                                "id": request["id"],
                                "ok": True,
                                "result": {"status": "pending", "request_id": target},
                            }
                        ),
                    ]
                )

        transport = SerialTransport("/dev/cu.test")
        transport._serial = WrongCompletionSerial()
        result = transport.request("GET_TX_RESULT", {"request_id": "tx-target"})
        self.assertEqual(result, {"status": "pending", "request_id": "tx-target"})
        self.assertIn("tx-other", transport._tx_complete_cache)
        self.assertNotIn("tx-target", transport._tx_complete_cache)

    def test_serial_tx_complete_cache_is_bounded_to_64_entries(self) -> None:
        class ManyCompletionsSerial(FakeSerial):
            def write(self, payload):
                self.writes.append(payload)
                request = json.loads(payload.decode("utf-8"))
                self.responses.extend(
                    serial_line(
                        {
                            "id": f"tx-{index}",
                            "ok": True,
                            "event": "TX_COMPLETE",
                            "result": {"status": "success", "request_id": f"tx-{index}"},
                        }
                    )
                    for index in range(65)
                )
                self.responses.append(serial_line({"id": request["id"], "ok": True, "result": {"status": "ok"}}))

        transport = SerialTransport("/dev/cu.test")
        transport._serial = ManyCompletionsSerial()
        self.assertEqual(transport.request("GET_STATUS"), {"status": "ok"})
        self.assertEqual(len(transport._tx_complete_cache), 64)
        self.assertNotIn("tx-0", transport._tx_complete_cache)
        self.assertIn("tx-64", transport._tx_complete_cache)

    def test_serial_timeout_is_a_transport_timeout_error(self) -> None:
        class SilentSerial(FakeSerial):
            def readline(self):
                return b""

        transport = SerialTransport("/dev/cu.test")
        transport._serial = SilentSerial()
        with self.assertRaises(TransportTimeoutError):
            transport.request("GET_TX_RESULT", {"request_id": "x"}, timeout=0.05)

    def test_http_timeout_is_a_transport_timeout_error(self) -> None:
        module = types.ModuleType("requests")
        exceptions = types.ModuleType("requests.exceptions")
        module.__path__ = []
        module.exceptions = exceptions

        class FakeRequestsTimeout(Exception):
            pass

        exceptions.Timeout = FakeRequestsTimeout
        original = sys.modules.get("requests")
        original_exceptions = sys.modules.get("requests.exceptions")
        sys.modules["requests"] = module
        sys.modules["requests.exceptions"] = exceptions
        try:
            class TimingOutSession(FakeSession):
                def post(self, url, json, timeout):
                    self.calls.append(("POST", url, json, timeout))
                    raise FakeRequestsTimeout("slow")

            transport = HttpTransport("http://board.local/")
            transport.session = TimingOutSession()
            with self.assertRaises(TransportTimeoutError):
                transport.request("GET_TX_RESULT", {"request_id": "x"})
        finally:
            if original is None:
                sys.modules.pop("requests", None)
            else:
                sys.modules["requests"] = original
            if original_exceptions is None:
                sys.modules.pop("requests.exceptions", None)
            else:
                sys.modules["requests.exceptions"] = original_exceptions

    def test_ble_timeout_is_a_transport_timeout_error(self) -> None:
        class SilentBleakClient(FakeBleakClient):
            async def write_gatt_char(self, uuid, payload, response=True):
                self.writes.append((uuid, payload, response))
                # Intentionally no notification, so the exchange times out.

        module = types.ModuleType("bleak")
        module.BleakClient = SilentBleakClient
        original = sys.modules.get("bleak")
        sys.modules["bleak"] = module
        try:
            transport = BleTransport("AA:BB", "write-uuid", "notify-uuid")
            transport.connected = True
            with self.assertRaises(TransportTimeoutError):
                transport.request("GET_TX_RESULT", {"request_id": "x"}, timeout=0.05)
        finally:
            if original is None:
                sys.modules.pop("bleak", None)
            else:
                sys.modules["bleak"] = original

    def test_http_request_and_health_shape(self) -> None:
        transport = HttpTransport("http://board.local/")
        self.assertTrue(hasattr(transport, "_lock"))
        session = FakeSession()
        transport.session = session
        result = transport.request("GET_INFO", {})
        self.assertEqual(session.calls[0][0:2], ("POST", "http://board.local/api/command"))
        body = session.calls[0][2]
        self.assertEqual(body["cmd"], "GET_INFO")
        self.assertEqual(body["args"], {})
        self.assertEqual(result["echo"]["cmd"], "GET_INFO")

    def test_http_rejects_mismatched_response_id(self) -> None:
        class WrongIdSession(FakeSession):
            def post(self, url, json, timeout):
                self.calls.append(("POST", url, json, timeout))
                return FakeResponse({"id": "other", "ok": True, "result": {}})

        transport = HttpTransport("http://board.local/")
        transport.session = WrongIdSession()
        with self.assertRaisesRegex(TransportError, "id"):
            transport.request("GET_INFO", {})

    def test_ble_gatt_json_exchange(self) -> None:
        module = types.ModuleType("bleak")
        module.BleakClient = FakeBleakClient
        original = sys.modules.get("bleak")
        sys.modules["bleak"] = module
        try:
            transport = BleTransport("AA:BB", "write-uuid", "notify-uuid")
            transport.connected = True
            result = transport.request("GET_INFO", {"verbose": True})
            client = FakeBleakClient.instances[-1]
            request = json.loads(client.writes[0][1].decode("utf-8"))
            self.assertEqual(client.writes[0][0], "write-uuid")
            self.assertEqual(request["cmd"], "GET_INFO")
            self.assertEqual(result["echo"]["args"], {"verbose": True})
        finally:
            if original is None:
                sys.modules.pop("bleak", None)
            else:
                sys.modules["bleak"] = original

    def test_ble_defaults_require_notify_and_use_fixed_service(self) -> None:
        transport = BleTransport("AA:BB")
        self.assertEqual(transport.service_uuid, BLE_SERVICE_UUID)
        self.assertEqual(transport.write_uuid, BLE_COMMAND_UUID)
        self.assertEqual(transport.notify_uuid, BLE_RESPONSE_UUID)
        transport.notify_uuid = ""
        transport.connected = True
        with self.assertRaisesRegex(TransportError, "通知"):
            transport.request("GET_INFO")

    def test_ble_scan_maps_device_name_and_address(self) -> None:
        module = types.ModuleType("bleak")
        module.BleakScanner = FakeBleakScanner
        original = sys.modules.get("bleak")
        sys.modules["bleak"] = module
        try:
            self.assertEqual(
                BleTransport.scan(timeout=0.01),
                [
                    {"name": "Other", "address": "AA:AA"},
                    {"name": "Lightstick N16R8", "address": "BB:BB"},
                ],
            )
        finally:
            if original is None:
                sys.modules.pop("bleak", None)
            else:
                sys.modules["bleak"] = original

    def test_ble_request_is_chunked_and_notification_is_reassembled(self) -> None:
        module = types.ModuleType("bleak")
        module.BleakClient = FakeBleakClient
        original = sys.modules.get("bleak")
        sys.modules["bleak"] = module
        try:
            transport = BleTransport("AA:BB")
            transport.connected = True
            result = transport.request("SET_PROFILE", {"profile": {"name": "long", "blob": "x" * 700}})
            client = FakeBleakClient.instances[-1]
            self.assertGreater(len(client.writes), 1)
            self.assertTrue(all(len(payload) <= 180 for _, payload, _ in client.writes))
            self.assertTrue(client.writes[-1][1].endswith(b"\n"))
            request_bytes = b"".join(payload for _, payload, _ in client.writes)
            self.assertEqual(json.loads(request_bytes.decode("utf-8"))["cmd"], "SET_PROFILE")
            self.assertEqual(result["echo"]["args"]["profile"]["name"], "long")
        finally:
            if original is None:
                sys.modules.pop("bleak", None)
            else:
                sys.modules["bleak"] = original

if __name__ == "__main__":
    unittest.main()
