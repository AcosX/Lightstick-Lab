# Protocols, Engine and external control

The Python application discovers trusted modules at startup. `protocols/` answers how to transmit; `server/` answers where logical updates arrive. Neither connector imports an air encoder. The ESP32 firmware remains unchanged.

## Running

From `python/`:

```sh
python app.py
python cli_app.py --protocol protocol_d8 --zone A --rgb 66CCFF --dry-run --json
python cli_app.py --protocol protocol_00 --zone A,P --rgb FF0000 --effect fade_in --dry-run --json
python -m unittest discover -s tests -v
PYTHONPATH=. python benchmarks/realtime.py --output ../docs/architecture/benchmark.json
```

The GUI has a protocol selector on the manual-control tab and a separate external-control tab for the connector selector, listener status and runtime statistics. Select a connector to start with saved settings; use **启动 / 配置** to change host/port. Settings and selected IDs persist in the existing user state directory. A saved connector selection is restored in the UI but does not automatically open a listener on application launch; select it or press the start button. Stop releases its socket before another connector starts. Bridge discovery/identity, transport ownership and transmission execution belong to Engine.

## Domain and scheduling

`LogicalUpdate(zones=("A", "B"), rgb=(15, 3, 8), effect="solid")` uses RGB components 0–15 and semantic effect names. A batch is validated atomically. Active unsupported zones/effects reject the batch and increment `unsupported`; an unlit solid/off zone outside the selected protocol's capacity is inactive and omitted centrally. LumaFlow J therefore does not block A–I when empty, but active J produces `unsupported_zone = J` under D8.

A scheduler worker owns one running state and at most one latest pending state. It merges partial zone updates before coalescing, preserving changes to different zones. Unchanged confirmed/running/pending state is deduplicated. Only changed zones are handed to a plugin, which rebases them onto the latest persisted protocol shadow under an inter-process lock. The lock covers load, TX completion and atomic save; no protocol candidate is committed before successful bridge completion.

`received`, `deduplicated`, `coalesced`, `unsupported`, `transmitted`, `failed`, pending depth, queue delay and TX duration are observable. Connector counters separately report packets, malformed input and deduplication. LumaFlow caches one parsed packet and still consults core dedupe so manual changes and failed transmissions are respected.

An uncertain submission/result or failed state commit suspends scheduling and drops pending input. Reconnect before resuming; no automatic waveform retransmission is attempted. A protocol change while running/pending is rejected rather than applying a queued update through the wrong protocol. Ordinary explicit CLI commands are synchronous Engine transactions; GUI and network updates use its scheduler.

## Protocol plugin contract

Create `python/lightstick_demo/protocols/example.py`:

```python
from ..model import Capabilities, TransmissionPlan

class Example:
    id = "example"
    display_name = "Example protocol"
    capabilities = Capabilities(zones=("A",), effects=("solid",))

    def initial_state(self):
        return {}

    def migrate_state(self, state, legacy=None):
        return dict(state)

    def build_plan(self, logical, state, radio):
        # Encode logical.values into this protocol's actual waveform/packet.
        # Return one or more ("TX_PULSES" or "TX_PACKET", args) pairs.
        # Never transmit here. next_state must be JSON serializable.
        raise NotImplementedError("Implement the protocol encoder")

PROTOCOL = Example()
```

No import-list or UI edit is needed. `pkgutil`/`importlib` discovery ignores `_*.py`, `base.py`, and `registry.py`; module failures, incomplete interfaces and duplicate IDs are reported without hiding other valid plugins. Modules are executable trusted Python, not sandboxed data files.

Built-ins are **00 短命令** (`protocol_00`, A–P, palette, fade/hold) and **D8 RGB** (`protocol_d8`, A–I, RGB444). Palette mapping belongs to the short-command plugin. Explicit palette selection preserves the original 00 frame. D8 keeps the original `2,1,0,2,1,0` phases, cadence and single `TX_PULSES` request with repeat 1 regardless of the generic repeat setting. No realtime/short-phase mode is enabled.

