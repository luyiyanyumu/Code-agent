param([string]$Root, [string]$OutputDirectory, [string]$CacheDirectory, [string]$RubyBuildTools)
$ErrorActionPreference = 'Stop'
if (-not $Root) { $Root = Split-Path -Parent $PSScriptRoot }
$workspace = (Resolve-Path -LiteralPath $Root).ProviderPath
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $workspace 'dist\installers' }
if (-not $CacheDirectory) { $CacheDirectory = Join-Path $workspace '.tmp\installer-downloads' }
# SDK members have long upstream names; keep the build prefix short as well.
$payload = Join-Path $workspace ('.tmp\wp-' + [Guid]::NewGuid().ToString('N').Substring(0, 16))
$payloadArguments = @('-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',(Join-Path $workspace 'scripts\build-windows-payload.ps1'),'-Root',$workspace,'-OutputDirectory',$payload,'-CacheDirectory',$CacheDirectory)
if ($RubyBuildTools) { $payloadArguments += @('-RubyBuildTools',[IO.Path]::GetFullPath($RubyBuildTools)) }
& powershell.exe @payloadArguments
if ($LASTEXITCODE -ne 0) { throw 'Application runtime payload build failed.' }
& powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $workspace 'scripts\build-windows-installer.ps1') -Root $workspace -PayloadDirectory $payload -OutputDirectory $OutputDirectory
if ($LASTEXITCODE -ne 0) { throw 'Windows installer build failed.' }
