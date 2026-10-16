param([switch]$UseExistingPython)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
try {
    $project = Split-Path $PSScriptRoot -Parent
    $state = Join-Path $env:LOCALAPPDATA 'MoyuTools\sheet_rows_simple'
    $choice = Join-Path $state 'python.json'
    $moyuMarker = Join-Path $project 'PORTABLE_RUNTIME.json'
    $moyuRuntime = Join-Path $project 'runtime'
    if ((Test-Path -LiteralPath $moyuMarker) -or (Test-Path -LiteralPath $moyuRuntime)) {
        if (-not [Environment]::Is64BitOperatingSystem) { throw 'This portable package requires 64-bit Windows 10/11.' }
        $python = Join-Path $moyuRuntime 'python.exe'
        if (-not (Test-Path -LiteralPath $moyuMarker -PathType Leaf) -or -not (Test-Path -LiteralPath $python -PathType Leaf)) {
            throw 'Portable runtime is incomplete. Extract the entire portable ZIP to a new folder. Do not select another Python.'
        }
        $env:TCL_LIBRARY = Join-Path $moyuRuntime 'tcl\tcl8.6'
        $env:TK_LIBRARY = Join-Path $moyuRuntime 'tcl\tk8.6'
        $env:PYTHONNOUSERSITE = '1'
        $env:PYTHONPATH = ''
        $env:PYTHONHOME = ''
        & $python -I -B (Join-Path $project 'scripts\check_moyu_runtime.py')
        if ($LASTEXITCODE -ne 0) { throw 'Bundled runtime check failed. Keep the console and portable_startup.json. No installation was attempted.' }
        & $python -I -B (Join-Path $project 'scripts\MoyuTools.py')
        exit $LASTEXITCODE
    }
    if (-not $UseExistingPython) {
        $moyuBorrowed = Join-Path $state 'borrowed_runtime.json'
        $moyuEnvironmentProject = ''
        if (Test-Path -LiteralPath $moyuBorrowed -PathType Leaf) {
            $moyuEnvironmentProject = (Get-Content -LiteralPath $moyuBorrowed -Raw -Encoding UTF8 | ConvertFrom-Json).project
        }
        if (-not $moyuEnvironmentProject -or -not (Test-Path -LiteralPath (Join-Path $moyuEnvironmentProject 'PORTABLE_RUNTIME.json') -PathType Leaf)) {
            $moyuDialog = New-Object System.Windows.Forms.FolderBrowserDialog
            $moyuDialog.Description = '请选择旧“摸鱼小工具带环境便携包”的解压文件夹（内有WDMS_Cloud_Project）。本次只借用环境，不改旧文件。'
            $moyuDialog.ShowNewFolderButton = $false
            if ($moyuDialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) { exit 1 }
            $moyuEnvironmentProject = $moyuDialog.SelectedPath
            if (Test-Path -LiteralPath (Join-Path $moyuEnvironmentProject 'WDMS_Cloud_Project') -PathType Container) {
                $moyuEnvironmentProject = Join-Path $moyuEnvironmentProject 'WDMS_Cloud_Project'
            }
        }
        $moyuRuntime = Join-Path $moyuEnvironmentProject 'runtime'
        $python = Join-Path $moyuRuntime 'python.exe'
        if (-not (Test-Path -LiteralPath $python -PathType Leaf) -or -not (Test-Path -LiteralPath (Join-Path $moyuEnvironmentProject 'PORTABLE_RUNTIME.json') -PathType Leaf)) {
            throw '所选文件夹没有完整的旧便携环境。请选择已完整解压、内含WDMS_Cloud_Project的便携包文件夹；没有复制或安装任何依赖。'
        }
        $env:TCL_LIBRARY = Join-Path $moyuRuntime 'tcl\tcl8.6'
        $env:TK_LIBRARY = Join-Path $moyuRuntime 'tcl\tk8.6'
        $env:PYTHONNOUSERSITE = '1'
        $env:PYTHONPATH = ''
        $env:PYTHONHOME = ''
        & $python -I -B (Join-Path $project 'scripts\check_moyu_runtime.py') --runtime-project $moyuEnvironmentProject
        if ($LASTEXITCODE -ne 0) { throw 'Old portable runtime check failed. Keep portable_startup.json and the console.' }
        $null = New-Item -ItemType Directory -Path $state -Force
        @{project=$moyuEnvironmentProject} | ConvertTo-Json | Set-Content -LiteralPath $moyuBorrowed -Encoding UTF8
        & $python -I -B (Join-Path $project 'scripts\MoyuTools.py')
        exit $LASTEXITCODE
    }
    $python = Join-Path $project '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        if (Test-Path -LiteralPath $choice) {
            $python = (Get-Content -LiteralPath $choice -Raw -Encoding UTF8 | ConvertFrom-Json).python
        }
    }
    if (-not $python -or -not (Test-Path -LiteralPath $python -PathType Leaf)) {
        $dialog = New-Object System.Windows.Forms.OpenFileDialog
        $dialog.Title = 'Select your existing Windows project python.exe (3.9 or later)'
        $dialog.Filter = 'Python interpreter (python.exe)|python.exe'
        if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) { exit 1 }
        $python = $dialog.FileName
    }
    & $python -c 'import sys, tkinter, openpyxl; sys.exit(0 if sys.version_info >= (3,9) else 2)'
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.9+ with tkinter and openpyxl is required. Choose the configured WDMS Python. No environment was changed.' }
    $null = New-Item -ItemType Directory -Path $state -Force
    @{python=$python} | ConvertTo-Json | Set-Content -LiteralPath $choice -Encoding UTF8
    & $python -B (Join-Path $project 'scripts\MoyuTools.py')
    exit $LASTEXITCODE
} catch {
    [System.Windows.Forms.MessageBox]::Show('启动未完成，请保留黑色窗口里的错误信息及原环境，不必重装。', '摸鱼小工具') | Out-Null
    Write-Error $_
    exit 1
}
