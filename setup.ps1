<#
.SYNOPSIS
    Install or update Grab and its Windows 11 dependencies.
.DESCRIPTION
    Checks/installs ffmpeg, aria2, deno, and uv via winget, creates .venv,
    installs Grab + yt-dlp[default,curl-cffi] + gallery-dl, and writes
    %USERPROFILE%\.local\bin\grab.cmd & grab-gui.cmd shims.
#>
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

function Ensure-WingetPackage {
    param([string]$CommandName, [string]$WingetId)
    if (Get-Command $CommandName -ErrorAction SilentlyContinue) { return }
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Host "Note: $CommandName not found and winget unavailable; install $WingetId manually." -ForegroundColor Yellow
        return
    }
    Write-Host "Installing $WingetId via winget..." -ForegroundColor Cyan
    winget install --id $WingetId --exact --accept-package-agreements --accept-source-agreements
}

Ensure-WingetPackage "ffmpeg" "Gyan.FFmpeg"
Ensure-WingetPackage "aria2c" "aria2.aria2"
Ensure-WingetPackage "deno" "DenoLand.Deno"
Ensure-WingetPackage "uv" "astral-sh.uv"

$uvCmd = Get-Command uv -ErrorAction SilentlyContinue
if (-not (Test-Path (Join-Path $PSScriptRoot ".venv\Scripts\python.exe"))) {
    if ($uvCmd) {
        & $uvCmd.Source venv .venv
    } elseif (Get-Command py -ErrorAction SilentlyContinue) {
        py -3 -m venv .venv
    } else {
        python -m venv .venv
    }
}

$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if ($uvCmd) {
    & $uvCmd.Source pip install --python $venvPy -U -e . "yt-dlp[default,curl-cffi]" gallery-dl
} else {
    & $venvPy -m pip install --upgrade pip
    & $venvPy -m pip install -U -e . "yt-dlp[default,curl-cffi]" gallery-dl
}

$localBin = Join-Path $env:USERPROFILE ".local\bin"
New-Item -ItemType Directory -Force -Path $localBin | Out-Null
$scriptsDir = Join-Path $PSScriptRoot ".venv\Scripts"

@"
@echo off
"$scriptsDir\grab.exe" %*
"@ | Set-Content -Path (Join-Path $localBin "grab.cmd") -Encoding Ascii

@"
@echo off
"$scriptsDir\grab-gui.exe" %*
"@ | Set-Content -Path (Join-Path $localBin "grab-gui.cmd") -Encoding Ascii

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (-not $userPath -or ($userPath.Split(";") -notcontains $localBin)) {
    $newPath = if ($userPath) { "$localBin;$userPath" } else { $localBin }
    [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
    Write-Host "Added $localBin to your User PATH (re-open terminals to pick it up)." -ForegroundColor Cyan
}

Write-Host ""
Write-Host "Grab is ready on Windows 11!" -ForegroundColor Green
& (Join-Path $scriptsDir "grab.exe") --version
Write-Host "Run 'grab --help', 'grab --doctor', or .\Grab` GUI.ps1"
