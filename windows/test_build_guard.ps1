$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'build_guard.ps1')

function Assert-Blocked {
    param([scriptblock]$Action, [string]$ExpectedMessage)
    try {
        & $Action
    } catch {
        if ($_.Exception.Message -notlike "*$ExpectedMessage*") {
            throw "Unexpected rejection: $($_.Exception.Message)"
        }
        return
    }
    throw 'Expected the build target to be rejected.'
}

# Synthetic process snapshots exercise the guard without starting or stopping apps.
$target = Join-Path $PSScriptRoot 'dist\Blog.exe'
$busy = @([pscustomobject]@{ ExecutablePath = $target; ProcessId = 12345 })
Assert-Blocked { Assert-BlogBuildTargetIdle -TargetExecutable $target -RunningProcesses $busy } '-DistPath'
$caseVariant = @([pscustomobject]@{ ExecutablePath = $target.ToUpperInvariant(); ProcessId = 12345 })
Assert-Blocked { Assert-BlogBuildTargetIdle -TargetExecutable $target -RunningProcesses $caseVariant } 'PID 12345'
$equivalent = Join-Path $PSScriptRoot 'dist\..\dist\Blog.exe'
Assert-Blocked { Assert-BlogBuildTargetIdle -TargetExecutable $equivalent -RunningProcesses $busy } '-DistPath'
Push-Location $PSScriptRoot
try {
    Assert-Blocked { Assert-BlogBuildTargetIdle -TargetExecutable 'dist\Blog.exe' -RunningProcesses $busy } '-DistPath'
} finally { Pop-Location }
Assert-BlogBuildTargetIdle -TargetExecutable $target -RunningProcesses @()
Assert-BlogBuildTargetIdle -TargetExecutable (Join-Path $PSScriptRoot 'dist-update\Blog.exe') -RunningProcesses $busy
$unknown = @([pscustomobject]@{ ExecutablePath = $null; ProcessId = 54321 })
Assert-Blocked { Assert-BlogBuildTargetIdle -TargetExecutable $target -RunningProcesses $unknown } 'Cannot verify'

# Invoke the real build entry point with mocked process discovery and Python.
# A busy output must never reach PyInstaller; a separate output keeps its arguments.
$buildCalls = [System.Collections.Generic.List[string]]::new()
function Get-CimInstance {
    param($ClassName, $Filter, $ErrorAction)
    if ($ClassName -ne 'Win32_Process' -or $Filter -ne "Name LIKE 'Blog%.exe'") {
        throw 'Unexpected process query.'
    }
    return $busy
}
function python {
    $buildCalls.Add(($args -join '|'))
    $global:LASTEXITCODE = 0
}
Assert-Blocked { & (Join-Path $PSScriptRoot 'build.ps1') } '-DistPath'
if ($buildCalls.Count -ne 0) { throw 'The busy build reached PyInstaller.' }
& (Join-Path $PSScriptRoot 'build.ps1') -DistPath 'dist-update' | Out-Null
if ($buildCalls.Count -ne 1 -or $buildCalls[0] -ne '-m|PyInstaller|--noconfirm|--clean|--distpath|dist-update|.\PictureCleanerPC.spec') {
    throw 'The separate build did not preserve the PyInstaller arguments.'
}
Write-Host 'PASS: 9 build guard checks (synthetic processes; no app, build, or settings changes).'
