@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
"C:\test\CORE\TEST_環境\python.exe" -B "%~dp0run_tests.py"
set "testResult=%ERRORLEVEL%"
pause
exit /b %testResult%
