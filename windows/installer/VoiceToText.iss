; Voice-To-Text (Windows) - Inno Setup installer.
; Build:  "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" windows\installer\VoiceToText.iss
; Produces dist\Voice-To-Text-Setup.exe (single self-contained installer).
;
; Per-user install (no UAC to install). The one admin approval happens inside
; install.ps1 -Configure (machine-wide mic toggle + Defender exclusion).

#define AppName "Voice-To-Text"
#define AppVersion "0.1.3"
#define ExeName "VoiceToText.exe"
#define Publisher "Gregory Esman"

[Setup]
AppId={{7E2C9B14-8D3A-4F5E-A1C6-2B9D4E7F0A31}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#Publisher}
DefaultDirName={localappdata}\Programs\{#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\..\dist
OutputBaseFilename=Voice-To-Text-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#ExeName}
UninstallDisplayName={#AppName}
ChangesEnvironment=yes
ArchitecturesInstallIn64BitMode=x64compatible
; Upgrade handling: close any running Voice-To-Text before replacing files, so the
; exe isn't locked and two copies never fight over the mic. Same AppId means this
; installs over any prior version in place (settings + Groq key preserved).
CloseApplications=yes
RestartApplications=no

[Files]
Source: "..\..\dist\{#ExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "install.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "README-INSTALL.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\config.example.toml"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#ExeName}"; Parameters: "--tray"

[Registry]
; Persist the Groq key as a User environment variable (only if one was entered).
Root: HKCU; Subkey: "Environment"; ValueType: string; ValueName: "GROQ_API_KEY"; \
  ValueData: "{code:GetGroqKey}"; Flags: preservestringtype; Check: HasGroqKey

[Run]
; Configure: settings + autostart + mic access + Defender exclusion, then launch.
; This shows ONE UAC prompt (for the machine-wide mic toggle + Defender).
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\install.ps1"" -Configure -InstallDir ""{app}"""; \
  StatusMsg: "Configuring Voice-To-Text (approve the Windows prompt)..."; \
  Flags: waituntilterminated

[UninstallRun]
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\install.ps1"" -Uninstall -InstallDir ""{app}"""; \
  Flags: waituntilterminated runhidden; RunOnceId: "VttCleanup"

[Code]
var
  KeyPage: TInputQueryWizardPage;

procedure InitializeWizard;
begin
  KeyPage := CreateInputQueryPage(wpSelectDir,
    'Groq API key',
    'Voice-To-Text transcribes with Groq''s free cloud API.',
    'Paste your free Groq API key from https://console.groq.com/keys .' + #13#10 +
    'Leave this blank to keep an existing key, or to set one up later.');
  KeyPage.Add('Groq API key (starts with gsk_):', False);
end;

function GetGroqKey(Param: String): String;
begin
  Result := Trim(KeyPage.Values[0]);
end;

function HasGroqKey: Boolean;
begin
  Result := Length(GetGroqKey('')) > 0;
end;
