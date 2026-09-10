# CuePilot OSC

导入文件：[`lightstick-cuepilot-schema.json`](lightstick-cuepilot-schema.json)。`lightstick-osc-schema.json` 是接口说明，不用于 CuePilot 导入。

> CuePilot 的 OSC 系统仅对 **MINI、PRO 或 MAX** 项目开放。SOLO 项目虽然可以导入 Schema、创建 OSC 轨道和 Cue，也可以把 Output 设为 Active，但播放时不会发送 OSC 数据。可在 **Settings → System → OSC** 检查：如果显示 “Available with MINI, PRO and MAX Projects”，需要先改用支持 OSC 的项目。

1. 在 CuePilot 项目的 **OSC Schemas** 页面点击 **+**，填写 Label（例如 Lightstick Lab）。
2. 选择 **Import Custom OSC Schema from File**，选择上述文件，再点击 **Save**。
3. 配置 OSC 输出目标：同机使用 `127.0.0.1`，跨机器使用运行 Lightstick Lab 的电脑局域网 IP；UDP 端口 `9000`。
4. 在 Lightstick Lab 的「外部控制」标签页启动 CuePilot OSC，并连接硬件、选择正确协议。在 CuePilot 的 OSC Cue 中选择此 Schema 和所需命令。
5. 在 CuePilot 的 OSC 轨道设置中选择已启用的输出（例如 Output 1），把播放头移到 Cue 之前，再开始播放；只有播放头经过 Cue 的起点时才会触发该颜色。

| 命令 | 参数 |
| --- | --- |
| Single Zone | 单个分区，如 `A`；R、G、B；effect |
| Multiple Zones | 英文逗号分隔分区，如 `A,B,C`；R、G、B；effect |
| All Zones | R、G、B；effect，作用于当前协议全部分区 |
| Blackout | 无参数，关闭当前协议全部分区 |

RGB 为 **0–15 的整数**。例如红色常亮：R=`15`、G=`0`、B=`0`、effect=`solid`。默认 RGB 为零，测试时需要设置非零颜色。

效果名称：`solid`（常亮）、`slow`、`medium`、`fast`（闪烁）、`off`（关闭）、`fade_in`（渐亮）、`fade_out`（渐暗）、`hold`（保持）。00 支持 A–P 及全部效果；D8 支持 A–I，效果仅支持 `solid/slow/medium/fast/off`。

格式依据：CuePilot 内置 OSC Schema 的 `integer`/`string` 参数定义，以及 [公开自定义 Schema 导入示例](https://github.com/MikOfClassX/LiveBoard-CuePilot-schema)。已验证配置对应的 OSC 数据能被 Lightstick Lab 解析；尚未在 CuePilot 界面实测导入或进行硬件联调。
