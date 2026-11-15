$ErrorActionPreference = 'Stop'
$appDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonw = Join-Path $appDirectory '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonw)) {
    throw 'Run Install HARITHA ENTERPRISES.cmd first.'
}
$desktop = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktop 'HARITHA ENTERPRISES.lnk'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $pythonw
$shortcut.Arguments = '"' + (Join-Path $appDirectory 'desktop_launcher.py') + '"'
$shortcut.WorkingDirectory = $appDirectory
$shortcut.IconLocation = Join-Path $appDirectory 'static\haritha.ico'
$shortcut.Description = 'Open HARITHA ENTERPRISES'
$shortcut.Save()
Write-Output "Created $shortcutPath"
