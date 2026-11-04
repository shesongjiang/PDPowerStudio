#requires -Version 5.1
<#
.SYNOPSIS
  一键构建 PDPower Studio 的三个可执行入口（GUI / CLI / MCP），并做冒烟测试。

.EXAMPLE
  .\build.ps1                  # 默认 one-folder -> dist\
  .\build.ps1 -OneFile         # one-file  -> dist-onefile\
  .\build.ps1 -Probe           # 额外跑一次只读 COM48 探针
#>
param(
    [switch]$OneFile,
    [switch]$SkipTests,
    [switch]$SkipBuild,
    [switch]$Probe,
    [string]$Port = "COM48"
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$outDir = if ($OneFile) { "dist-onefile" } else { "dist" }
$env:PDPOWER_ONEFILE = if ($OneFile) { "1" } else { "0" }

Write-Host "==> 检查依赖" -ForegroundColor Cyan
python -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "    安装 PyInstaller ..."
    python -m pip install --quiet pyinstaller
}

Write-Host "==> 生成图标与版本资源" -ForegroundColor Cyan
python tools\make_icon.py
python tools\make_version_info.py

Write-Host "==> PyInstaller 构建（$outDir）" -ForegroundColor Cyan
$buildStart = Get-Date
if ($SkipBuild) {
    Write-Host "    [SKIP] 复用已有产物"
    $buildSeconds = 0
} else {
    python -m PyInstaller --noconfirm --clean --distpath $outDir pdpower_studio.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 构建失败" }
    $buildSeconds = [math]::Round(((Get-Date) - $buildStart).TotalSeconds, 1)
}

$guiExe = Join-Path $outDir "PDPowerStudio\PDPowerStudio.exe"
$cliExe = Join-Path $outDir "pdpower-cli\pdpower-cli.exe"
$mcpExe = Join-Path $outDir "pdpower-mcp\pdpower-mcp.exe"
if ($OneFile) {
    $guiExe = Join-Path $outDir "PDPowerStudio.exe"
    $cliExe = Join-Path $outDir "pdpower-cli.exe"
    $mcpExe = Join-Path $outDir "pdpower-mcp.exe"
}

Write-Host "==> 产物" -ForegroundColor Cyan
foreach ($item in @(
    @{ Name = "GUI"; Path = $guiExe },
    @{ Name = "CLI"; Path = $cliExe },
    @{ Name = "MCP"; Path = $mcpExe }
)) {
    if (Test-Path -LiteralPath $item.Path) {
        $size = (Get-Item -LiteralPath $item.Path).Length / 1MB
        Write-Host ("    {0}: {1} ({2:N1} MiB)" -f $item.Name, $item.Path, $size)
    } else {
        throw "缺少产物: $($item.Path)"
    }
}
$total = (Get-ChildItem -LiteralPath $outDir -Recurse -File | Measure-Object -Property Length -Sum).Sum / 1MB
Write-Host ("    目录合计: {0:N1} MiB；构建耗时 {1}s" -f $total, $buildSeconds)

if ($SkipTests) { return }

Write-Host "==> 冒烟测试" -ForegroundColor Cyan

# 1) CLI --help
$sw = [Diagnostics.Stopwatch]::StartNew()
& $cliExe --help | Out-Null
$cliCode = $LASTEXITCODE
$sw.Stop()
if ($cliCode -ne 0) { throw "CLI --help 退出码 $cliCode" }
Write-Host ("    [PASS] CLI --help（{0} ms）" -f $sw.ElapsedMilliseconds)

# 1b) CLI --version（版本常量是否随打包生效）
& $cliExe --version | Out-Null
if ($LASTEXITCODE -ne 0) { throw "CLI --version 退出码 $LASTEXITCODE" }
Write-Host "    [PASS] CLI --version"

