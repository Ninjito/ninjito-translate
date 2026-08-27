; Wraps the PyInstaller onedir output in a single Windows installer.
;
; Installs per-user into %LOCALAPPDATA% on purpose, NOT into Program Files.
; The app writes config.json and logs/ next to its EXE, and on first voice
; use it downloads a ~500 MB Whisper model into <exe dir>\models. None of
; that is writable in Program Files without elevation, so a machine-wide
; install would look like the app silently failing.
;
; AppVersion is supplied by CI:  ISCC /DAppVersion=0.1.0 ...

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

#define AppName "Ninjito Translate"
#define AppExe  "NinjitoTranslate.exe"

[Setup]
AppId={{7B3C2E14-9D4A-4F6B-8C21-5A0E6F1D93B7}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Ninjito
VersionInfoVersion={#AppVersion}

; Per-user install: no UAC prompt, and the app can write beside its own EXE.
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\NinjitoTranslate
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes

SourceDir=..
OutputDir=installer\out
OutputBaseFilename=NinjitoTranslate-{#AppVersion}-windows
SetupIconFile=gg.ico
UninstallDisplayIcon={app}\{#AppExe}
WizardStyle=modern

; Show the MIT terms during setup. build.py also drops LICENSE.txt beside
; the EXE -- the license requires the notice to travel with every copy, so
; showing it once in the wizard is not enough on its own.
LicenseFile=LICENSE

; The payload is a few hundred MB of DLLs and OCR data, so max compression
; is worth the extra CI minutes -- it comes straight off the download size.
Compression=lzma2/max
SolidCompression=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Files]
Source: "dist\NinjitoTranslate\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\README.txt"; Description: "Read the quick-start notes"; Flags: shellexec nowait postinstall skipifsilent unchecked
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent
