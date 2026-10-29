@echo off
setlocal
rem Windows-native build entry point. Default target is win-cpu.
rem Usage: scripts\build_windows.bat
rem        scripts\build_windows.bat -Target win-gpu -Variant CUDA12
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_executable.ps1" %*
exit /b %ERRORLEVEL%
