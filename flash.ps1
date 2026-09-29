# Compile and flash a Maple sketch to a Heltec WiFi LoRa 32 V3.
#   .\flash.ps1 gateway COM6
#   .\flash.ps1 node COM7
param(
  [Parameter(Mandatory = $true)][ValidateSet("gateway", "node")][string]$Role,
  [Parameter(Mandatory = $true)][string]$Port
)
$cli = "$env:LOCALAPPDATA\arduino-cli\arduino-cli.exe"
if (-not (Test-Path $cli)) { $cli = "arduino-cli" }
$fqbn = "esp32:esp32:heltec_wifi_lora_32_V3"
$sketch = if ($Role -eq "gateway") { "maple_gateway" } else { "maple_loadcell_node" }
$dir = Join-Path $PSScriptRoot "firmware\$sketch"

& $cli compile --fqbn $fqbn --output-dir (Join-Path $PSScriptRoot "firmware\build\$sketch") $dir
if ($LASTEXITCODE -ne 0) { Write-Error "compile failed"; exit 1 }
& $cli upload --fqbn $fqbn -p $Port --input-dir (Join-Path $PSScriptRoot "firmware\build\$sketch") $dir
if ($LASTEXITCODE -ne 0) { Write-Error "upload failed (close any serial monitor / server.py using $Port)"; exit 1 }
Write-Host "Flashed $sketch to $Port"
