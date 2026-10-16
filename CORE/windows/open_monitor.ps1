param([switch]$不開啟瀏覽器)
# 此啟動器僅啟動唯讀監控伺服器，不呼叫挖礦啟動或控制程式。
$ErrorActionPreference = 'Stop'
$monitorRoot = $PSScriptRoot
$monitorUrl = 'http://127.0.0.1:18776/'
function Test-MonitorReady {
    try {
        $reply = Invoke-RestMethod -Uri ($monitorUrl + 'api/health') -TimeoutSec 2
        return $reply.identity -eq 'test-mining-efficiency-monitor-v1'
    } catch { return $false }
}
if (-not (Test-MonitorReady)) {
    $python = 'C:\test\CORE\TEST_環境\python.exe'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw '找不到現有 Python 環境。' }
    $process = Start-Process -FilePath $python -ArgumentList @('-B', ('"' + (Join-Path $monitorRoot 'server.py') + '"'), '--port', '18776') -WorkingDirectory $monitorRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $monitorRoot '監控輸出.log') -RedirectStandardError (Join-Path $monitorRoot '監控錯誤.log')
    $ready = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Milliseconds 300
        if (Test-MonitorReady) { $ready = $true; break }
        if ($process.HasExited) { break }
    }
    if (-not $ready) { throw '效率監控未能啟動，請查看監控錯誤.log；現有挖礦不受影響。' }
}
Write-Output ('效率監控已開啟：' + $monitorUrl)
if (-not $不開啟瀏覽器) { Start-Process $monitorUrl }
