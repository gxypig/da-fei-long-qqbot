param([string]$OneBotUrl = '')
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$edge = Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe'
if (-not (Test-Path -LiteralPath $edge)) { $edge = Join-Path $env:ProgramFiles 'Microsoft\Edge\Application\msedge.exe' }
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$items = @([pscustomobject]@{Id='astrbot';Url='http://127.0.0.1:6185/'})
if ($OneBotUrl) {
    $uri = [Uri]$OneBotUrl
    if ($uri.Scheme -notin @('http','https') -or $uri.UserInfo) { throw 'OneBot console URL must be http(s), without credentials' }
    $items += [pscustomobject]@{Id='onebot';Url=$uri.AbsoluteUri}
}
$processes = @(Get-CimInstance Win32_Process)
$windows = [System.Windows.Automation.AutomationElement]::RootElement.FindAll([System.Windows.Automation.TreeScope]::Children,[System.Windows.Automation.Condition]::TrueCondition)
foreach ($item in $items) {
    $profile = Join-Path $root ('logs\console-profile\' + $item.Id)
    $owned = @($processes | Where-Object { $_.Name -eq 'msedge.exe' -and $_.CommandLine -and $_.CommandLine.Contains($profile) } | ForEach-Object { [int]$_.ProcessId })
    $visible = $false
    foreach ($window in $windows) { try { if ($window.Current.ProcessId -in $owned) { $visible = $true } } catch {} }
    if ($visible) { Write-Output ($item.Id + ' console is open'); continue }
    if (Test-Path -LiteralPath $edge) {
        # Each console has its own app window/profile; no remote debugging port.
        Start-Process -FilePath $edge -ArgumentList @('--no-first-run','--disable-background-mode',('--user-data-dir="' + $profile + '"'),('--app=' + $item.Url)) -WindowStyle Normal | Out-Null
    } else { Start-Process $item.Url -WindowStyle Normal }
    Write-Output ($item.Id + ' console opened')
}
