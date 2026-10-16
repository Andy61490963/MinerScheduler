@echo off
chcp 65001 >nul
powershell.exe -NoProfile -Command "& ([scriptblock]::Create([IO.File]::ReadAllText('C:\test\CORE\TEST_啟動.ps1')))"
