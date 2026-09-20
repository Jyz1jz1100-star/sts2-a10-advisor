@echo off
REM Rebuild the NativeAOT emulator library that the simulator backends load.
REM NativeAOT needs the MSVC toolchain, and a plain shell is not a VS developer
REM prompt, so vcvars64 has to supply VCToolsInstallDir/WindowsSdkDir first.
REM
REM Usage: scripts\build_emulator.cmd [emulator_root]

setlocal
set "REPO=%~dp0.."
set "EMULATOR_ROOT=%~1"
if "%EMULATOR_ROOT%"=="" set "EMULATOR_ROOT=%REPO%\..\third_party\slay-the-spire-2-emulator-main"

set "DOTNET=%REPO%\.tools\dotnet\dotnet.exe"
set "NUGET_PACKAGES=%REPO%\.cache\nuget"
set "DOTNET_CLI_TELEMETRY_OPTOUT=1"
set "DOTNET_NOLOGO=1"

if not exist "%DOTNET%" echo ERROR: project-local .NET SDK not found at %DOTNET% & exit /b 2
if not exist "%EMULATOR_ROOT%\src\Sts2Emulator\Sts2Emulator.csproj" echo ERROR: no emulator project at %EMULATOR_ROOT% & exit /b 2

call "C:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat" >"%TEMP%\sts2_vcvars.log" 2>&1
if not defined VCToolsInstallDir echo ERROR: vcvars64 did not set VCToolsInstallDir & exit /b 3

"%DOTNET%" publish "%EMULATOR_ROOT%\src\Sts2Emulator\Sts2Emulator.csproj" -c Release -r win-x64 --self-contained -o "%EMULATOR_ROOT%\out"
if errorlevel 4 echo ERROR: dotnet publish failed & exit /b 4
echo BUILT %EMULATOR_ROOT%\out\Sts2Emulator.dll
