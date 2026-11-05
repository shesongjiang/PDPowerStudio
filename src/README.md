# PDPower Studio（P0）

WeAct PDPowerMiniV1-Buck 的第三方上位机，Python + PySide6 + pyqtgraph + pyserial。
协议按仓库 `Communication protocol/`（USB v1.2 / UART v1.1）自行实现，未内嵌官方升级脚本。

> **非官方第三方工具**：协议信息来自 WeAct Studio 公开文档，与本项目作者无关。固件（`.fpk`）请自行从 WeAct Studio 官方仓库获取。

当前版本 **v0.1.2**。本版变更：

- 输出控制改为双态按钮（绿色“开启输出”/ 红色“关闭输出”），点击后回读 `0x82` 确认；
- 写后回读改为 1 s 内重试（实测固件 V1.0.2.0 的 `0x02` 需 200–300 ms 才生效，旧的固定 80 ms 会误报失败）；
- 串口列表默认只显示 PDPower（VID:PID `1A86:FE0C` + SN 前缀 `AC`/`B3`），新增“显示全部串口”开关；
- 曲线时间轴支持滚轮缩放 / 拖拽平移 / 框选，档位改为 30 s / 5 min / 30 min / 全部，缩放后不再被自动滚动拉回；
- Y 轴固定从 0 起（`padding=0`），噪声负读数在显示层 clamp 到 0，原始值仍写入日志；
- 高 DPI：`Round` 缩放策略 + `AA_UseHighDpiPixmaps` + exe DPI manifest（`permonitorv2`），曲线 2px 整数宽 + `cosmetic`、坐标轴 12px、网格透明度 0.18；
- 按钮与对话框统一高度/圆角/hover/pressed/disabled 态，四变体（primary/secondary/danger/ghost）按 token 派生，危险动作用红色区分；
- 主题拆成 `ui/theme_dark.qss` / `ui/theme_light.qss`（色板 token：bg/surface/border/text/accent/danger/success），主窗、抽屉、对话框共用一份应用级样式表；
- “只读联读模式”文案修正为“只读模式（禁止写入设备）”，功能保留（真实拦截写入）。

## 1. 可直接运行

```powershell
cd pdpower_studio          # 进入源码目录（clone 后的仓库根目录）
pip install -r requirements.txt

python main.py                     # 启动 GUI（默认可写；只读需显式勾选）
python -m pdpower_studio list      # 列出 PDPower 串口（VID 1A86 / PID FE0C）
```

只读联调 / 验证（CLI 在上级目录运行，包名即目录名）：

```powershell
cd ..                      # 仓库父目录，包名即目录名
python -m pdpower_studio.cli probe --port COM48 --seconds 10 --check-defaults --json pdpower_studio\artifacts\probe_com48.json
python -m pdpower_studio.cli mcp                 # stdio MCP，默认只读
python -m pdpower_studio.cli mcp --allow-write   # 显式解锁写入类工具
python -m pdpower_studio.cli upgrade-verify --file xxx.fpk --model PDPOWERMINIV1 --current V1.0.0.4
python -m pdpower_studio.cli log-estimate --hz 5 --hours 24 --format csv
```

测试（无设备）：

```powershell
cd pdpower_studio
python -m pytest -q
```

## 2. 模块

