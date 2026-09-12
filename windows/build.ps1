param([string]$DistPath = "dist")
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
. (Join-Path $PSScriptRoot 'build_guard.ps1')
Assert-BlogBuildTargetIdle -TargetExecutable (Join-Path $DistPath 'Blog.exe')
python -m PyInstaller --noconfirm --clean --distpath $DistPath .\PictureCleanerPC.spec
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed with exit code $LASTEXITCODE"
}
Write-Host "완료: $DistPath\Blog.exe"
