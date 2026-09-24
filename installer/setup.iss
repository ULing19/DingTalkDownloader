; 钉钉回放批量下载器 - Inno Setup 安装脚本
; 编译：ISCC.exe setup.iss
; 源文件目录：dist\DingTalkDownloader_<版本>（由 build_exe.bat 生成）

#ifdef X86Build
  #define MyAppName "钉钉回放下载器 32位实验版"
  #define MyAppEngine "GoDingtalk_x86.exe"
  #define PackageSuffix "_x86"
#else
  #define MyAppName "钉钉回放下载器"
  #define MyAppEngine "GoDingtalk_v2.5.2_windows_amd64.exe"
  #define PackageSuffix ""
#endif
#define MyAppNameEn "DingTalkDownloader"
#ifndef MyAppVersion
  #define MyAppVersion "1.3.19"
#endif
#define MyAppPublisher "DingTalkDownloader"
#define MyAppExeName "DingTalkDownloader.exe"
#ifndef ReleaseDir
  #define ReleaseDir "..\dist\DingTalkDownloader_" + MyAppVersion
#endif

[Setup]
#ifdef X86Build
AppId={{F1B2C3D4-5E6F-4071-8293-A4B5C6D7E8F9}
#else
AppId={{A8F3C2E1-7B4D-4E9A-9C1F-2D6B8A0E5F31}
#endif
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
; 安装包与绿色版压缩包归集到同一个版本目录
OutputDir={#ReleaseDir}
; 使用 ASCII 文件名，便于不同 Git 客户端和浏览器稳定下载
OutputBaseFilename=DingTalkDownloader_{#MyAppVersion}{#PackageSuffix}_Setup
SetupIconFile=..\assets\download.ico
Compression=lzma2/normal
LZMANumBlockThreads=1
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
#ifndef X86Build
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
#endif
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
; 卸载仅移除安装文件，运行时视频和登录配置始终保留
CloseApplications=yes
RestartApplications=no
; 较大文件（含 ffmpeg）
DiskSpanning=no
InternalCompressLevel=normal

[Languages]
; 简体中文语言包放在 installer\ChineseSimplified.isl（随仓库分发，无需装到 Inno 目录）
Name: "chinesesimplified"; MessagesFile: "ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
; 默认创建桌面快捷方式，用户可在安装向导中取消
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加图标:"

[Files]
; 主程序与依赖（发布目录整包安装）
Source: "{#ReleaseDir}\DingTalkDownloader.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#ReleaseDir}\{#MyAppEngine}"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#ReleaseDir}\ffmpeg.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#ReleaseDir}\mediago.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#ReleaseDir}\使用说明.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#ReleaseDir}\回放链接一键获取说明.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#ReleaseDir}\README.txt"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "{#ReleaseDir}\PASSWORD_SHARE_BETA.md"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "{#ReleaseDir}\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#ReleaseDir}\THIRD_PARTY_NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#ReleaseDir}\ZBAR-LICENSE.txt"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "{#ReleaseDir}\PYZBAR-LICENSE.txt"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "{#ReleaseDir}\LIBICONV-NOTICE.txt"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "{#ReleaseDir}\WEBSOCKET_CLIENT_LICENSE.txt"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "{#ReleaseDir}\mediago_checksums.txt"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "{#ReleaseDir}\MEDIAGO_LICENSE.txt"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "{#ReleaseDir}\FFMPEG_LICENSE.txt"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "{#ReleaseDir}\FFMPEG_BUILD_INFO.txt"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "..\assets\download.ico"; DestDir: "{app}\assets"; Flags: ignoreversion
; 新安装预置可解析的空会话；升级和卸载都不得覆盖或删除用户登录状态。
Source: "{#ReleaseDir}\.goDingtalkConfig\config.json"; DestDir: "{app}\.goDingtalkConfig"; Flags: ignoreversion onlyifdoesntexist uninsneveruninstall
Source: "{#ReleaseDir}\.goDingtalkConfig\cookies.json"; DestDir: "{app}\.goDingtalkConfig"; Flags: ignoreversion onlyifdoesntexist uninsneveruninstall

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{group}\使用说明"; Filename: "{app}\使用说明.txt"; WorkingDir: "{app}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "立即运行 {#MyAppName}"; Flags: nowait postinstall skipifsilent
Filename: "{app}\使用说明.txt"; Description: "打开使用说明"; Flags: postinstall shellexec skipifsilent unchecked

[UninstallDelete]
; 仅删除空目录。用户视频、Cookies 和其他运行时文件始终不参与卸载删除。
Type: filesandordirs; Name: "{localappdata}\DingTalkReplayLinkCollector"
Type: dirifempty; Name: "{app}\video"
Type: dirifempty; Name: "{app}\.goDingtalkConfig"
Type: dirifempty; Name: "{app}"
