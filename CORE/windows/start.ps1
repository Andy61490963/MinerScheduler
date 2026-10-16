param([switch]$登入啟動,[switch]$AI恢復,[string]$AI暫停標記,[string]$監控停止驗證)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
if (Test-Path -LiteralPath (Join-Path $PSScriptRoot '.搬移中.flag')) { throw '正式程式正在搬移，完成前不啟動負載。' }
$taskRoot='C:\test\CORE'
$taskName='TEST_台灣平日功耗排程'
$taskPython='C:\test\CORE\TEST_環境\python.exe'
$taskStop='C:\test\CORE\TEST_停止.flag'
function Assert-MonitorStop {
    if (-not $監控停止驗證) { return }
    $actual='無'
    if (Test-Path -LiteralPath $taskStop) {
        $actual=(Get-FileHash -LiteralPath $taskStop -Algorithm SHA256).Hash.ToLower()+':'+(Get-Item -LiteralPath $taskStop).LastWriteTimeUtc.Ticks
    }
    if ($actual -cne $監控停止驗證) { throw '人工停止狀態已變更，取消監控畫面的恢復要求。' }
}
Assert-MonitorStop
if (([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw '請以一般帳號點兩下TEST_開始.cmd；礦工不使用管理員權限。' }
if ((Get-FileHash -LiteralPath 'C:\test\CORE\TEST_Core\TEST.exe' -Algorithm SHA256).Hash -ne '2cb529f44a06f172a18753267e363c1df1106b31c09c64e6b29627f99c816aa4') { throw '礦工內容與已核對的官方檔案不符。' }
if (-not $AI恢復) {
    $aiController=@(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.ExecutablePath -eq $taskPython -and $_.CommandLine -like '*C:\test\CORE\TEST_AI協調.py*' })
    if ($aiController.Count -eq 0) {
        Start-Process -FilePath $taskPython -ArgumentList '-B C:\test\CORE\TEST_AI協調.py' -WorkingDirectory $taskRoot -WindowStyle Hidden -RedirectStandardOutput 'C:\test\CORE\TEST_AI協調輸出.log' -RedirectStandardError 'C:\test\CORE\TEST_AI協調錯誤.log' | Out-Null
    }
}
if ($登入啟動 -and (Test-Path -LiteralPath $taskStop)) { Write-Host '保留人工停止狀態，不自動挖礦。'; exit }
function Get-MiningEntry {
    @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.ExecutablePath -eq $taskPython -and $_.CommandLine -like '*TEST_入口.py*' })
}
if ((Get-MiningEntry).Count -gt 0 -and -not (Test-Path -LiteralPath $taskStop)) { Write-Host '挖礦時段排程已在背景運行。'; exit }
$taskDeadline=[DateTimeOffset]::UtcNow.AddSeconds(15)
while ((Get-MiningEntry).Count -gt 0) {
    if ([DateTimeOffset]::UtcNow -ge $taskDeadline) { throw '前一個挖礦程序尚未結束，請稍後再開始。' }
    Start-Sleep -Milliseconds 200
}
$env:PSModulePath=(Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\Modules')+';C:\Program Files\WindowsPowerShell\Modules'
$taskRegistered=Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if (-not $taskRegistered) { & 'C:\test\CORE\TEST_功耗保護.ps1'; Write-Host '排程安裝後，再點TEST_開始.cmd。'; exit }
if (Test-Path -LiteralPath $taskStop) {
    $taskDeadline=[DateTimeOffset]::UtcNow.AddSeconds(15)
    while ((Get-ScheduledTask -TaskName $taskName).State -eq 'Running') {
        if ([DateTimeOffset]::UtcNow -ge $taskDeadline) { throw '前一個功耗程序尚未停止，保留停止標記。' }
        Start-Sleep -Milliseconds 200
    }
    if ($AI恢復 -and ((-not $AI暫停標記.StartsWith('Emerald 本地模型暫停挖礦：')) -or [IO.File]::ReadAllText($taskStop) -cne $AI暫停標記)) { throw '停止狀態已變更，保留人工停止，不自動恢復。' }
    Assert-MonitorStop
    Remove-Item -LiteralPath $taskStop
}
& ([scriptblock]::Create([System.IO.File]::ReadAllText('C:\test\CORE\TEST_啟動風扇排程.ps1',[System.Text.Encoding]::UTF8)))
Start-ScheduledTask -TaskName $taskName
Start-Process -FilePath $taskPython -ArgumentList '-B C:\test\CORE\TEST_入口.py' -WorkingDirectory $taskRoot -WindowStyle Hidden -RedirectStandardOutput 'C:\test\CORE\TEST_入口輸出.log' -RedirectStandardError 'C:\test\CORE\TEST_入口錯誤.log' | Out-Null
$taskCurrentConfig=Get-Content -LiteralPath 'C:\test\CORE\TEST_設定.json' -Raw -Encoding UTF8 | ConvertFrom-Json
Write-Host ('挖礦排程已啟用：平日日間{0}W；離峰及週末{1}W；顯卡{2}%、主機板三組{3}%；溫度保護優先。' -f $taskCurrentConfig.功耗排程.平日固定功耗上限W,$taskCurrentConfig.功耗排程.離峰固定功耗上限W,$taskCurrentConfig.功耗排程.尖峰風扇百分比,$taskCurrentConfig.功耗排程.主機板風扇百分比)
