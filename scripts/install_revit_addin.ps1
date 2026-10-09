# Install MEP Tray Revit add-in (external command).
# Usage: powershell -File scripts/install_revit_addin.ps1 [-RevitVersion 2025]
# - Builds Release and copies DLLs to %APPDATA%\Autodesk\Revit\Addins\<ver>\MEPTray-<hash>\
# - Writes MepTray.addin pointing at that absolute path.
#   Never modifies OS security policy and never uses Unblock-File.
# - Revit 2027 is not machine-verified (override at your own risk); see docs/VERIFICATION.md.
# NOTE: keep this file ASCII-only; PowerShell 5.1 misreads UTF-8 without BOM.
[CmdletBinding()]
param(
    [string]$RevitVersion = "2025",
    [string]$RevitInstallDir = "C:\Program Files\Autodesk\Revit $RevitVersion\"
)
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

# 1. Locate dotnet (SDK 10)
$dotnet = python -c "from tests.dotnet_util import find_dotnet; print(find_dotnet())"
if ($LASTEXITCODE -ne 0) { throw "dotnet not found; .NET SDK 10 required" }
if (-not (Test-Path (Join-Path $RevitInstallDir "RevitAPI.dll"))) {
    throw "Revit install dir not found: $RevitInstallDir (use -RevitInstallDir)"
}

# 2. Build (trailing backslash must be doubled or MSBuild eats the closing quote)
$dirArg = $RevitInstallDir.TrimEnd('\') + '\\'
& $dotnet build revit/MepTrayImport/MepTrayImport.csproj -c Release -nologo `
    -p:RevitInstallDir="$dirArg"
if ($LASTEXITCODE -ne 0) { throw "build failed" }

$out = Get-Item "revit/MepTrayImport/bin/Release/*/MepTrayImport.dll" |
       Sort-Object LastWriteTime -Descending | Select-Object -First 1
$stage = $out.Directory.FullName

# 3. Deploy to versioned folder (old versions kept for manual rollback)
$short = (Get-FileHash (Join-Path $stage "MepTrayImport.dll") -Algorithm SHA256).Hash.Substring(0, 7)
$dest = Join-Path $env:APPDATA "Autodesk\Revit\Addins\$RevitVersion\MEPTray-$short"
New-Item -ItemType Directory $dest -Force | Out-Null
Copy-Item (Join-Path $stage "*") $dest -Recurse -Force

# 4. Write add-in manifest with absolute assembly path
$manifest = Join-Path $env:APPDATA "Autodesk\Revit\Addins\$RevitVersion\MepTray.addin"
$text = Get-Content "revit/MepTrayImport/MepTray.addin" -Raw -Encoding UTF8
$text = $text.Replace("<Assembly>MepTrayImport.dll</Assembly>",
                      "<Assembly>$dest\MepTrayImport.dll</Assembly>")
[System.IO.File]::WriteAllText($manifest, $text, (New-Object System.Text.UTF8Encoding($false)))

# 5. Report
Write-Host "Installed: $manifest -> $dest"
Write-Host "Restart Revit $RevitVersion; command appears under Add-Ins > External Commands as 'MEP Tray Import'."
Write-Host "If load fails, check CodeIntegrity events; do NOT weaken OS policy (see docs/VERIFICATION.md)."