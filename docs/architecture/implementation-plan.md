没有新的必须讨论项，方案已经可以直接进入实施。我已经把完整计划同步到 ClickUp 的 `Lightstick-Lab → 协议插件化改造` 任务中。

当前实现也支持这个迁移方向：现有 CLI 现在仍承担 discovery → connect → GET_INFO → TX → state commit → disconnect 的完整生命周期，所以第一步应把这部分真正的业务执行能力沉到共享 Engine，而不是让实时 connector 长期 subprocess 调 CLI。 AutoDiscovery 当前确实会建立 transport 并验证 bridge 身份，也进一步说明长生命周期连接应由 Engine 持有。

## 最终目标架构

```text
                           Lightstick-Lab
                                 │
                  ┌──────────────┴──────────────┐
                  │                             │
             server/                       protocols/
          “从哪里接收”                     “怎么发出去”
                  │                             │
      ┌───────────┼───────────┐                 │
      │           │           │                 │
 CuePilot     LumaFlow      Future           00 / D8 / Future
   OSC          UDP        Connector            │
      │           │           │                 │
      └───────────┴───────────┘                 │
                  │                             │
                  ↓                             │
        LogicalState / Update                   │
                  │                             │
                  ↓                             │
              Scheduler                         │
          dedupe/latest-wins                    │
                  │                             │
                  └──────────────┬──────────────┘
                                 ↓
                         Protocol.build_plan()
                                 ↓
                         TransmissionPlan
                                 ↓
                              Engine
                                 ↓
                    USB / Wi-Fi / BLE Transport
                                 ↓
                              ESP32
                                 ↓
                             CC1101
```

### 1. 建立迁移基线

先不改行为，固定当前版本作为回归基准。

* 确认 GUI、CLI、USB、Wi-Fi、BLE、现有 00/D8 控制测试全部通过。
* 把现有协议、controller、state、CLI、transport、UI 测试作为迁移保护。
* 在新架构完全验收前不删除旧代码路径。
* 每一阶段独立提交，保证随时可回滚。

这一阶段的核心目标是：**重构可以改变内部结构，但不能偷偷改变 RF 行为。**

### 2. 建立协议无关的领域模型

这是后续两个插件系统共同依赖的基础。

建议增加：

```text
lightstick_demo/
    model.py
```

核心对象：

```python
LogicalState
LogicalUpdate
Capabilities
TransmissionPlan
```

`LogicalState` 表达的是：

```text
A = RGB + effect
B = RGB + effect
C = RGB + effect
...
```

它绝不能出现：

```text
00
C0
D8
checksum
OOK
CC1101
```

例如：

```python
LogicalUpdate(
    zones=("A", "B"),
    rgb=(15, 3, 8),
    effect="solid",
)
```

`TransmissionPlan` 则是协议插件输出给 Engine 的东西：

```text
LogicalState
     ↓
protocol_d8.py
     ↓
TransmissionPlan
```

可以表示一个或多个：

```text
TX_PULSES
TX_PACKET
```

以及必要的协议状态更新。

### 3. 完成 `protocols/` 插件化

延续刚才另一会话已经确定的方案。

建议结构：

```text
lightstick_demo/
    protocols/
        __init__.py
        _common.py
        protocol_00.py
        protocol_d8.py
        registry.py
```

用：

```text
pkgutil
importlib
```

扫描。

每个协议模块暴露统一：

```python
PROTOCOL
```

例如：

```python
PROTOCOL.id
PROTOCOL.display_name
PROTOCOL.capabilities
PROTOCOL.initial_state()
PROTOCOL.migrate_state(...)
PROTOCOL.build_plan(...)
```

以下划线开头的：

```text
_common.py
```

不注册。

首批显示名继续按之前决定：

```text
00 短命令
D8 RGB
```

不再出现 `2511T V2`。

GUI 不再写：

```python
if protocol == "D8":
...
elif protocol == "00":
...
```

而只读取：

```python
protocol.capabilities
```

决定：

* 可用区域
* 颜色方式
* effect
* 是否支持 Fade
* 是否支持保持状态
* 其他能力

状态迁移到：

```json
{
  "selected_protocol": "protocol_d8",
  "protocol_states": {
    "protocol_d8": {},
    "protocol_00": {}
  }
}
```

现有 `d8_slots` 自动迁入 D8 插件状态。

### 4. 抽出共享 Control Engine

这一阶段是 connector 能否正确落地的关键。

建议：

```text
lightstick_demo/
    engine.py
```

Engine 持有：

```text
Bridge connection
Transport
Current protocol
Protocol state
Logical state
Scheduler
TX state
```

生命周期大概为：

```python
engine.start()
engine.connect()
engine.submit_update(...)
engine.status()
engine.disconnect()
engine.stop()
```

