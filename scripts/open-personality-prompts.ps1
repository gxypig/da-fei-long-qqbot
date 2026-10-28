param([switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$helper = Join-Path $projectRoot 'tools\personality_locations.py'
if (-not (Test-Path -LiteralPath $helper -PathType Leaf)) {
    throw 'Missing tools/personality_locations.py. Keep this script in the project; create a desktop shortcut instead of copying it.'
}
$pythonCandidates = @(
    (Join-Path $projectRoot '.venv\Scripts\python.exe'),
    (Join-Path $env:APPDATA 'uv\tools\astrbot\Scripts\python.exe')
)
$pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
if ($pythonCommand) { $pythonCandidates += $pythonCommand.Source }
$pythonExe = $pythonCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
if (-not $pythonExe) { throw 'Python was not found. Install this project first, then run again.' }
$oldEncoding = $env:PYTHONIOENCODING
try {
    $env:PYTHONIOENCODING = 'utf-8'
    $raw = & $pythonExe -B $helper
    if ($LASTEXITCODE -ne 0) { throw 'Could not build the prompt index; no files were opened.' }
    $plan = $raw | ConvertFrom-Json
} finally {
    $env:PYTHONIOENCODING = $oldEncoding
}
if ($CheckOnly) { $raw; exit 0 }

# Explicit editors only: invoking a .py association could execute the bot code.
$editorCandidates = @(
    (Join-Path $env:LOCALAPPDATA 'Programs\Microsoft VS Code\Code.exe'),
    (Join-Path $env:ProgramFiles 'Microsoft VS Code\Code.exe'),
    (Join-Path $env:LOCALAPPDATA 'Programs\cursor\Cursor.exe')
)
foreach ($command in @('code.cmd', 'cursor.cmd')) {
    $found = Get-Command $command -ErrorAction SilentlyContinue
    if ($found) {
        $install = Split-Path -Parent (Split-Path -Parent $found.Source)
        $editorCandidates += Join-Path $install $(if ($command -eq 'code.cmd') { 'Code.exe' } else { 'Cursor.exe' })
    }
}
$editor = $editorCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
$failed = @()
$paths = @($plan.files | ForEach-Object { $_.path })
if ($editor) {
    try {
        $arguments = @('--new-window') + @($paths | ForEach-Object { '"' + $_ + '"' })
        Start-Process -FilePath $editor -ArgumentList $arguments | Out-Null
    } catch { $failed += 'Editor: ' + $_.Exception.Message }
} else {
    foreach ($path in $paths) {
        try { Start-Process -FilePath (Join-Path $env:WINDIR 'System32\notepad.exe') -ArgumentList ('"' + $path + '"') | Out-Null }
        catch { $failed += $path + ': ' + $_.Exception.Message }
    }
}
foreach ($page in $plan.pages) {
    try { Start-Process -FilePath $page.url | Out-Null }
    catch { $failed += $page.url + ': ' + $_.Exception.Message }
}
Write-Host ('Opened {0} source files and {1} dashboard entries.' -f $paths.Count, $plan.pages.Count)
foreach ($warning in $plan.warnings) { Write-Warning $warning }
if ($failed.Count) { throw ($failed -join [Environment]::NewLine) }
