# Lightstick Lab

Lightstick Lab 是一个用于控制特定应援棒的跨平台控制工具。它由两部分组成：

- ESP32-S3 桥接固件：通过 CC1101 收发 433 MHz 应援棒空口信号，支持 USB Serial、BLE 与 Wi-Fi HTTP 三种主机接口。**固件中未包含任何协议。**
- Python 客户端：提供 Tkinter 图形界面的 `应援棒` 与 `ESP32` 两个控制页，也提供无界面 CLI 与编程调用接口。

本项目是独立实现的协议研究工具与控制软件，仅供实验和学习使用。请遵守所在地的无线电法规，并在低功率、近距离条件下验证。
请参见免责声明部分。

## 兼容性

| 应援棒 | 结果 |
| --- | --- |
| 2026 洛天依纯蓝幻乐演唱会应援棒 | 已实测可以控制 |
| 2023 原神音乐会应援棒 | 已实测可以控制 |
| 洛天依环球生日会应援棒 | 不能使用本项目控制 |
| 其他应援棒 | 可能可以控制，但尚未验证 |

不同应援棒即使外观相同，也可能使用不同的频率、校验、分区或指令。反之，不同应援棒即使外观和活动完全不同，也可能使用相同的指令控制。

以上型号是我个人测试过的。**仅作举例。项目本身不针对任何艺人，活动或应援棒型号。** 欢迎PR以扩充。
对其他应援棒是否有效、对频率与功率是否合规，均不做任何保证。

## 功能

- 应援棒控制：按协议操作颜色、常亮/慢闪/中闪/快闪、Fade in/out、保持状态、分区变色、全局短脉冲与实体按键解锁。
- 多区布局：支持 A-P 十六分区掩码与 D8 九槽连续事务。
- D8 连续事务预览：可生成并查看 D8 六帧连续事务及空口波形参数。
- ESP32 桥接：扫描并连接 USB Serial、BLE 或 Wi-Fi；读取状态、参数、Profile 与录制数据。
- BLE / USB / Wi-Fi 配网：向板内写入 Wi-Fi 配置并回传 DHCP IP。
- 客户端录制：通过 USB/BLE/Wi-Fi 持续读取 ESP32 捕获的空口数据并落盘为 LSR1 文件。
- CLI：`python3 app.py --zone A --rgb 66CCFF --effect solid` 免手动连接自动尝试 USB、Wi-Fi 与 BLE。

## 目录结构

```text
firmware/  ESP32-S3 N16R8 桥接固件 0.5.2 (`GPL-3.0-only`)
python/    Lightstick Lab Tkinter GUI、CLI 与测试 0.4.0
docs/      应援棒空口协议与 ESP32 JSON 控制 API
.github/   GitHub Actions CI
LICENSE    GNU General Public License v3.0
```

## 所需硬件

| 部件 | 说明 |
| --- | --- |
| 控制板 | ESP32-S3 DevKitC-1 N16R8（16 MB Flash、8 MB PSRAM） |
| 射频模块 | CC1101 模块（433 MHz 频段） |
| 主机 | 支持 Python 3.10+ 的 macOS / Windows / Linux 电脑 |

### CC1101 接线

CC1101 使用 3.3 V 逻辑与供电，不能直接接 5 V。ESP32-S3 与 CC1101 必须共地；若外部供电，额外电源的地线必须与 ESP32 地线连接。

| CC1101 引脚 | ESP32-S3 GPIO |
| --- | --- |
| SCK | GPIO4 |
| MOSI (SI) | GPIO5 |
| MISO (SO) | GPIO6 |
| CS (CSN) | GPIO7 |
| GDO0 | GPIO15 |
| GDO2 | GPIO16 |
| VCC | 3.3 V |
| GND | 公共地 |

接线完成后可通过 USB 连接并发送 `GET_INFO`，返回中的引脚与 CC1101 检测结果可确认接线正确。

## 固件

目标板为 ESP32-S3 DevKitC-1 N16R8（16 MB Flash、8 MB PSRAM）。固件不固定空口协议，它只按上位机 JSON 指令操作 CC1101，并通过 USB Serial、BLE 或 Wi-Fi HTTP 返回结果。接收参数、录制参数与信号 Profile 持久化在 ESP32 NVS，客户端录制数据可由上位机持续读取。

### 构建与烧录

安装 PlatformIO 后，在项目根目录执行：

```bash
pio run -d firmware
pio device list
pio run -d firmware -t upload
```

