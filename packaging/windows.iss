; ZCode 续命监视器 Windows 安装包
; 构建: python packaging/build_windows.py
; 默认当前用户安装（不需要管理员）；安装向导会显示目录选择页，用户可改到任意盘符。
; 也可在安装模式对话框选择“为所有用户安装”（需要管理员）。

#define MyAppName "ZCode 续命监视器"
#define MyAppExeName "ZCodeResumeWatcher.exe"
#ifndef MyAppVersion
  #define MyAppVersion "1.2.0"
#endif

[Setup]
AppId={{B6A7E3D2-4C1F-4E8A-9B2D-7F5C1A3E9D44}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=ZCode Resume Watcher
DefaultDirName={autopf}\ZCodeResumeWatcher
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
DisableDirPage=no
UsePreviousAppDir=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist\installer
OutputBaseFilename=ZCodeResumeWatcher-{#MyAppVersion}-Setup
SetupIconFile=..\desktop\assets\app.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Tasks]
; 桌面快捷方式默认勾选；登录启动默认不勾选。
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："
Name: "startup"; Description: "登录 Windows 时自动启动"; GroupDescription: "附加任务："; Flags: unchecked

[Files]
Source: "..\dist\ZCodeResumeWatcher\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "ZCodeResumeWatcher"; ValueData: """{app}\{#MyAppExeName}"""; Flags: uninsdeletevalue; Tasks: startup

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent
