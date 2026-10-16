$ErrorActionPreference='Stop'
$taskFanProcess=@(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.ExecutablePath -eq 'C:\test\CORE\TEST_環境\python.exe' -and $_.CommandLine -like '*TEST_風扇排程.py*' })
if ($taskFanProcess.Count -eq 0) {
    Start-Process -FilePath 'C:\test\CORE\TEST_環境\python.exe' -ArgumentList '-B C:\test\CORE\TEST_風扇排程.py' -WorkingDirectory 'C:\test\CORE' -WindowStyle Hidden -RedirectStandardOutput 'C:\test\CORE\TEST_風扇排程輸出.log' -RedirectStandardError 'C:\test\CORE\TEST_風扇排程錯誤.log' | Out-Null
}

$taskFanMonitor=@(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.ExecutablePath -eq 'C:\test\CORE\TEST_環境\python.exe' -and $_.CommandLine -like '*TEST_風扇監測.py*' })
if ($taskFanMonitor.Count -eq 0) {
    Start-Process -FilePath 'C:\test\CORE\TEST_環境\python.exe' -ArgumentList '-B C:\test\CORE\TEST_風扇監測.py' -WorkingDirectory 'C:\test\CORE' -WindowStyle Hidden -RedirectStandardOutput 'C:\test\CORE\TEST_風扇監測輸出.log' -RedirectStandardError 'C:\test\CORE\TEST_風扇監測錯誤.log' | Out-Null
}
