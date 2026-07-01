<#
  Voice-To-Text - one-click setup (Windows, online / Groq build).

  Run it by double-clicking "Install Voice-To-Text.bat" in this folder.

  What it does:
    - copies VoiceToText.exe into %LOCALAPPDATA%\Programs\Voice-To-Text
    - asks for a free Groq API key and saves it (user environment variable)
    - writes sensible default settings (config.toml)
    - turns on start-at-login (the same Startup .cmd the app's tray toggle uses)
    - turns on microphone access (per-user now; machine-wide via one admin prompt)
    - adds a Windows Defender exclusion (via that same admin prompt)
    - launches the app

  Per-user steps run as you. Only the machine-wide microphone toggle and the
  Defender exclusion need admin, so those run in a single elevated sub-step.
#>
param(
    [switch]$Elevated,
    [string]$InstallDir
)

$ErrorActionPreference = 'Stop'
$AppName    = 'Voice-To-Text'
$ExeName    = 'VoiceToText.exe'
$MicKeyHKLM = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone'
$MicKeyHKCU = 'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone'

function Step($m){ Write-Host "`n>> $m" -ForegroundColor Cyan }
function Ok($m){   Write-Host "   [OK]  $m" -ForegroundColor Green }
function Note($m){ Write-Host "   [!]   $m" -ForegroundColor Yellow }

# ─────────── ELEVATED SUB-STEP: machine-wide settings only ───────────
if ($Elevated) {
    # 1) System-wide "Microphone access" + "let desktop apps use the mic" = Allow.
    try {
        New-Item -Path $MicKeyHKLM -Force | Out-Null
        Set-ItemProperty -Path $MicKeyHKLM -Name 'Value' -Value 'Allow'
        $np = Join-Path $MicKeyHKLM 'NonPackaged'
        New-Item -Path $np -Force | Out-Null
        Set-ItemProperty -Path $np -Name 'Value' -Value 'Allow'
    } catch { }
    # 2) Windows Defender folder exclusion (no-op if Defender is off / 3rd-party AV).
    if ($InstallDir) {
        try { Add-MpPreference -ExclusionPath $InstallDir -ErrorAction Stop } catch { }
    }
    exit 0
}

# ───────────────────────────── main (as the user) ─────────────────────────────
$InstallDir = Join-Path $env:LOCALAPPDATA "Programs\$AppName"

Write-Host ""
Write-Host "  ============================================" -ForegroundColor White
Write-Host "     Voice-To-Text  -  Setup" -ForegroundColor White
Write-Host "  ============================================" -ForegroundColor White

# 1. Copy the app in.
Step "Installing the app to $InstallDir"
$srcExe = Join-Path $PSScriptRoot $ExeName
if (-not (Test-Path $srcExe)) {
    Note "Can't find $ExeName next to this script. Make sure you unzipped the whole folder."
    Read-Host "Press Enter to exit"; exit 1
}
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Copy-Item $srcExe $InstallDir -Force
$exe = Join-Path $InstallDir $ExeName
Ok "Copied $ExeName"

# 2. Groq API key -> user environment variable (masked entry, not shown on screen).
Step "Groq API key (free)"
Write-Host "   Voice-To-Text transcribes using Groq's free cloud API."
Write-Host "   I'll open the key page - sign in, click 'Create API Key', copy it."
Start-Process "https://console.groq.com/keys" | Out-Null
$sec = Read-Host "   Paste your Groq API key (starts with gsk_)" -AsSecureString
$key = ''
if ($sec.Length -gt 0) {
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
    try { $key = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    $key = $key.Trim()
}
if ($key.Length -gt 0) {
    [Environment]::SetEnvironmentVariable('GROQ_API_KEY', $key, 'User')
    $env:GROQ_API_KEY = $key   # so the app we launch below sees it immediately
    Ok "Groq key saved (user environment variable GROQ_API_KEY)"
} else {
    Note "No key entered. The app installs, but won't transcribe until you set GROQ_API_KEY."
}

# 3. Default settings.
Step "Writing default settings"
$cfgDir  = Join-Path $env:APPDATA $AppName
New-Item -ItemType Directory -Force -Path $cfgDir | Out-Null
$cfgPath = Join-Path $cfgDir 'config.toml'
if (Test-Path $cfgPath) {
    Note "Existing settings kept ($cfgPath)"
} else {
    $cfg = @"
# Voice-To-Text settings. Each hotkey: tap once to start, tap again to stop.
[hotkey]
dictate_key = "f9"       # tap F9, speak, tap F9  -> your words are typed
command_key = "ctrl_r"   # tap Right Ctrl, speak an instruction -> it writes / edits
# Other options: "f9", "ctrl_r", "tilde", "shift+tilde". Avoid "alt" and "f10"
# (a lone tap of those pops the window menu bar and steals focus).
"@
    [IO.File]::WriteAllText($cfgPath, $cfg, (New-Object System.Text.UTF8Encoding($false)))
    Ok "Default settings written ($cfgPath)"
}

# 4. Start-at-login: the SAME Startup .cmd the app's tray "Start at login" uses,
#    so there's exactly one launcher (two launchers cause a mic-collision bug).
Step "Enabling start-at-login"
$startup = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup'
New-Item -ItemType Directory -Force -Path $startup | Out-Null
$cmdBody = "@echo off`r`nstart `"`" `"$exe`" --tray`r`n"
[IO.File]::WriteAllText((Join-Path $startup 'VoiceToText.cmd'), $cmdBody, (New-Object System.Text.ASCIIEncoding))
Ok "Voice-To-Text will start in the tray at login"

# 4b. A Start-menu shortcut (nice to have).
try {
    $lnk = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\$AppName.lnk"
    $sh  = New-Object -ComObject WScript.Shell
    $s   = $sh.CreateShortcut($lnk)
    $s.TargetPath = $exe; $s.Arguments = '--tray'; $s.WorkingDirectory = $InstallDir
    $s.Save()
    Ok "Added a Start-menu shortcut"
} catch { }

# 5. Microphone access - per-user part (no admin needed).
Step "Turning on microphone access (your account)"
try {
    New-Item -Path $MicKeyHKCU -Force | Out-Null
    Set-ItemProperty -Path $MicKeyHKCU -Name 'Value' -Value 'Allow'
    Ok "Microphone allowed for your account"
} catch { Note "Couldn't set per-user mic access automatically - I'll remind you below." }

# 6. Machine-wide mic toggle + Defender exclusion - one admin prompt.
Step "System microphone + antivirus settings (one admin approval)"
Write-Host "   Windows will ask for permission (UAC). Click Yes to finish setup."
try {
    Start-Process powershell -Verb RunAs -Wait -ArgumentList @(
        '-NoProfile','-ExecutionPolicy','Bypass','-File',"`"$PSCommandPath`"",
        '-Elevated','-InstallDir',"`"$InstallDir`"")
    Ok "System mic access + Defender exclusion applied"
} catch {
    Note "Admin step was declined or failed. See the checklist below."
}

# 7. Launch.
Step "Starting Voice-To-Text"
Start-Process -FilePath $exe -ArgumentList '--tray'
Ok "Running - look for the amber microphone icon in the system tray"

# 8. Wrap-up.
Write-Host ""
Write-Host "  ============================================" -ForegroundColor White
Write-Host "     Setup complete." -ForegroundColor Green
Write-Host "  ============================================" -ForegroundColor White
Write-Host ""
Write-Host "  How to use it:"
Write-Host "    - Tap F9, speak, tap F9 again  -> your words get typed where the cursor is."
Write-Host "    - Tap Right Ctrl, say an instruction, tap again -> it writes or edits text."
Write-Host "    - Settings & Quit live in the tray icon (bottom-right of the taskbar)."
Write-Host ""
Write-Host "  If dictation ever plays an error sound with no mic icon:" -ForegroundColor Yellow
Write-Host "    1) Settings > Privacy & security > Microphone: make sure it's ON and"
Write-Host "       'Let desktop apps access your microphone' is ON."
Write-Host "    2) If you use Bitdefender/Norton/etc., add this folder as an exception:"
Write-Host "       $InstallDir"
Write-Host "       (Windows Defender was handled automatically.)"
Write-Host ""
Read-Host "  Press Enter to close"