| 文件 | 职责 |
| --- | --- |
| `protocol.py` | 帧编解码、CRC8(UART, poly 0x31/init 0xFF)、CRC16-XMODEM、定长/变长帧表、只读命令白名单 |
| `device_model.py` | 型号（SN 前缀 `AC`/`B3`）、版本门槛、能力位、快照合并、`DeviceService` 唯一写入口、`Calibration` |
| `transport.py` | 单写者 IO 线程、55 ms 写节流、0x85/0x82/0x8A 独立轮询、端口锁文件、`read_only` 守卫 |
| `serial_worker.py` | QThread 适配，10 Hz 把快照推给 UI |
| `logging_store.py` | CSV/JSONL，writer 线程 + 队列，按天/按大小滚动，末行修复，区间回读，体积估算 |
| `ui/plots.py` | 双 Y 轴实时图、LTTB 降采样、历史曲线 |
| `ui/main_window.py` | 主窗口、侧栏设备抽屉、输出设置、日志面板、历史回看、升级对话框 |
| `i18n/` | 仅 zh/en，运行时切换（QSettings 记忆） |
| `updater/fpk.py` | 96 B 头解析、`head_crc32` 校验、型号/分区/降级规则 |
| `updater/github_source.py` | Contents API 列 `Firmware/`，取四段版本最大值；限流/离线用 7 天缓存 |
| `updater/ymodem.py` | 1K 块 + CRC16 的 Y-Modem 发送器（含 EOT 双发与空包收尾） |
| `updater/service.py` | 发现 → 下载 → 校验 → 确认 → 进 bootloader → 传输 → 重枚举读 `0xC2` 复核；默认 `dry_run=True`，真刷由 GUI 运行时勾选并输入 `I-UNDERSTAND` |
| `mcp_server/` | stdlib stdio JSON-RPC，最小工具集 + 量程校验 + `confirm` 字段 |
| `cli.py` | `list` / `probe` / `mcp` / `upgrade-verify` / `log-estimate` |

## 3. 功能对应

1. **UI**：V/A/功率/模式/能量 五张卡；设备信息（型号、SN、版本、LCD、能力位）在右侧抽屉；曲线档位 30 s / 5 min / 30 min / 全部，滚轮缩放 X、拖拽平移、可切“框选”模式，绘制前 LTTB 降采样，更早数据从日志按时间段回读。
2. **电流精度**：直接显示设备返回的 mA 整数，不补零；可切换即时 / 平均（5 点滑窗）/ 保持；`Calibration` 默认 gain=1.0、offset=0。
3. **输出设置**：电压/电流各一条滑杆 + 直接输入框 + 少量预设（5/9/12/15/20 V、0.5/1/2/3 A），无多余挡位。
4. **语言**：仅中文/English，切换即时生效。
5. **PD 电压**：`capabilities_for()` 按版本算能力位；V1.0.0.4 下控件禁用并提示“需固件 ≥ 1.0.2.0（V1.0.0.4）”。
6. **对标功能**：OCP 软限提示（阈值 mA）、Wh 能量统计、读数平均/保持、超阈值/OC 告警条；序列/定时测试未实现（依赖固件侧能力）。
7. **MCP**：`list_devices/connect/disconnect/get_status/get_device_info/set_voltage_current/set_output/set_group/output_enable/save_config/start_log/stop_log/estimate_log_size`；写入经 `Capabilities.clamp()`；`output_enable`、`save_config` 必须 `confirm=true`；`read_only=True` 时全部写入被拒。串口独占由 `PortLock`（pid 存活性判断）+ 单写者 IO 线程保证。
8. **日志**：CSV 表头 `ts,unix_ms,v_mv,i_ma,mode,out_en,in_state,in_mv,pd_dv`，可选 JSONL；行级 append，每 20 行 flush，`fsync` 可配；按天或 32 MiB 滚动；启动时截断崩溃遗留的半行；`read_range()` 回读；`estimate_size()` 给体积估算。落盘在独立 writer 线程，采集/UI 不阻塞。

固件升级：`fpk` 头解析 + `head_crc32` 校验 + 型号/分区/版本门槛；默认只升不降（降级需勾选，`cache_previous()` 保留上一版）；失败自动重试 3 次（`UpgradeService.retries=3`，结果含 `attempts`）；GUI 升级对话框可选择本地 `.fpk`，选后展示型号/版本/大小/sha256，校验不过直接拒绝。真刷必须在对话框勾选“允许真实刷写”并输入 `I-UNDERSTAND`；执行链为 `0x41 A5 AA 0A` → 端口重枚举 → 115200 Y-Modem 1K/CRC16 → EOT → 重新握手读 `0xC2` 复核版本。

MCP 接入（Trae / Claude Desktop 配置、13 个工具参数表、只读/可写切换、端口锁注意事项）见 `MCP.md`。

