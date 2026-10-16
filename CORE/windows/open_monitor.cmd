@echo off
chcp 65001 >nul
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "C:\test\CORE\TEST_效率監控\開啟監控.ps1"
if errorlevel 1 pause