目前 CLI 每次执行都会完整 discovery、连接、发送再断开。

改造后：

```text
CLI
 ↓
Engine
```

而不是：

```text
Engine
 ↓
CLI
```

GUI 同样：

```text
GUI
 ↓
Engine
```

这样最终：

```text
GUI ──────┐
CLI ──────┼→ Engine
Server ───┘
```

只保留一套业务逻辑。

CLI 继续存在，适合：

* 手工操作
* Shell
* 调试
* 单次 TX
* 自动化脚本
* 故障恢复

但实时 server 不再 subprocess 调它。

### 5. 实现统一 Scheduler

Scheduler 应该在 connector 和 protocol 之间，而不是写在 LumaFlow connector 里。

例如：

```text
server
  ↓
LogicalUpdate
  ↓
Scheduler
  ↓
Protocol
```

必须实现四条核心规则：

```text
状态级去重
单 RF TX
latest-state-wins
有限 pending
```

例如：

```text
A 正在发送

收到 B
收到 C
收到 D
收到 E
```

状态变为：

```text
running = A
pending = E
```

而不是：

```text
queue = B,C,D,E
```

A 完成：

```text
立即发送 E
```

同时记录：

```text
received
deduplicated
coalesced
unsupported
transmitted
failed
```

这对 CuePilot 和 LumaFlow完全一样。

### 6. 建立 `server/` 插件框架

建议：

```text
lightstick_demo/
    server/
        __init__.py
        _base.py
        registry.py
        manager.py
        cuepilot.py
        lumaflow.py
```

和 `protocols/` 一样动态扫描。

每个模块暴露：

```python
CONNECTOR
```

统一接口建议：

```python
CONNECTOR.id
CONNECTOR.display_name
CONNECTOR.config_schema
CONNECTOR.default_config

CONNECTOR.start(context)
CONNECTOR.stop()
CONNECTOR.status()
```

其中 `context` 只给 connector 类似：

```python
context.submit_update(...)
context.status(...)
```

**绝不能暴露具体 D8 builder。**

所以禁止：

```python
lumaflow.py
    -> build_d8_frame()
```

必须：

```python
lumaflow.py
    -> LogicalUpdate(...)
```

然后 Engine 自己送到当前选中的 protocol。

### 7. 加入 ServerManager

它负责：

```text
发现插件
启动
停止
切换
异常隔离
状态汇报
```

切换逻辑：

```text
LumaFlow
   ↓ 用户选择 CuePilot

stop LumaFlow
确认 socket/thread 已释放
start CuePilot
```

如果 CuePilot 启动失败：

```text
显示错误
connector = inactive
```

不能影响：

```text
GUI
Bridge
Manual control
CLI
```

程序退出时同样必须：

```text
stop connector
stop scheduler
disconnect bridge
```

不能残留 UDP socket 或线程。

### 8. UI 增加“外部控制”选择

最终界面应该有两个彼此独立的选择：

```text
协议
[D8 RGB            ▼]

外部控制
[LumaFlow UDP      ▼]
```

外部控制选项来自：

```text
server registry
```

而不是 UI 硬编码。

例如安装：

```text
server/grandma3.py
```

重启应用后自动出现：

```text
外部控制
[grandMA3           ▼]
```

协议同理。

建议同时显示：

```text
外部控制：LumaFlow UDP
状态：● 正在监听
地址：0.0.0.0:32712

Bridge：● Wi-Fi 已连接
协议：D8 RGB
TX：发送中
Pending：1

Received: 1830
Deduplicated: 1097
Coalesced: 422
TX: 311
Failed: 0
```

选中项和 connector 配置持久化。

### 9. 首先实现 LumaFlow Connector

因为它的协议现在最明确。

实现：

```text
server/lumaflow.py
```

默认监听：

```text
UDP 32712
```

解析：

```text
EB 90
TLV
E0 AUTH
D8 STREAM
20-byte payload
```

LumaFlow 当前 STREAM 为十通道，每通道是 `function + RGB444`。之前检查源码已经确认它通过 Serial/BLE/UDP 输出，UDP 固定 32712。

映射：

```text
ch0 → A
ch1 → B
...
ch9 → J
```

但只是逻辑区域。

之后由协议 capabilities 判断：

```text
D8 RGB → A-I
00 → A-P
其他协议 → 自己声明
```

如果选 D8 而 LumaFlow 的 J 有有效内容：

```text
unsupported_zone = J
```

必须显式统计/提示。

不能静默丢掉。

LumaFlow 会进行 UDP STREAM 重复发送，所以 connector 第一层还要做 packet/state dedupe。

### 10. 再实现 CuePilot OSC Connector

新增：

```text
server/cuepilot.py
```

并提供：

```text
Lightstick-Lab OSC Schema JSON
```

OSC namespace 应当保持协议无关，例如：

