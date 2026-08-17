# Lightstick N16R8 control API (firmware 0.5.2)

SPDX-License-Identifier: GPL-3.0-only
Copyright (C) 2026 lightstick-control contributors

This documentation is part of the Lightstick Lab project and is distributed
under the GNU General Public License v3.0.

The ESP32-S3 exposes one generic transparent JSON command API over UART, BLE,
and Wi-Fi HTTP. The same command names, argument objects, and response objects
are used on all three transports; UART and BLE are newline-framed while HTTP
uses the same JSON objects in the request body and response. BLE is enabled and uses the service and
characteristic UUIDs returned by `GET_INFO`.

## Connections

- UART: 921600 baud, one UTF-8 JSON object per line. This is the maintenance and
  recovery transport.
- Wi-Fi STA: this public source copy contains no preconfigured network
  credentials. `SET_WIFI_CONFIG` persists a user-supplied network configuration
  and starts a non-blocking association. `CONNECT_WIFI` retries the persisted
  configuration. Both return the current status and IP; poll
  `GET_NETWORK_STATUS` while association is in progress. The address is
  DHCP-assigned; use `lightstick-n16r8.local` or UDP discovery rather than
  hard-coding it.
- HTTP: `POST /api/command`; `GET /health`, `/api/info`, `/api/status`, and
  `/api/commands` are convenience endpoints. CORS is enabled for local clients.
- UDP discovery: send the ASCII request `LIGHTSTICK_DISCOVER` to port `4210`.
  The response is a JSON object containing `ip`, `hostname`, `mdns`, `http_port`,
  and `api_path`.
- BLE: write newline-delimited JSON requests to the command characteristic and
  subscribe to newline-delimited JSON responses on the response characteristic.
  Long responses may be split into notification chunks and reassembled at the
  newline boundary.
- Recovery: after about 30 seconds without STA association, the firmware enables
  the open AP `Lightstick-Recovery` while continuing to retry a configured STA.

The HTTP service has no application-level authentication or TLS. Access control is
provided only by the local Wi-Fi network, so do not expose port 80 outside the
trusted hotspot. The firmware serializes command execution; clients should keep one
recording transaction per board and use unique request IDs.

Request:

```json
{"id":"42","cmd":"GET_STATUS","args":{}}
```

Success and error responses:

```json
{"id":"42","ok":true,"result":{}}
{"id":"42","ok":false,"error":"message"}
```

## Commands

