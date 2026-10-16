$ErrorActionPreference='Stop'
$taskName='TEST_台灣平日功耗排程'
$taskResult='C:\test\CORE\TEST_排程同步結果.json'
$taskRecord=@{狀態='同步中';時區='Asia/Taipei'}
try {
    if ([Security.Principal.WindowsIdentity]::GetCurrent().User.Value -ne 'S-1-5-18') { throw '排程時間由既有SYSTEM功耗程序同步。' }
    $config=Get-Content -LiteralPath 'C:\test\CORE\TEST_設定.json' -Raw -Encoding UTF8 | ConvertFrom-Json
    $schedule=$config.功耗排程
    $weekdayStart=if ($schedule.平日時段開始) { [string]$schedule.平日時段開始 } else { '09:15' }
    $weekdayEnd=if ($schedule.平日時段結束) { [string]$schedule.平日時段結束 } else { '17:45' }
    $weekendStart=if ($schedule.週末時段開始) { [string]$schedule.週末時段開始 } else { '09:15' }
    $weekendEnd=if ($schedule.週末時段結束) { [string]$schedule.週末時段結束 } else { '17:45' }
    $weekendEnabled=$schedule.週末使用平日功耗 -eq $true
    # PowerShell 使用具名欄位以避免巢狀陣列展平，時段格式與 Python 共用 HH:mm 規則。
    $windows=@(
        @{ Name='平日'; Start=$weekdayStart; End=$weekdayEnd },
        @{ Name='週末'; Start=$weekendStart; End=$weekendEnd }
    )
    foreach ($window in $windows) {
        $first=[TimeSpan]::ParseExact($window.Start,'hh\:mm',[Globalization.CultureInfo]::InvariantCulture)
        $last=[TimeSpan]::ParseExact($window.End,'hh\:mm',[Globalization.CultureInfo]::InvariantCulture)
        if ($first -ge $last) { throw ($window.Name+'排程結束時間必須晚於開始時間。') }
    }
    $service=New-Object -ComObject Schedule.Service
    $service.Connect()
    $task=$service.GetFolder('\').GetTask($taskName)
    $definition=$task.Definition
    if ($definition.Actions.Count -ne 1 -or $definition.Actions.Item(1).Path -ne 'C:\test\CORE\TEST_環境\python.exe') { throw '排程動作與既有功耗程序不同，不修改。' }

    # 此 Task 是常駐保護程序：時窗切換由 Python 每個循環決定；
    # 週期觸發器僅提供程序不在時的重新啟動機會。
    $weekdays=@()
    for ($index=1; $index -le $definition.Triggers.Count; $index++) {
        $trigger=$definition.Triggers.Item($index)
        if ($trigger.Type -eq 3 -and $trigger.DaysOfWeek -eq 62) { $weekdays+=@($trigger) }
    }
    if ($weekdays.Count -ne 2) { throw '找不到兩筆原始平日補救觸發器，不修改。' }
    $ordered=@($weekdays | Sort-Object { [DateTimeOffset]::Parse($_.StartBoundary).TimeOfDay })
    $ordered[0].StartBoundary="2026-10-14T$($weekdayStart):00+08:00"
    $ordered[1].StartBoundary="2026-10-14T$($weekdayEnd):00+08:00"
    $ordered[0].Id='MinerScheduler-WeekdayStart'
    $ordered[1].Id='MinerScheduler-WeekdayEnd'

    # 週末補救觸發器由本程式管理；關閉週末時段時移除，不動既有週六午夜備援觸發器。
    for ($index=$definition.Triggers.Count; $index -ge 1; $index--) {
        $trigger=$definition.Triggers.Item($index)
        if ($trigger.Id -in @('MinerScheduler-WeekendStart','MinerScheduler-WeekendEnd')) {
            $definition.Triggers.Remove($index)
        }
    }
    if ($weekendEnabled) {
        foreach ($entry in @(@('MinerScheduler-WeekendStart',$weekendStart),@('MinerScheduler-WeekendEnd',$weekendEnd))) {
            $trigger=$definition.Triggers.Create(3)
            $trigger.Id=$entry[0]
            $trigger.WeeksInterval=1
            $trigger.DaysOfWeek=65
            $trigger.StartBoundary="2026-10-17T$($entry[1]):00+08:00"
            $trigger.Enabled=$true
        }
    }

    $definition.RegistrationInfo.Description=('GPU保護常駐；平日{0}～{1}為{2}W；週末自訂時段{3}；其他時段{4}W。執行期由Python切換。' -f $weekdayStart,$weekdayEnd,$schedule.平日固定功耗上限W,$weekendEnabled,$schedule.離峰固定功耗上限W)
    $security=$task.GetSecurityDescriptor(7)
    $service.GetFolder('\').RegisterTaskDefinition($taskName,$definition,6,'SYSTEM',$null,5,$security) | Out-Null
    $taskRecord['狀態']='已同步'
    $taskRecord['平日開始']=$weekdayStart
    $taskRecord['平日結束']=$weekdayEnd
    $taskRecord['週末啟用']=$weekendEnabled
    $taskRecord['週末開始']=$weekendStart
    $taskRecord['週末結束']=$weekendEnd
} catch {
    $taskRecord['狀態']='同步失敗'
    $taskRecord['原因']=$_.Exception.Message
    throw
} finally {
    $taskRecord['更新UTC']=[DateTimeOffset]::UtcNow.ToString('o')
    [IO.File]::WriteAllText($taskResult,($taskRecord | ConvertTo-Json -Depth 5),[Text.UTF8Encoding]::new($false))
}