```text
/lightstick/zone
/lightstick/state
/lightstick/blackout
/lightstick/all
```

参数表达：

```text
zone
R
G
B
effect
```

而绝不要设计：

```text
/lightstick/d8
/lightstick/00
```

否则会破坏 connector / protocol 解耦。

CuePilot：

```text
OSC
 ↓
cuepilot.py
 ↓
LogicalUpdate
```

到这里就结束它的职责。

### 11. 处理协议能力不匹配

不要让每个 connector 自己猜。

例如：

```text
LogicalState:
J = RGB(15,8,3), fast
```

如果当前协议：

```text
D8 RGB
zones = A-I
```

应由核心能力校验统一决定：

```text
J unsupported
```

同样，如果某协议只有 16 色：

```text
RGB444
 ↓
palette mapping
```

这种转换应该属于协议插件。

不是 LumaFlow connector。

这点非常重要：

> **connector 保证输入语义正确；protocol 保证输出能力正确。**

### 12. 实时性能测试

完成共享 Engine 后再测真正值得测的数据：

```text
网络包到达
↓
connector parse
↓
LogicalUpdate
↓
scheduler
↓
protocol build
↓
bridge submit
↓
RF complete
```

至少模拟：

```text
1 Hz
2 Hz
5 Hz
6 Hz
10 Hz
```

测试两种情况：

```text
状态每帧都变化
状态大量重复
```

验证：

```text
不会无限积压
不会随着运行时间越来越延迟
busy 时只保存最新 pending
重复状态不重复 TX
```

CuePilot 和 LumaFlow使用**同一套压力模型**，不再假定其中哪个天然更低频。

### 13. D8 实时模式单独实验

生产默认继续：

```text
2,1,0,2,1,0
```

也就是目前已经验证的六 phase。

不要为了 connector 直接修改协议行为。

之后另开实验：

```text
Realtime D8 test
```

验证：

```text
2,1,0
```

是否已经足够可靠。

只有实际棒测试通过，才可以考虑加入协议选项，例如：

```text
D8 transmission mode
Reliable
Realtime
```

单 phase 更不能在未验证前默认使用。

这样“实时优化”仍然属于：

```text
protocol_d8.py
```

而不是 LumaFlow/CuePilot connector。

### 14. 测试矩阵

最终至少覆盖：

```text
Protocol Registry
Server Registry
LogicalState
Capabilities
TransmissionPlan
Scheduler
Engine lifecycle
State migration
00 plugin
D8 plugin
LumaFlow parser
LumaFlow UDP loopback
CuePilot OSC parser
CuePilot UDP loopback
Server switching
Port conflict
Malformed packets
Connector crash isolation
Application shutdown
USB
Wi-Fi
BLE
CLI regression
GUI regression
```

集成测试重点验证：

```text
fake network input
        ↓
connector
        ↓
LogicalUpdate
        ↓
scheduler
        ↓
protocol
        ↓
TransmissionPlan
        ↓
fake transport
```

这条完整链路。

### 15. 推荐提交顺序

为了避免一次 PR 改得不可审，建议严格按下面拆：

```text
A. LogicalState / Capabilities / TransmissionPlan
B. protocols registry
C. 00/D8 plugin migration
D. shared Engine
E. CLI + GUI migrate to Engine
F. Scheduler
G. server registry + ServerManager
H. UI external-control selector
I. LumaFlow connector
J. CuePilot connector + OSC Schema
K. observability + performance benchmark
L. docs + final regression cleanup
```

其中 A–H 都不依赖外部软件。

也就是说，在写第一行 LumaFlow/CuePilot 特定代码以前，**Lightstick-Lab 自己的插件和核心架构就应该已经稳定。**

---

## 最终验收标准

完成后应当满足：

```text
放入 protocols/foo.py
→ 不修改 UI
→ foo 自动出现在协议列表

放入 server/bar.py
→ 不修改 UI
→ bar 自动出现在外部控制列表
```

并且：

```text
CuePilot ─┐
          ├→ LogicalState → 任意协议
LumaFlow ─┘
```

以及反过来：

```text
任意 Connector
      ↓
LogicalState
      ↓
00 / D8 / Future Protocol
```

整个系统最终不存在：

```text
CuePilotD8Connector
LumaFlow00Connector
LumaFlowD8Connector
```

这种组合爆炸。

最重要的四条架构验收条件是：

**Connector 不知道空口协议；Protocol 不知道 CuePilot/LumaFlow；Engine 不包含某个具体协议的特殊分支；ESP32 不知道外部软件和具体应援棒协议。**

这样 Lightstick-Lab 才真正从目前的“控制一套已研究协议的工具”升级成一个**可扩展的应援棒控制 Bridge 平台**。完整十阶段版本和验收项已经写入 ClickUp 任务 `86eyvu4fq`。