| Command | Main arguments | Purpose |
| --- | --- | --- |
| `GET_INFO` | none | Hardware, N16R8 memory, pins, network, CC1101 and storage information. |
| `GET_STATUS` | none | Live radio, receiver, recording and USB status. |
| `GET_COMMANDS` | none | Return the complete command list and transport framing. |
| `GET_NETWORK_STATUS` | none | Return STA address, RSSI, mDNS name, discovery port and recovery state. |
| `SET_WIFI_CONFIG` | `ssid`, optional `password` | Persist credentials and begin a non-blocking STA connection. |
| `CONNECT_WIFI` | none | Retry the persisted credentials and return connection status/IP. |
| `GET_CAPTURE_CONFIG` | none | Return the persistent receiver and recording configuration. |
| `SET_RX_CONFIG` | `frequency_hz`, `rx_bandwidth_hz`, `modulation`, `data_rate_kbaud` | Persist and apply receiver settings. |
| `SET_RECORDING_CONFIG` | `edge_mode`, `min_edge_interval_us`, `max_duration_ms`, `max_edges`, `client_chunk_records`, `buffer_stop_threshold_percent`, `stop_on_buffer_overflow` | Persist recording settings. |
| `SET_CAPTURE_CONFIG` | `receiver`, `recording` | Persist both receiver and recording settings atomically from the client's perspective. |
| `START_RECORDING` | `name`, `utc_epoch_ms`, optional `profile`, `receiver`, `recording` | Start asynchronous GDO0 edge capture to the FAT32 USB drive. |
| `STOP_RECORDING` | none | Drain PSRAM, flush the file and finalize metadata. |
| `LIST_RECORDINGS` | none | List files in `/lightstick` on the USB drive. |
| `DELETE_RECORDING` | `name` | Delete one listed recording file. |
| `START_CLIENT_RECORDING` | `name`, `utc_epoch_ms`, optional `profile`, `receiver`, `recording` | Start GDO0 capture for an acknowledged client stream. Nested settings are session-only overrides. |
| `GET_CLIENT_RECORDING_INFO` | `session_id` | Return session metadata, counters, drop counts and EOS state. |
| `READ_CLIENT_RECORDING` | `session_id`, `ack_sequence`, `max_records` | Acknowledge the last durable block and read up to 512 LSR1 records. |
| `STOP_CLIENT_RECORDING` | `session_id` | Stop capture, preserve the unread PSRAM tail, and allow reads through EOS. |
| `USB_PORT_STATUS` | none | Return USB Host controller and line-state diagnostics. |
| `USB_SET_PINS_SWAP` | `swap` | Enable or disable the USB PHY D+/D- swap diagnostic. |
| `LIST_PROFILES` | none | List names and summaries of persistent signal profiles. |
| `GET_PROFILE` | `name` | Return one complete persistent signal profile. |
| `SET_PROFILE` | `name`, profile fields | Create or replace a persistent signal profile. |
| `DELETE_PROFILE` | `name` | Delete one persistent signal profile. |
| `CC_RESET` | none | Reset and re-detect CC1101. |
| `CC_SELF_TEST` | none | Reset, detect, apply RX configuration, run `SCAL`, and leave CC1101 idle without transmitting. |
| `REG_READ` | `address` | Read one CC1101 register or status register. |
| `REG_WRITE` | `address`, `value` | Write one CC1101 register. |
| `STROBE` | `value` | Send one CC1101 command strobe, including direct `STX` when required. |
| `FIFO_READ` | `length` | Read 1 to 64 bytes from CC1101 FIFO. |
| `FIFO_WRITE` | `data_hex` | Write 1 to 64 bytes to CC1101 FIFO. |
| `RX_PACKET` | `frequency_hz`, `timeout_ms`, `max_length`, optional `profile` | Perform one bounded packet-mode receive. |
| `RX_PULSES` | `frequency_hz`, `timeout_ms`, `max_edges`, optional `profile` | Perform one bounded in-memory pulse capture. |
| `TX_PACKET` | `data_hex`, `frequency_hz`, `power_dbm`, `repeat`, `gap_us`, optional `profile` | Send a bounded packet-mode signal directly. |
| `TX_PULSES` | `durations_us`, `start_level`, `frequency_hz`, `power_dbm`, `repeat`, `gap_us`, optional `profile` | Queue a bounded asynchronous OOK pulse signal with ESP32-S3 RMT hardware timing. |
| `GET_TX_RESULT` | `request_id` | Poll the cached final result of an asynchronous `TX_PULSES` request. |
| `ABORT` | none | Abort active TX or stop an active recording and idle CC1101. |

CC1101 modulation values are `0` for 2-FSK, `1` for GFSK, `2` for ASK/OOK,
`3` for 4-FSK, and `4` for MSK.

TX `repeat` is an integer from 1 through 10 on the firmware and Python client. TX and RX
frequency values must be inside one of the complete CC1101 bands listed below;
the firmware rejects an unsupported value and never clamps it into 387--464 MHz.
`TX_PULSES` returns immediately with `status: "pending"` and a `request_id`.
Poll `GET_TX_RESULT` with that ID until `status` is `pending`, `success`, or
`error`; `success` means the ESP32-S3 RMT waveform has completed, not merely
that the task loop ended. The same request/result objects are available over
UART, BLE, and HTTP.

## Recording targets

`START_RECORDING` writes directly to `/lightstick` on a mounted FAT32 USB mass
storage device. A recording has one JSON metadata file and one or more
`name_NNN.lsr` data segments. USB segments roll over at 1 GiB, below FAT32's
4 GiB single-file limit.

`START_CLIENT_RECORDING` does not require a USB device. It returns a session ID
and Base64 LSR1 header, then retains records in PSRAM for `READ_CLIENT_RECORDING`.
The client writes the 40-byte header followed by each decoded block and durably
syncs it before sending that block's `sequence_end` as the next `ack_sequence`.
A retry with the old acknowledgement returns the same pending block. Each block
also includes `offset_bytes`, `next_offset_bytes`, and a standard CRC-32/IEEE
`data_crc32`; clients should verify all three before acknowledging. After
`STOP_CLIENT_RECORDING`, the client continues reading until `active=false` and
`end_of_stream=true`.

