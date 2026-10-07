[CmdletBinding()]
param(
    [ValidateRange(10, 3600)]
    [double]$DurationSeconds = 60,

    [ValidateRange(0.05, 20)]
    [double]$ReplaySpeed = 2.0,

    [ValidateRange(640, 3840)]
    [int]$WindowWidth = 800,

    [ValidateRange(400, 2160)]
    [int]$WindowHeight = 480
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Clip times are offsets into the recorded MP4, not the source CSV timeline.
# Keep each clip within DurationSeconds when changing the recording length.
$WebPClips = @(
    [pscustomobject]@{ Name = "cold-start";   StartSeconds = 2;  DurationSeconds = 4 },
    [pscustomobject]@{ Name = "acceleration"; StartSeconds = 16; DurationSeconds = 5 },
    [pscustomobject]@{ Name = "braking";      StartSeconds = 25; DurationSeconds = 5 },
    [pscustomobject]@{ Name = "dpf";          StartSeconds = 47; DurationSeconds = 5 }
)

$RepoRoot = $PSScriptRoot
$OutputDirectory = Join-Path $RepoRoot "website-export"
$Mp4Path = Join-Path $OutputDirectory "foxdash-demo.mp4"
$ReplayScript = Join-Path $RepoRoot "scripts\windows\run_replay.bat"
$EnsureEnvironmentScript = Join-Path $RepoRoot "scripts\windows\ensure_env.bat"
$WindowTitle = "FoxDash Website Export"
$NativeConsoleFontHeight = 16
$script:FoxDashProcess = $null
$script:FfmpegProcess = $null
$script:FoxDashWindowHandle = [IntPtr]::Zero
$script:LaunchGate = $null
$script:FontReadyGate = $null

if ($env:OS -ne "Windows_NT") {
    throw "WebsiteExport.ps1 only supports Windows."
}

Add-Type -TypeDefinition @"
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;

public struct FoxDashWindowRect
{
    public int Left;
    public int Top;
    public int Right;
    public int Bottom;
}

public static class FoxDashWindowApi
{
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool EnumWindows(EnumWindowsProc callback, IntPtr lParam);

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool IsWindowVisible(IntPtr hWnd);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int count);

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool MoveWindow(IntPtr hWnd, int x, int y, int width, int height, bool repaint);

    [DllImport("user32.dll", EntryPoint = "GetWindowLongPtr")]
    public static extern IntPtr GetWindowLongPtr(IntPtr hWnd, int index);

    [DllImport("user32.dll")]
    public static extern IntPtr GetMenu(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern uint GetDpiForWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool AdjustWindowRectEx(ref FoxDashWindowRect rect, uint style, bool menu, uint extendedStyle);

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool AdjustWindowRectExForDpi(ref FoxDashWindowRect rect, uint style, bool menu, uint extendedStyle, uint dpi);

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool SetWindowPos(IntPtr hWnd, IntPtr insertAfter, int x, int y, int width, int height, uint flags);

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool GetWindowRect(IntPtr hWnd, out FoxDashWindowRect rect);

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool GetClientRect(IntPtr hWnd, out FoxDashWindowRect rect);

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool SetForegroundWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool PostMessage(IntPtr hWnd, uint message, IntPtr wParam, IntPtr lParam);

    public static Dictionary<IntPtr, string> VisibleWindows()
    {
        var windows = new Dictionary<IntPtr, string>();
        EnumWindows(delegate (IntPtr hWnd, IntPtr lParam) {
            if (!IsWindowVisible(hWnd)) return true;
            var text = new StringBuilder(512);
            if (GetWindowText(hWnd, text, text.Capacity) > 0) windows[hWnd] = text.ToString();
            return true;
        }, IntPtr.Zero);
        return windows;
    }
}
"@

function Format-FileSize([long]$Bytes) {
    if ($Bytes -ge 1MB) { return "{0:N2} MiB" -f ($Bytes / 1MB) }
    if ($Bytes -ge 1KB) { return "{0:N1} KiB" -f ($Bytes / 1KB) }
    return "$Bytes bytes"
}

function ConvertTo-ProcessArguments([string[]]$Arguments) {
    return (($Arguments | ForEach-Object {
        if ($_ -eq "") { '""' }
        elseif ($_ -match '[\s"]') { '"' + $_.Replace('"', '\"') + '"' }
        else { $_ }
    }) -join " ")
}

function Start-And-WaitForProcess([string]$FilePath, [string[]]$Arguments) {
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $FilePath
    $startInfo.Arguments = ConvertTo-ProcessArguments $Arguments
    $startInfo.WorkingDirectory = $RepoRoot
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true

    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    if (-not $process.Start()) {
        throw "Could not start $FilePath."
    }

    $script:FfmpegProcess = $process
    $standardOutput = $process.StandardOutput.ReadToEndAsync()
    $standardError = $process.StandardError.ReadToEndAsync()
    $process.WaitForExit()
    $exitCode = $process.ExitCode
    $output = $standardOutput.Result
    $errorOutput = $standardError.Result
    $process.Dispose()
    $script:FfmpegProcess = $null
    return [pscustomobject]@{
        ExitCode = $exitCode
        Output = $output
        ErrorOutput = $errorOutput
    }
}

function Stop-ProcessTree([int]$RootProcessId) {
    $children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $RootProcessId" -ErrorAction SilentlyContinue)
    foreach ($child in $children) {
        Stop-ProcessTree -RootProcessId ([int]$child.ProcessId)
    }
    Stop-Process -Id $RootProcessId -Force -ErrorAction SilentlyContinue
}

function Find-FoxDashWindow {
    foreach ($entry in [FoxDashWindowApi]::VisibleWindows().GetEnumerator()) {
        if ($entry.Value -eq $WindowTitle -or $entry.Value -like "*$WindowTitle*") {
            return [pscustomobject]@{ Handle = $entry.Key; Title = $entry.Value }
        }
    }
    return $null
}

function Set-FoxDashClientSize([IntPtr]$Handle, [int]$Width, [int]$Height) {
    # gdigrab's title mode records the client area. Compute the decorated outer
    # rectangle from the real style and per-window DPI; do not subtract a fixed
    # title-bar size or squeeze FoxDash into an 800x480 outer rectangle.
    $GWL_STYLE = -16
    $GWL_EXSTYLE = -20
    $SWP_NOZORDER = 0x0004
    $SWP_NOACTIVATE = 0x0010
    $style = [uint32][FoxDashWindowApi]::GetWindowLongPtr($Handle, $GWL_STYLE).ToInt64()
    $extendedStyle = [uint32][FoxDashWindowApi]::GetWindowLongPtr($Handle, $GWL_EXSTYLE).ToInt64()
    $hasMenu = [FoxDashWindowApi]::GetMenu($Handle) -ne [IntPtr]::Zero
    $dpi = [FoxDashWindowApi]::GetDpiForWindow($Handle)
    $outerRect = New-Object FoxDashWindowRect
    $outerRect.Right = $Width
    $outerRect.Bottom = $Height

    $adjusted = if ($dpi -gt 0) {
        [FoxDashWindowApi]::AdjustWindowRectExForDpi([ref]$outerRect, $style, $hasMenu, $extendedStyle, $dpi)
    } else {
        [FoxDashWindowApi]::AdjustWindowRectEx([ref]$outerRect, $style, $hasMenu, $extendedStyle)
    }
    if (-not $adjusted) {
        throw "FoxDash's window frame could not be calculated for the current DPI."
    }

    $outerWidth = $outerRect.Right - $outerRect.Left
    $outerHeight = $outerRect.Bottom - $outerRect.Top
    if (-not [FoxDashWindowApi]::SetWindowPos($Handle, [IntPtr]::Zero, 40, 40, $outerWidth, $outerHeight, ($SWP_NOZORDER -bor $SWP_NOACTIVATE))) {
        throw "FoxDash's window was found, but it could not be resized for recording."
    }
    Start-Sleep -Milliseconds 300

    # Console scrollbars may change after the first resize. Correct from the
    # measured client rectangle without assuming a scrollbar or border width.
    for ($attempt = 0; $attempt -lt 3; $attempt++) {
        $clientRect = New-Object FoxDashWindowRect
        $windowRect = New-Object FoxDashWindowRect
        if (-not [FoxDashWindowApi]::GetClientRect($Handle, [ref]$clientRect) -or
            -not [FoxDashWindowApi]::GetWindowRect($Handle, [ref]$windowRect)) {
            throw "FoxDash's client dimensions could not be verified after resizing."
        }
        $clientWidth = $clientRect.Right - $clientRect.Left
        $clientHeight = $clientRect.Bottom - $clientRect.Top
        if ($clientWidth -eq $Width -and $clientHeight -eq $Height) { return }

        $measuredOuterWidth = $windowRect.Right - $windowRect.Left
        $measuredOuterHeight = $windowRect.Bottom - $windowRect.Top
        $correctedOuterWidth = $measuredOuterWidth + ($Width - $clientWidth)
        $correctedOuterHeight = $measuredOuterHeight + ($Height - $clientHeight)
        if (-not [FoxDashWindowApi]::SetWindowPos($Handle, [IntPtr]::Zero, 40, 40, $correctedOuterWidth, $correctedOuterHeight, ($SWP_NOZORDER -bor $SWP_NOACTIVATE))) {
            throw "FoxDash's window could not be corrected to the requested client dimensions."
        }
        Start-Sleep -Milliseconds 250
    }
    throw "FoxDash's client area must be ${Width}x${Height}, but the window host reported ${clientWidth}x${clientHeight}."
}

function Stop-FoxDash {
    if ($script:FoxDashWindowHandle -ne [IntPtr]::Zero) {
        # WM_CLOSE gives Textual and the replay runtime a chance to shut down cleanly.
        [void][FoxDashWindowApi]::PostMessage($script:FoxDashWindowHandle, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero)
        Start-Sleep -Milliseconds 750
    }

    if ($null -ne $script:FoxDashProcess -and -not $script:FoxDashProcess.HasExited) {
        Stop-ProcessTree -RootProcessId $script:FoxDashProcess.Id
        $script:FoxDashProcess.WaitForExit(3000) | Out-Null
    }
}

$ffmpegCommand = Get-Command ffmpeg.exe -ErrorAction SilentlyContinue
if ($null -eq $ffmpegCommand) {
    throw "FFmpeg was not found on PATH. Install a Windows FFmpeg build, open a new terminal, and confirm that 'ffmpeg -version' succeeds."
}
$FfmpegPath = $ffmpegCommand.Source

foreach ($clip in $WebPClips) {
    if (($clip.StartSeconds + $clip.DurationSeconds) -gt $DurationSeconds) {
        throw "WebP clip '$($clip.Name)' ends after the configured $DurationSeconds-second recording. Adjust DurationSeconds or the WebPClips table near the top of WebsiteExport.ps1."
    }
}

if (-not (Test-Path -LiteralPath $ReplayScript)) {
    throw "The existing Windows replay launcher was not found: $ReplayScript"
}

try {
    Write-Host "[1/5] Checking the normal FoxDash Windows development environment..."
    & $EnsureEnvironmentScript
    if ($LASTEXITCODE -ne 0) {
        throw "FoxDash environment setup/build failed with exit code $LASTEXITCODE."
    }

    New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
    Get-ChildItem -LiteralPath $OutputDirectory -Filter "foxdash-demo-*.webp" -File -ErrorAction SilentlyContinue |
        Remove-Item -Force
    Remove-Item -LiteralPath $Mp4Path -Force -ErrorAction SilentlyContinue

    Write-Host "[2/5] Starting the existing replay simulator at ${ReplaySpeed}x..."
    # Developer shells and automation hosts may export NO_COLOR or TERM=dumb.
    # Do not let those parent-only settings push Textual into its grayscale
    # fallback. Window geometry is handled separately and does not change the
    # console font or reduce FoxDash content to make room for window chrome.
    $gateName = "Local\FoxDashWebsiteExport-$PID-$([Guid]::NewGuid().ToString('N'))"
    $fontReadyGateName = "$gateName-font-ready"
    $createdGate = $false
    $script:LaunchGate = New-Object System.Threading.EventWaitHandle($false, [Threading.EventResetMode]::ManualReset, $gateName, [ref]$createdGate)
    $createdFontReadyGate = $false
    $script:FontReadyGate = New-Object System.Threading.EventWaitHandle($false, [Threading.EventResetMode]::ManualReset, $fontReadyGateName, [ref]$createdFontReadyGate)
    $escapedReplayScript = $ReplayScript.Replace("'", "''")
    $consoleSetupScript = @"
`$env:NO_COLOR = `$null
`$env:TERM = 'xterm-256color'
`$env:COLORTERM = 'truecolor'
`$Host.UI.RawUI.WindowTitle = '$WindowTitle'
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

[StructLayout(LayoutKind.Sequential)]
public struct FoxDashConsoleCoord
{
    public short X;
    public short Y;
}

[StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
public struct FoxDashConsoleFont
{
    public uint cbSize;
    public uint nFont;
    public FoxDashConsoleCoord dwFontSize;
    public int FontFamily;
    public int FontWeight;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)]
    public string FaceName;
}

public static class FoxDashConsoleApi
{
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern IntPtr GetStdHandle(int handle);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool GetCurrentConsoleFontEx(IntPtr output, bool maximumWindow, ref FoxDashConsoleFont font);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool SetCurrentConsoleFontEx(IntPtr output, bool maximumWindow, ref FoxDashConsoleFont font);
}
'@
`$output = [FoxDashConsoleApi]::GetStdHandle(-11)
`$font = New-Object FoxDashConsoleFont
`$font.cbSize = [Runtime.InteropServices.Marshal]::SizeOf([type][FoxDashConsoleFont])
if (-not [FoxDashConsoleApi]::GetCurrentConsoleFontEx(`$output, `$false, [ref]`$font)) { throw 'Could not read the export console font.' }
`$font.dwFontSize.X = 0
`$font.dwFontSize.Y = $NativeConsoleFontHeight
`$font.FontFamily = 54
`$font.FontWeight = 400
`$font.FaceName = 'Consolas'
if (-not [FoxDashConsoleApi]::SetCurrentConsoleFontEx(`$output, `$false, [ref]`$font)) { throw 'Could not set the native export console grid.' }
`$verifiedFont = New-Object FoxDashConsoleFont
`$verifiedFont.cbSize = [Runtime.InteropServices.Marshal]::SizeOf([type][FoxDashConsoleFont])
if (-not [FoxDashConsoleApi]::GetCurrentConsoleFontEx(`$output, `$false, [ref]`$verifiedFont) -or `$verifiedFont.FaceName -ne 'Consolas' -or `$verifiedFont.dwFontSize.Y -ne $NativeConsoleFontHeight) { throw 'The export console did not apply the required Consolas font.' }
`$fontReadyGate = [Threading.EventWaitHandle]::OpenExisting('$fontReadyGateName')
try {
    [void]`$fontReadyGate.Set()
} finally {
    `$fontReadyGate.Dispose()
}
`$launchGate = [Threading.EventWaitHandle]::OpenExisting('$gateName')
try {
    if (-not `$launchGate.WaitOne([TimeSpan]::FromSeconds(30))) { throw 'Timed out waiting for the export window size.' }
} finally {
    `$launchGate.Dispose()
}
`$consoleSize = `$Host.UI.RawUI.WindowSize
`$Host.UI.RawUI.BufferSize = `$consoleSize
& '$escapedReplayScript' --replay-speed '$($ReplaySpeed.ToString([Globalization.CultureInfo]::InvariantCulture))' --no-frame-counter
exit `$LASTEXITCODE
"@
    $encodedConsoleSetup = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($consoleSetupScript))
    $script:FoxDashProcess = Start-Process -FilePath "powershell.exe" -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', $encodedConsoleSetup) -WorkingDirectory $RepoRoot -PassThru

    if (-not $script:FontReadyGate.WaitOne([TimeSpan]::FromSeconds(15))) {
        throw "The FoxDash export console did not initialise its native grid within 15 seconds."
    }

    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    $window = $null
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($script:FoxDashProcess.HasExited) {
            throw "FoxDash simulator exited before its window became ready (exit code $($script:FoxDashProcess.ExitCode))."
        }
        $window = Find-FoxDashWindow
        if ($null -ne $window) { break }
        Start-Sleep -Milliseconds 200
    }
    if ($null -eq $window) {
        throw "FoxDash started, but a window titled '$WindowTitle' was not found within 30 seconds."
    }

    $script:FoxDashWindowHandle = $window.Handle
    Set-FoxDashClientSize -Handle $window.Handle -Width $WindowWidth -Height $WindowHeight
    [void][FoxDashWindowApi]::SetForegroundWindow($window.Handle)
    [void]$script:LaunchGate.Set()
    Start-Sleep -Seconds 2
    if ($script:FoxDashProcess.HasExited -or $null -eq (Find-FoxDashWindow)) {
        throw "FoxDash's window closed while waiting for the dashboard to render."
    }
    Set-FoxDashClientSize -Handle $window.Handle -Width $WindowWidth -Height $WindowHeight
    Write-Host "      Verified FoxDash client area: ${WindowWidth}x${WindowHeight}"

    Write-Host "[3/5] Recording the FoxDash window for $DurationSeconds seconds..."
    $captureArguments = @(
        '-hide_banner', '-y',
        '-f', 'gdigrab',
        '-framerate', '30',
        '-draw_mouse', '0',
        '-i', "title=$($window.Title)",
        '-t', $DurationSeconds.ToString([Globalization.CultureInfo]::InvariantCulture),
        '-vf', 'scale=1280:768:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos,pad=1280:768:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,format=yuv420p',
        '-an',
        '-c:v', 'libx264',
        '-preset', 'medium',
        '-crf', '27',
        '-movflags', '+faststart',
        $Mp4Path
    )
    $captureResult = Start-And-WaitForProcess -FilePath $FfmpegPath -Arguments $captureArguments
    if ($captureResult.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $Mp4Path)) {
        throw "FFmpeg window recording failed with exit code $($captureResult.ExitCode).`n$($captureResult.ErrorOutput.Trim())"
    }

    Stop-FoxDash

    Write-Host "[4/5] Creating animated WebP clips from the MP4..."
    $webpPaths = @()
    foreach ($clip in $WebPClips) {
        $webpPath = Join-Path $OutputDirectory ("foxdash-demo-{0}.webp" -f $clip.Name)
        $webpArguments = @(
            '-hide_banner', '-y',
            '-ss', ([double]$clip.StartSeconds).ToString([Globalization.CultureInfo]::InvariantCulture),
            '-i', $Mp4Path,
            '-t', ([double]$clip.DurationSeconds).ToString([Globalization.CultureInfo]::InvariantCulture),
            '-vf', 'fps=12,scale=720:432:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos,pad=720:432:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1',
            '-an',
            '-c:v', 'libwebp',
            '-preset', 'picture',
            '-compression_level', '6',
            '-quality', '68',
            '-loop', '0',
            $webpPath
        )
        $webpResult = Start-And-WaitForProcess -FilePath $FfmpegPath -Arguments $webpArguments
        if ($webpResult.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $webpPath)) {
            throw "FFmpeg failed to create '$($clip.Name)' WebP with exit code $($webpResult.ExitCode).`n$($webpResult.ErrorOutput.Trim())"
        }
        $webpPaths += $webpPath
    }

    Write-Host "[5/5] Website export complete."
    Write-Host ("  Recording: {0:N1} seconds" -f $DurationSeconds)
    $mp4 = Get-Item -LiteralPath $Mp4Path
    Write-Host ("  MP4:       {0} ({1})" -f $mp4.FullName, (Format-FileSize $mp4.Length))
    foreach ($webpPath in $webpPaths) {
        $webp = Get-Item -LiteralPath $webpPath
        Write-Host ("  WebP:      {0} ({1})" -f $webp.FullName, (Format-FileSize $webp.Length))
    }
}
finally {
    if ($null -ne $script:FontReadyGate) {
        $script:FontReadyGate.Dispose()
        $script:FontReadyGate = $null
    }
    if ($null -ne $script:LaunchGate) {
        [void]$script:LaunchGate.Set()
        $script:LaunchGate.Dispose()
        $script:LaunchGate = $null
    }
    if ($null -ne $script:FfmpegProcess -and -not $script:FfmpegProcess.HasExited) {
        Stop-Process -Id $script:FfmpegProcess.Id -Force -ErrorAction SilentlyContinue
    }
    Stop-FoxDash
}
