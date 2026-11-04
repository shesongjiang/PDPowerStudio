# PDPower Studio MCP 接入说明（stdio）

本文档对应 v0.1.2 的 `pdpower-mcp.exe`。MCP server 走 **stdio JSON-RPC**（协议版本 `2024-11-05`），
不需要监听端口，也不会弹出窗口。

## 1. 可执行文件路径

目录版（推荐）：

```
<解压目录>\pdpower-mcp\pdpower-mcp.exe
```

同目录还有 `PDPowerStudio.exe`（GUI）与 `pdpower-cli\pdpower-cli.exe`（CLI）。

> 如果换了安装位置，把下文所有路径替换成你实际的 `pdpower-mcp.exe` 绝对路径即可。
> 不要把路径写成相对路径；部分客户端的工作目录不可预期。

## 2. 启动命令与参数

```powershell
# 默认：只读模式（唯一安全的默认）
& "<解压目录>\pdpower-mcp\pdpower-mcp.exe"

# 允许写操作（只有需要 set_voltage_current / output_enable 等工具时才加）
& "<解压目录>\pdpower-mcp\pdpower-mcp.exe" --allow-write
```

- 不带参数等价于 `mcp` 子命令。
- 所有运行日志都写 **stderr**（客户端看不到也不影响协议）；stdout 只输出 JSON-RPC 响应。
- `initialize` 返回 `serverInfo = {"name": "pdpower-studio", "version": "0.1.2"}`。

## 3. 客户端配置示例

### Trae / VSCode 风格（`.trae/mcp.json` 或 `settings.json` 的 `mcpServers`）

```json
{
  "mcpServers": {
    "pdpower": {
      "command": "<解压目录>\\pdpower-mcp\\pdpower-mcp.exe",
      "args": ["--allow-write"],
      "env": {}
    }
  }
}
```

只读用法：把 `args` 改成 `[]` 或直接删掉。也可以把同一个 exe 配成两个条目，一个只读、一个可写，
但同一时刻只能连接一个（见第 5 节端口锁）。

### Claude Desktop（`claude_desktop_config.json`）

```json
{
  "mcpServers": {
    "pdpower": {
      "command": "<解压目录>\\pdpower-mcp\\pdpower-mcp.exe",
      "args": []
    }
  }
}
```

Windows 路径在 JSON 里必须写成双反斜杠 `\\`，或改用正斜杠 `/`。

## 4. 13 个工具与参数

| # | 工具 | 作用 | 参数（`*` 为必填） |
|---|------|------|--------------------|
| 1 | `list_devices` | 列出 VID `1A86` / PID `FE0C` 的 PDPower 串口 | 无 |
| 2 | `connect` | 独占打开串口并只读握手 | `port`*、`read_only`（默认 false；进程只读时强制 true） |
| 3 | `disconnect` | 关闭串口、停止日志、释放端口锁 | 无 |
| 4 | `get_device_info` | 型号 / SN / 版本 / 能力位 | 无 |
| 5 | `get_status` | 输出状态、显示值、M0–M4 组设置、输入状态 | 无 |
| 6 | `set_voltage_current` | 设置当前组（或指定组）电压电流 | `voltage_v`*、`current_a`*、`group`(0–4)、`confirm` |
| 7 | `set_output` | `set_voltage_current` 的别名（兼容旧调用） | `voltage_v`*、`current_a`*、`confirm` |
| 8 | `set_group` | 切换记忆组 M0–M4 | `group`*(0–4)、`confirm` |
| 9 | `output_enable` | 开/关输出 | `enabled`*、`confirm`（开启时必须 `true`） |
| 10 | `save_config` | 保存配置到设备 NVM（0x44） | `confirm`*（必须 `true`） |
| 11 | `start_log` | 开始挂测记录（CSV/JSONL） | `directory`*、`format`(`csv`/`jsonl`)、`hz` |
| 12 | `stop_log` | 停止记录并 flush | 无 |
| 13 | `estimate_log_size` | 估算挂测体积 | `hz`*、`hours`*、`format` |

所有工具返回 `content: [{type: "text", text: "<JSON 字符串>"}]`；业务错误返回 `isError: true` 并在
`text` 里给出可读原因。协议级错误码：`-32700` 解析失败、`-32601` 未知方法、`-32602` 参数错误。

写入规则（MCP 侧也会再校验一次）：

- 进程只读（未加 `--allow-write`）时，6–10 全部拒绝。
- 电压/电流按设备能力位夹紧：V1 为 `0.5–20 V` / `0–3 A`。
- 输出开启时修改设定或切组，必须显式 `"confirm": true`。
- 每次写入成功后服务会回读 `0x84/0x83/0x82` 校验，不一致按错误返回。

