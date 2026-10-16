$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$taskRoot=$PSScriptRoot
$env:PSModulePath=(Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\Modules')+';C:\Program Files\WindowsPowerShell\Modules'
$env:PYTHONIOENCODING='utf-8'
$taskLog=Join-Path $taskRoot 'TEST_登入啟動.log'
function Write-LoginLog([string]$taskMessage) {
    Add-Content -LiteralPath $taskLog -Encoding UTF8 -Value ((Get-Date -Format o)+' '+$taskMessage)
}
try {
    if (([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw '登入啟動必須以一般使用者執行，礦工不使用管理員權限。'
    }
    Write-LoginLog '開始一般使用者登入啟動。'
    # 網站先啟動，風扇初始化或人工停挖都不阻止開啟控制畫面。
    & (Join-Path $taskRoot 'TEST_效率監控\開啟監控.ps1') -不開啟瀏覽器
    $taskPython=Join-Path $taskRoot 'TEST_環境\python.exe'
    $taskMiner=Join-Path $taskRoot 'TEST_Core\TEST.exe'
    $taskExisting=@(Get-CimInstance Win32_Process -Filter "Name='TEST.exe' OR Name='python.exe'" | Where-Object {
        $_.ExecutablePath -eq $taskMiner -or ($_.ExecutablePath -eq $taskPython -and $_.CommandLine -like '*TEST_入口.py*')
    })
    if ($taskExisting.Count -eq 0 -and -not (Test-Path -LiteralPath (Join-Path $taskRoot 'TEST_停止.flag'))) {
        # 初次載入風扇程式可能較慢；在啟動負載與監測程序之前先等候硬體就緒。
        $taskTime=[TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTimeOffset]::UtcNow,'Taipei Standard Time')
        $taskConfig=Get-Content -LiteralPath (Join-Path $taskRoot 'TEST_設定.json') -Raw -Encoding UTF8 | ConvertFrom-Json
        $taskSchedule=$taskConfig.功耗排程
        $taskWeekday=$taskTime.DayOfWeek -notin @([DayOfWeek]::Saturday,[DayOfWeek]::Sunday)
        $taskDay=$false
        $taskStart=$null
        $taskEnd=$null
        if ($taskWeekday) {
            $taskStart=if ($taskSchedule.平日時段開始) { $taskSchedule.平日時段開始 } else { '09:15' }
            $taskEnd=if ($taskSchedule.平日時段結束) { $taskSchedule.平日時段結束 } else { '17:45' }
        } elseif ($taskSchedule.週末使用平日功耗 -eq $true) {
            $taskStart=if ($taskSchedule.週末時段開始) { $taskSchedule.週末時段開始 } else { '09:15' }
            $taskEnd=if ($taskSchedule.週末時段結束) { $taskSchedule.週末時段結束 } else { '17:45' }
        }
        if ($taskStart -and $taskEnd) {
            $taskNow=[TimeSpan]::FromHours($taskTime.Hour)+[TimeSpan]::FromMinutes($taskTime.Minute)
            $taskBegin=[TimeSpan]::ParseExact($taskStart,'hh\:mm',[Globalization.CultureInfo]::InvariantCulture)
            $taskFinish=[TimeSpan]::ParseExact($taskEnd,'hh\:mm',[Globalization.CultureInfo]::InvariantCulture)
            if ($taskBegin -ge $taskFinish) { throw '儲存的時段起迄無效，取消自動啟動。' }
            $taskDay=($taskNow -ge $taskBegin -and $taskNow -lt $taskFinish)
        }
        $taskProfile=if($taskDay){'TEST_尖峰風扇70.json'}else{'TEST_離峰風扇100.json'}
        $taskFan=Join-Path $taskRoot 'TEST_風扇\FanControl.exe'
        $taskFanConfig=Join-Path $taskRoot ('TEST_風扇\Configurations\'+$taskProfile)
        Start-Process -FilePath $taskFan -ArgumentList @('-c',('"'+$taskFanConfig+'"'),'-m') -WorkingDirectory (Split-Path $taskFan -Parent) -WindowStyle Hidden | Out-Null
        $taskReadCode=@'
from pathlib import Path
from datetime import datetime
import json,sys,time
root=Path(sys.argv[1]); peak=sys.argv[2]=='True'
sys.path[:0]=[str(root),str(root/'TEST_效率監控')]
import settings
schedule=json.loads((root/'TEST_設定.json').read_text(encoding='utf-8-sig'))['功耗排程']
values={'gpu_fan':schedule['尖峰風扇百分比' if peak else '離峰風扇百分比'],'board_fan':schedule['主機板風扇百分比']}
def check():
    if (root/'TEST_停止.flag').exists():raise RuntimeError('收到停止標記，不自動恢復。')
deadline=time.monotonic()+180
while True:
    check()
    try:
        settings.verify_fans(values,check)
        print('登入前五組風扇已就緒。',flush=True)
        break
    except Exception:
        check()
        if time.monotonic()>=deadline:raise
        time.sleep(2)
'@
        & $taskPython -B -c $taskReadCode $taskRoot ([string]$taskDay) 2>&1 | ForEach-Object { Write-LoginLog ([string]$_) }
        if ($LASTEXITCODE -ne 0) { throw '登入前風扇未就緒，未啟動負載。' }
    }
    & (Join-Path $taskRoot 'TEST_啟動風扇排程.ps1')
    & (Join-Path $taskRoot 'TEST_啟動.ps1') -登入啟動 2>&1 | ForEach-Object { Write-LoginLog ([string]$_) }
    Write-LoginLog '登入啟動流程完成；人工停止與硬體保護優先。'
} catch {
    Write-LoginLog ('登入啟動未完成：'+$_.Exception.Message)
    throw
}
