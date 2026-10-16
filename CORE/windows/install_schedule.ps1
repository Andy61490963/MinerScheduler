param([switch]$已提升權限)
$ErrorActionPreference = 'Stop'
$taskRoot = 'C:\test\CORE'
$taskResult = Join-Path $taskRoot 'TEST_排程安裝結果.json'
$taskPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$taskAdministrator = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $taskAdministrator) {
    # 只透過Windows正常的權限批准，不能自行略過UAC。
    Start-Process -FilePath $taskPowerShell -ArgumentList '-NoProfile -ExecutionPolicy Bypass -File "C:\test\CORE\TEST_安裝排程.ps1" -已提升權限' -Verb RunAs -WindowStyle Hidden
    exit
}
$taskRecord = @{狀態='安裝中';開始UTC=[DateTimeOffset]::UtcNow.ToString('o');原因=''}
function Save-Install {
    [IO.File]::WriteAllText($taskResult,($taskRecord | ConvertTo-Json -Depth 5),(New-Object Text.UTF8Encoding($false)))
}
try {
    Save-Install
    if ((Get-FileHash -LiteralPath 'C:\test\CORE\TEST_Core\TEST.exe' -Algorithm SHA256).Hash -ne '2cb529f44a06f172a18753267e363c1df1106b31c09c64e6b29627f99c816aa4') { throw '負載程式雜湊與既有核對不符。' }
    $taskSmi = Join-Path $env:SystemRoot 'System32\nvidia-smi.exe'
    $taskCheck = & $taskSmi '-i' 'GPU-__LOCAL_UUID__' '--power-limit=150'
    if ($LASTEXITCODE -ne 0) { throw ('無法設定150W：'+($taskCheck -join ' ')) }
    $taskOld = Get-Content -LiteralPath 'C:\test\CORE\TEST_功耗狀態.json' -Raw -Encoding UTF8 | ConvertFrom-Json
    $taskOldPid = [int]$taskOld.程序編號
    [IO.File]::WriteAllText('C:\test\CORE\TEST_停止.flag','切換至台灣平日排程，先停止既有負載與舊保護。',(New-Object Text.UTF8Encoding($false)))
    $taskDeadline = [DateTimeOffset]::UtcNow.AddSeconds(45)
    do {
        Start-Sleep -Milliseconds 250
        $taskMiner = @(Get-CimInstance Win32_Process -Filter "Name='TEST.exe'" | Where-Object ExecutablePath -eq 'C:\test\CORE\TEST_Core\TEST.exe')
        $taskOldProcess = Get-Process -Id $taskOldPid -ErrorAction SilentlyContinue
        if ([DateTimeOffset]::UtcNow -gt $taskDeadline) { throw '舊負載或舊保護未在45秒內結束，沒有提高功耗。' }
    } while ($taskMiner.Count -gt 0 -or $taskOldProcess)
    # 舊保護可能還原原始450W；在沒有負載時立即重新設定150W。
    $taskBaseline = & $taskSmi '-i' 'GPU-__LOCAL_UUID__' '--power-limit=150'
    if ($LASTEXITCODE -ne 0) { throw '停止舊保護後，150W基準未套用。' }
    $taskClock = & $taskSmi '-i' 'GPU-__LOCAL_UUID__' '--lock-gpu-clocks=210,900'
    if ($LASTEXITCODE -ne 0) { throw '900MHz基準未套用。' }
    $taskAction = New-ScheduledTaskAction -Execute 'C:\test\CORE\TEST_環境\python.exe' -Argument '-B "C:\test\CORE\TEST_功耗排程.py"' -WorkingDirectory $taskRoot
    $taskTriggers = @(
        (New-ScheduledTaskTrigger -AtStartup),
        (New-ScheduledTaskTrigger -AtLogOn),
        (New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At '09:15'),
        (New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At '17:45'),
        (New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Saturday -At '00:00')
    )
    $taskPrincipal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $taskSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName 'TEST_台灣平日功耗排程' -Action $taskAction -Trigger $taskTriggers -Principal $taskPrincipal -Settings $taskSettings -Description '以台灣時間判斷星期一至星期五09:15至17:45，兩組顯卡風扇70%優先，平日時段固定功耗；其餘時段150W；過熱或監控失效停止負載。' -Force | Out-Null
    # 僅讓目前帳號讀取與啟動本次顯卡排程，保留原有管理員及SYSTEM權限。
    $taskService=New-Object -ComObject Schedule.Service
    $taskService.Connect()
    $taskRegistered=$taskService.GetFolder('\').GetTask('TEST_台灣平日功耗排程')
    $taskDescriptor=New-Object Security.AccessControl.RawSecurityDescriptor($taskRegistered.GetSecurityDescriptor(4))
    $taskUserSid=[Security.Principal.WindowsIdentity]::GetCurrent().User
    $taskAccessAce=New-Object Security.AccessControl.CommonAce([Security.AccessControl.AceFlags]::None,[Security.AccessControl.AceQualifier]::AccessAllowed,-1610612736,$taskUserSid,$false,$null)
    $taskDescriptor.DiscretionaryAcl.InsertAce($taskDescriptor.DiscretionaryAcl.Count,$taskAccessAce)
    $taskRegistered.SetSecurityDescriptor($taskDescriptor.GetSddlForm([Security.AccessControl.AccessControlSections]::Access),0)
    # 只有安裝程序明確移除本次切換標記；排程不自行忽略使用者的停止指令。
    Remove-Item -LiteralPath 'C:\test\CORE\TEST_停止.flag'
    Start-ScheduledTask -TaskName 'TEST_台灣平日功耗排程'
    $taskDeadline = [DateTimeOffset]::UtcNow.AddSeconds(30)
    do {
        Start-Sleep -Milliseconds 500
        $taskState = Get-Content -LiteralPath 'C:\test\CORE\TEST_功耗狀態.json' -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($taskState.模式 -eq '平日排程，70%風扇優先' -and $taskState.狀態 -eq '排程保護已就緒') { break }
        if ([DateTimeOffset]::UtcNow -gt $taskDeadline) { throw ('排程未完成兩組70%風扇核對：'+$taskState.原因) }
    } while ($true)
    $taskRecord['狀態']='已安裝並啟動'
    $taskRecord['排程名稱']='TEST_台灣平日功耗排程'
    $taskRecord['保護識別碼']=$taskState.執行識別碼
    $taskRecord['功耗上限W']=$taskState.顯卡.功耗上限W
    $taskRecord['風扇百分比']=$taskState.顯卡.風扇百分比
    $taskRecord['負載啟動']='由一般帳號啟動TEST_開始.cmd，不提升礦工權限。'
} catch {
    $taskRecord['狀態']='安裝未完成'
    $taskRecord['原因']=$_.Exception.Message
    if ([IO.File]::Exists('C:\test\CORE\TEST_停止.flag')) {
        $taskRecord['負載啟動']='保留停止標記，未恢復負載。'
    }
} finally {
    $taskRecord['結束UTC']=[DateTimeOffset]::UtcNow.ToString('o')
    Save-Install
}
