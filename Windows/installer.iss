#define MyAppName "LoudStream"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "Andrea Mazzurana"
#define MyAppExeName "LoudStream.exe"

; {#SourcePath} è automatico — Inno Setup lo risolve da solo
; basta che dist\ e ffmpeg_bin\ siano nella stessa cartella del .iss
#define SourceApp SourcePath + "dist\LoudStream"
#define SourceFfmpeg SourcePath + "ffmpeg_bin"
#define SourceIcons SourcePath + "\icons"

[Setup]
AppId={{B73015F5-B54B-41C0-AAD1-7748A105639C}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL=https://github.com/Jasssbo
AppSupportURL=https://github.com/Jasssbo/LoudStream/issues
AppUpdatesURL=https://github.com/Jasssbo/LoudStream/releases

; L'utente sceglie dove installare durante il wizard
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={#SourceIcons}\LoudStream.ico

ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
MinVersion=10.0

; Output nella cartella Output\ accanto al .iss — relativo e automatico
OutputDir={#SourcePath}Output
OutputBaseFilename=LoudStream_installer
SetupIconFile={#SourceIcons}\LoudStream.ico

; License agreement - user must accept before installing
LicenseFile={#SourcePath}\LICENSE

Compression=lzma
SolidCompression=no
WizardStyle=modern
AppMutex=LoudStreamMutex
UninstallDisplaySize=150000000

[Languages]
Name: "italian"; MessagesFile: "compiler:Languages\Italian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
italian.CreateStartMenu=Crea collegamento nel Menu Start
english.CreateStartMenu=Create Start Menu shortcut

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "startmenuicon"; Description: "{cm:CreateStartMenu}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "{#SourceApp}\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceApp}\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SourceFfmpeg}\ffmpeg.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceFfmpeg}\ffplay.exe"; DestDir: "{app}"; Flags: ignoreversion
; User-customizable folder - contains presets, metering_standards, email_template
Source: "{#SourcePath}customization\*"; DestDir: "{app}\customization"; Flags: ignoreversion recursesubdirs createallsubdirs

[Dirs]
Name: "{app}\logs"

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: startmenuicon
Name: "{group}\Disinstalla {#MyAppName}"; Filename: "{uninstallexe}"; Tasks: startmenuicon
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "taskkill"; Parameters: "/F /IM {#MyAppExeName}"; Flags: runhidden nowait; RunOnceId: "KillApp"
[UninstallDelete]
Type: filesandordirs; Name: "{app}\logs"

[Code]
function InitializeSetup(): Boolean;
begin
  Result := True;
  if not IsWin64 then
  begin
    MsgBox(
      'Questo programma richiede Windows 10 a 64 bit o superiore.',
      mbError, MB_OK
    );
    Result := False;
  end;
end;