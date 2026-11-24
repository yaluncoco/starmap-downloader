#ifndef AppVersion
#define AppVersion "1.8.0"
#endif
#ifndef AppRoot
#define AppRoot "..\dist\StarmapDownloader"
#endif
#ifndef OutputRoot
#define OutputRoot "..\dist"
#endif

[Setup]
AppId={{64D676BE-9FB7-4DC8-BA41-F498E21BC854}
AppName=星图影像下载器
AppVersion={#AppVersion}
AppPublisher=星图影像下载器项目
#ifdef ReleaseRepository
AppPublisherURL=https://github.com/{#ReleaseRepository}
AppSupportURL=https://github.com/{#ReleaseRepository}/issues
#endif
DefaultDirName={localappdata}\Programs\StarmapDownloader
DefaultGroupName=星图影像下载器
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputRoot}
OutputBaseFilename=StarmapDownloader-Setup-{#AppVersion}-win-x64
Compression=lzma2/fast
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\ui\assets\globe-orbit.ico
ChangesAssociations=yes
DisableWelcomePage=no
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\_internal\ui\assets\globe-orbit.ico
CloseApplications=yes
RestartApplications=no
VersionInfoVersion={#AppVersion}

[Languages]
Name: "chinesesimp"; MessagesFile: "ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
chinesesimp.Launch=打开星图影像下载器
english.Launch=Open Starmap Downloader
chinesesimp.Desktop=创建桌面快捷方式
english.Desktop=Create a desktop shortcut
chinesesimp.Prerequisite=桌面界面需要 Microsoft Edge WebView2 Runtime。安装程序不另外下载运行时；如本机未安装，请从微软官网下载。
english.Prerequisite=The desktop app requires Microsoft Edge WebView2 Runtime. Setup does not download it; install it from Microsoft if missing.
chinesesimp.MissingRuntime=缺少 Microsoft Edge WebView2 Runtime。请从微软官网安装 Evergreen Runtime 后重试：https://developer.microsoft.com/microsoft-edge/webview2/#download-section
english.MissingRuntime=Microsoft Edge WebView2 Runtime is missing. Install the Evergreen Runtime from https://developer.microsoft.com/microsoft-edge/webview2/#download-section and retry.

[Tasks]
Name: "desktopicon"; Description: "{cm:Desktop}"

[Files]
Source: "{#AppRoot}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "*.log,*.partial.tif,*.part,*.local.json,preferences.json,accounts.v1.dpapi,accounts.v1.dpapi.*.tmp"

[Icons]
Name: "{group}\星图影像下载器"; Filename: "{app}\StarmapDownloader.exe"; IconFilename: "{app}\_internal\ui\assets\globe-orbit.ico"; IconIndex: 0
Name: "{userdesktop}\星图影像下载器"; Filename: "{app}\StarmapDownloader.exe"; IconFilename: "{app}\_internal\ui\assets\globe-orbit.ico"; IconIndex: 0; Check: ShouldUpdateDesktopIcon

[Run]
Filename: "{app}\StarmapDownloader.exe"; Description: "{cm:Launch}"; Flags: nowait postinstall skipifsilent unchecked

[Code]
function ShouldUpdateDesktopIcon(): Boolean;
begin
  Result := WizardIsTaskSelected('desktopicon') or FileExists(ExpandConstant('{userdesktop}\星图影像下载器.lnk'));
end;

function HasRuntimeVersion(RootKey: Integer): Boolean;
var
  Version: string;
begin
  Result := RegQueryStringValue(RootKey, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0');
end;

function InitializeSetup(): Boolean;
begin
  Result := HasRuntimeVersion(HKLM32) or HasRuntimeVersion(HKCU64) or HasRuntimeVersion(HKCU32);
  if not Result then
    SuppressibleMsgBox(CustomMessage('MissingRuntime'), mbError, MB_OK, IDOK);
end;

procedure InitializeWizard();
begin
  WizardForm.WelcomeLabel2.Caption := WizardForm.WelcomeLabel2.Caption + #13#10#13#10 + CustomMessage('Prerequisite');
end;
