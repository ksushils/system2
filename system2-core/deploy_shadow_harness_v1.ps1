[CmdletBinding()]
param([switch]$Execute,[string]$Target='/root/system2-shadow-harness')
$ErrorActionPreference='Stop'
function Invoke-ShadowCertificationCapture {
    param([Parameter(Mandatory=$true)][string]$SshExecutable,[Parameter(Mandatory=$true)][string[]]$SshArguments)
    $watch=[Diagnostics.Stopwatch]::StartNew()
    & $SshExecutable @SshArguments
    $remoteProcessExitCode=$LASTEXITCODE
    $watch.Stop()
    $result=[pscustomobject]@{remote_process_exit_code=$remoteProcessExitCode;elapsed_ms=[int64]$watch.ElapsedMilliseconds;wrapper_timeout_seconds=120}
    if($remoteProcessExitCode -ne 0){throw ("REMOTE_CERTIFICATION_FAILED exit="+$remoteProcessExitCode+" elapsed_ms="+$result.elapsed_ms)}
    return $result
}
$Allowed='/root/system2-shadow-harness'
if ([string]::IsNullOrWhiteSpace($Target) -or -not [System.IO.Path]::IsPathRooted($Target) -or $Target -match '\.\.' -or $Target -eq '/' -or $Target -eq '/root' -or $Target -eq '/root/system2-core' -or $Target -like '/root/system2-core/*' -or $Target -ne $Allowed) { throw 'DEPLOY_BLOCK_UNSAFE_PATH' }
if (-not $Execute) { Write-Output 'DRY_RUN: no remote operation performed'; exit 0 }
throw 'EXECUTION_REQUIRES_SEPARATE_OWNER_REVIEW'
