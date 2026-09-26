[CmdletBinding()]
param([switch]$Execute,[string]$Target='/root/system2-shadow-harness')
$ErrorActionPreference='Stop'
$Allowed='/root/system2-shadow-harness'
if ([string]::IsNullOrWhiteSpace($Target) -or -not [System.IO.Path]::IsPathRooted($Target) -or $Target -match '\.\.' -or $Target -eq '/' -or $Target -eq '/root' -or $Target -eq '/root/system2-core' -or $Target -like '/root/system2-core/*' -or $Target -ne $Allowed) { throw 'DEPLOY_BLOCK_UNSAFE_PATH' }
if (-not $Execute) { Write-Output 'DRY_RUN: no remote operation performed'; exit 0 }
throw 'EXECUTION_REQUIRES_SEPARATE_OWNER_REVIEW'
