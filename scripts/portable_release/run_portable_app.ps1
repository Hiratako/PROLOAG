$ErrorActionPreference = "Stop"

$ReleaseDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppDir = Join-Path $ReleaseDir "app"
$PythonDir = Join-Path $ReleaseDir "runtime\python311"
$PythonExe = Join-Path $PythonDir "python.exe"
$DepsDir = Join-Path $AppDir ".deps311"
$Port = 8502
$script:AppExitCode = 0
# EXE/BAT use -NoExit so the existing diagnostic console remains available.
# Direct/non-interactive -File execution must instead return a useful exit code.
$KeepDiagnosticsOpen = [Environment]::GetCommandLineArgs() -contains '-NoExit'

try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
    $env:PYTHONUTF8 = "1"
    $env:PYTHONDONTWRITEBYTECODE = "1"
} catch {
}

function Decode-Utf8Text($base64) {
    return [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($base64))
}

function Write-WelcomeBanner {
    $banner = @'
  ====================================================================

     ____   ____    ___   _       ___      _      ____
    |  _ \ |  _ \  / _ \ | |     / _ \    / \    / ___|
    | |_) || |_) || | | || |    | | | |  / _ \  | |  _
    |  __/ |  _ < | |_| || |___ | |_| | / ___ \ | |_| |
    |_|    |_| \_\ \___/ |_____| \___/ /_/   \_\ \____|

                        P R O L O A G
       PROGRESS & REFLECTION OS FOR LEARNING,
             ORGANIZATION, AND GROWTH

  ====================================================================
'@
    Write-Host ""
    Write-Host $banner -ForegroundColor DarkCyan
    Write-Host "  PROLOAGへようこそ！" -ForegroundColor Green
    Write-Host "  今日の記録を、次の物語へ。" -ForegroundColor Gray
    Write-Host "  Progress & Reflection OS for Learning, Organization, And Growth." -ForegroundColor DarkGray
    Write-Host ""
}

function Write-Step($message) {
    Write-Host ""
    Write-Host "== $message ==" -ForegroundColor Cyan
}

function Assert-File($path, $message) {
    if (-not (Test-Path -LiteralPath $path)) {
        Write-Host ""
        Write-Host $message -ForegroundColor Red
        Write-Host "Path: $path"
        throw $message
    }
}

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Content
    )

    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $utf8NoBom)
}

function Ensure-AppDataFiles {
    $dirs = @(
        "study_logs",
        "daily_plans",
        "diary_entries",
        "health_logs",
        "exercise_logs",
        "statistics_practice",
        "notes_data",
        "master_data",
        "settings",
        "backups"
    )
    foreach ($dir in $dirs) {
        New-Item -ItemType Directory -Force -Path (Join-Path $AppDir $dir) | Out-Null
    }

    $notesPath = Join-Path $AppDir "notes_data\notebooks.json"
    if (-not (Test-Path -LiteralPath $notesPath)) {
        Write-Utf8NoBom -Path $notesPath -Content '{"notebooks":[]}'
    }

    $masterPath = Join-Path $AppDir "master_data\master.json"
    if (-not (Test-Path -LiteralPath $masterPath)) {
        Write-Utf8NoBom -Path $masterPath -Content '{"projects":[],"subjects":[],"materials":[],"units":[],"tags":[]}'
    }

    foreach ($jsonFile in @("flashcards.json", "reminders.json")) {
        $path = Join-Path $AppDir $jsonFile
        if (-not (Test-Path -LiteralPath $path)) {
            Write-Utf8NoBom -Path $path -Content '[]'
        }
    }
}

function Get-AppPort {
    $demoPath = Join-Path $AppDir 'settings\demo_mode.json'
    if (-not (Test-Path -LiteralPath $demoPath)) {
        return 8502
    }
    $demoText = Get-Content -LiteralPath $demoPath -Raw -Encoding UTF8
    if ([string]::IsNullOrWhiteSpace($demoText) -or -not $demoText.TrimStart().StartsWith('{')) {
        throw "demo_mode.json must contain a JSON object."
    }
    $demo = $demoText | ConvertFrom-Json
    if ($null -eq $demo -or $demo.GetType().FullName -ne 'System.Management.Automation.PSCustomObject') {
        throw "demo_mode.json must contain a JSON object."
    }
    if ($demo.PSObject.Properties['enabled'] -and $demo.enabled -isnot [bool]) {
        throw "demo_mode.json enabled must be true or false."
    }
    if ($demo.enabled -eq $true) {
        return 8503
    }
    return 8502
}