**主题**：深色/浅色可切换，记住上次选择；窗口 QSS、曲线背景、坐标轴文字、V/I 曲线与历史曲线配色一起变化。

## 4. 实测记录（COM48）

```
name  : WeAct Studio PD Power Mini V1 BUCK
model : PDPOWERMINIV1    sn: acde750d9f3d    version: V1.0.2.0_6a997d9a
lcd   : HAN              caps: input_state=1 pd_vset=1 uart=1  20V/3A
M0 5000/2000  M1 6662/1000  M2 9000/3000  M3 9000/2000  M4 11000/3000
OCP=off  comp=on  brightness=10  group=M1  输出=关闭
```

v0.1.2 写闭环（5 V / 0.1 A，测完已还原 M1 = 6662 mV / 1000 mA、输出关闭、组号 M1）：

```
写入后回读 0x84: gid=1 mv=5000 ma=100 (期望 1 5000 100)
开启后 0x82: output_enabled = True
关闭后 0x82: output_enabled = False
还原后 M0-M4: {0:(5000,2000) 1:(6662,1000) 2:(9000,3000) 3:(9000,2000) 4:(11000,3000)}
```

输出使能的时间线实测（`0x02=1` 后连续读 `0x82`）：+115 ms 仍为 `False`，+299 ms 起稳定 `True`。
因此 `_write_and_verify()` 改为 1 s 内每 100 ms 重试回读，避免把“设备还没生效”误判成写失败——这也是 v0.1.1 里点“开启输出”偶尔报错的根因。

## 5. 已验证 / 未验证

**已验证（实机 + 无设备）**

- `pytest -q` → 67 passed（协议真实帧 decode、`41 A5 AA 0A` encode、CRC、能力位、服务写入编码、日志滚动/修复、fpk 校验、Y-Modem 假 bootloader、MCP JSON-RPC、LTTB、时间档位定义）。
- COM48 实机写闭环：`0x04` 写 5 V / 0.1 A → `0x84` 回读一致；`0x02` 开启 → `0x82` 回读 `True`；关闭 → `False`；M0–M4 与组号、输出状态已还原。
- 输出使能生效延迟 200–300 ms（`0x82` 时间线），已按此调整回读重试策略。
- 串口过滤：`comports()` 全量 2 个（COM25 CH340 `1A86:7523` 无 SN、COM48 `1A86:FE0C` SN `ACDE750D9F3D`），`list_pdpower_ports()` 只返回 COM48。
- 高 DPI 实测：`screen.geometry=(0,0,2752,1152)`、`devicePixelRatio=1.25`、`logicalDpi=120`；窗口 `grab()` 输出 `1600×1075 @dpr=1.25`（1280×860 逻辑尺寸），曲线 2px 对应 2.5 物理像素。
- GUI 真实窗口截图（`artifacts/v012_dark_100.png`、`v012_light_100.png`、`v012_upgrade_dialog_100.png`，另有 125%/150% 同名变体），暗/浅两套主题、档位按钮、输出按钮、对话框均正常；DPI 数值见 `artifacts/v012_dpi_100.json` / `_125.json` / `_150.json`。
- 打包后从桌面副本实测：GUI `--version` 与 `--selftest` 退出码 0（`ok:true`，`DualAxisPlot ok`、`MainWindow 构造成功`）、真实窗口标题 `PDPower Studio`；CLI `--version` 输出 `0.1.2`；MCP `initialize` 返回 `serverInfo.version=0.1.2` 且 `tools/list` 13 个工具。
- QSS 以数据文件随 GUI 打包，冻结点从 `_internal/pdpower_studio/ui/theme_*.qss` 读取（源码运行则读 `pdpower_studio/ui/`）。
- `DeviceLink(read_only=True)` 对 `0x41/0x02/0x04` 抛 `PermissionError`（单测覆盖）。

**未验证（需设备/授权/网络）**

