param()

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$sourceDirectory = Join-Path $projectRoot "mt5"
$sourceMq5 = Join-Path $sourceDirectory "ForexAISessionExporter.mq5"
$sourceEx5 = Join-Path $sourceDirectory "ForexAISessionExporter.ex5"

if (-not (Test-Path -LiteralPath $sourceMq5)) {
    throw "Missing MQL5 source: $sourceMq5"
}
if (-not (Test-Path -LiteralPath $sourceEx5)) {
    throw "Missing compiled MQL5 service: $sourceEx5"
}

Set-Location $projectRoot
$terminalDataPath = & uv run python -c "import MetaTrader5 as mt5; from forexai.config import load_settings; s=load_settings(); ok=mt5.initialize(path=str(s.mt5.terminal_path), timeout=s.mt5.initialize_timeout_ms); assert ok, mt5.last_error(); print(mt5.terminal_info().data_path); mt5.shutdown()"
if ($LASTEXITCODE -ne 0 -or -not $terminalDataPath) {
    throw "Could not detect the active MT5 terminal data path."
}

$terminalDataPath = ($terminalDataPath | Select-Object -Last 1).Trim()
$serviceDirectory = Join-Path $terminalDataPath "MQL5\Services"
New-Item -ItemType Directory -Force -Path $serviceDirectory | Out-Null
Copy-Item -Force -LiteralPath $sourceMq5 -Destination $serviceDirectory
Copy-Item -Force -LiteralPath $sourceEx5 -Destination $serviceDirectory

Write-Output "Session exporter installed in: $serviceDirectory"
Write-Output "In MT5 Navigator, open Services, Refresh, then start ForexAISessionExporter."
Write-Output "After the first export, validate with: uv run forexai market-check"
