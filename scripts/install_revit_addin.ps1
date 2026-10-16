# Install MEP Tray Revit add-in (external commands).
# Usage: powershell -ExecutionPolicy Bypass -File scripts/install_revit_addin.ps1 [-RevitVersion 2025] [-DryRun]
# - Clean-rebuilds Release for the TFM that Revit's own RevitAPI.runtimeconfig.json declares (fail-fast on mismatch).
# - Verifies every <FullClassName> in MepTray.addin exists in the freshly built MepTrayImport.dll.
# - Lists staged files with timestamp + SHA256.
# - -DryRun: build + verify + list only; nothing is copied into %APPDATA%\Autodesk\Revit\Addins.
# - Otherwise copies DLLs to %APPDATA%\Autodesk\Revit\Addins\<ver>\MEPTray-<hash>\ and writes MepTray.addin
#   with the absolute assembly path, then lists the deployed files with SHA256.
#   Never modifies Revit, its runtime, or OS security policy; never uses Unblock-File.
# - Revit 2027 is not machine-verified (override at your own risk); see docs/VERIFICATION.md.
# NOTE: keep this file ASCII-only; PowerShell 5.1 misreads UTF-8 without BOM.
[CmdletBinding()]
param(
    [string]$RevitVersion = "2025",
    [string]$RevitInstallDir = "C:\Program Files\Autodesk\Revit $RevitVersion\",
    [switch]$DryRun
)
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

function Show-Files([string]$dir) {
    Get-ChildItem $dir -File | Sort-Object Name | ForEach-Object {
        $h = (Get-FileHash $_.FullName -Algorithm SHA256).Hash
        "{0,-36} {1,9} {2:yyyy-MM-dd HH:mm:ss} {3}" -f $_.Name, $_.Length, $_.LastWriteTime, $h
    }
}

# 1. Locate dotnet (SDK 10); prefer the repo venv python
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
$dotnet = & $py -c "from tests.dotnet_util import find_dotnet; print(find_dotnet())"
if ($LASTEXITCODE -ne 0 -or -not $dotnet -or $dotnet -eq "None") { throw "dotnet not found; .NET SDK 10 required" }

# 2. Revit version + runtime (read-only)
$api = Join-Path $RevitInstallDir "RevitAPI.dll"
if (-not (Test-Path $api)) { throw "Revit install dir not found: $RevitInstallDir (use -RevitInstallDir)" }
$exe = Join-Path $RevitInstallDir "Revit.exe"
if (Test-Path $exe) { Write-Host ("Revit.exe    " + (Get-Item $exe).VersionInfo.FileVersion + "  " + (Get-Item $exe).VersionInfo.ProductVersion) }
Write-Host ("RevitAPI.dll " + (Get-Item $api).VersionInfo.FileVersion)
$cfgPath = Join-Path $RevitInstallDir "RevitAPI.runtimeconfig.json"
if (-not (Test-Path $cfgPath)) { throw "RevitAPI.runtimeconfig.json not found; cannot determine Revit .NET runtime" }
$revitTfm = (Get-Content $cfgPath -Raw | ConvertFrom-Json).runtimeOptions.tfm
if (-not $revitTfm) { throw "cannot read tfm from $cfgPath" }
$tfm = "$revitTfm-windows"
Write-Host "Revit runtime tfm: $revitTfm -> building $tfm"

# 3. Clean rebuild into a fresh staging folder (no stale files)
$stage = Join-Path $root "revit\MepTrayImport\bin\Release\$tfm"
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
$dirArg = $RevitInstallDir.TrimEnd('\') + '\\'    # trailing backslash must be doubled or MSBuild eats the closing quote
& $dotnet build revit/MepTrayImport/MepTrayImport.csproj -c Release -nologo --no-incremental `
    -p:RevitInstallDir="$dirArg" -p:RevitTfm=$tfm
if ($LASTEXITCODE -ne 0) { throw "build failed" }
$dll = Join-Path $stage "MepTrayImport.dll"
if (-not (Test-Path $dll)) { throw "build output not found: $dll" }

# 4. Verify built runtime matches Revit
$builtTfm = (Get-Content (Join-Path $stage "MepTrayImport.runtimeconfig.json") -Raw | ConvertFrom-Json).runtimeOptions.tfm
if ($builtTfm -ne $revitTfm) { throw "runtime mismatch: add-in $builtTfm vs Revit $revitTfm" }
foreach ($ref in "RevitAPI.dll", "RevitAPIUI.dll") {
    if (Test-Path (Join-Path $stage $ref)) { throw "$ref was copied to output (must be Private=false)" }
}

# 5. Verify every command class in the manifest exists in the built DLL (metadata names are UTF-8)
[xml]$addin = Get-Content "revit/MepTrayImport/MepTray.addin" -Raw -Encoding UTF8
$bytes = [System.IO.File]::ReadAllBytes($dll)
$latin = [System.Text.Encoding]::GetEncoding(28591).GetString($bytes)
foreach ($a in $addin.RevitAddIns.AddIn) {
    $full = $a.FullClassName
    $ns = $full.Substring(0, $full.LastIndexOf('.'))
    $cls = $full.Substring($full.LastIndexOf('.') + 1)
    if ($a.Assembly -ne "MepTrayImport.dll") { throw "unexpected Assembly in template: $($a.Assembly)" }
    if (-not $latin.Contains($cls) -or -not $latin.Contains($ns)) { throw "class $full not found in $dll" }
    Write-Host "command OK: $full"
}

Write-Host "`nStaged build ($stage):"
Show-Files $stage

if ($DryRun) {
    $short = (Get-FileHash $dll -Algorithm SHA256).Hash.Substring(0, 7)
    Write-Host "`nDRY RUN: nothing installed. Would deploy to:"
    Write-Host (Join-Path $env:APPDATA "Autodesk\Revit\Addins\$RevitVersion\MEPTray-$short")
    Write-Host (Join-Path $env:APPDATA "Autodesk\Revit\Addins\$RevitVersion\MepTray.addin")
    exit 0
}

# 6. Deploy to versioned folder (old versions kept for manual rollback)
$short = (Get-FileHash $dll -Algorithm SHA256).Hash.Substring(0, 7)
$dest = Join-Path $env:APPDATA "Autodesk\Revit\Addins\$RevitVersion\MEPTray-$short"
New-Item -ItemType Directory $dest -Force | Out-Null
Copy-Item (Join-Path $stage "*") $dest -Recurse -Force

# 7. Write add-in manifest with absolute assembly path
$manifest = Join-Path $env:APPDATA "Autodesk\Revit\Addins\$RevitVersion\MepTray.addin"
$text = Get-Content "revit/MepTrayImport/MepTray.addin" -Raw -Encoding UTF8
$text = $text.Replace("<Assembly>MepTrayImport.dll</Assembly>",
                      "<Assembly>$dest\MepTrayImport.dll</Assembly>")
[System.IO.File]::WriteAllText($manifest, $text, (New-Object System.Text.UTF8Encoding($false)))

# 8. Report
Write-Host "`nInstalled: $manifest -> $dest"
Show-Files $dest
Write-Host "Restart Revit $RevitVersion; commands appear under Add-Ins > External Tools."
Write-Host "If load fails, check CodeIntegrity events; do NOT weaken OS policy (see docs/VERIFICATION.md)."