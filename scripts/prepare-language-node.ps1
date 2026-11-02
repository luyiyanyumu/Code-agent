param(
    [string]$CacheDirectory = (Join-Path (Split-Path -Parent $PSScriptRoot) '.tmp\language-node-python-build'),
    [Parameter(Mandatory=$true)][string]$NodeExecutable,
    [string]$PythonExecutable = (Join-Path (Split-Path -Parent $PSScriptRoot) '.venv\Scripts\python.exe'),
    [string]$PayloadDirectory
)
$ErrorActionPreference = 'Stop'
$arguments = @('-B','-X','utf8',(Join-Path $PSScriptRoot 'prepare_language_node.py'),
    '--cache',[IO.Path]::GetFullPath($CacheDirectory),'--node',[IO.Path]::GetFullPath($NodeExecutable),
    '--python',[IO.Path]::GetFullPath($PythonExecutable))
if($PayloadDirectory){$arguments += @('--payload',[IO.Path]::GetFullPath($PayloadDirectory))}
& $PythonExecutable @arguments
if($LASTEXITCODE -ne 0){throw 'Pinned Node/Python language service preparation failed.'}
