; Inno Setup script for Ore Hold Watcher.
;
; Wraps the Nuitka --standalone program folder (dist\OreHoldWatcher) into an
; installer - a folder-based app plus an installer, never a self-extracting
; single exe (which trips AV dropper heuristics). Build it with:
;
;   build.bat                                   (calls ISCC when installed)
;   ISCC.exe packaging\ore-hold-watcher.iss     (manually)
;
; Install Inno Setup with:  winget install JRSoftware.InnoSetup

#define AppName "Ore Hold Watcher"
#define AppExe  "OreHoldWatcher.exe"
; no-spaces name for on-disk artifacts: the staged program folder is
; dist\OreHoldWatcher and release.yml attaches OreHoldWatcher-*-setup.exe
#define ArtifactName "OreHoldWatcher"
; CI passes the tag version with /DAppVersion=1.2.3
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{700FAE93-417A-461E-A25D-48A0D6BCF0FF}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=LittlePhish
VersionInfoVersion={#AppVersion}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; Per-user install by default: no UAC prompt (elevation is one more thing for
; endpoint protection to scrutinise), and the program folder stays writable so
; the in-app folder-swap updater works without elevation.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist
OutputBaseFilename={#ArtifactName}-{#AppVersion}-setup
SetupIconFile=..\icon.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExe}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
; The whole Nuitka standalone program folder.
Source: "..\dist\{#ArtifactName}\*"; Excludes: "update\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Settings, state and the mining ledger live in %APPDATA%\OreHoldWatcher and
; are intentionally left in place, so reinstalling keeps your data. Remove the
; updater's staging folder and only-empty app folders.
Type: filesandordirs; Name: "{app}\update"
Type: dirifempty; Name: "{app}"
