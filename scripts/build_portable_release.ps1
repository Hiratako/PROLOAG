param(
    [string]$OutputRoot = "release",
    [string]$ReleaseName = "proloag_portable_light",
    [string]$PythonExe = "",
    [string]$FFmpegDirectory = "",
    [switch]$IncludeSampleMaster,
    [switch]$NoZip,
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$OutputRootPath = [System.IO.Path]::GetFullPath((Join-Path $ProjectRoot $OutputRoot))
$ReleaseDir = [System.IO.Path]::GetFullPath((Join-Path $OutputRootPath $ReleaseName))
$AppDir = Join-Path $ReleaseDir "app"
$RuntimeDir = Join-Path $ReleaseDir "runtime\python311"
$TemplateDir = Join-Path $ProjectRoot "scripts\portable_release"

function Assert-PathWithin {
    param([string]$Path, [string]$AllowedRoot, [switch]$AllowRoot)
    $targetPath = [System.IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
    $rootPath = [System.IO.Path]::GetFullPath($AllowedRoot).TrimEnd('\', '/')
    $isRoot = $targetPath.Equals($rootPath, [System.StringComparison]::OrdinalIgnoreCase)
    if (($isRoot -and -not $AllowRoot) -or (-not $isRoot -and -not $targetPath.StartsWith($rootPath + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase))) {
        throw "Path is outside the allowed release directory: $targetPath"
    }
    # Reject junctions/symlinks in existing ancestors before any copy or removal.
    $ancestor = $targetPath
    while ($ancestor) {
        if (Test-Path -LiteralPath $ancestor) {
            $entry = Get-Item -LiteralPath $ancestor -Force
            if ($entry.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
                throw "Release paths cannot traverse a reparse point: $ancestor"
            }
        }
        $ancestor = Split-Path -Parent $ancestor
    }
}

function Assert-ReleaseTargets {
    if ([string]::IsNullOrWhiteSpace($ReleaseName) -or $ReleaseName -in @('.', '..') -or $ReleaseName -ne [System.IO.Path]::GetFileName($ReleaseName) -or $ReleaseName.IndexOfAny([System.IO.Path]::GetInvalidFileNameChars()) -ge 0 -or $ReleaseName -match '[. ]$') {
        throw "ReleaseName must be a single safe directory name."
    }
    $releaseRoot = Join-Path $ProjectRoot "release"
    Assert-PathWithin -Path $OutputRootPath -AllowedRoot $releaseRoot -AllowRoot
    Assert-PathWithin -Path $ReleaseDir -AllowedRoot $OutputRootPath
    Assert-PathWithin -Path $AppDir -AllowedRoot $ReleaseDir
    Assert-PathWithin -Path $RuntimeDir -AllowedRoot $ReleaseDir
    Assert-PathWithin -Path (Join-Path $OutputRootPath "$ReleaseName.zip") -AllowedRoot $OutputRootPath
}

function Remove-ReleaseItem {
    param([string]$Path, [string]$AllowedRoot, [switch]$AllowRoot)
    Assert-ReleaseTargets
    Assert-PathWithin -Path $Path -AllowedRoot $AllowedRoot -AllowRoot:$AllowRoot
    Remove-Item -LiteralPath ([System.IO.Path]::GetFullPath($Path)) -Recurse -Force
}

function Write-Step($message) {
    Write-Host ""
    Write-Host "== $message ==" -ForegroundColor Cyan
}

function Decode-Utf8Text($base64) {
    return [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($base64))
}

function Invoke-Python($command, $arguments) {
    & $command @arguments
}

function Get-PythonCandidate {
    param([string]$Command, [string[]]$Arguments = @())
    try {
        $code = "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}|{sys.executable}')"
        $allArgs = @()
        $allArgs += $Arguments
        $allArgs += @("-c", $code)
        $output = & $Command @allArgs 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $output) {
            return $null
        }
        $parts = ($output | Select-Object -First 1).Split("|")
        if ($parts[0] -ne "3.11") {
            return $null
        }
        return $parts[1]
    } catch {
        return $null
    }
}

function Resolve-Python311 {
    if ($PythonExe) {
        if (-not (Test-Path -LiteralPath $PythonExe)) {
            throw "Specified Python was not found: $PythonExe"
        }
        $version = & $PythonExe -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
        if ($version -ne "3.11") {
            throw "Please specify Python 3.11. Detected: $version"
        }
        return (Resolve-Path $PythonExe).Path
    }

    $candidates = @(
        @{ Command = "py"; Args = @("-3.11") },
        @{ Command = "python"; Args = @() },
        @{ Command = (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"); Args = @() }
    )
    foreach ($candidate in $candidates) {
        if ($candidate.Command.EndsWith(".exe") -and -not (Test-Path -LiteralPath $candidate.Command)) {
            continue
        }
        $resolved = Get-PythonCandidate -Command $candidate.Command -Arguments $candidate.Args
        if ($resolved) {
            return $resolved
        }
    }
    throw "Python 3.11 was not found. Use -PythonExe to specify python.exe."
}

function Copy-DirectoryFiltered {
    param(
        [string]$Source,
        [string]$Destination,
        [string[]]$ExcludeNames = @()
    )
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    Get-ChildItem -LiteralPath $Source -Force | ForEach-Object {
        if ($ExcludeNames -contains $_.Name) {
            return
        }
        $target = Join-Path $Destination $_.Name
        Copy-Item -LiteralPath $_.FullName -Destination $target -Recurse -Force
    }
}

function Copy-PythonRuntime {
    param([string]$SourcePythonExe, [string]$Destination)
    $sourceDir = Split-Path -Parent $SourcePythonExe
    Write-Host "Python source: $sourceDir"
    Write-Host "Runtime dest : $Destination"
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null

    $runtimeFiles = @('python.exe', 'pythonw.exe', 'python3.dll', 'python311.dll', 'vcruntime140.dll', 'vcruntime140_1.dll', 'LICENSE.txt')
    foreach ($file in $runtimeFiles) {
        $sourceFile = Join-Path $sourceDir $file
        if (Test-Path -LiteralPath $sourceFile -PathType Leaf) {
            Copy-Item -LiteralPath $sourceFile -Destination $Destination -Force
        } elseif ($file -in @('python.exe', 'python311.dll')) {
            throw "Required Python runtime file is missing: $sourceFile"
        }
    }
    $sourceDlls = Join-Path $sourceDir 'DLLs'
    if (Test-Path -LiteralPath $sourceDlls -PathType Container) {
        $destDlls = Join-Path $Destination 'DLLs'
        New-Item -ItemType Directory -Force -Path $destDlls | Out-Null
        Get-ChildItem -LiteralPath $sourceDlls -File | Where-Object { $_.Extension -in @('.pyd', '.dll') } | ForEach-Object {
            Copy-Item -LiteralPath $_.FullName -Destination $destDlls -Force
        }
    }

    $sourceLib = Join-Path $sourceDir "Lib"
    $destLib = Join-Path $Destination "Lib"
    if (Test-Path -LiteralPath $sourceLib) {
        Copy-DirectoryFiltered -Source $sourceLib -Destination $destLib -ExcludeNames @("site-packages", "__pycache__")
    }

    $sitePackages = Join-Path $Destination "Lib\site-packages"
    if (Test-Path -LiteralPath $sitePackages) {
        Write-Host "Removing bundled site-packages to keep the ZIP light."
        Remove-ReleaseItem -Path $sitePackages -AllowedRoot $ReleaseDir
    }
    New-Item -ItemType Directory -Force -Path $sitePackages | Out-Null

    Get-ChildItem -LiteralPath $Destination -Recurse -Force -Directory -Filter "__pycache__" | ForEach-Object {
        Remove-ReleaseItem -Path $_.FullName -AllowedRoot $ReleaseDir
    }
    Get-ChildItem -LiteralPath $Destination -Recurse -Force -File -Filter "*.pyc" | ForEach-Object {
        Remove-ReleaseItem -Path $_.FullName -AllowedRoot $ReleaseDir
    }
}

function Build-Launcher {
    param([string]$OutputPath)
    Write-Step "Build lightweight launcher"
    $sourcePath = Join-Path $TemplateDir "PROLOAGLauncher.cs"
    if (-not (Test-Path -LiteralPath $sourcePath)) {
        throw "Launcher source was not found: $sourcePath"
    }
    if (Test-Path -LiteralPath $OutputPath) {
        Remove-ReleaseItem -Path $OutputPath -AllowedRoot $ReleaseDir
    }

    $source = Get-Content -LiteralPath $sourcePath -Raw -Encoding UTF8
    Add-Type `
        -TypeDefinition $source `
        -Language CSharp `
        -ReferencedAssemblies @("System.Windows.Forms.dll") `
        -OutputAssembly $OutputPath `
        -OutputType WindowsApplication

    if (-not (Test-Path -LiteralPath $OutputPath)) {
        throw "Launcher build failed: $OutputPath"
    }
    Write-Host "Launcher: $OutputPath" -ForegroundColor Green
}

function Copy-AppFiles {
    Write-Step "Copy app files"
    New-Item -ItemType Directory -Force -Path $AppDir | Out-Null

    $topFiles = @(
        "main.py",
        "config.py",
        "data_utils.py",
        "master_ui.py",
        "requirements.txt",
        "requirements-google.txt",
        "README.md"
    )
    foreach ($file in $topFiles) {
        Copy-Item -LiteralPath (Join-Path $ProjectRoot $file) -Destination $AppDir -Force
    }

    $asrRequirements = Join-Path $ProjectRoot "requirements-transcription.txt"
    if (Test-Path -LiteralPath $asrRequirements -PathType Leaf) {
        Copy-Item -LiteralPath $asrRequirements -Destination $AppDir -Force
    }

    $ocrRequirements = Join-Path $ProjectRoot "requirements-ocr.txt"
    if (Test-Path -LiteralPath $ocrRequirements -PathType Leaf) {
        Copy-Item -LiteralPath $ocrRequirements -Destination $AppDir -Force
    }

    # Include only public setup guides; never recursively copy docs or auth files.
    $docsDir = Join-Path $AppDir "docs"
    New-Item -ItemType Directory -Path $docsDir -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $ProjectRoot "docs\google-sheets-setup.md") -Destination $docsDir -Force
    Copy-Item -LiteralPath (Join-Path $ProjectRoot "docs\google-docs-reports.md") -Destination $docsDir -Force
    Copy-Item -LiteralPath (Join-Path $ProjectRoot "docs\consultation-proposals.md") -Destination $docsDir -Force
    $mediaGuide = Join-Path $ProjectRoot 'docs\media-conversion.md'
    if (Test-Path -LiteralPath $mediaGuide -PathType Leaf) {
        Copy-Item -LiteralPath $mediaGuide -Destination $docsDir -Force
    }

    $asrGuide = Join-Path $ProjectRoot "docs\transcription.md"
    if (Test-Path -LiteralPath $asrGuide -PathType Leaf) {
        Copy-Item -LiteralPath $asrGuide -Destination $docsDir -Force
    }

    $ocrGuide = Join-Path $ProjectRoot "docs\ocr.md"
    if (Test-Path -LiteralPath $ocrGuide -PathType Leaf) {
        Copy-Item -LiteralPath $ocrGuide -Destination $docsDir -Force
    }

    foreach ($roadmapImportDoc in @('roadmap-ai-import.md', 'ai-roadmap-import-prompt.md')) {
        $roadmapImportDocPath = Join-Path (Join-Path $ProjectRoot 'docs') $roadmapImportDoc
        if (Test-Path -LiteralPath $roadmapImportDocPath -PathType Leaf) {
            Copy-Item -LiteralPath $roadmapImportDocPath -Destination $docsDir -Force
        }
    }

    foreach ($dir in @("pages", "app_core", "prolog_ui")) {
        $source = Join-Path $ProjectRoot $dir
        if (-not (Test-Path -LiteralPath $source -PathType Container)) {
            throw "Required source directory is missing: $source"
        }
        $entries = @(Get-ChildItem -LiteralPath $source -Recurse -Force)
        if ($entries | Where-Object { $_.Attributes -band [System.IO.FileAttributes]::ReparsePoint }) {
            throw "App source directories cannot contain junctions or symlinks: $source"
        }
        # Ship code and the shared stylesheet, never JSON, credentials, or caches.
        foreach ($entry in $entries | Where-Object { -not $_.PSIsContainer -and ($_.Extension -eq '.py' -or ($dir -eq 'prolog_ui' -and $_.Name -eq 'styles.css')) }) {
            $relative = $entry.FullName.Substring($source.Length).TrimStart('\', '/')
            $destination = Join-Path (Join-Path $AppDir $dir) $relative
            Assert-PathWithin -Path $destination -AllowedRoot $AppDir
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destination) | Out-Null
            Copy-Item -LiteralPath $entry.FullName -Destination $destination -Force
        }
    }
    $streamlitDir = Join-Path $AppDir '.streamlit'
    New-Item -ItemType Directory -Force -Path $streamlitDir | Out-Null
    Copy-Item -LiteralPath (Join-Path $ProjectRoot '.streamlit\config.toml') -Destination $streamlitDir -Force
    foreach ($required in @('app_core\startup_launcher.py', 'app_core\navigation.py', 'app_core\countdown_service.py', 'app_core\personalization_ui.py', 'prolog_ui\styles.css')) {
        if (-not (Test-Path -LiteralPath (Join-Path $AppDir $required) -PathType Leaf)) {
            throw "Required app asset was not copied: $required"
        }
    }
}

function Copy-MediaTools {
    # Optional, explicit distribution directory. Never download tools or copy
    # arbitrary contents of a user's media folder into the release.
    if ([string]::IsNullOrWhiteSpace($FFmpegDirectory)) { return }
    $sourceRoot = (Resolve-Path -LiteralPath $FFmpegDirectory).Path
    $binaryRoot = $sourceRoot
    if (Test-Path -LiteralPath (Join-Path $sourceRoot 'bin\ffmpeg.exe') -PathType Leaf) {
        $binaryRoot = Join-Path $sourceRoot 'bin'
    }
    $licenses = @((Join-Path $sourceRoot 'LICENSE'), (Join-Path $sourceRoot 'LICENSE.txt'),
                  (Join-Path (Split-Path -Parent $binaryRoot) 'LICENSE'), (Join-Path (Split-Path -Parent $binaryRoot) 'LICENSE.txt'))
    $licensePath = $licenses | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
    if (-not $licensePath) { throw 'FFmpeg distribution LICENSE or LICENSE.txt is required for bundling.' }
    $sourceFiles = @((Join-Path $binaryRoot 'ffmpeg.exe'), (Join-Path $binaryRoot 'ffprobe.exe'), $licensePath)
    foreach ($sourceFile in $sourceFiles) {
        $entry = Get-Item -LiteralPath $sourceFile -ErrorAction Stop
        if ($entry.PSIsContainer -or ($entry.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
            throw "FFmpeg source must be a regular file: $sourceFile"
        }
    }
    $destination = Join-Path $AppDir 'tools\ffmpeg\bin'
    Assert-PathWithin -Path $destination -AllowedRoot $AppDir
    New-Item -ItemType Directory -Force -Path $destination | Out-Null
    foreach ($name in @('ffmpeg.exe', 'ffprobe.exe')) {
        Assert-PathWithin -Path (Join-Path $destination $name) -AllowedRoot $AppDir
        Copy-Item -LiteralPath (Join-Path $binaryRoot $name) -Destination (Join-Path $destination $name) -Force
    }
    Assert-PathWithin -Path (Join-Path (Split-Path -Parent $destination) 'LICENSE') -AllowedRoot $AppDir
    Copy-Item -LiteralPath $licensePath -Destination (Join-Path (Split-Path -Parent $destination) 'LICENSE') -Force
}

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Content
    )

    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $utf8NoBom)
}

function Initialize-EmptyData {
    Write-Step "Initialize empty local data"
    foreach ($dir in @("study_logs", "daily_plans", "diary_entries", "health_logs", "exercise_logs", "statistics_practice", "notes_data", "master_data", "settings", "backups")) {
        New-Item -ItemType Directory -Force -Path (Join-Path $AppDir $dir) | Out-Null
    }

    Write-Utf8NoBom -Path (Join-Path $AppDir "notes_data\notebooks.json") -Content '{"notebooks":[]}'
    Write-Utf8NoBom -Path (Join-Path $AppDir "flashcards.json") -Content '[]'
    Write-Utf8NoBom -Path (Join-Path $AppDir "reminders.json") -Content '[]'

    if ($IncludeSampleMaster) {
        $masterJson = @'
{
  "projects": [
    {"project_id": "project_default_001", "name": "\u533b\u5b66", "description": "", "is_active": true, "created_at": "2026-01-01T00:00:00", "updated_at": "2026-01-01T00:00:00"},
    {"project_id": "project_default_002", "name": "\u82f1\u8a9e", "description": "", "is_active": true, "created_at": "2026-01-01T00:00:00", "updated_at": "2026-01-01T00:00:00"}
  ],
  "subjects": [
    {"subject_id": "subject_default_001", "project_id": "project_default_001", "name": "\u89e3\u5256\u5b66", "description": "", "is_active": true, "created_at": "2026-01-01T00:00:00", "updated_at": "2026-01-01T00:00:00"},
    {"subject_id": "subject_default_002", "project_id": "project_default_001", "name": "\u751f\u7406\u5b66", "description": "", "is_active": true, "created_at": "2026-01-01T00:00:00", "updated_at": "2026-01-01T00:00:00"},
    {"subject_id": "subject_default_003", "project_id": "project_default_001", "name": "\u751f\u5316\u5b66", "description": "", "is_active": true, "created_at": "2026-01-01T00:00:00", "updated_at": "2026-01-01T00:00:00"},
    {"subject_id": "subject_default_004", "project_id": "project_default_001", "name": "\u514d\u75ab\u5b66", "description": "", "is_active": true, "created_at": "2026-01-01T00:00:00", "updated_at": "2026-01-01T00:00:00"},
    {"subject_id": "subject_default_005", "project_id": "project_default_001", "name": "\u5206\u5b50\u751f\u7269\u5b66", "description": "", "is_active": true, "created_at": "2026-01-01T00:00:00", "updated_at": "2026-01-01T00:00:00"},
    {"subject_id": "subject_default_006", "project_id": "project_default_001", "name": "\u75be\u60a3\u77e5\u8b58", "description": "", "is_active": true, "created_at": "2026-01-01T00:00:00", "updated_at": "2026-01-01T00:00:00"},
    {"subject_id": "subject_default_007", "project_id": "project_default_002", "name": "\u82f1\u8a9e\u8ad6\u6587", "description": "", "is_active": true, "created_at": "2026-01-01T00:00:00", "updated_at": "2026-01-01T00:00:00"}
  ],
  "materials": [],
  "units": [],
  "tags": []
}
'@
        Write-Utf8NoBom -Path (Join-Path $AppDir "master_data\master.json") -Content $masterJson
    } else {
        Write-Utf8NoBom -Path (Join-Path $AppDir "master_data\master.json") -Content '{"projects":[],"subjects":[],"materials":[],"units":[],"tags":[]}'
    }
}

function Write-FirstReadme {
    $launcherName = "PROLOAG.exe"
    $startName = Decode-Utf8Text "6LW35YuV44GZ44KLLmJhdA=="
    $readme = @"
# PROLOAG - First Run

1. Extract the ZIP file.
2. Usually, double-click $launcherName.
3. If the exe does not work in your environment, double-click $startName.
4. On the first run, the app installs Python libraries into app/.deps311.
5. This first setup may take several minutes and requires an internet connection.
6. From the second run onward, the existing app/.deps311 is reused and startup is usually faster.
7. The regular app opens at http://localhost:8502/.
8. When app/settings/demo_mode.json has enabled=true, the demo opens at http://localhost:8503/ instead, keeping it separate from a regular app already running on 8502.

## Bundled Python

Python is bundled under runtime/python311/.
Your friend does not need to install Python separately.

## Local data location

Your records are saved under the app/ folder:

- app/study_logs/
- app/daily_plans/
- app/notes_data/
- app/master_data/
- app/diary_entries/
- app/health_logs/
- app/exercise_logs/
- app/statistics_practice/
- app/flashcards.json
- app/reminders.json

## Notes

- Do not close the PowerShell window during the first setup.
- If startup fails, read the message in the PowerShell window.
- The exe is only a lightweight launcher. It does not contain the Streamlit app itself.
- Launcher errors are written to logs/launcher_YYYYMMDD.log.
- The app stores personal data locally. Be careful when sharing a used app folder.
"@
    $readme | Set-Content -Encoding UTF8 -LiteralPath (Join-Path $ReleaseDir "README_FIRST.md")
}

Assert-ReleaseTargets
if (-not $NoZip -and (Test-Path -LiteralPath (Join-Path $OutputRootPath "$ReleaseName.zip")) -and -not $Force) {
    throw "Release ZIP already exists. Use a new ReleaseName or explicitly specify -Force."
}
Write-Step "Prepare release directory"
if (Test-Path -LiteralPath $ReleaseDir) {
    if (-not $Force) {
        throw "Release directory already exists. Use -Force to overwrite: $ReleaseDir"
    }
    Remove-ReleaseItem -Path $ReleaseDir -AllowedRoot $OutputRootPath
}
New-Item -ItemType Directory -Force -Path $ReleaseDir | Out-Null

$startBatName = Decode-Utf8Text "6LW35YuV44GZ44KLLmJhdA=="
$launcherExeName = "PROLOAG.exe"
Copy-Item -LiteralPath (Join-Path $TemplateDir "start_app.bat") -Destination (Join-Path $ReleaseDir $startBatName) -Force
Copy-Item -LiteralPath (Join-Path $TemplateDir "run_portable_app.ps1") -Destination $ReleaseDir -Force
Build-Launcher -OutputPath (Join-Path $ReleaseDir $launcherExeName)

$resolvedPython = Resolve-Python311
Write-Step "Copy Python 3.11 runtime"
Copy-PythonRuntime -SourcePythonExe $resolvedPython -Destination $RuntimeDir

Copy-AppFiles
Copy-MediaTools
Initialize-EmptyData
Write-FirstReadme

if (-not $NoZip) {
    Write-Step "Create ZIP"
    $zipPath = Join-Path $OutputRootPath "$ReleaseName.zip"
    if (Test-Path -LiteralPath $zipPath) {
        Remove-ReleaseItem -Path $zipPath -AllowedRoot $OutputRootPath
    }
    Compress-Archive -LiteralPath $ReleaseDir -DestinationPath $zipPath -Force
    Write-Host "ZIP: $zipPath" -ForegroundColor Green
}

Write-Step "Done"
Write-Host "Release folder: $ReleaseDir" -ForegroundColor Green
Write-Host "Friend entry point: $launcherExeName"
Write-Host "Fallback entry point: $startBatName"
