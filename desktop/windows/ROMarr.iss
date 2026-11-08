#define AppName "ROMarr"
#define AppPublisher "MoveWeight"

[Setup]
AppId={{827D1AA9-4D11-47D7-A623-64E1FC4EBFA5}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Programs\ROMarr
DefaultGroupName={#AppName}
UninstallDisplayIcon={app}\ROMarr.exe
SetupIconFile="{#BundlePath}\ROMarr.ico"
OutputDir="{#OutputPath}"
OutputBaseFilename=ROMarr-{#AppVersion}-Setup-x64
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
PrivilegesRequired=lowest
WizardStyle=modern
Compression=lzma2/ultra64
SolidCompression=yes
DisableProgramGroupPage=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
Source: "{#BundlePath}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "ROMarr.portable"

[Icons]
Name: "{group}\ROMarr"; Filename: "{app}\ROMarr.exe"
Name: "{autodesktop}\ROMarr"; Filename: "{app}\ROMarr.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\ROMarr.exe"; Description: "Launch ROMarr"; Flags: nowait postinstall skipifsilent

[Code]
procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    DeleteFile(ExpandConstant('{app}\ROMarr.portable'));
end;
