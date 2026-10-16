param([Parameter(Mandatory=$true)][int]$ParentProcess)
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
try {
Add-Type -AssemblyName System.Windows.Forms, System.Drawing, System.Runtime.WindowsRuntime
$accessibility = [Reflection.Assembly]::Load('Accessibility, Version=4.0.0.0, Culture=neutral, PublicKeyToken=b03f5f7f11d50a3a')
Add-Type -Path (Join-Path $PSScriptRoot 'windows_native.cs') -ReferencedAssemblies @(
    [System.Diagnostics.Process].Assembly.Location,
    [System.Windows.Forms.Clipboard].Assembly.Location,
    [System.Drawing.Bitmap].Assembly.Location,
    $accessibility.Location
)
[MoyuNative]::Initialize($ParentProcess)
} catch {
    # Compilation happens before any UI capture; keep actionable compiler errors locally.
    $startupFolder = Join-Path $env:LOCALAPPDATA 'MoyuTools\sheet_rows_simple'
    $null = New-Item -ItemType Directory -Path $startupFolder -Force
    ($_ | Out-String) | Set-Content -LiteralPath (Join-Path $startupFolder 'bridge_startup.txt') -Encoding UTF8
    exit 3
}

function Await-WinRT($Operation, [Type]$ResultType) {
    $method = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetGenericArguments().Count -eq 1 -and
        $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    } | Select-Object -First 1
    $task = $method.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
    $task.Wait()
    return $task.Result
}

function Read-Region([int[]]$Rect) {
    # OCR only for the remote desktop's pixels. WDMS uses native accessible values.
    # Windows.Media.Ocr runs locally; missing language support is reported, never installed.
    $null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
    $null = [Windows.Storage.Streams.InMemoryRandomAccessStream, Windows.Storage.Streams, ContentType=WindowsRuntime]
    $null = [Windows.Storage.Streams.DataWriter, Windows.Storage.Streams, ContentType=WindowsRuntime]
    $null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
    $null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
    $null = [Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType=WindowsRuntime]
    $null = [Windows.Globalization.Language, Windows.Globalization, ContentType=WindowsRuntime]
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new('en-US'))
    if ($null -eq $engine) { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() }
    if ($null -eq $engine) { throw 'offline_ocr_unavailable' }
    $stream = [Windows.Storage.Streams.InMemoryRandomAccessStream]::new()
    $bitmap = $null
    try {
        $writer = [Windows.Storage.Streams.DataWriter]::new($stream.GetOutputStreamAt(0))
        try {
            $writer.WriteBytes([MoyuNative]::Capture($Rect))
            $null = Await-WinRT ($writer.StoreAsync()) ([uint32])
            $null = $writer.DetachStream()
        } finally { $writer.Dispose() }
        $stream.Seek(0)
        $decoder = Await-WinRT ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bitmap = Await-WinRT ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
        $result = Await-WinRT ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
        $lines = @()
        foreach ($line in $result.Lines) {
            $words = @($line.Words)
            if ($words.Count -eq 0) { continue }
            $left = ($words | ForEach-Object {$_.BoundingRect.X} | Measure-Object -Minimum).Minimum
            $top = ($words | ForEach-Object {$_.BoundingRect.Y} | Measure-Object -Minimum).Minimum
            $right = ($words | ForEach-Object {$_.BoundingRect.X + $_.BoundingRect.Width} | Measure-Object -Maximum).Maximum
            $bottom = ($words | ForEach-Object {$_.BoundingRect.Y + $_.BoundingRect.Height} | Measure-Object -Maximum).Maximum
            $lines += @{text=$line.Text; rect=@([int]$left,[int]$top,[int]$right,[int]$bottom)}
        }
        return @{text=$result.Text; lines=$lines}
    } finally {
        if ($null -ne $bitmap) { $bitmap.Dispose() }
        $stream.Dispose()
    }
}

