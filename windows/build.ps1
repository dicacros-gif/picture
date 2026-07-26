$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
python -m PyInstaller --noconfirm --clean --onefile --windowed `
  --name "PictureCleanerPC" `
  --collect-all PIL `
  picture_cleaner_pc.py
Write-Host "완료: $PSScriptRoot\dist\PictureCleanerPC.exe"
