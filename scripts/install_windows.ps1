<#
.SYNOPSIS
  Install hantek-mcp (library, MCP server and GUI) into a local virtual environment on Windows 10/11.

.DESCRIPTION
  * Requires Python 3.11+ (python.org installer or `winget install Python.Python.3.12`).
  * Creates .venv next to this repository and installs the package with the GUI extra.
  * Does NOT install, replace or modify any USB driver. To talk to real hardware bind
    WinUSB to the oscilloscope with Zadig yourself - see docs\windows-setup.md (includes rollback).

.PARAMETER Dev
  Also install development tools (pytest, ruff, mypy).

.PARAMETER NoGui
  Install without PySide6/pyqtgraph.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\install_windows.ps1
#>
[CmdletBinding()]
param(
    [switch]$Dev,
    [switch]$NoGui
)
$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$Venv = Join-Path $Root ".venv"

function Find-Python {
    # Returns @{ Exe = ...; Args = @(...) } for the first Python >= 3.11 found.
    $candidates = @(
        @{ Exe = "py"; Args = @("-3.13") },
        @{ Exe = "py"; Args = @("-3.12") },
        @{ Exe = "py"; Args = @("-3.11") },
        @{ Exe = "python"; Args = @() }
    )
    foreach ($c in $candidates) {
        try {
            $ver = & $c.Exe @($c.Args) -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and $ver -and [version]$ver -ge [version]"3.11") { return $c }
        } catch { }
    }
    throw "Python 3.11 or newer was not found. Install it from https://www.python.org/downloads/ and re-run."
}

Write-Host "== hantek-mcp Windows installer ==" -ForegroundColor Cyan
$py = Find-Python
Write-Host "Using Python: $($py.Exe) $($py.Args -join ' ')"

if (-not (Test-Path (Join-Path $Venv "Scripts\python.exe"))) {
    Write-Host "Creating virtual environment in $Venv"
    & $py.Exe @($py.Args) -m venv $Venv
    if ($LASTEXITCODE -ne 0) { throw "could not create the virtual environment" }
}
$VPy = Join-Path $Venv "Scripts\python.exe"
& $VPy -m pip install --upgrade pip | Out-Null

$extras = @()
if (-not $NoGui) { $extras += "gui" }
if ($Dev) { $extras += "dev" }
$spec = if ($extras.Count) { "$($Root.Path)[$($extras -join ',')]" } else { "$($Root.Path)" }
Write-Host "Installing $spec"
& $VPy -m pip install -e $spec
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

$Exe = Join-Path $Venv "Scripts\hantek-mcp.exe"
Write-Host "`nVerifying installation with the simulator..." -ForegroundColor Cyan
& $Exe capture --simulate --measure frequency | Out-Null
if ($LASTEXITCODE -ne 0) { throw "simulated self-check failed" }
Write-Host "Simulator check passed."

Write-Host "`nUSB backend diagnostics (read-only):" -ForegroundColor Cyan
& $Exe diagnose

Write-Host @"

Next steps
  1. Real hardware: bind WinUSB to 'USB device 049F:505A' with Zadig (docs\windows-setup.md).
  2. Check the connection:   $Exe info
  3. GUI:                    scripts\run_gui.bat          (add --simulate to try without hardware)
  4. MCP (stdio) command:    $Exe serve --transport stdio
     Client configuration examples: docs\mcp-setup.md
"@
