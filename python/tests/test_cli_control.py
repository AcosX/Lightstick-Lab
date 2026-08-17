# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 lightstick-control contributors
import argparse
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lightstick_demo.cli import (
    EXIT_USAGE,
    build_parser,
    execute,
    main,
    parse_zones,
    prepare_command,
    validate_radio_args,
)
from lightstick_demo.controller import (
    Controller,
    TxOutcomeUncertainError,
    TxSubmissionUncertainError,
)
from lightstick_demo.discovery import (
    AutoDiscovery,
    Candidate,
    DiscoveryError,
    DiscoveryResult,
    IdentityError,
    is_target_ble_device,
    validate_bridge_info,
)
from lightstick_demo.protocol import D8_ZONES, d8_word
from lightstick_demo.state import DEFAULT_D8_SLOTS, StateStore
from lightstick_demo.transports import (
    BaseTransport,
    TransportError,
    TransportStatus,
    TransportTimeoutError,
    sorted_serial_ports,
)


VALID_INFO = {
    "firmware": "lightstick-bridge",
    "hardware_profile": "ESP32-S3-N16R8",
    "cc1101_found": True,
    "version": "0.5.2",
}


class FakeTransport(BaseTransport):
    def __init__(self, endpoint, responses=None, info=None):
        self.endpoint = endpoint
        self.responses = list(responses or [])
        self.info = dict(VALID_INFO if info is None else info)
        self.calls = []
        self.connected = False
        self.disconnected = False

    def connect(self):
        self.connected = True
        return TransportStatus("test", True, self.endpoint)

    def disconnect(self):
        self.disconnected = True
        return TransportStatus("test", False, self.endpoint)

    def request(self, command, args=None, timeout=8.0):
        self.calls.append((command, args or {}, timeout))
        if command == "GET_INFO":
            return self.info
        if self.responses:
            result = self.responses.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        if command == "TX_PULSES":
            return {"status": "pending", "request_id": "tx-1"}
        if command == "GET_TX_RESULT":
            return {"status": "success", "request_id": "tx-1"}
        if command == "ABORT":
            return {"status": "success"}
        raise AssertionError(command)


class FixedDiscovery:
    def __init__(self, transports):
        self.transports = list(transports)
        self.calls = []

    def connect(self, requested="auto", cached=None):
        self.calls.append((requested, cached))
        transport = self.transports.pop(0)
        return DiscoveryResult(
            transport=transport,
            kind="usb",
            endpoint=transport.endpoint,
            info=dict(VALID_INFO),
            attempts=[{"transport": "usb", "endpoint": transport.endpoint, "source": "test", "ok": True}],
        )


