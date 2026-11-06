param([ValidateRange(1,65535)][int]$Port=61970)
$ErrorActionPreference='Stop'
$ProjectDir=Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonExe=Join-Path $ProjectDir '.venv\Scripts\python.exe'
$KeepOpen=([Environment]::GetCommandLineArgs() | Where-Object { $_ -ieq '-NoExit' }).Count -gt 0
$resultCode=0
function Decode-Utf8Text($base64) {
    return [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($base64))
}
$welcome=Decode-Utf8Text "UFJPTE9BR+OBuOOCiOOBhuOBk+OBne+8gQ=="
$description=Decode-Utf8Text "5LuK5pel44Gu6KiY6Yyy44KS44CB5qyh44Gu54mp6Kqe44G444CC"
Write-Host $welcome -ForegroundColor Green
Write-Host $description -ForegroundColor Gray
try {
    if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
        throw 'Prepare Python 3.11 and .venv using the four commands in README.md first.'
    }
    $env:PYTHONNOUSERSITE='1'
    $env:PYTHONDONTWRITEBYTECODE='1'
    $env:PYTHONUTF8='1'
    $env:PYTHONPATH=$ProjectDir
    $version=& $PythonExe -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")'
    if ($LASTEXITCODE -ne 0 -or $version -ne '3.11') { throw 'This source launcher expects a Python 3.11 .venv.' }
    $listeners=[System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()
    if (@($listeners | Where-Object { $_.Port -eq $Port }).Count) {
        throw "Port $Port is in use. No existing server has been opened or stopped. Choose an unused port with -Port."
    }
    Set-Location -LiteralPath $ProjectDir
    & $PythonExe -c 'import streamlit,pandas,matplotlib,scipy,plotly; print("Dependencies ready")'
    if ($LASTEXITCODE -ne 0) { throw 'Install requirements.txt into .venv as described in README.md.' }
    & $PythonExe -c 'from web_demo.data import verify_demo_dataset; print(verify_demo_dataset())'
    if ($LASTEXITCODE -ne 0) { throw 'Fixed exhibition data verification failed.' }
    & $PythonExe (Join-Path $ProjectDir 'app_core\startup_launcher.py') --app (Join-Path $ProjectDir 'main.py') --url "http://localhost:$Port/" --timeout 90
    $resultCode=$LASTEXITCODE
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    $resultCode=1
}
$global:LASTEXITCODE=$resultCode
if ($KeepOpen) { Write-Host "Source launcher result: $resultCode" } else { exit $resultCode }
