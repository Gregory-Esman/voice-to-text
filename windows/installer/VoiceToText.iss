; Voice-To-Text (Windows) - Inno Setup installer.
; Build:  "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" windows\installer\VoiceToText.iss
; Produces dist\Voice-To-Text-Setup.exe (single self-contained installer).
;
; Per-user install (no UAC to install). The one admin approval happens inside
; install.ps1 -Configure (machine-wide mic toggle + Defender exclusion).

#define AppName "Voice-To-Text"
#define AppVersion "0.2.0"
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
; v0.2.0 ships ~700 MB (torch) — lzma2/fast keeps the ISCC build minutes, not
; half-hours, for a modest size cost.
Compression=lzma2/fast
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#ExeName}
UninstallDisplayName={#AppName}
ChangesEnvironment=yes
ArchitecturesInstallIn64BitMode=x64compatible
; Upgrade handling: we force-kill any running Voice-To-Text in PrepareToInstall
; (below) BEFORE files are replaced, so the exe is never locked. Restart Manager's
; own close is disabled because it can't reliably close the tray app (it prompts
; "unable to close applications"). Same AppId installs over any prior version in
; place (settings + Groq key preserved).
CloseApplications=no

[Files]
; v0.2.0: PyInstaller onedir build — ship the whole folder (torch et al).
Source: "..\..\dist\VoiceToText\*"; DestDir: "{app}"; \
  Flags: ignoreversion recursesubdirs createallsubdirs
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

// True if a Groq key is already saved (User environment variable) on this PC.
function HasExistingKey: Boolean;
var v: String;
begin
  Result := RegQueryStringValue(HKCU, 'Environment', 'GROQ_API_KEY', v) and (Trim(v) <> '');
end;

procedure InitializeWizard;
var lbl: TNewStaticText;
begin
  KeyPage := CreateInputQueryPage(wpSelectDir,
    'Groq API key',
    'Voice-To-Text transcribes with Groq''s free cloud API.',
    'Paste your free Groq API key from https://console.groq.com/keys .' + #13#10 +
    'Leave this blank to keep an existing key, or to set one up later.');
  KeyPage.Add('Groq API key (starts with gsk_):', False);

  // Reassure returning users: a key is already set, so they can skip this.
  if HasExistingKey then
  begin
    lbl := TNewStaticText.Create(KeyPage);
    lbl.Parent := KeyPage.Surface;
    lbl.Left := KeyPage.Edits[0].Left;
    lbl.Top := KeyPage.Edits[0].Top + KeyPage.Edits[0].Height + ScaleY(12);
    lbl.AutoSize := True;
    lbl.Font.Style := [fsBold];
    lbl.Font.Color := $00006400;  // dark green (BGR)
    lbl.Caption := 'A Groq key is already saved on this PC - you can leave this blank to keep it.';
  end;
end;

// Force-close any running instance BEFORE files are replaced (runs pre-install).
function PrepareToInstall(var NeedsRestart: Boolean): String;
var rc: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM VoiceToText.exe /T', '',
       SW_HIDE, ewWaitUntilTerminated, rc);
  Sleep(500);
  Result := '';
end;

function GetGroqKey(Param: String): String;
begin
  Result := Trim(KeyPage.Values[0]);
end;

function HasGroqKey: Boolean;
begin
  Result := Length(GetGroqKey('')) > 0;
end;
