$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
python -m PyInstaller --noconfirm --clean .\PictureCleanerPC.spec
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed with exit code $LASTEXITCODE"
}
Write-Host "완료: $PSScriptRoot\dist\PictureCleanerPC.exe"
