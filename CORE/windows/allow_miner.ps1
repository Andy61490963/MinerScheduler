$ErrorActionPreference='Stop'
$taskMiner='C:\test\CORE\TEST_Core\TEST.exe'
$taskExpected='2cb529f44a06f172a18753267e363c1df1106b31c09c64e6b29627f99c816aa4'
$taskResult='C:\test\CORE\TEST_白名單結果.json'
$taskRecord=@{狀態='設定中';範圍=$taskMiner;時間UTC=[DateTimeOffset]::UtcNow.ToString('o')}
try {
    if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Windows Defender排除清單需要管理員權限。' }
    if ((Get-FileHash -LiteralPath $taskMiner -Algorithm SHA256).Hash -ine $taskExpected) { throw '礦工與已核對官方版本的SHA256不同，未設定白名單。' }
    Add-MpPreference -ExclusionPath $taskMiner -ErrorAction Stop
    if ($taskMiner -notin @((Get-MpPreference).ExclusionPath)) { throw '未讀回指定執行檔的Defender排除設定。' }
    $taskRecord['狀態']='已加入指定執行檔排除清單'
    $taskRecord['礦工SHA256']=$taskExpected
} catch {
    $taskRecord['狀態']='設定未完成'
    $taskRecord['原因']=$_.Exception.Message
} finally {
    [IO.File]::WriteAllText($taskResult,($taskRecord | ConvertTo-Json -Depth 3),[Text.UTF8Encoding]::new($false))
}
