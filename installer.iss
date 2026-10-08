; Inno Setup 6 script  -  Live Wallpaper
#ifndef AppVersion
  #define AppVersion "2.0.0"
#endif

[Setup]
AppId={{B7C4E2A1-5D3F-4E6A-9A21-7F0C8D1E4B52}
AppName=Live Wallpaper
AppVersion={#AppVersion}
AppPublisher=Live Wallpaper
DefaultDirName={autopf}\LiveWallpaper
DefaultGroupName=Live Wallpaper
DisableProgramGroupPage=yes
; 管理者権限不要 (ユーザー単位でインストール)
PrivilegesRequired=lowest
OutputDir=installer_output
OutputBaseFilename=LiveWallpaper-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\LiveWallpaper.exe
; 実行中チェックはしない (トレイ常駐アプリでアンインストールが止まるため)。
; 代わりに下の [Code] でインストール/アンインストール前にアプリを自動終了する。
CloseApplications=no

[Languages]
Name: "japanese"; MessagesFile: "compiler:Languages\Japanese.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "startup"; Description: "Windows 起動時に自動で実行する"; GroupDescription: "オプション:"
Name: "desktopicon"; Description: "デスクトップにショートカットを作成"; GroupDescription: "オプション:"; Flags: unchecked

[Files]
Source: "dist\LiveWallpaper\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion createallsubdirs

[Icons]
Name: "{autoprograms}\Live Wallpaper"; Filename: "{app}\LiveWallpaper.exe"
Name: "{autodesktop}\Live Wallpaper"; Filename: "{app}\LiveWallpaper.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "LiveWallpaper"; ValueData: """{app}\LiveWallpaper.exe"""; Flags: uninsdeletevalue; Tasks: startup

[Run]
Filename: "{app}\LiveWallpaper.exe"; Description: "Live Wallpaper を今すぐ起動"; Flags: nowait postinstall skipifsilent

[Code]
procedure KillApp();
var
  rc: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM LiveWallpaper.exe', '', SW_HIDE, ewWaitUntilTerminated, rc);
  Sleep(800);
end;

function InitializeSetup(): Boolean;
begin
  KillApp();
  Result := True;
end;

function InitializeUninstall(): Boolean;
begin
  KillApp();
  Result := True;
end;