while ($null -ne ($line = [Console]::ReadLine())) {
    $request = $null
    try {
        if ($line.Length -gt 65536) { throw 'request_too_large' }
        $request = $line | ConvertFrom-Json
        $p = $request.payload
        if ($null -ne $p.root) {
            [MoyuNative]::Guard([long]$p.root.hwnd, [uint32]$p.root.pid, [int[]]$p.root.rect, $true)
        }
        $result = switch ($request.action) {
            'windows' { @{windows=@([MoyuNative]::Windows())}; break }
            'preview' { [MoyuNative]::Preview([long]$p.root.hwnd,[uint32]$p.root.pid,[int]$p.width,[int]$p.height); break }
            'point' { [MoyuNative]::PointInfo([int]$p.x, [int]$p.y); break }
            'read' { [MoyuNative]::ReadPoint([int]$p.point[0], [int]$p.point[1], [long]$p.root.hwnd, [uint32]$p.root.pid); break }
            'date' { @{value=[MoyuNative]::DateValue([long]$p.hwnd, [string]$p.value, ($null -ne $p.PSObject.Properties['value']), [long]$p.root.hwnd, [uint32]$p.root.pid)}; break }
            'edit' { @{value=[MoyuNative]::EditValue([long]$p.hwnd, [string]$p.value, ($null -ne $p.PSObject.Properties['value']), [long]$p.root.hwnd, [uint32]$p.root.pid)}; break }
            'scroll' { @{position=[MoyuNative]::Scroll([long]$p.hwnd, [int]$p.position, ($null -ne $p.PSObject.Properties['position']), [long]$p.root.hwnd, [uint32]$p.root.pid)}; break }
            'hash' {
                [MoyuNative]::Unobscured([int[]]$p.rect,[long]$p.root.hwnd,[uint32]$p.root.pid)
                @{hash=[MoyuNative]::Hash([int[]]$p.rect)}; break
            }
            'conditions' { @{hash=[MoyuNative]::Conditions([long]$p.root.hwnd, [int[]]$p.rect, [long[]]$p.dates)}; break }
            'ocr' {
                $regions = @()
                foreach ($rect in $p.rects) {
                    [MoyuNative]::Unobscured([int[]]$rect,[long]$p.root.hwnd,[uint32]$p.root.pid)
                    $regions += (Read-Region ([int[]]$rect))
                    [MoyuNative]::Unobscured([int[]]$rect,[long]$p.root.hwnd,[uint32]$p.root.pid)
                }
                @{regions=$regions}; break
            }
            'clear' { @{sequence=[MoyuNative]::ClearClipboard()}; break }
            'text' { [MoyuNative]::ClipboardText(); break }
            'file' { [MoyuNative]::ClipboardFile(); break }
            'open_folder' {
                $shell = New-Object -ComObject Shell.Application
                try { $shell.Open([string]$p.path) }
                finally { $null = [Runtime.InteropServices.Marshal]::ReleaseComObject($shell) }
                @{}; break
            }
            'navigate_folder' {
                $shell = New-Object -ComObject Shell.Application
                $found = $false
                try {
                    foreach ($window in @($shell.Windows())) {
                        try {
                            if ([long]$window.HWND -eq [long]$p.hwnd -and [string]$window.Document.Folder.Self.Path -ieq [string]$p.previous) {
                                $window.Navigate([string]$p.path)
                                $found = $true
                                break
                            }
                        } catch { # Only our previously confirmed staging window may navigate.
                        }
                    }
                } finally { $null = [Runtime.InteropServices.Marshal]::ReleaseComObject($shell) }
                if (-not $found) { throw 'staging_window_changed' }
                @{}; break
            }
            'folder' {
                $shell = New-Object -ComObject Shell.Application
                $matches = @()
                try {
                    foreach ($window in @($shell.Windows())) {
                        try {
                            if ([string]$window.Document.Folder.Self.Path -ieq [string]$p.path) {
                                $matches += [MoyuNative]::Metadata([long]$window.HWND)
                            }
                        } catch { # Non-Explorer windows expose no Folder; do not navigate them.
                        }
                    }
                } finally { $null = [Runtime.InteropServices.Marshal]::ReleaseComObject($shell) }
                @{windows=$matches}; break
            }
            'close' { @{}; break }
            default { throw 'unknown_action' }
        }
        if ($null -ne $p.root) {
            [MoyuNative]::Guard([long]$p.root.hwnd, [uint32]$p.root.pid, [int[]]$p.root.rect, $true)
        }
        $response = @{id=$request.id; ok=$true; result=$result}
    } catch {
        # Never serialize exception arguments, paths, captured text or raw UI content.
        $reason = $_.Exception.GetBaseException().Message
        $messages = @{
            offline_ocr_unavailable='本机Windows缺少可用的离线文字识别语言，未安装或联网下载；请先报告该缺项。'
            date_control_unsupported='选中的日期控件不是支持的DateTimePicker，未写入；请重新定位。'
            date_access_denied='日期控件访问被拒绝，未继续；请确认脚本与WDMS使用相同用户权限。'
            edit_control_unsupported='合同编号定位不是支持的原生文本框，未填写；请重新定位。'
            edit_value_rejected='合同编号文本框不可写或编号不完整，未提交查询。'
            edit_write_unconfirmed='合同编号写入或回读未确认，未重复填写或提交查询。'
            edit_read_unconfirmed='合同编号未能完整回读，未提交查询或搜索ZIP。'
            accessible_unavailable='该位置没有可读取的原生控件值，未改用猜测或OCR读取WDMS。'
            conditions_text_unavailable='查询条件区没有完整可读的原生字段，未混用条件；请重新定位。'
            window_changed='目标窗口或布局已变化，未继续输入。'
            window_missing='目标窗口句柄无效或已关闭，未继续输入。'
            window_not_visible='目标窗口不可见，未继续输入。'
            window_minimized='目标窗口已最小化，未继续输入。'
            window_rect_failed='无法读取目标窗口坐标大小，未继续输入。'
            window_client_rect_failed='无法读取目标窗口客户区范围，未继续输入。'
            window_client_to_screen_failed='无法转换目标窗口客户区屏幕坐标，未继续输入。'
            window_obscured='目标读取区域被其他窗口遮挡，未继续识别或输入。'
            child_window_missing='目标窗口内采样点句柄无效，未继续识别。'
            file_clipboard_missing='剪贴板尚无文件，或远程桌面未开放文件剪贴板。'
            file_count_not_one='文件剪贴板不是唯一一个文件，未粘贴。'
            staging_window_changed='工具使用的暂存资源管理器窗口已变化，未导航或粘贴。'
        }
        $message = '本机控件、剪贴板或离线文字读取未确认；请重新定位或回传本轮运行日志。'
        if ($messages.ContainsKey($reason)) {
            $message = $messages[$reason]
        } elseif ($reason -like 'window_rect_changed:*') {
            $detail = $reason.Substring('window_rect_changed:'.Length).Trim()
            $message = "目标窗口位置或大小已变化（$detail），未继续输入。"
        } elseif ($reason -like 'window_pid_changed:*') {
            $detail = $reason.Substring('window_pid_changed:'.Length).Trim()
            $message = "目标窗口进程已变化（$detail），未继续输入。"
        } elseif ($reason -like 'window_not_foreground:*') {
            $detail = $reason.Substring('window_not_foreground:'.Length).Trim()
            $message = "目标窗口离开前台（$detail），未继续输入。"
        } elseif ($reason -like 'child_root_mismatch:*') {
            $detail = $reason.Substring('child_root_mismatch:'.Length).Trim()
            $message = "目标窗口内部区域所属窗口不匹配（$detail），未继续输入。"
        } elseif ($reason -like 'child_pid_mismatch:*') {
            $detail = $reason.Substring('child_pid_mismatch:'.Length).Trim()
            $message = "目标窗口内部区域进程不匹配（$detail），未继续输入。"
        } elseif ($reason -like 'child_not_visible:*') {
            $detail = $reason.Substring('child_not_visible:'.Length).Trim()
            $message = "目标窗口内部区域控件不可见（$detail），未继续输入。"
        }
        $code = if ($reason -eq 'file_count_not_one') { 'MOYU_CLIPBOARD_MULTIPLE' } else { 'MOYU_NATIVE_UNAVAILABLE' }
        $response = @{id=$request.id; ok=$false; error=@{code=$code; message=$message}}
    }
    [Console]::WriteLine(($response | ConvertTo-Json -Depth 16 -Compress))
    if ($null -ne $request -and $request.action -eq 'close') { break }
}
