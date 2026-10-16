param([string]$PythonPath = '', [switch]$SkipInstall)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
Set-Location $projectRoot
if (-not $PythonPath) { $PythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe' }
if (-not (Test-Path -LiteralPath $PythonPath)) { throw 'Provide -PythonPath pointing to an installed Windows Python or the current project venv.' }
& $PythonPath -c "import tkinter; import struct; print('Python bits:', struct.calcsize('P')*8)"
if ($LASTEXITCODE -ne 0) { throw 'This Python installation needs tkinter/Tcl support before building the demo.' }
if (-not $SkipInstall) {
    & $PythonPath -m pip install 'pyinstaller>=6,<7' 'openpyxl>=3.1,<4' 'psutil>=5.9,<8'
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
}
& $PythonPath -m PyInstaller --noconfirm --clean --onedir --windowed --name WDMS_Inventory_Demo --add-data 'scripts/wdms_menu_msaa.ps1;scripts' --add-data 'scripts/wdms_toolbar_icon.ps1;scripts' --add-data 'scripts/toolbar_icon_matcher.cs;scripts' --add-data 'assets/toolbar_inventory_18.png;assets' --hidden-import psutil --hidden-import openpyxl --hidden-import desktop_demo.owner_discovery --hidden-import services.owner_catalog scripts/WDMS_Demo.py
if ($LASTEXITCODE -ne 0) { throw 'Demo build failed.' }
Write-Host 'Built: dist\WDMS_Inventory_Demo\WDMS_Inventory_Demo.exe'
Write-Host 'Distribute the entire WDMS_Inventory_Demo folder, including _internal.'
