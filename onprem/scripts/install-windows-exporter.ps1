#Requires -RunAsAdministrator
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$VERSION    = "0.29.2"
$PORT       = 9182
$COLLECTORS = "cpu,cs,logical_disk,memory,net,os,service,system,tcp,time"
$MSI        = "windows_exporter-$VERSION-amd64.msi"
$URL        = "https://github.com/prometheus-community/windows_exporter/releases/download/v$VERSION/$MSI"
$TMP        = [System.IO.Path]::GetTempPath()
$MSIPATH    = Join-Path $TMP $MSI

Write-Host "==> Downloading windows_exporter $VERSION"
Invoke-WebRequest -Uri $URL -OutFile $MSIPATH -UseBasicParsing

Write-Host "--> Installing (port $PORT, collectors: $COLLECTORS)"
$result = Start-Process msiexec.exe -Wait -PassThru -ArgumentList @(
    "/i", $MSIPATH,
    "ENABLED_COLLECTORS=$COLLECTORS",
    "LISTEN_PORT=$PORT",
    "/quiet",
    "/norestart"
)

if ($result.ExitCode -ne 0) {
    Write-Error "Installation failed with exit code $($result.ExitCode)"
    exit 1
}

Write-Host "--> Configuring firewall"
New-NetFirewallRule -DisplayName "Windows Exporter (Prometheus)" -Direction Inbound -Protocol TCP -LocalPort $PORT -RemoteAddress "192.168.164.0/24","192.168.8.0/24" -Action Allow -Profile Any -ErrorAction SilentlyContinue
Set-NetFirewallRule -DisplayName "Windows Exporter (Prometheus)" -RemoteAddress "192.168.164.0/24","192.168.8.0/24" -ErrorAction SilentlyContinue

Write-Host "--> Waiting for service to start"
Start-Sleep -Seconds 3

$svc = Get-Service -Name "windows_exporter" -ErrorAction SilentlyContinue
if ($svc -and $svc.Status -eq "Running") {
    Write-Host ""
    Write-Host "==> windows_exporter is running. Test with:"
    Write-Host "    curl http://localhost:$PORT/metrics"
} else {
    Write-Error "windows_exporter service is not running after install"
    Get-Service -Name "windows_exporter" -ErrorAction SilentlyContinue | Format-List
    exit 1
}

Remove-Item $MSIPATH -Force
