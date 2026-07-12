<#
  Voice-To-Text setup engine (Windows, online / Groq build).

  Modes:
    (default)    Standalone: copy exe in, prompt for key, configure, launch.
                 Used when a user double-clicks "Install Voice-To-Text.bat".
    -Configure   Files are already installed (by the Inno Setup .exe); take the
                 Groq key via -GroqKey, write settings, enable autostart, set mic
                 access, add a Defender exclusion. Does NOT copy files or launch.
    -Uninstall   Reverse the per-user changes (autostart, settings, Defender
                 exclusion). Invoked by the Inno uninstaller.
    -Elevated    Internal: the machine-wide sub-step (HKLM mic + Defender), run
                 via a single UAC elevation.

  Per-user steps run as the user; only the machine-wide microphone toggle and the
  Defender exclusion need admin (one elevated sub-step).
#>
param(
    [switch]$Elevated,
    [switch]$Configure,
    [switch]$Uninstall,
    [string]$InstallDir,
    [string]$GroqKey
)

$ErrorActionPreference = 'Stop'
$AppName = 'Voice-To-Text'
$ExeName = 'VoiceToText.exe'
$MicHKLM = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone'
$MicHKCU = 'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone'

function Step($m){ Write-Host "`n>> $m" -ForegroundColor Cyan }
function Ok($m){   Write-Host "   [OK]  $m" -ForegroundColor Green }
function Note($m){ Write-Host "   [!]   $m" -ForegroundColor Yellow }

function Set-GroqKeyValue([string]$key){
    if ($key -and $key.Trim().Length -gt 0) {
        $k = $key.Trim()
        [Environment]::SetEnvironmentVariable('GROQ_API_KEY', $k, 'User')
        $env:GROQ_API_KEY = $k
        Ok "Groq key saved (GROQ_API_KEY)"
    } else {
        Note "No Groq key provided - set GROQ_API_KEY later, or the app won't transcribe."
    }
}

function Write-DefaultConfig {
    $cfgDir = Join-Path $env:APPDATA $AppName
    New-Item -ItemType Directory -Force -Path $cfgDir | Out-Null
    $cfgPath = Join-Path $cfgDir 'config.toml'
    if (Test-Path $cfgPath) { Note "Existing settings kept ($cfgPath)"; return }
    $cfg = @"
# Voice-To-Text settings. Each hotkey: tap once to start, tap again to stop.
[hotkey]
dictate_key = "tilde"        # tap the tilde key, speak, tap again -> your words are typed
command_key = "shift+tilde"  # hold Shift + tap tilde -> speak an instruction, it writes / edits
# The tilde key is suppressed while running, so it never types a backtick or ~.
# Other options: "f9", "ctrl_r", "tilde", "shift+tilde". Avoid "alt" and "f10".

[personal]
# YOUR name and email (or set them in the app: Settings tab). Used to spell
# them correctly when you dictate and for the "type my email" voice command.
name = ""
email = ""

[auto_dictate]
# Hands-free mode: a focused text box = live mic. OFF until you enroll your
# voice in the app's Settings; speech is verified on-device before upload.
enabled = false
"@
    [IO.File]::WriteAllText($cfgPath, $cfg, (New-Object System.Text.UTF8Encoding($false)))
    Ok "Default settings written ($cfgPath)"
}

function Enable-Autostart([string]$exe){
    $startup = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup'
    New-Item -ItemType Directory -Force -Path $startup | Out-Null
    $body = "@echo off`r`nstart `"`" `"$exe`" --tray`r`n"
    [IO.File]::WriteAllText((Join-Path $startup 'VoiceToText.cmd'), $body, (New-Object System.Text.ASCIIEncoding))
    Ok "Start-at-login enabled"
}

function Set-MicHKCU {
    try {
        New-Item -Path $MicHKCU -Force | Out-Null
        Set-ItemProperty -Path $MicHKCU -Name 'Value' -Value 'Allow'
        Ok "Microphone allowed (your account)"
    } catch { Note "Couldn't set per-user mic access automatically." }
}

function Disable-CommsDucking {
    # Sound > Communications > "Do nothing": stop Windows from ducking/muting other
    # audio (incl. our start cue) while the mic is active. Per-user, no admin.
    try {
        $k = 'HKCU:\Software\Microsoft\Multimedia\Audio'
        if (-not (Test-Path $k)) { New-Item -Path $k -Force | Out-Null }
        Set-ItemProperty -Path $k -Name 'UserDuckingPreference' -Value 3 -Type DWord
        Ok "Audio ducking set to 'Do nothing' (start cue won't get muted)"
    } catch { Note "Couldn't set the audio ducking preference." }
}