State v2 holds `selected_protocol`, `protocol_states`, `selected_connector`, and `connector_configs`. Legacy `d8_slots` migrates into `protocol_states.protocol_d8.slots` once and remains a compatibility mirror; existing files and unrelated plugin state are preserved. Canonical v2 plugin state takes precedence over that mirror. A missing selected plugin falls back to an available plugin with a visible warning.

The historical `protocol.py`, legacy controller helpers and advanced C0/A6/DA dialogs remain available for migration compatibility. Active protocol control uses Engine and capabilities. Legacy helper API compatibility is tested, and generic advanced TX also shares Engine's RF lock.

## Connector contract

A module in `server/` exports `CONNECTOR` with `id`, `display_name`, `config_schema`, `default_config`, `start(context)`, `stop()` and `status()`. Its configuration schema maps field names to `string` or `integer`. The context exposes only `submit_update`, `status` (including available logical zones), and configuration. Descriptors must support deep copying to create an independent runtime per manager. The provided `_base.UdpConnector` implements this, bounded datagram reception, socket cleanup and exception isolation.

```python
from ._base import UdpConnector
from ..model import LogicalUpdate

class Example(UdpConnector):
    id = "example_input"
    display_name = "Example UDP"
    default_config = {"host": "127.0.0.1", "port": 9001}
    config_schema = {"host": "string", "port": "integer"}

    def parse(self, packet):
        if packet != b"red":
            raise ValueError("Expected red")
        return (LogicalUpdate(("A",), (15, 0, 0)),)

CONNECTOR = Example()
```

Stop must release sockets and join threads before returning. Startup failure leaves the manager inactive; parser failure does not disconnect the bridge. Application shutdown closes the manager, then stops the scheduler and disconnects the bridge. UDP listeners accept traffic on their configured bind address; use loopback when only local software should send.

## LumaFlow and CuePilot

LumaFlow defaults to UDP 32712. Host framing is `EB 90 length command payload checksum ED`; length includes the command, checksum is the byte sum of length/command/payload. STREAM `D8` has exactly 20 bytes: ten big-endian function/RGB444 words. Function values 0–3 map to solid/slow/medium/fast and channels map to logical A–J. AUTH `E0` shape/timestamp ordering is validated and generates no control update; signatures are not verified and AUTH is not claimed as an authentication boundary. Multiple TLVs are parsed atomically, and malformed/truncated/checksum-invalid datagrams are rejected.

CuePilot defaults to UDP 9000. See [OSC schema](../lightstick-osc-schema.json). `/lightstick/zone` and `/lightstick/state` accept `siiis` (zone or comma-separated zones, R, G, B, effect); `/lightstick/all` accepts `iiis`; `/lightstick/blackout` takes no arguments. OSC integer channels are 0–15. Strings, types, padding and packet lengths are checked. Immediate bundles (timetag 1) are supported up to four nested levels; future timetags are rejected rather than executed early. The schema is a documented integration contract, not a claim of having tested CuePilot software itself.

## Validation evidence

Baseline: 82 existing Python tests passed before edits. New tests cover plugin discovery/isolation, golden waveform equivalence, capability errors, migration, failed state commit, scheduler interleavings, parser errors, UDP loopback, both built-in protocols, connector switch/port reuse, port conflicts and crash isolation. GUI startup and both protocol switches were exercised with a temporary state directory.

[Benchmark results](benchmark.json) contain 20 UDP-to-fake-Bridge scenarios: both connectors at 1/2/5/6/10 Hz, changing and repeated states, using a 785.75 ms simulated TX completion delay. All cases kept pending at 0–1 with no failed TX; repeated states transmitted once. These short runs demonstrate bounded scheduling, not long-duration hardware latency or reliability. USB/Wi-Fi/BLE hardware, physical stick behavior, live LumaFlow/CuePilot software and shorter D8 phases have not been tested in this migration.

Final local regression: **104 tests passed**, plus D8 and 00 CLI dry runs. Cross-platform CI is configured for Python 3.11/3.12 on Linux, macOS and Windows. Screenshot-based visual inspection was not performed.