# 2) MCP initialize + tools/list
# 注意：MCP 的日志走 stderr（PS 5.1 会把它当成 ErrorRecord，Stop 模式下会抛异常），
# 因此临时放宽错误策略，并只取 stdout 里以 { 开头的 JSON 行。
$req = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' + "`n" +
       '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' + "`n"
$prevEap = $ErrorActionPreference
$ErrorActionPreference = "SilentlyContinue"
$mcpOut = @($req | & $mcpExe 2>$null) | Where-Object { $_ -match '^\s*\{' }
$ErrorActionPreference = $prevEap
try {
    if ($mcpOut.Count -lt 2) { throw "stdout 中没有 2 条 JSON 响应（实际 $($mcpOut.Count) 行）" }
    $init = $mcpOut[0] | ConvertFrom-Json
    $tools = $mcpOut[1] | ConvertFrom-Json
    if ($init.result.protocolVersion -ne "2024-11-05") { throw "protocolVersion 不符" }
    $names = @($tools.result.tools | ForEach-Object { $_.name })
    if (-not ($names -contains "get_status")) { throw "工具列表缺失 get_status" }
    Write-Host ("    [PASS] MCP initialize + tools/list（{0} 个工具）" -f $names.Count)
} catch {
    Write-Host ("    [DEBUG] 行数={0}；首行={1}" -f $mcpOut.Count, ($mcpOut | Select-Object -First 1)) -ForegroundColor Yellow
    throw "MCP stdio 测试失败: $_"
}

# 3) GUI 离屏启动 -> 显示窗口 -> 1.2 s 后正常退出
$sw = [Diagnostics.Stopwatch]::StartNew()
$env:PDPOWER_SMOKE_EXIT_MS = "1200"
$env:QT_QPA_PLATFORM = "offscreen"
$proc = Start-Process -FilePath $guiExe -Wait -PassThru
Remove-Item Env:PDPOWER_SMOKE_EXIT_MS -ErrorAction SilentlyContinue
Remove-Item Env:QT_QPA_PLATFORM -ErrorAction SilentlyContinue
$sw.Stop()
if ($proc.ExitCode -ne 0) { throw "GUI 冒烟退出码 $($proc.ExitCode)" }
$startupMs = [math]::Max(0, $sw.ElapsedMilliseconds - 1200)
Write-Host ("    [PASS] GUI 离屏启动并退出（进程总计 {0} ms，估算启动 {1} ms）" -f $sw.ElapsedMilliseconds, $startupMs)

# 4) GUI --selftest：逐项验证 Qt / pyqtgraph / 串口导入链（离屏，无需人工点击）
$selftestReport = Join-Path (Resolve-Path -LiteralPath $outDir).Path "selftest-gui.json"
$env:QT_QPA_PLATFORM = "offscreen"
$proc = Start-Process -FilePath $guiExe -ArgumentList "--selftest", "`"$selftestReport`"" -Wait -PassThru
Remove-Item Env:QT_QPA_PLATFORM -ErrorAction SilentlyContinue
if ($proc.ExitCode -ne 0) {
    throw "GUI --selftest 失败（退出码 $($proc.ExitCode)），详见 $selftestReport"
}
$self = Get-Content -Raw -Encoding UTF8 -LiteralPath $selftestReport | ConvertFrom-Json
if (-not $self.ok) { throw "GUI --selftest 报告 ok=false，详见 $selftestReport" }
Write-Host ("    [PASS] GUI --selftest（{0} 项全通过，{1} ms）" -f $self.results.Count, $self.elapsed_ms)

# 5) 可选只读探针
if ($Probe) {
    & $cliExe probe --port $Port --seconds 5
    if ($LASTEXITCODE -ne 0) { Write-Host "    [WARN] probe 返回非 0（设备状态与默认基线不一致）" -ForegroundColor Yellow }
    else { Write-Host "    [PASS] probe 只读联调" }
}

Write-Host "==> 完成：$outDir" -ForegroundColor Green