The persistent settings are changed by `SET_RX_CONFIG`, `SET_RECORDING_CONFIG`, or
`SET_CAPTURE_CONFIG`. A `receiver` or `recording` object supplied to
`START_CLIENT_RECORDING` or `START_RECORDING` is a session snapshot and does not
change those persistent settings. Configuration changes are rejected while a
recording is active.

## Signal profiles

Profiles are named, persistent signal presets stored in NVS. `SET_PROFILE` accepts
`name`, `kind`, `frequency_hz`, `power_dbm`, `repeat`, `gap_us`, `start_level`,
`durations_us`, `data_hex`, an optional `receiver` object, and an optional
`recording` object. The limits match the corresponding TX, RX, and recording
commands. `GET_PROFILE` returns the complete preset; `LIST_PROFILES` returns
summaries; `DELETE_PROFILE` removes one name.

Pass `profile` (or `profile_name`) to `TX_PACKET`, `TX_PULSES`, `RX_PACKET`,
`RX_PULSES`, `START_RECORDING`, or `START_CLIENT_RECORDING`. The saved frequency,
packet/pulse data, TX bounds, receiver configuration, and recording configuration
are applied to the relevant operation. Explicit command fields override profile
values.

Recording-setting limits are: `edge_mode` = `both|rising|falling`,
`min_edge_interval_us` = 0..1,000,000, `max_duration_ms` = 0..86,400,000 (zero
means unlimited), `max_edges` = 0 for unlimited or any positive 64-bit count,
`client_chunk_records` = 1..512, and `buffer_stop_threshold_percent` = 50..95.
Receiver limits are CC1101 bands 300--348, 378--464, or 779--928 MHz,
`rx_bandwidth_hz` 58,000..812,500, modulation 0..4, and data rate 0.6..500
kbaud.


## Recording format

Each `.lsr` segment begins with an `LSR1` binary header. Every following record
is 16 bytes, little-endian:

| Offset | Size | Field |
| --- | ---: | --- |
| 0 | 8 | Microseconds since recording start (`uint64`). |
| 8 | 1 | GDO0 logic level after the edge. |
| 9 | 1 | Flags, currently zero. |
| 10 | 6 | Reserved, currently zero. |

The firmware first queues edges in internal RAM, moves them to a 4 MiB ring in
the N16R8's 8 MB Octal PSRAM, and writes batches to USB from a separate task.
Metadata reports ISR and PSRAM drop counters so a recording can be checked for
completeness.

## 433.92 MHz capture setup

`CC_SELF_TEST` is the non-transmitting preflight for the receiver. A passing result
requires `PARTNUM=0`, the expected `VERSION=20`, successful `SCAL`, and idle
MARCSTATE. `SET_RX_CONFIG`, `START_RECORDING`, and `START_CLIENT_RECORDING` also
calibrate immediately before entering RX. TX commands and direct `STROBE`
operations are bounded by the physical limits in this API and do not require a
separate authorization command.

For the currently connected RTL2832U/FC0013, keep tuner correction at `ppm=0` when
capturing unknown transmitters. The measured offset against this CC1101 is a
combined two-oscillator error and must not be stored as an absolute RTL calibration.
Use a 433.700 MHz center, 2.048 MS/s, and 19.7 dB tuner gain for initial 433.92 MHz
captures so the target is away from the RTL-SDR DC spur while remaining in band.

## HTTP examples

Discover a board from a Windows or other client:

```text
UDP <subnet-broadcast>:4210 -> LIGHTSTICK_DISCOVER
HTTP POST http://<ip>/api/command
{"id":"1","cmd":"GET_NETWORK_STATUS","args":{}}
```

Start a bounded client recording with session-only settings:

```json
{
  "id": "capture-1",
  "cmd": "START_CLIENT_RECORDING",
  "args": {
    "name": "unknown_433920",
    "receiver": {"frequency_hz": 433920000, "modulation": 2},
    "recording": {"max_duration_ms": 60000, "client_chunk_records": 512}
  }
}
```

Read the first block with `ack_sequence: 0`. Persist and verify the returned
Base64 bytes and CRC, then use its `sequence_end` as the next acknowledgement:

```json
{
  "id": "capture-2",
  "cmd": "READ_CLIENT_RECORDING",
  "args": {"session_id": "client-...", "ack_sequence": 0, "max_records": 512}
}
```
