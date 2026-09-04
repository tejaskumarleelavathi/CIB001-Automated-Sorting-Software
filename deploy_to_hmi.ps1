<#
.SYNOPSIS
    Deploy this project to the HMI over the network. Run this FROM THE DEV
    LAPTOP -- the HMI never needs a keyboard for this at all.

.DESCRIPTION
    Mirrors this project folder to C:\LabAutomation on the HMI (matching
    where main.py's production config paths already point -- see
    _PROD_CONFIG in main.py and the `paths:` section in
    config\machine_config.yaml). Excludes dev-only folders (.git, .venv,
    __pycache__, local output/) so the HMI gets clean source, not this
    laptop's dev environment.

    Then flips USE_MOCKS = True -> False in the DEPLOYED copy's main.py
    only -- this laptop's own main.py is left untouched (stays True for
    dev/bring-up), so you never have to remember to flip it back after
    testing here.

    First-time setup on the HMI, before running this the first time:
      1. Python installed, and a venv created at C:\LabAutomation\.venv
         with this project's dependencies (same as this laptop's .venv --
         see pyproject.toml's [project.optional-dependencies] "dev" and
         the base "dependencies" list).
      2. A Windows file share reachable from this laptop -- either the
         built-in administrative share (\\<HMI-HOST>\C$), which needs a
         Windows account with admin rights on the HMI, or a plain shared
         folder someone creates once (right-click C:\LabAutomation on the
         HMI -> Properties -> Sharing -> Share..., grant your laptop's
         account read/write).

.PARAMETER HmiHost
    The HMI's hostname or IP address on the lab network (172.16.9.x).

.EXAMPLE
    .\deploy_to_hmi.ps1 -HmiHost 172.16.9.50
    .\deploy_to_hmi.ps1 -HmiHost CIB-HMI01
#>

param(
    [Parameter(Mandatory = $true)]
    [string]$HmiHost
)

$ErrorActionPreference = "Stop"

$SourceRoot = $PSScriptRoot
# Administrative share by default (\\host\C$\LabAutomation). If that's not
# available/permitted on your network, change this to whatever shared path
# you set up instead, e.g. "\\$HmiHost\LabAutomation".
$TargetRoot = "\\$HmiHost\C`$\LabAutomation"

Write-Host "Deploying from:" -ForegroundColor Cyan
Write-Host "  $SourceRoot"
Write-Host "Deploying to:" -ForegroundColor Cyan
Write-Host "  $TargetRoot"
Write-Host ""

if (-not (Test-Path "\\$HmiHost")) {
    Write-Host "[ERROR] Cannot reach '$HmiHost' on the network. Check the HMI is powered on," -ForegroundColor Red
    Write-Host "        connected, and the hostname/IP is correct." -ForegroundColor Red
    exit 1
}

# robocopy mirrors the tree (/MIR) -- adds new/changed files, removes files
# on the target that no longer exist in source. /XD excludes directories
# entirely (never even scanned); /XF excludes by filename pattern.
$excludeDirs = @(
    (Join-Path $SourceRoot ".git"),
    (Join-Path $SourceRoot ".venv"),
    (Join-Path $SourceRoot "__pycache__"),
    (Join-Path $SourceRoot ".pytest_cache"),
    (Join-Path $SourceRoot "output"),
    (Join-Path $SourceRoot "cib\output")
)

Write-Host "Copying files (robocopy /MIR) ..." -ForegroundColor Cyan
robocopy $SourceRoot $TargetRoot /MIR /XD $excludeDirs /XF "*.pyc" /NFL /NDL /NP /R:2 /W:2
# robocopy's exit codes 0-7 all mean success (bitflags for what changed);
# 8+ means a real failure.
if ($LASTEXITCODE -ge 8) {
    Write-Host "[ERROR] robocopy reported a failure (exit code $LASTEXITCODE)." -ForegroundColor Red
    exit 1
}
Write-Host "[OK] Files copied.`n" -ForegroundColor Green

# Flip USE_MOCKS in the DEPLOYED copy only -- this laptop's own main.py is
# untouched, still True for dev/bring-up.
$deployedMain = Join-Path $TargetRoot "main.py"
Write-Host "Setting USE_MOCKS = False in the deployed copy ..." -ForegroundColor Cyan
(Get-Content $deployedMain -Raw) `
    -replace 'USE_MOCKS = True(\s*# )', 'USE_MOCKS = False$1' `
    -replace 'USE_MOCKS = True(\s*)$', 'USE_MOCKS = False$1' |
    Set-Content $deployedMain -NoNewline

$check = Select-String -Path $deployedMain -Pattern '^USE_MOCKS = (True|False)' | Select-Object -First 1
Write-Host "[OK] Deployed main.py: $($check.Line.Trim())`n" -ForegroundColor Green

Write-Host "Done. On the HMI, run:" -ForegroundColor Cyan
Write-Host "  C:\LabAutomation\Start CIB Sorting Cell.bat"
