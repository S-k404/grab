$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$guiExe = Join-Path $PSScriptRoot ".venv\Scripts\grab-gui.exe"
if (Test-Path $guiExe) {
    & $guiExe @args
} else {
    $venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
    if (Test-Path $venvPy) {
        & $venvPy -m grab.web @args
    } else {
        python -m grab.web @args
    }
}
