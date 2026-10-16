$ErrorActionPreference='Stop'
$taskName='TEST_台灣平日功耗排程'
$taskResult='C:\test\CORE\TEST_排程同步結果.json'
$taskRecord=@{狀態='同步中';尖峰開始='09:15';尖峰結束='17:45';時區='Asia/Taipei'}
try {
    if ([Security.Principal.WindowsIdentity]::GetCurrent().User.Value -ne 'S-1-5-18') { throw '排程時間由既有SYSTEM功耗程序同步。' }
    $taskService=New-Object -ComObject Schedule.Service
    $taskService.Connect()
    $taskFolder=$taskService.GetFolder('\')
    $taskRegistered=$taskFolder.GetTask($taskName)
    $taskDefinition=$taskRegistered.Definition
    if ($taskDefinition.Actions.Count -ne 1 -or $taskDefinition.Actions.Item(1).Path -ne 'C:\test\CORE\TEST_環境\python.exe') { throw '排程動作與既有功耗程序不同，不修改。' }
    $taskFoundStart=$false
    $taskFoundEnd=$false
    for ($taskIndex=1;$taskIndex -le $taskDefinition.Triggers.Count;$taskIndex++) {
        $taskTrigger=$taskDefinition.Triggers.Item($taskIndex)
        if ($taskTrigger.Type -eq 3 -and $taskTrigger.DaysOfWeek -eq 62) {
            $taskBoundary=[DateTimeOffset]::Parse($taskTrigger.StartBoundary)
            if ($taskBoundary.Hour -eq 9 -and $taskBoundary.Minute -in @(15,30)) {
                $taskTrigger.StartBoundary='2026-10-14T09:15:00+08:00'
                $taskFoundStart=$true
            } elseif ($taskBoundary.Hour -eq 17 -and $taskBoundary.Minute -eq 45) {
                $taskTrigger.StartBoundary='2026-10-14T17:45:00+08:00'
                $taskFoundEnd=$true
            }
        }
    }
    if (-not $taskFoundStart -or -not $taskFoundEnd) { throw '找不到預期的平日開始與結束觸發器，不修改。' }
    $taskConfig=Get-Content -LiteralPath 'C:\test\CORE\TEST_設定.json' -Raw -Encoding UTF8 | ConvertFrom-Json
    $taskDefinition.RegistrationInfo.Description=('正式程式C:\test\CORE；台灣平日09:15至17:45為{0}W，離峰及週末{1}W；依核准風扇設定與溫度保護運行。' -f $taskConfig.功耗排程.平日固定功耗上限W,$taskConfig.功耗排程.離峰固定功耗上限W)
    $taskSecurity=$taskRegistered.GetSecurityDescriptor(7)
    $taskFolder.RegisterTaskDefinition($taskName,$taskDefinition,6,'SYSTEM',$null,5,$taskSecurity) | Out-Null
    $taskRecord['狀態']='已同步'
} catch {
    $taskRecord['狀態']='同步失敗'
    $taskRecord['原因']=$_.Exception.Message
    throw
} finally {
    $taskRecord['更新UTC']=[DateTimeOffset]::UtcNow.ToString('o')
    [IO.File]::WriteAllText($taskResult,($taskRecord | ConvertTo-Json -Depth 4),[Text.UTF8Encoding]::new($false))
}