## 5. 只读 / 可写切换与端口锁

- **只读**：`pdpower-mcp.exe`（默认）。`connect` 会强制 `read_only=true`，写工具直接报错。
- **可写**：`pdpower-mcp.exe --allow-write`。`connect` 时仍可传 `"read_only": true` 临时降级为只读。

端口锁文件位于：

```
%LOCALAPPDATA%\pdpower_studio\port_COM48.lock
```

- GUI、CLI `probe`、MCP 都通过同一把锁独占串口；**同一时刻只能有一个程序连接 COM48**。
- 如果 GUI 正开着并已连接，MCP 的 `connect` 会返回 `port_COM48 已被进程 xxxx 占用`。
  **先断开 GUI（或关闭 GUI）再让 MCP 连接。**
- 进程异常退出留下的锁会被自动识别（PID 不存在即视为陈旧锁），无需手工删除。
- 反过来，MCP 进程还连着时打开 GUI 也会提示端口占用，这是预期行为。

## 6. 典型调用示例

### 示例 A：读状态（只读）

```json
{"jsonrpc":"2.0","id":3,"method":"tools/call",
 "params":{"name":"connect","arguments":{"port":"COM48"}}}
{"jsonrpc":"2.0","id":4,"method":"tools/call",
 "params":{"name":"get_status","arguments":{}}}
```

真实响应（本机 COM48，V1.0.0.4，已截断长字段）：

```json
{"jsonrpc":"2.0","id":3,"result":{"content":[{"type":"text","text":"{\"connected\": true, \"read_only\": true, \"port\": \"COM48\", \"version\": \"V1.0.0.4_0e318123\", \"sn\": \"acde750d9f3d\", \"model\": \"PDPOWERMINIV1\", \"group\": 1, \"groups\": [{\"id\":0,\"mv\":5000,\"ma\":2000},{\"id\":1,\"mv\":5000,\"ma\":3000},{\"id\":2,\"mv\":9000,\"ma\":3000},{\"id\":3,\"mv\":9000,\"ma\":2000},{\"id\":4,\"mv\":11000,\"ma\":3000}], \"output_enabled\": true, \"display\": {\"mv\": 5002, \"ma\": 615}}"}],"isError":false}}
```

### 示例 B：设电压（可写模式，5 V / 0.1 A）

```json
{"jsonrpc":"2.0","id":5,"method":"tools/call",
 "params":{"name":"set_voltage_current",
           "arguments":{"voltage_v":5.0,"current_a":0.1,"group":1,"confirm":true}}}
```

返回：

```json
{"jsonrpc":"2.0","id":5,"result":{"content":[{"type":"text",
 "text":"{\"ok\": true, \"voltage_v\": 5.0, \"current_a\": 0.1}"}],"isError":false}}
```

服务内部实际下发 `04 01 88 13 64 00 0A`，并回读 `0x84` 确认 `gid=1 mv=5000 ma=100`。

### 示例 C：开始记录日志

```json
{"jsonrpc":"2.0","id":6,"method":"tools/call",
 "params":{"name":"start_log",
           "arguments":{"directory":"D:\\PDPowerLogs",
                        "format":"csv","hz":5}}}
```

返回：

```json
{"jsonrpc":"2.0","id":6,"result":{"content":[{"type":"text",
 "text":"{\"logging\": true, \"path\": \"C:\\\\Users\\\\13223\\\\Documents\\\\PDPowerLogs\\\\20261104_....csv\"}"}],"isError":false}}
```

## 7. 排错

| 现象 | 原因 | 处理 |
|------|------|------|
| `connect` 报“已被进程 xxxx 占用” | GUI/CLI/另一个 MCP 正在用 COM48 | 断开或关闭占用方 |
| `connect` 报“打开失败（被其他程序占用或无权限）” | 官方上位机或其他串口工具占用 | 关掉对应工具后重试 |
| 写工具返回 `isError` + “只读模式” | 启动时没加 `--allow-write` | 改 `args` 后重启客户端 |
| `-32602` | 参数缺失/类型不对 | 对照第 4 节参数表 |
| 客户端收不到响应 | 用了相对路径 / exe 被杀软拦截 | 改用绝对路径，检查 stderr 日志 |

---

实测环境：Windows 11 + COM48（WeAct PDPowerMiniV1，固件 V1.0.0.4），
`initialize` / `tools/list`（13 个工具）/ `connect` / `get_status` 均返回正常，进程退出码 0。
