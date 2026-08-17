# Lightstick firmware 0.5.2

SPDX-License-Identifier: GPL-3.0-only
Copyright (C) 2026 lightstick-control contributors

This documentation is part of the Lightstick Lab project and is distributed
under the GNU General Public License v3.0.

This firmware targets the ESP32-S3 DevKitC-1 N16R8 profile in `platformio.ini`.
It is a generic transparent JSON bridge for the CC1101, uses Wi-Fi STA as the
primary host interface, and supports direct bounded TX/RX operations plus two
recording targets:

- `START_RECORDING` writes LSR1 segments to a FAT32 USB mass-storage device.
- `START_CLIENT_RECORDING` keeps the same edge records in the 4 MiB PSRAM ring
  for a Wi-Fi client to persist, such as an Android SAF directory on an SD card.

The client target does not require USB Host enumeration. It is intended for any
of the UART, BLE, or Wi-Fi HTTP transports, which use the same JSON command
objects and responses. BLE is enabled in 0.5.2.

## Network interface

This public source copy contains no preconfigured Wi-Fi credentials. Existing
credentials in NVS are retained across a normal firmware upload; a new or erased
board should be provisioned over BLE or UART with `SET_WIFI_CONFIG`. The command
persists the supplied network configuration, starts a non-blocking association,
and returns the current status and IP. `CONNECT_WIFI` retries the persisted
configuration and returns the same status object. Use mDNS name
`lightstick-n16r8.local` or send
`LIGHTSTICK_DISCOVER` to UDP port `4210` to find the DHCP address. The HTTP
endpoint is `POST /api/command`; the convenience endpoints are `/health`,
`/api/info`, `/api/status`, and `/api/commands`. If STA association fails for
about 30 seconds, the board enables the open recovery AP
`Lightstick-Recovery`. Provision Wi-Fi immediately and do not expose the HTTP
service outside a trusted local network.

`GET_COMMANDS` reports API version `0.5.2`, the complete command list, recording
limits, `ble_enabled: true`, and the UART/BLE/HTTP transport description.

TX `repeat` is consistently bounded to `1..10` across profiles, `TX_PACKET`,
`TX_PULSES`, and the macOS client. `TX_PULSES` is asynchronous: its immediate
response is `status: "pending"` plus a `request_id`. Poll `GET_TX_RESULT` with
that ID over the same UART, BLE, or HTTP transport until it returns `pending`,
`success`, or `error`; a `tx_busy: false` status alone is not a success result.
The ESP32-S3 RMT provides the pulse timing, and `success` means the RMT waveform
has completed, not merely that the task loop ended.
The firmware validates the complete CC1101 frequency bands (300--348,
378--464, or 779--928 MHz) and rejects unsupported frequencies without
silently retuning them.

## Client recording contract

`START_CLIENT_RECORDING` accepts optional `name`, `utc_epoch_ms`, `receiver`, and
`recording` arguments. Its
result contains `session_id`, `recording_name`, `header_base64`, and the fixed LSR1
format values: version 1, a 40-byte header, and 16-byte records. Write the decoded
header to `recording_name_000.lsr` before any record data.

`READ_CLIENT_RECORDING` accepts:

```json
{"session_id":"client-...","ack_sequence":0,"max_records":512}
```

Each response contains `sequence_start`, `sequence_end`, `offset_bytes`,
`next_offset_bytes`, `record_count`, `data_crc32`, and `data_base64`. The first request uses
`ack_sequence: 0`. After the client has durably written a response at
`offset_bytes`, it sends that response's `sequence_end` as the next acknowledgement.
The firmware then removes the prior chunk and returns the next one. Retrying with
the prior acknowledgement returns the same unacknowledged chunk.

`STOP_CLIENT_RECORDING` requires the same `session_id`. It disables the GDO0 ISR,
drains the ISR queue, and leaves the PSRAM tail available for further reads. Continue
reading until a response has `active: false` and `end_of_stream: true`; that final
empty response confirms that the last data chunk was acknowledged.

Neither recording-start command replaces a client session with pending or buffered
records. Resume `READ_CLIENT_RECORDING` using the reported session ID until EOS
before starting another USB or client recording.

`GET_STATUS` reports the active target plus client session, acknowledged, pending,
and buffered record counters. `ABORT` also stops an active client recording while
preserving its unread tail for recovery and reports that session ID.

## Configuration

`SET_RX_CONFIG`, `SET_RECORDING_CONFIG`, and `SET_CAPTURE_CONFIG` persist settings
in NVS. `SET_WIFI_CONFIG` persists network credentials without waiting for
association. Commands that apply receiver settings calibrate the CC1101 before
returning; a recording-only update does not touch the radio. A nested `receiver`
or `recording` object passed to a recording-start command overrides settings for
that session only and does not modify NVS. All configuration changes are rejected
while recording is active. The complete field list and limits are in
`docs/receiver-control-api.md`.

## Signal profiles

`SET_PROFILE`, `GET_PROFILE`, `LIST_PROFILES`, and `DELETE_PROFILE` manage up to
16 named profiles in NVS. A profile can contain pulse durations, packet bytes,
frequency, power, repeat/gap settings, receiver settings, and recording settings.
Pass `profile` or `profile_name` to `TX_PULSES`, `TX_PACKET`, `RX_PACKET`,
`RX_PULSES`, `START_RECORDING`, or `START_CLIENT_RECORDING` to apply the saved
values; explicit command fields override the profile. Profile receiver and
recording settings are applied to the corresponding RX or recording operation.

## RF preparation

The persisted receive default is 433.920 MHz, ASK/OOK, 203 kHz bandwidth, and
4.8 kbaud. `CC_SELF_TEST` resets and detects the CC1101, reapplies the saved receive
configuration, performs an explicit `SCAL` calibration, and leaves the radio idle.
Its result reports `PARTNUM`, `VERSION`, calibration state, and the final MARCSTATE.

`SET_RX_CONFIG`, `START_RECORDING`, and `START_CLIENT_RECORDING` also calibrate
before entering receive mode. `TX_PACKET`, `TX_PULSES`, and a `STROBE` value of
`STX` execute directly when within their documented physical bounds.
