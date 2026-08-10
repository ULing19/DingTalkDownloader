; 钉钉回放批量下载器 - Inno Setup 安装脚本
; 编译：ISCC.exe setup.iss
; 源文件目录：dist\DingTalkDownloader_Release（由 build_exe.bat 生成）

#define MyAppName "钉钉回放下载器"
#define MyAppNameEn "DingTalkDownloader"
#define MyAppVersion "1.0.1"
#define MyAppPublisher "DingTalkDownloader"
#define MyAppExeName "DingTalkDownloader.exe"
#define MyAppEngine "GoDingtalk_v2.5.2_windows_amd64.exe"

[Setup]
AppId={{A8F3C2E1-7B4D-4E9A-9C1F-2D6B8A0E5F31}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL=https://github.com/ULing19/DingTalkDownloader
AppSupportURL=https://github.com/ULing19/DingTalkDownloader/issues
AppUpdatesURL=https://github.com/ULing19/DingTalkDownloader/releases
Uninstallable=yes
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
; 输出到项目 dist 目录
OutputDir=..\dist
OutputBaseFilename=钉钉回放下载器_安装程序
SetupIconFile=..\assets\download.ico
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
DisableProgramGroupPage=no
UninstallDisplayIcon={app}\{#MyAppExeName}
; 安装信息
InfoBeforeFile=
LicenseFile=
; 安装后可打开使用说明
; 中文向导
ShowLanguageDialog=no
; 允许用户装到任意目录（绿色用户目录亦可）
UsePreviousAppDir=yes
DirExistsWarning=auto
; 附带卸载时删除可选数据的确认在 [Code] 中处理
CloseApplications=yes
RestartApplications=no
; 较大文件（含 ffmpeg）
DiskSpanning=no
InternalCompressLevel=ultra64

[Languages]
; 简体中文语言包放在 installer\ChineseSimplified.isl（随仓库分发，无需装到 Inno 目录）
Name: "chinesesimplified"; MessagesFile: "ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
; 默认创建桌面快捷方式，用户可在安装向导中取消
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加图标:"

[Files]
; 主程序与依赖（发布目录整包安装）
Source: "..\dist\DingTalkDownloader_Release\DingTalkDownloader.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\DingTalkDownloader_Release\GoDingtalk_v2.5.2_windows_amd64.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\DingTalkDownloader_Release\ffmpeg.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\使用说明.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\DingTalkDownloader_Release\README.txt"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\THIRD_PARTY_NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\assets\download.ico"; DestDir: "{app}\assets"; Flags: ignoreversion
; 预建空目录用占位（可选）
; video 与配置目录在 [Dirs] / [Code] 创建

[Dirs]
; 运行时目录由程序使用。仅在为空时由卸载器清理，避免误删用户视频和登录信息。
Name: "{app}\video"
Name: "{app}\.goDingtalkConfig"

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{group}\使用说明"; Filename: "{app}\使用说明.txt"; WorkingDir: "{app}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "立即运行 {#MyAppName}"; Flags: nowait postinstall skipifsilent
Filename: "{app}\使用说明.txt"; Description: "打开使用说明"; Flags: postinstall shellexec skipifsilent unchecked

[UninstallDelete]
; 卸载时清理运行时产生的空目录（用户视频与 cookies 见下方确认）
Type: dirifempty; Name: "{app}\video"
Type: dirifempty; Name: "{app}\.goDingtalkConfig"
Type: dirifempty; Name: "{app}"

[Code]
var
  DeleteUserData: Boolean;

function InitializeUninstall(): Boolean;
begin
  DeleteUserData := False;
  Result := True;
  if MsgBox('是否同时删除已下载的视频（video 文件夹）和登录配置（.goDingtalkConfig）？' + #13#10 + #13#10 +
            '选择「是」：彻底清除' + #13#10 +
            '选择「否」：仅卸载程序，保留视频与登录信息',
            mbConfirmation, MB_YESNO) = IDYES then
  begin
    DeleteUserData := True;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    if DeleteUserData then
    begin
      DelTree(ExpandConstant('{app}\video'), True, True, True);
      DelTree(ExpandConstant('{app}\.goDingtalkConfig'), True, True, True);
    end;
  end;
end;