- 真实固件刷写全链路（`0x41` → 重枚举 → Y-Modem → 复核）：按约束只实现代码与校验，仍为 dry-run。
- `0x44/0x45/0x47`：按约束从未下发。
- 鼠标滚轮/框选的真实手感：只做了代码路径与离屏构建验证，未做人工操作录屏。
- GitHub 在线发现/下载：未做联网实测；仓库无 Releases，只能依赖 Contents API，且限流 60 次/小时。
- UART 模式（CRC8 + len）与 V1.1 型号 `user_string`：仅按协议表实现，无实机。
- `C7` 66 字节工厂数据帧未抓包。

## 6. 限制与安全边界

- `.fpk` 载荷熵 7.96（加密），仅能校验头部 CRC 与型号/分区/版本，无法验证解密后镜像的 `pkg_crc32`；第三方无法自制固件包。
- 日志采样上限受轮询周期约束（默认 5 Hz，可调 `--interval`）；`hz` 配置只影响估算值与记录语义。
- MCP 与 GUI 的“同进程共享连接”未做进程内复用；跨进程靠端口锁互斥，第二个进程会收到 `PortBusy`。
- 不要把 `UpgradeService(dry_run=True, allow_real_flash=False)` 改成可写状态去试刷，除非你接受设备变砖风险并自备恢复手段。

## 7. 构建与分发

### 7.1 一键构建

```powershell
cd pdpower_studio
.\build.ps1                  # one-folder -> dist\        （默认，推荐分发）
.\build.ps1 -OneFile         # one-file   -> dist-onefile\
.\build.ps1 -SkipBuild       # 不重建，只跑冒烟测试
.\build.ps1 -Probe           # 额外跑一次只读 COM48 探针
```

脚本会依次生成图标（`tools/make_icon.py`，Qt 离屏渲染，不依赖外部素材）、版本资源（`tools/make_version_info.py`，读取 `pdpower_studio.__version__`），再执行 `python -m PyInstaller --clean pdpower_studio.spec`，最后跑四项冒烟：

1. `pdpower-cli.exe --help/--version`（退出码 0）；
2. `pdpower-mcp.exe` 管道输入 `initialize` + `tools/list`（校验 `protocolVersion` 与工具数量）；
3. `PDPowerStudio.exe` 离屏启动 → 显示 1.2 s → 正常退出；
4. `PDPowerStudio.exe --selftest` 逐项自检 Qt / pyqtgraph / 串口 / 协议链（退出码 0）。

### 7.2 产物与实测数据

| 产物 | one-folder（`dist/`） | one-file（`dist-onefile/`） |
| --- | --- | --- |
| `PDPowerStudio.exe`（GUI，无控制台） | 目录 107.6 MiB / 285 文件，exe 6.0 MiB | 单文件 42.0 MiB |
| `pdpower-cli\pdpower-cli.exe` | 16.8 MiB | 7.5 MiB |
| `pdpower-mcp\pdpower-mcp.exe` | 16.8 MiB | 7.5 MiB |
| 真实窗口出现耗时 | ≈1.27 s | ≈2.47 s（需先解压到 `%TEMP%\_MEIxxxx`） |
| 离屏启动（含 1.2 s 冒烟等待） | 2.07 s，估算启动 0.87 s | 4.23 s，估算启动 3.03 s |
| `--selftest` 耗时 | 0.37 s | 0.68 s |

默认 one-folder：启动快、可增量替换单个 DLL、杀软误报更少。one-file 的优点只有“单文件好拷贝”，代价是每次冷启动都要解压 42 MiB，且更易被杀软启发式拦截。

### 7.3 三个入口的取舍

同一份 `.spec` 里做三个独立 `Analysis`，而不是一个 exe 用参数分派：

- CLI/MCP 完全不链接 PySide6/pyqtgraph，体积从 107 MiB 降到 16.8 MiB，故障面也小；
- GUI 用 `console=False`（双击不弹黑框），CLI/MCP 用 `console=True`（`probe`/JSON-RPC 需要标准输入输出）；
- 代价是构建时间约 75–100 s（三个 target 串行分析），可接受。

### 7.4 PySide6 / pyqtgraph 的坑（本次实际踩到）

