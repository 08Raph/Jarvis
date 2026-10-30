; Inno Setup 6.3+ script for Project JARVIS. NOT YET COMPILED OR TESTED (no Windows/Inno in the authoring sandbox).
; Build:  iscc /DAppVersion=0.6.0 [/DArchName=x64 for Inno older than 6.3] packaging\installer.iss     (run scripts\build_windows.ps1 first)
; The Qwen3-1.7B model file IS shipped when the build was made with -BundleModel (scripts\build_windows.ps1): it sits in {app}\bundled_models and is copied into the user's own
; model folder on first start (src/jarvis/models/bundled.py). Training datasets are never bundled. A build made without -BundleModel simply has no bundled_models folder.
#ifndef ArchName
  ; x64compatible needs Inno Setup 6.3+. build_windows.ps1 passes /DArchName=x64 automatically for older 6.x versions.
  #define ArchName "x64compatible"
#endif
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{AE7B89F5-3AB7-427E-9C24-989358F05A9D}
AppName=Project JARVIS
AppVersion={#AppVersion}
AppPublisher=PROJECT JARVIS contributors
DefaultDirName={autopf}\ProjectJarvis
DefaultGroupName=Project JARVIS
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
MinVersion=10.0.17763
ArchitecturesAllowed={#ArchName}
ArchitecturesInstallIn64BitMode={#ArchName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
LicenseFile=..\LICENSE
OutputDir=..\dist\installer
OutputBaseFilename=ProjectJarvis-Setup-{#AppVersion}
UninstallDisplayName=Project JARVIS
CloseApplications=yes
InfoBeforeFile=..\docs\INSTALLER_NOTICE.txt

[Files]
Source: "..\dist\ProjectJarvis\*"; DestDir: "{app}"; Excludes: "bundled_models\*"; Flags: recursesubdirs ignoreversion
; The model is already 4-bit compressed: storing it as is keeps the build fast and the installer about the size of the file.
Source: "..\dist\ProjectJarvis\bundled_models\*"; DestDir: "{app}\bundled_models"; Flags: recursesubdirs ignoreversion nocompression skipifsourcedoesntexist
Source: "..\docs\INSTALLER_NOTICE.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\THIRD_PARTY_NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; Flags: unchecked

[Icons]
; Start menu entries are always created and are listed in the wizard text below; the desktop shortcut is an unchecked option.
Name: "{group}\Project JARVIS"; Filename: "{app}\jarvis-gui.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\Project JARVIS"; Filename: "{app}\jarvis-gui.exe"; WorkingDir: "{app}"; Tasks: desktopicon
Name: "{group}\Project JARVIS setup guide (command line)"; Filename: "{cmd}"; Parameters: "/k ""{app}\jarvis.exe"" setup"; WorkingDir: "{app}"
Name: "{group}\Project JARVIS (command line)"; Filename: "{cmd}"; Parameters: "/k ""{app}\jarvis.exe"" --help"; WorkingDir: "{app}"
Name: "{group}\Project JARVIS local sign-in server (this PC only)"; Filename: "{cmd}"; Parameters: "/k ""{app}\jarvis.exe"" local-server"; WorkingDir: "{app}"
Name: "{group}\Uninstall Project JARVIS"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\jarvis.exe"; Parameters: "first-run"; Description: "Create local data folders and check hardware (no network access)"; Flags: postinstall skipifsilent nowait runhidden
Filename: "{app}\jarvis-gui.exe"; Description: "Launch Project JARVIS (its setup wizard checks this PC and sets up the local AI)"; Flags: postinstall skipifsilent nowait

; Nothing here starts Project JARVIS at login, registers a service or scheduled task, or edits system settings.
; User data (models, memory, settings, backups) lives in %LOCALAPPDATA%\ProjectJarvis, outside {app},
; so upgrades keep it and uninstall asks before deleting it.

[Code]
function DataDir(): String;
begin
  Result := ExpandConstant('{localappdata}\ProjectJarvis');
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  // Upgrade safety: back up the memory database with the OLD build before files are replaced.
  if (CurStep = ssInstall) and FileExists(ExpandConstant('{app}\jarvis.exe')) then
    Exec(ExpandConstant('{app}\jarvis.exe'), 'backup', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usPostUninstall) and (not UninstallSilent) and DirExists(DataDir()) then
    if MsgBox('Also delete your Project JARVIS models, memory, missions, schedules, logs and settings?' + #13#10 + DataDir() + #13#10#13#10 +
              'Only this folder is removed. Choose No to keep everything for a later reinstall. Silent uninstalls always keep it.', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      DelTree(DataDir(), True, True, True);
end;