function Invoke-ElevatedStep([string]$dir, [switch]$ForUninstall){
    $a = @('-NoProfile','-ExecutionPolicy','Bypass','-File',"`"$PSCommandPath`"",'-Elevated','-InstallDir',"`"$dir`"")
    if ($ForUninstall) { $a += '-Uninstall' }
    try { Start-Process powershell -Verb RunAs -Wait -ArgumentList $a }
    catch { Note "Admin step skipped or declined." }
}

function Remove-Autostart {
    $c = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup\VoiceToText.cmd'
    if (Test-Path $c) { Remove-Item $c -Force; Ok "Removed start-at-login" }
}
function Remove-ConfigDir {
    $d = Join-Path $env:APPDATA $AppName
    if (Test-Path $d) { Remove-Item $d -Recurse -Force; Ok "Removed settings + logs" }
}

function Stop-RunningApp {
    # Kill any running instance (incl. the PyInstaller child) so an upgrade can
    # overwrite the exe and so two copies never fight over the mic (-9999 bug).
    $p = Get-CimInstance Win32_Process -Filter "Name='VoiceToText.exe'" -ErrorAction SilentlyContinue
    if ($p) {
        $p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Start-Sleep -Milliseconds 400
        Ok "Closed a previous Voice-To-Text instance"
    }
}

# ─────────── ELEVATED sub-step: machine-wide settings only ───────────
if ($Elevated) {
    if ($Uninstall) {
        try { Remove-MpPreference -ExclusionPath $InstallDir -ErrorAction Stop } catch { }
    } else {
        try {
            New-Item -Path $MicHKLM -Force | Out-Null
            Set-ItemProperty -Path $MicHKLM -Name 'Value' -Value 'Allow'
            $np = Join-Path $MicHKLM 'NonPackaged'
            New-Item -Path $np -Force | Out-Null
            Set-ItemProperty -Path $np -Name 'Value' -Value 'Allow'
        } catch { }
        if ($InstallDir) { try { Add-MpPreference -ExclusionPath $InstallDir -ErrorAction Stop } catch { } }
        # Remove a stale elevated autostart task left by an old source-style install
        # (harmless if none exists) so it can't relaunch a second instance at login.
        try { Unregister-ScheduledTask -TaskName 'VoiceToText' -Confirm:$false -ErrorAction Stop } catch { }
    }
    exit 0
}

# ─────────── UNINSTALL (per-user) — called by the Inno uninstaller ───────────
if ($Uninstall) {
    Step "Removing Voice-To-Text settings"
    Get-CimInstance Win32_Process -Filter "Name='VoiceToText.exe'" -ErrorAction SilentlyContinue |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Remove-Autostart
    Remove-ConfigDir
    if ($InstallDir) { Invoke-ElevatedStep $InstallDir -ForUninstall }
    Ok "Uninstall cleanup done"
    exit 0
}

# ─────────── CONFIGURE (files already installed by Setup.exe) ───────────
if ($Configure) {
    if (-not $InstallDir) { $InstallDir = Join-Path $env:LOCALAPPDATA "Programs\$AppName" }
    $exe = Join-Path $InstallDir $ExeName
    Step "Configuring Voice-To-Text"
    Stop-RunningApp
    if ($GroqKey) { Set-GroqKeyValue $GroqKey }
    # The installer may have written the key to the User environment; pull it into
    # this process so the app we launch below sees it without a re-login.
    $k = [Environment]::GetEnvironmentVariable('GROQ_API_KEY', 'User')
    if ($k) { $env:GROQ_API_KEY = $k; Ok "Groq key detected" } else { Note "No Groq key set yet." }
    Write-DefaultConfig
    Enable-Autostart $exe
    Set-MicHKCU
    Disable-CommsDucking
    Step "System microphone + antivirus (one admin approval)"
    Invoke-ElevatedStep $InstallDir
    Step "Starting Voice-To-Text"
    Start-Process -FilePath $exe -ArgumentList '--tray'
    Ok "Configuration complete"
    exit 0
}

# ─────────── STANDALONE (double-clicked .bat) — full flow ───────────
$InstallDir = Join-Path $env:LOCALAPPDATA "Programs\$AppName"
Write-Host ""
Write-Host "  ============================================" -ForegroundColor White
Write-Host "     Voice-To-Text  -  Setup" -ForegroundColor White
Write-Host "  ============================================" -ForegroundColor White

Step "Installing the app to $InstallDir"
# v0.2.0 builds are a folder (onedir); older builds were a single exe.
$srcDir = Join-Path $PSScriptRoot 'VoiceToText'
$srcExe = Join-Path $PSScriptRoot $ExeName
if (-not (Test-Path $srcDir) -and -not (Test-Path $srcExe)) {
    Note "Can't find the VoiceToText folder (or $ExeName) next to this script. Unzip the whole folder first."
    Read-Host "Press Enter to exit"; exit 1
}
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Stop-RunningApp
if (Test-Path $srcDir) {
    Copy-Item (Join-Path $srcDir '*') $InstallDir -Recurse -Force
    Ok "Copied the app folder"
} else {
    Copy-Item $srcExe $InstallDir -Force
    Ok "Copied $ExeName"
}
$exe = Join-Path $InstallDir $ExeName

Step "Groq API key (free)"
Write-Host "   Sign in, click 'Create API Key', copy it."
Start-Process "https://console.groq.com/keys" | Out-Null
$sec = Read-Host "   Paste your Groq API key (starts with gsk_)" -AsSecureString
$key = ''
if ($sec.Length -gt 0) {
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
    try { $key = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}
Set-GroqKeyValue $key

Write-DefaultConfig
Enable-Autostart $exe
try {
    $lnk = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\$AppName.lnk"
    $sh = New-Object -ComObject WScript.Shell
    $s = $sh.CreateShortcut($lnk); $s.TargetPath = $exe; $s.Arguments = '--tray'; $s.WorkingDirectory = $InstallDir; $s.Save()
    Ok "Added a Start-menu shortcut"
} catch { }
Set-MicHKCU
Disable-CommsDucking
Step "System microphone + antivirus (one admin approval)"
Invoke-ElevatedStep $InstallDir

Step "Starting Voice-To-Text"
Start-Process -FilePath $exe -ArgumentList '--tray'
Ok "Running - look for the amber microphone icon in the system tray"
Write-Host ""
Write-Host "  Setup complete. Tap F9 to dictate, Right Ctrl to write/edit." -ForegroundColor Green
Read-Host "  Press Enter to close"
