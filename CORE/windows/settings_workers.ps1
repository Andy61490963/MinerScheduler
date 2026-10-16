param([ValidateSet('狀態','停止風扇','啟動風扇')][string]$模式)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$taskPython='C:\test\CORE\TEST_環境\python.exe'
$processes=@(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='TEST.exe'")
$mining=@($processes | Where-Object { $_.ExecutablePath -eq 'C:\test\CORE\TEST_Core\TEST.exe' -or ($_.ExecutablePath -eq $taskPython -and $_.CommandLine -like '*C:\test\CORE\TEST_入口.py*') })
$guardRunning=(Get-ScheduledTask -TaskName 'TEST_台灣平日功耗排程').State -eq 'Running'
if ($模式 -eq '狀態') { @{mining=@($mining | Select-Object -ExpandProperty ProcessId);guard_running=$guardRunning} | ConvertTo-Json -Compress; exit }
if ($mining.Count -gt 0 -or $guardRunning) { throw '負載或功耗背景尚未結束，不更換風扇背景。' }
if ($模式 -eq '停止風扇') {
    $session=(Get-Process -Id $PID).SessionId
    foreach ($process in $processes) {
        if ($process.ExecutablePath -eq $taskPython -and $process.SessionId -eq $session -and ($process.CommandLine -like '*C:\test\CORE\TEST_風扇排程.py*' -or $process.CommandLine -like '*C:\test\CORE\TEST_風扇監測.py*')) {
            $live=Get-Process -Id $process.ProcessId -ErrorAction SilentlyContinue
            if ($live -and $live.Path -eq $taskPython -and [Math]::Abs(($live.StartTime.ToUniversalTime()-$process.CreationDate.ToUniversalTime()).TotalSeconds) -lt 1) {
                $live.Kill()
                if (-not $live.WaitForExit(5000)) { throw '風扇背景未能結束。' }
            }
        }
    }
} else {
    & 'C:\test\CORE\TEST_啟動風扇排程.ps1'
}
