; The Seedbox 2 installer for Windows. A per-user install: no administrator rights, nothing outside the user's folders.
; Compiled by packaging/build_seedbox2.py, which passes:
;   /DAppVersion=<version> /DSourceDir=<the one-folder build> /DOutputDir=<folder> /DOutputName=<file name, no .exe>
;   /DWebView2Setup=<MicrosoftEdgeWebview2Setup.exe> /DIconFile=<Seedbox.ico>

#define AppName "Seedbox 2"
#define AppExe "Seedbox 2.exe"
; The WebView2 runtime's client id. Edge Update writes "pv" there when the runtime is installed.
#define WebView2Key "Microsoft\EdgeUpdate\Clients\{{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"

[Setup]
AppId={{6F1E8C42-7B1D-4C39-9A55-2D3F6B0C9E17}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Seedbox
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename={#OutputName}
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes

[Tasks]
Name: desktopicon; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#WebView2Setup}"; DestDir: "{tmp}"; DestName: "MicrosoftEdgeWebview2Setup.exe"; Flags: deleteafterinstall; Check: NeedsWebView2

[Icons]
Name: "{autoprograms}\{#AppName}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autoprograms}\{#AppName}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
; The small bootstrapper downloads the runtime. Windows 10 and 11 normally have it already, and then this is skipped.
Filename: "{tmp}\MicrosoftEdgeWebview2Setup.exe"; Parameters: "/silent /install"; StatusMsg: "Installing the Microsoft Edge WebView2 runtime..."; Check: NeedsWebView2
Filename: "{app}\{#AppExe}"; Description: "Open {#AppName}"; Flags: nowait postinstall skipifsilent

[Code]
function HasVersion(Root: Integer; SubKey: String): Boolean;
var
  Version: String;
begin
  Result := RegQueryStringValue(Root, SubKey, 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0');
end;

function NeedsWebView2: Boolean;
begin
  Result := not (HasVersion(HKLM32, 'SOFTWARE\{#WebView2Key}') or HasVersion(HKCU, 'Software\{#WebView2Key}'));
end;