- **`PySide6.QtOpenGL` 隐式依赖（真实故障）**：`pyqtgraph/Qt/OpenGLHelpers.py` 用 `importlib.import_module("PySide6.QtOpenGL")` 动态导入，PyInstaller 静态分析看不见。若把 `QtOpenGL`/`QtOpenGLWidgets` 写进 `excludes`，exe 启动即 `ModuleNotFoundError: No module named 'PySide6.QtOpenGL'`。现在 spec 里显式 `hiddenimports` 了这两个模块，并保留了 `Qt6OpenGL.dll` / `QtOpenGL.pyd`。
- **Qt 平台插件**：`_internal/PySide6/plugins/platforms/qwindows.dll` 是 Windows 必需项，缺失会报 “could not find or load the Qt platform plugin windows”；离屏冒烟还需要 `qoffscreen.dll`。两者都已保留。
- **图像格式插件**：保留 `imageformats/qico.dll`（窗口/任务栏图标）与 `qjpeg.dll`，其余（tiff/webp/icns/tga/wbmp/gif/svg/pdf）剪掉。
- **DLL 与模块的排除是两件事**：`hook-PySide6` 会把整个 PySide6 目录的 Qt DLL 收进来，即使模块已 exclude。spec 里的 `prune_pyside()` 按目标路径二次裁剪，把 QtQuick/Qml/Pdf/Network/VirtualKeyboard/`opengl32sw.dll` 等共 22 项去掉，GUI 从 152.5 MiB 降到 107.6 MiB。
- **pyqtgraph 资源**：contrib 的 `hook-pyqtgraph` 负责收集 data 与 Template 子模块；spec 里再 `collect_data_files("pyqtgraph")` 兜底，避免 hook 版本差异导致缺配色表。
- **serialport**：本程序用 pyserial 而非 QtSerialPort，因此不需要 `Qt6SerialPort`；但要 hidden-import `serial.tools.list_ports_windows`，否则 `list` 子命令枚举不到端口。
- **中文路径**：one-folder 直接从中文目录运行已实测通过；one-file 运行时解压到 `%TEMP%\_MEIxxxx`（同样支持中文路径，但要求 `%TEMP%` 可写）。`--distpath` 与源码路径含中文时，PyInstaller 6.x 正常。
- **控制台编码**：`console_io.setup_console_encoding()` 把 CP 设为 65001 并把 stdin/stdout/stderr 统一为 UTF-8；否则 cp936 控制台下 `--help` 中文乱码，MCP 的中文参数也会解错。

### 7.5 自检与故障排查

```powershell
dist\PDPowerStudio\PDPowerStudio.exe --version
dist\PDPowerStudio\PDPowerStudio.exe --selftest dist\selftest-gui.json   # 退出码 0=通过
dist\pdpower-cli\pdpower-cli.exe --version
```

`--selftest` 会检查：Qt 五模块（含 QtOpenGL）、QApplication、pyqtgraph PlotWidget、`DualAxisPlot`、`MainWindow` 构造、pyserial 枚举、协议/能力位/日志估算；结果写入 JSON（默认 `%TEMP%\pdpower_selftest.json`）。一旦再出现缺模块，报告里会带完整 traceback。

### 7.6 分发注意事项

- **未签名**：exe 没有代码签名证书，首次运行会触发 SmartScreen“未知发布者”，需点“更多信息 → 仍要运行”；企业环境建议用 `signtool` 加 EV 证书。
- **杀软误报**：PyInstaller one-file 打包的程序常被启发式误报（尤其 Defender/360）。建议分发 one-folder，并把整个目录加入白名单；不要用 UPX（本 spec 已 `upx=False`，压缩会显著提高误报率）。
- **首次启动慢**：one-file 首次启动需解压，冷启动 2–3 s 属正常；one-folder 约 1 s。首次运行还会被杀软全量扫描，可能更慢。
- **系统依赖**：Windows 10/11 x64；VC++ 运行库已随包（`MSVCP140.dll`/`VCRUNTIME140*.dll`），无需另装。不支持 Win7（PySide6 6.12 不再支持）。
- **版本号**：`pdpower_studio/__init__.py` 的 `__version__` 是唯一来源，同时写入 exe 版本资源、`--version` 输出与 MCP `serverInfo.version`。
