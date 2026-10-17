; MagnetoClip Inno Setup installer script.
; Compile with: ISCC.exe scripts/installer.iss  (run from project root)
;
; Keep MyAppVersion in step with src/magnetoclip/version.py, which is the single
; source of truth used by the release zip and manifest.
;
; Inno Setup resolves relative Source/OutputDir paths against the directory
; holding this script, not the working directory, so they are anchored on
; ProjectRoot, which defaults to the repository root one level up. It stays a
; #ifndef so a build system can override it with /DProjectRoot=... when the
; checkout path has no spaces (ISCC splits define values on spaces).

#ifndef ProjectRoot
  #define ProjectRoot ".."
#endif

#define MyAppName "MagnetoClip"
#define MyAppVersion "1.1.1"
#define MyAppPublisher "MagnetoClip"
#define MyAppExeName "MagnetoClip.exe"

[Setup]
AppId={{8E1F6C2A-4B3D-4A2F-9C7E-2D5B1A0F6E8A}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir={#ProjectRoot}\dist
OutputBaseFilename=MagnetoClip-Setup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; Paths reference ProjectRoot directly: ISCC substitutes a #define value
; literally, so an intermediate macro holding {#ProjectRoot} would not expand.
; The second entry recurses, so it also carries the bundled ffmpeg.exe that
; previews depend on.
Source: "{#ProjectRoot}\dist\MagnetoClip\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#ProjectRoot}\dist\MagnetoClip\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{userappdata}\MagnetoClip"
