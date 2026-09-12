# Third-Party Notices

This project is licensed under the GNU General Public License version 3
(SPDX: `GPL-3.0-only`); see [LICENSE](LICENSE). The following third-party
components are used by the software in this repository.

## Python client

| Component | Purpose | License |
| --- | --- | --- |
| [bleak](https://github.com/hbldh/bleak) | BLE transport | MIT |
| [pyserial](https://github.com/pyserial/pyserial) | USB serial transport | BSD-3-Clause |
| [requests](https://pypi.org/project/requests/) | Wi-Fi HTTP transport | Apache-2.0 |

## ESP32 firmware

| Component | Purpose | License |
| --- | --- | --- |
| [ArduinoJson](https://github.com/bblanchon/ArduinoJson) | JSON parsing and serialization | MIT |
| [SmartRC-CC1101-Driver-Lib](https://github.com/LSatan/SmartRC-CC1101-Driver-Lib) | CC1101 driver | MIT |
| [usb-host-msc](https://github.com/firechip/espressif32-lib-usb-host-msc) | FAT32 USB mass storage recording | Apache-2.0 |

The PlatformIO build also obtains these toolchain components. They are build
dependencies rather than source files copied into this repository:

| Component | Purpose | License |
| --- | --- | --- |
| [PlatformIO Core](https://github.com/platformio/platformio-core) | Build and upload tooling | Apache-2.0 |
| [platform-espressif32](https://github.com/platformio/platform-espressif32) | PlatformIO ESP32 platform package | Apache-2.0 |
| [Arduino-ESP32](https://github.com/espressif/arduino-esp32) | Arduino framework for ESP32 | LGPL-2.1-or-later |
| [ESP-IDF](https://github.com/espressif/esp-idf) | Espressif low-level SDK used by the framework | Apache-2.0 |

The firmware is built with [PlatformIO](https://platformio.org/) and the
Espressif Arduino/ESP-IDF toolchain. The table above records the top-level
licenses; bundled subcomponents may carry their own notices, which are retained
by the respective upstream distributions.

## Independent implementation notice

The air-interface protocol documented in this repository was produced by this
project's own RF capture, analysis, and hardware tests. This project is an
independent implementation and is not affiliated with, endorsed by, or
sponsored by the venues, artists, event organizers, or game publishers
associated with the compatible lightsticks. LumaFlow was consulted only as
public background material; it is not a build dependency or a source of copied
code/data here. This repository contains no LumaFlow source code, CSV samples,
or other copied implementation material. The Python connector independently
implements the documented EB 90 TLV host input for interoperability; AUTH
messages are validated structurally and are not an authentication boundary.
