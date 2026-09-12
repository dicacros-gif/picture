function Assert-BlogBuildTargetIdle {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$TargetExecutable,
        [object[]]$RunningProcesses
    )

    # PowerShell's Set-Location does not update .NET's process working directory.
    # Resolve relative outputs against the PowerShell location used by PyInstaller.
    $targetPath = [System.IO.Path]::GetFullPath(
        $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($TargetExecutable))
    if (-not $PSBoundParameters.ContainsKey('RunningProcesses')) {
        # Query executable paths, without inspecting command lines or account data.
        $RunningProcesses = @(Get-CimInstance -ClassName Win32_Process -Filter "Name LIKE 'Blog%.exe'" -ErrorAction Stop)
    }

    foreach ($runningProcess in $RunningProcesses) {
        if ([string]::IsNullOrWhiteSpace($runningProcess.ExecutablePath)) {
            throw "Cannot verify the executable path of Blog process $($runningProcess.ProcessId). Close that process before building."
        }
        $runningPath = [System.IO.Path]::GetFullPath($runningProcess.ExecutablePath)
        if ([string]::Equals($targetPath, $runningPath, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Blog is running from '$targetPath' (PID $($runningProcess.ProcessId)). Build to another -DistPath, such as -DistPath dist-update, or close Blog before replacing this executable. Never rename or overwrite a running onefile executable."
        }
    }
}
