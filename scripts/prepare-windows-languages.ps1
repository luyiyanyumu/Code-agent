param(
    [Parameter(Mandatory=$true)][string]$PayloadDirectory,
    [string]$CacheDirectory = (Join-Path (Split-Path -Parent $PSScriptRoot) '.tmp\language-build'),
    [string]$PythonExecutable = (Join-Path (Split-Path -Parent $PSScriptRoot) '.venv\Scripts\python.exe'),
    [string]$NodeExecutable,
    [string]$RubyBuildTools,
    [string[]]$GroupRoot = @(),
    [switch]$MergeExisting,
    [switch]$Resume,
    [ValidateSet('compare','error','keep')][string]$ExistingPolicy = 'compare',
    [bool]$DownloadMissing = $true
)
$ErrorActionPreference = 'Stop'
$arguments = @('-B','-X','utf8',(Join-Path $PSScriptRoot 'prepare_windows_languages.py'),
    '--payload',[IO.Path]::GetFullPath($PayloadDirectory),'--cache',[IO.Path]::GetFullPath($CacheDirectory),
    '--python',[IO.Path]::GetFullPath($PythonExecutable),'--existing-policy',$ExistingPolicy)
if ($NodeExecutable) { $arguments += @('--node',[IO.Path]::GetFullPath($NodeExecutable)) }
if ($RubyBuildTools) { $arguments += @('--ruby-build-tools',[IO.Path]::GetFullPath($RubyBuildTools)) }
foreach ($group in $GroupRoot) { $arguments += @('--group-root',$group) }
if ($MergeExisting) { $arguments += '--merge-existing' }
if ($Resume) { $arguments += '--resume' }
if (-not $DownloadMissing) { $arguments += '--no-download' }
& $PythonExecutable @arguments
if ($LASTEXITCODE -ne 0) { throw 'Bundled Windows language service preparation failed.' }