class CliParsingTests(unittest.TestCase):
    def test_exact_requested_command_prepares_one_d8_transaction(self):
        args = build_parser().parse_args(
            ["--zone", "A", "--rgb", "66CCFF", "--effect", "solid"]
        )
        prepared = prepare_command(args, DEFAULT_D8_SLOTS)
        self.assertEqual(prepared.zones, ("A",))
        self.assertEqual(prepared.rgb, "66CCFF")
        self.assertEqual(len(prepared.frames_hex), 6)
        self.assertEqual([command for command, _ in prepared.commands], ["TX_PULSES"])
        self.assertEqual(prepared.slots[0], d8_word(6, 12, 15, 0))

    def test_zone_all_comma_and_repeated_forms(self):
        self.assertEqual(parse_zones(["A,C", "b"]), ("A", "B", "C"))
        self.assertEqual(parse_zones(["all"]), D8_ZONES)

    def test_frequency_validation_matches_firmware_middle_band(self):
        validate_radio_args(378_000_000, -20)
        validate_radio_args(464_000_000, -20)
        with self.assertRaisesRegex(ValueError, "CC1101"):
            validate_radio_args(377_999_999, -20)

    def test_dry_run_does_not_discover_or_persist(self):
        args = build_parser().parse_args(
            ["--zone", "A", "--rgb", "66CCFF", "--dry-run", "--json"]
        )
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory))

            class NoDiscovery:
                def connect(self, *_args, **_kwargs):
                    raise AssertionError("dry-run must not discover hardware")

            result = execute(args, store=store, discovery=NoDiscovery())
            self.assertTrue(result["dry_run"])
            self.assertEqual(result["transaction"]["frames"], 6)
            self.assertFalse(store.path.exists())

    def test_json_usage_error_has_stable_exit_code_and_shape(self):
        with patch("sys.stdout") as stdout:
            code = main(["--zone", "A", "--rgb", "bad", "--json"])
        self.assertEqual(code, EXIT_USAGE)
        payload = json.loads("".join(call.args[0] for call in stdout.write.call_args_list if call.args))
        self.assertEqual(payload["error"]["code"], "usage")

    def test_app_cli_path_never_imports_tkinter(self):
        script = (
            "import sys; sys.argv=['app.py','--zone','A','--rgb','66CCFF','--effect','solid','--dry-run','--json']; "
            "import app; code=app.main(); "
            "sys.stderr.write('tk=' + str(any(k == 'tkinter' or k.startswith('tkinter.') for k in sys.modules)) + '\\n'); "
            "raise SystemExit(code)"
        )
        with tempfile.TemporaryDirectory() as home:
            env = dict(os.environ, HOME=home)
            process = subprocess.run(
                [sys.executable, "-c", script],
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertFalse(json.loads(process.stdout)["dry_run"] is False)
        self.assertIn("tk=False", process.stderr)


class DiscoveryTests(unittest.TestCase):
    def test_identity_requires_all_three_bridge_markers(self):
        self.assertEqual(validate_bridge_info(VALID_INFO), VALID_INFO)
        for field in ("firmware", "hardware_profile", "cc1101_found"):
            invalid = dict(VALID_INFO)
            invalid[field] = None
            with self.subTest(field=field), self.assertRaises(IdentityError):
                validate_bridge_info(invalid)

    def test_cached_identity_failure_falls_back_to_ranked_usb(self):
        made = []

        def factory(candidate: Candidate):
            made.append((candidate.transport, candidate.endpoint, candidate.source))
            if candidate.source == "cache":
                return FakeTransport(candidate.endpoint, info={"firmware": "other"})
            return FakeTransport(candidate.endpoint)

        discovery = AutoDiscovery(
            list_serial_ports=lambda: [
                {"device": "/dev/ttyUSB1", "description": "USB"},
                {"device": "/dev/ttyACM0", "description": "ESP32"},
            ],
            scan_ble=lambda **_: [],
            discover_wifi=lambda **_: [],
            transport_factory=factory,
        )
        result = discovery.connect(
            "auto", cached={"transport": "wifi", "endpoint": "http://old.local"}
        )
        self.assertEqual(result.kind, "usb")
        self.assertEqual(result.endpoint, "/dev/ttyACM0")
        self.assertEqual(
            made,
            [
                ("wifi", "http://old.local", "cache"),
                ("usb", "/dev/ttyACM0", "scan"),
            ],
        )
        self.assertEqual(result.attempts[0]["error"]["type"], "identity")

    def test_serial_ranking_covers_macos_linux_and_windows(self):
        ports = [
            {"device": "COM9", "description": "USB Serial", "vid": 0x303A},
            {"device": "/dev/ttyUSB0", "description": "USB"},
            {"device": "/dev/cu.usbmodem01", "description": "ESP32"},
            {"device": "/dev/ttyACM0", "description": "ESP32"},
            {"device": "/dev/cu.Bluetooth-Incoming-Port", "description": "Bluetooth"},
        ]
        self.assertEqual(
            [port["device"] for port in sorted_serial_ports(ports)],
            ["/dev/cu.usbmodem01", "/dev/ttyACM0", "/dev/ttyUSB0", "COM9"],
        )

    def test_wifi_udp_candidate_is_attempted_before_mdns(self):
        made = []

        def factory(candidate: Candidate):
            made.append((candidate.endpoint, candidate.source))
            return FakeTransport(candidate.endpoint)

        discovery = AutoDiscovery(
            list_serial_ports=lambda: [],
            scan_ble=lambda **_: [],
            discover_wifi=lambda **_: ["http://192.0.2.169"],
            transport_factory=factory,
        )
        result = discovery.connect("wifi")
        self.assertEqual(result.endpoint, "http://192.0.2.169")
        self.assertEqual(made, [("http://192.0.2.169", "udp")])

    def test_ble_discovery_only_attempts_explicit_bridge_names(self):
        self.assertTrue(is_target_ble_device({"name": "lightstick-bridge"}))
        self.assertTrue(is_target_ble_device({"name": "LIGHTSTICK N16R8"}))
        self.assertFalse(is_target_ble_device({"name": "Nearby Lightstick"}))
        self.assertFalse(is_target_ble_device({"name": None}))
        made = []

        def factory(candidate: Candidate):
            made.append(candidate.endpoint)
            return FakeTransport(candidate.endpoint)

        discovery = AutoDiscovery(
            list_serial_ports=lambda: [],
            scan_ble=lambda **_: [
                {"name": None, "address": "missing-name"},
                {"name": "Headphones", "address": "other"},
                {"name": "LIGHTSTICK_BRIDGE", "address": "bridge"},
            ],
            discover_wifi=lambda **_: [],
            transport_factory=factory,
        )
        result = discovery.connect("ble")
        self.assertEqual(result.endpoint, "bridge")
        self.assertEqual(made, ["bridge"])

    def test_ble_discovery_does_not_probe_when_target_name_is_absent(self):
        made = []
        discovery = AutoDiscovery(
            list_serial_ports=lambda: [],
            scan_ble=lambda **_: [
                {"name": None, "address": "missing-name"},
                {"name": "Other", "address": "other"},
            ],
            discover_wifi=lambda **_: [],
            transport_factory=lambda candidate: made.append(candidate) or FakeTransport(candidate.endpoint),
        )
        with self.assertRaises(DiscoveryError):
            discovery.connect("ble")
        self.assertEqual(made, [])


class ControllerAndStateTests(unittest.TestCase):
    def test_controller_submits_tx_exactly_once(self):
        transport = FakeTransport("test")
        result = Controller(transport).execute_commands(
            [("TX_PULSES", {"durations_us": [250], "repeat": 1, "gap_us": 0})]
        )
        self.assertEqual(result[-1]["status"], "success")
        self.assertEqual([call[0] for call in transport.calls].count("TX_PULSES"), 1)

    def test_post_submit_poll_timeout_aborts_without_resend(self):
        transport = FakeTransport("test")
        with patch(
            "lightstick_demo.controller.wait_for_tx_result",
            side_effect=TransportTimeoutError("poll timed out"),
        ):
            with self.assertRaises(TxOutcomeUncertainError):
                Controller(transport).execute_commands(
                    [("TX_PULSES", {"durations_us": [250], "repeat": 1, "gap_us": 0})]
                )
        commands = [call[0] for call in transport.calls]
        self.assertEqual(commands.count("TX_PULSES"), 1)
        self.assertEqual(commands.count("ABORT"), 1)

    def test_submit_ack_timeout_is_marked_uncertain_and_not_resent(self):
        transport = FakeTransport("test", responses=[TransportTimeoutError("ack lost")])
        with self.assertRaises(TxSubmissionUncertainError):
            Controller(transport).execute_commands(
                [("TX_PULSES", {"durations_us": [250], "repeat": 1, "gap_us": 0})]
            )
        commands = [call[0] for call in transport.calls]
        self.assertEqual(commands.count("TX_PULSES"), 1)
        self.assertEqual(commands.count("ABORT"), 1)

    def test_d8_shadow_persists_success_and_preserves_unselected_slots(self):
        first_args = build_parser().parse_args(
            ["--zone", "B", "--rgb", "66CCFF", "--effect", "medium"]
        )
        second_args = build_parser().parse_args(
            ["--zone", "A", "--rgb", "00FF00", "--effect", "solid"]
        )
        transports = [FakeTransport("usb-one"), FakeTransport("usb-two")]
        discovery = FixedDiscovery(transports)
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory))
            execute(first_args, store=store, discovery=discovery)
            with store.locked():
                after_first = store.load_unlocked()
            first_b = after_first["d8_slots"][1]
            self.assertEqual(first_b, d8_word(6, 12, 15, 2))

            execute(second_args, store=store, discovery=discovery)
            with store.locked():
                after_second = store.load_unlocked()
            self.assertEqual(after_second["d8_slots"][0], d8_word(0, 15, 0, 0))
            self.assertEqual(after_second["d8_slots"][1], first_b)
            self.assertEqual(after_second["connection"]["endpoint"], "usb-two")

    def test_failed_tx_does_not_commit_candidate_shadow(self):
        args = build_parser().parse_args(["--zone", "B", "--rgb", "66CCFF"])
        transport = FakeTransport("usb", responses=[TransportError("rejected")])
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory))
            with self.assertRaises(TransportError):
                execute(args, store=store, discovery=FixedDiscovery([transport]))
            with store.locked():
                state = store.load_unlocked()
            self.assertEqual(tuple(state["d8_slots"]), DEFAULT_D8_SLOTS)
            self.assertEqual(state["connection"]["endpoint"], "usb")

    def test_gui_shared_transaction_preserves_prior_cli_zone(self):
        cli_args = build_parser().parse_args(
            ["--zone", "B", "--rgb", "66CCFF", "--effect", "medium"]
        )
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory))
            execute(
                cli_args,
                store=store,
                discovery=FixedDiscovery([FakeTransport("cli-usb")]),
            )
            from lightstick_demo.ui import LightstickApp

            gui_result = LightstickApp._execute_persistent_d8_tx(
                SimpleNamespace(_state_store=store),
                FakeTransport("gui-usb"),
                ("A",),
                d8_word(0, 15, 0, 0),
                433_920_000,
                -20,
                20_000,
            )
            self.assertTrue(gui_result.state_changed)
            with store.locked():
                state = store.load_unlocked()
            self.assertEqual(state["d8_slots"][0], d8_word(0, 15, 0, 0))
            self.assertEqual(state["d8_slots"][1], d8_word(6, 12, 15, 2))


if __name__ == "__main__":
    unittest.main()
