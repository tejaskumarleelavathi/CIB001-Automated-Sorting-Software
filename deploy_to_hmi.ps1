<#
.SYNOPSIS
    Deploy this project to the HMI over the network. Run this FROM THE DEV
    LAPTOP -- the HMI never needs a keyboard for this at all.

.DESCRIPTION
    Copies ONLY the files actually needed to run the app to C:\LabAutomation
    on the HMI (matching where main.py's production config paths already
    point -- see _PROD_CONFIG in main.py and the `paths:` section in
    config\machine_config.yaml). This is a curated ALLOWLIST, not a mirror
    of the whole repo -- hardware_tests/, software_tests/, tests/,
    architecture/, camera_calibration/, robot_programs/, .git, .venv, and
    every doc/session-log file at the repo root are deliberately never
    copied. robot_programs/sort_server.prg in particular does NOT belong
    here at all -- it runs on the Epson robot CONTROLLER, deployed
    separately via F5 in RC+, never touched by this script.

    Deployed:
      cib/                          the actual application package
      main.py
      config/machine_config.yaml
      config/plate_configs/         real plate layouts for the pre-run screen
      Start CIB Sorting Cell.bat
      pyproject.toml                dependency list, for recreating the venv

    DELIBERATELY NEVER TOUCHED ON THE TARGET, even if already present there
    (2026-09-04 finding -- the first version of this script used
    `robocopy /MIR`, which mirrors a folder by DELETING anything on the
    target not present in the source. Since calibration_overhead.npy /
    calibration_eoa.npy / tool_offset.json / the picks database / run
    history are all real, machine-specific data that is NEVER checked into
    git -- see .gitignore's own "machine-specific, stored on HMI at
    C:\LabAutomation" comment on *.npy -- an /MIR deploy would have
    SILENTLY DELETED that real data the first time this ran against the
    real HMI. Every copy below is a plain, non-mirroring, overwrite-only
    copy: it can create/update the files listed above, and nothing else,
    full stop.):
      C:\LabAutomation\config\*.npy, tool_offset.json  (camera calibration)
      C:\LabAutomation\data\                            (picks DB, run history)
      anything else already sitting in C:\LabAutomation not listed above

    Also flips USE_MOCKS = True -> False in the DEPLOYED copy's main.py
    only -- this laptop's own main.py is left untouched (stays True for
    dev/bring-up), so you never have to remember to flip it back after
    testing here.

    First-time setup on the HMI, before running this the first time:
      1. Python installed, and a venv created at C:\LabAutomation\.venv
         with this project's dependencies (see pyproject.toml).
      2. Camera calibration + tool offset already captured for that
         physical setup, at C:\LabAutomation\config\*.npy /
         tool_offset.json (see camera_calibration/ scripts -- NOT copied
         by this script, run once directly against the HMI's own cameras).
      3. A Windows file share reachable from this laptop -- either the
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

# ── The allowlist -- everything this app actually needs to run ─────────────
# (path, isDirectory) pairs, all relative to $SourceRoot / $TargetRoot.
$items = @(
    @{ Path = "cib";                     IsDir = $true  },
    @{ Path = "main.py";                 IsDir = $false },
    @{ Path = "config\machine_config.yaml"; IsDir = $false },
    @{ Path = "config\plate_configs";    IsDir = $true  },
    @{ Path = "Start CIB Sorting Cell.bat"; IsDir = $false },
    @{ Path = "pyproject.toml";          IsDir = $false }
)

Write-Host "Copying required files only (no mirror -- nothing on the target" -ForegroundColor Cyan
Write-Host "is ever deleted, e.g. calibration data / the picks database):" -ForegroundColor Cyan
Write-Host ""

foreach ($item in $items) {
    $src = Join-Path $SourceRoot $item.Path
    $dst = Join-Path $TargetRoot $item.Path

    if (-not (Test-Path $src)) {
        Write-Host "[ERROR] Expected source path not found: $src" -ForegroundColor Red
        exit 1
    }

    if ($item.IsDir) {
        Write-Host "  $($item.Path)\ ..."
        New-Item -ItemType Directory -Force -Path $dst | Out-Null
        # Plain recursive copy-and-overwrite -- NOT /MIR, so nothing already
        # in $dst but absent from $src is ever removed. /XD excludes
        # __pycache__ and this app's own dev-local output folder wherever
        # they appear inside the tree (e.g. cib\output, cib\core\__pycache__).
        robocopy $src $dst /E /XD "__pycache__" "output" /XF "*.pyc" /NFL /NDL /NP /R:2 /W:2 | Out-Null
        if ($LASTEXITCODE -ge 8) {
            Write-Host "[ERROR] robocopy failed on $($item.Path) (exit code $LASTEXITCODE)." -ForegroundColor Red
            exit 1
        }
    } else {
        Write-Host "  $($item.Path)"
        $dstDir = Split-Path $dst -Parent
        New-Item -ItemType Directory -Force -Path $dstDir | Out-Null
        Copy-Item -Path $src -Destination $dst -Force
    }
}
Write-Host "`n[OK] Files copied.`n" -ForegroundColor Green

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
Write-Host ""
Write-Host "Not touched by this deploy (verify these already exist on the HMI" -ForegroundColor DarkYellow
Write-Host "before your first real run there):" -ForegroundColor DarkYellow
Write-Host "  C:\LabAutomation\.venv\                  (Python env -- one-time setup)"
Write-Host "  C:\LabAutomation\config\calibration_*.npy, tool_offset.json"
Write-Host "  C:\LabAutomation\data\                    (created automatically on first run)"