function Assert-AppPortAvailable {
    param([Parameter(Mandatory = $true)][ValidateRange(1, 65535)][int]$PortNumber)
    # Inspect the local listener table only. Do not connect to or stop an
    # existing application, and do not silently open another package's server.
    $listeners = [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()
    if (@($listeners | Where-Object { $_.Port -eq $PortNumber }).Count -gt 0) {
        throw "Port $PortNumber is already in use. This package has not been started and no existing app was opened. Close the other app yourself, then start this package again. Package: $ReleaseDir"
    }
}

function Get-DependencyRequirements {
    $locked = Join-Path $AppDir 'requirements-release.lock.txt'
    if (Test-Path -LiteralPath $locked -PathType Leaf) {
        return $locked
    }
    $standard = Join-Path $AppDir 'requirements.txt'
    Assert-File $standard 'Dependency requirements were not found.'
    return $standard
}

function Test-AppImports {
    $env:PYTHONPATH = "$DepsDir;$AppDir"
    & $PythonExe -c "import streamlit, pandas, matplotlib, scipy, plotly; print('deps ok')"
    return ($LASTEXITCODE -eq 0)
}

function Ensure-Pip {
    Write-Step "Pip check"
    & $PythonExe -m pip --version
    if ($LASTEXITCODE -eq 0) {
        return
    }

    Write-Host "pip was not found. Enabling pip in the bundled Python runtime." -ForegroundColor Yellow
    & $PythonExe -m ensurepip --upgrade
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Host "Failed to enable pip." -ForegroundColor Red
        Write-Host "Please check that runtime\python311 was created from a standard Python install."
        throw "pip setup failed"
    }
}

function Install-Dependencies {
    Write-Step "Dependency setup"
    if (Test-Path -LiteralPath (Join-Path $DepsDir "streamlit")) {
        if (Test-AppImports) {
            Write-Host "Dependencies are already installed."
            return
        }
        Write-Host "Dependency check failed. Reinstalling dependencies." -ForegroundColor Yellow
    } else {
        Write-Host "First setup is starting. This may take several minutes."
        Write-Host "An internet connection is required for the first setup." -ForegroundColor Yellow
    }

    $requirementsPath = Get-DependencyRequirements
    Write-Host "Dependency definition: $requirementsPath"
    Ensure-Pip
    New-Item -ItemType Directory -Force -Path $DepsDir | Out-Null
    & $PythonExe -m pip install -r $requirementsPath --target $DepsDir --upgrade
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Host "Failed to install dependencies." -ForegroundColor Red
        Write-Host "Please check your internet connection, proxy, or security software settings."
        throw "dependency install failed"
    }

    if (-not (Test-AppImports)) {
        Write-Host ""
        Write-Host "Dependency import check failed after installation." -ForegroundColor Red
        throw "dependency import failed"
    }
}

function Start-App {
    # Setup can take several minutes; check again before the Python helper.
    Assert-AppPortAvailable -PortNumber $Port
    Write-Step "Starting Streamlit"
    $env:PYTHONPATH = "$DepsDir;$AppDir"
    $url = "http://localhost:$Port/"
    Write-Host "URL: $url"
    Write-Host "Press Ctrl+C to stop the app."
    $launcher = Join-Path $AppDir "app_core\startup_launcher.py"
    & $PythonExe $launcher --app (Join-Path $AppDir "main.py") --url $url --timeout 90
    if ($LASTEXITCODE -eq 130) {
        $script:AppExitCode = 130
        return
    }
    if ($LASTEXITCODE -ne 0) {
        throw "PROLOAG launcher failed with exit code $LASTEXITCODE"
    }
}

try {
    Write-WelcomeBanner
    Write-Host "Release: $ReleaseDir"

    Assert-File $PythonExe "Bundled Python was not found."
    Assert-File (Join-Path $AppDir "main.py") "App main.py was not found."
    Assert-File (Join-Path $AppDir "requirements.txt") "requirements.txt was not found."
    Assert-File (Join-Path $AppDir "app_core\startup_launcher.py") "Startup readiness helper was not found."

    $Port = Get-AppPort
    Assert-AppPortAvailable -PortNumber $Port
    Ensure-AppDataFiles
    Install-Dependencies
    Start-App
} catch {
    $script:AppExitCode = 1
    Write-Host ""
    Write-Host "Startup failed." -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
} finally {
    if ($KeepDiagnosticsOpen) {
        Write-Host ""
        Write-Host "This window remains open for diagnostics." -ForegroundColor Yellow
        Write-Host "Startup result code: $script:AppExitCode"
    }
}

$global:LASTEXITCODE = $script:AppExitCode
if (-not $KeepDiagnosticsOpen) {
    exit $script:AppExitCode
}


