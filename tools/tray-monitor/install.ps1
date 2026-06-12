# Crown Monitoring Tray — install dependencies and optionally add to Windows startup
Set-Location $PSScriptRoot

Write-Host "Installing Python dependencies..."
pip install -r requirements.txt

$answer = Read-Host "Add to Windows startup so it runs automatically? (y/n)"
if ($answer -eq 'y') {
    $script  = (Resolve-Path "monitor.py").Path
    $pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue)?.Source
    if (-not $pythonw) {
        $pythonw = (Get-Command python.exe).Source -replace 'python\.exe$', 'pythonw.exe'
    }
    $startup = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup"
    $vbs     = "$startup\CrownMonitoring.vbs"

    # VBS wrapper launches pythonw (no console window) silently on login
    @"
Set WshShell = CreateObject("WScript.Shell")
WshShell.Run """$pythonw"" ""$script""", 0, False
"@ | Set-Content $vbs

    Write-Host "Startup entry created: $vbs"
}

Write-Host ""
Write-Host "To run now:  pythonw monitor.py"
Write-Host "To stop:     right-click the tray icon -> Quit"