发布源码中不包含任何预置 Wi-Fi 名称或密码。普通烧录不会主动擦除 NVS，因此板内已有的网络配置通常会被保留；全新或已擦除的板需要通过 BLE 或 USB Serial 配网。

### 供电

ESP32-S3、CC1101 与 USB 外设同时工作时，实际电流取决于模块与发射功率。若出现复位、USB 枚举不稳或发射异常，可能需要额外供电；额外电源必须与 ESP32 共地。

## Python 客户端

Python 3.10 或更高版本：

```bash
cd python
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 app.py
```

Windows 激活虚拟环境可使用 `.venv\Scripts\activate`，Linux 发行版可能需要另行安装 Tkinter 系统包。图形界面支持应援棒控制、ESP32 状态/参数/Profile、BLE Wi-Fi 配网与客户端录制。

### 无界面 CLI

```bash
python3 app.py --zone A --rgb 66CCFF --effect solid
```

该命令会先尝试上次成功的连接，再自动尝试 USB、Wi-Fi 与 BLE；每个候选设备都必须通过 ESP32-S3 N16R8/CC1101 身份检查。连接结果与 D8 槽状态保存在当前用户的应用状态目录。

只生成并查看将要发射的 D8 连续事务、不连接硬件：

```bash
python3 app.py --zone A --rgb 66CCFF --effect solid --dry-run --json
```

常用可选参数包括 `--transport auto|usb|wifi|ble`、`--frequency`、`--power`、`--effect solid|slow|medium|fast`、`--json` 与 `--dry-run`。完整参数可用 `python3 app.py --help` 查看。

### 测试

```bash
cd python
python3 -m unittest discover -s tests -v
```

CI 同时覆盖 Python 单测与固件构建，见 `.github/workflows/ci.yml`。

## Wi-Fi 配网

推荐在 GUI 的 `ESP32` 页扫描并连接 BLE，再在 Wi-Fi 弹窗中填写网络配置。也可通过 USB Serial 或 BLE 发送同一 JSON 命令：

```json
{"id":"wifi-1","cmd":"SET_WIFI_CONFIG","args":{"ssid":"<WIFI_NAME>","password":"<WIFI_PASSWORD>"}}
```

随后轮询 `GET_NETWORK_STATUS`；连接成功后响应会返回 DHCP IP。网络配置保存在板内 NVS。未连上 STA 约 30 秒后，固件会开启无密码的 `Lightstick-Recovery` 恢复热点。

HTTP 接口没有 TLS 或应用层鉴权，恢复热点也是开放网络。完成配网后请仅在可信局域网使用，不要把设备的 80 端口暴露到互联网。

## 协议说明

应援棒控制的帧结构、校验、分区、颜色、效果与 D8 槽位推测见 [docs/2511t-v2-air-protocol-control.md](docs/2511t-v2-air-protocol-control.md)。其中 `W0` 约等于 A 区是当前实物支持的强候选，`W1..W8` 约等于 B-I 仍是假设；程序没有把未验证的 K-P 或 D8 映射当成确定事实。

ESP32 三种传输共用的请求、响应、录制与 Profile 接口见 [docs/receiver-control-api.md](docs/receiver-control-api.md)。

`TX_PULSES` 的 `success` 只表示 ESP32/CC1101 已完成波形发射。应援棒没有 ACK，因此它不表示目标棒一定接收或执行了命令。

## 安全与免责声明

- 本项目仅用于实验学习，不构成对任何演唱会、艺人、活动组织方或发行方的授权或认可。
- 项目中包含的协议、连接器等仅作举例，起一个抛砖引玉的作用。项目自身不针对任何应援棒、软件。
- 使用本项目的任何部分前，请自行确认当地法规允许的频率、功率与使用场景。勿在未获授权的情况下干扰/控制他人活动和演出。
- 因使用者自身的不合理使用造成的一切后果由用户自行承担。
- 目标应援棒没有数据回传 ACK，干扰、距离、天线方向等因素均可能影响实际效果。

## 致谢
感谢以下文章和项目：

[《可遥控应援棒的控制信号》](https://www.bilibili.com/opus/980740947264405513)

[LumaFlow](https://github.com/ltyridium/LumaFlow) 

## 许可证

本项目以 [GNU General Public License v3.0](LICENSE) 发布（SPDX: `GPL-3.0-only`）。

[![License: GPL-3.0-only](https://img.shields.io/badge/License-GPL--3.0--only-blue.svg)](LICENSE)
