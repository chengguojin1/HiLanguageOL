; ============================================================
;  HiLanguageOL 俄语网课实时翻译助手 —— Inno Setup 安装脚本
;  编译: build.bat 会自动调用；也可手动：
;        "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" installer.iss
;  产物: installer\HiLanguageOL-Setup-<版本>.exe
; ============================================================

#define MyAppName "HiLanguageOL"
#define MyAppNameCN "俄语网课实时翻译助手"
#define MyAppVersion "1.0.6"
#define MyAppPublisher "HiLanguage"
#define MyAppExeName "HiLanguageOL.exe"
#define MyAppURL "https://github.com/hilanguage/HiLanguageOL"

[Setup]
; AppId 必须保持不变，升级安装才能覆盖旧版本
AppId={{B7E3F2A1-4D5C-4E8F-9A2B-1C3D5E7F9A1B}
AppName={#MyAppName} {#MyAppNameCN}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
AppCopyright=Copyright (C) 2026 {#MyAppPublisher}
VersionInfoVersion=1.0.6.0
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} {#MyAppNameCN} 安装程序
VersionInfoProductName={#MyAppName} {#MyAppNameCN}
VersionInfoProductVersion={#MyAppVersion}
VersionInfoCopyright=Copyright (C) 2026 {#MyAppPublisher}

; 默认按当前用户安装（不弹 UAC，装到 %LOCALAPPDATA%\Programs）；
; 安装向导里可切换为"为所有用户安装"（装到 Program Files，需要管理员）
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog commandline
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
AllowNoIcons=yes

; 目标平台：64 位 Windows 10 及以上
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0

; 程序正在运行时提示先关闭（与程序内单实例互斥体同名）
AppMutex=Global\hilanguage-ol-single-instance
CloseApplications=yes
RestartApplications=no

OutputDir=installer
OutputBaseFilename={#MyAppName}-Setup-{#MyAppVersion}
SetupIconFile=app\icon\app.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName} {#MyAppNameCN}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: checkedonce

[Files]
; 打包后的程序文件（整个目录，含 _internal）
Source: "dist\{#MyAppName}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Comment: "{#MyAppNameCN}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Comment: "{#MyAppNameCN}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 卸载默认保留用户数据（%APPDATA%\HiLanguageOL：配置、日志、课堂转写）。
; 如需一并删除，取消下面这行注释：
; Type: filesandordirs; Name: "{userappdata}\{#MyAppName}"
