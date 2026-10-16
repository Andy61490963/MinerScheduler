# 僅讀取 FanControl 已快取的感測值，不重新掃描硬體、不設定風扇。
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
try {
    [Reflection.Assembly]::LoadFrom('C:\test\CORE\TEST_風扇\FanControl.IPC.dll') | Out-Null
    $client = [FanControl.IPC.IPCFactory]::GetSensorClient()
    $request = [GetAllSensorsRequest]::new()
    $reply = $client.GetAllSensors($request, $null, [DateTime]::UtcNow.AddSeconds(2), [Threading.CancellationToken]::None)
    $fans = @(foreach ($entry in @(@{Index=0; Name='主機板 Fan #1'}, @{Index=1; Name='主機板 Fan #2'}, @{Index=5; Name='主機板 Fan #6'})) {
        $rpm = $reply.Sensors | Where-Object { $_.Identifier -eq "/lpc/it8696e/fan/$($entry.Index)" } | Select-Object -First 1
        $control = $reply.Sensors | Where-Object { $_.Identifier -eq "/lpc/it8696e/control/$($entry.Index)" } | Select-Object -First 1
        [ordered]@{name=$entry.Name; rpm=$(if ($null -ne $rpm -and $rpm.HasValue) { [double]$rpm.Value } else { $null }); percent=$(if ($null -ne $control -and $control.HasValue) { [double]$control.Value } else { $null })}
    })
    @{fans=$fans} | ConvertTo-Json -Depth 5 -Compress
} catch {
    [Console]::Error.WriteLine('無法讀取 FanControl 感測快取')
    exit 1
}
