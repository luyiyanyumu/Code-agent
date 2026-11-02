param([string]$Root, [string]$OutputDirectory, [string]$BaselineSource)
$ErrorActionPreference = 'Stop'
if (-not $Root) { $Root = Split-Path -Parent $PSScriptRoot }
$workspace = (Resolve-Path -LiteralPath $Root).ProviderPath
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $workspace '.tmp\installer-async-verification' }
[void][System.IO.Directory]::CreateDirectory($OutputDirectory)
$program = if ($BaselineSource) { (Resolve-Path -LiteralPath $BaselineSource).ProviderPath } else { Join-Path $workspace 'desktop\installer\Program.cs' }
$suffix = if ($BaselineSource) { '-baseline' } else { '' }
$exe = Join-Path $OutputDirectory ('InstallerAsyncVerification' + $suffix + '.exe')
$reportPath = Join-Path $OutputDirectory ('async-report' + $suffix + '.json')
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$arguments = @('/nologo', '/target:winexe', '/platform:x64', '/utf8output', '/main:CodeAgent.Installer.InstallerAsyncVerification',
    ('/out:' + $exe), ('/win32manifest:' + (Join-Path $workspace 'desktop\installer\app.manifest')),
    '/reference:System.dll', '/reference:System.Core.dll', '/reference:System.Drawing.dll', '/reference:System.Windows.Forms.dll',
    '/reference:System.Web.Extensions.dll', '/reference:System.IO.Compression.dll', '/reference:System.IO.Compression.FileSystem.dll',
    $program, (Join-Path $workspace 'desktop\installer\Uninstall.cs'), (Join-Path $workspace 'desktop\installer\AsyncVerification.cs'))
& $compiler @arguments
if ($LASTEXITCODE -ne 0) { throw 'Installer async fixture compilation failed.' }
$fixtureArgs = '"' + $reportPath + '"' + $(if ($BaselineSource) { ' baseline' } else { '' })
$fixture = Start-Process -FilePath $exe -ArgumentList $fixtureArgs -WorkingDirectory $workspace -WindowStyle Hidden -PassThru
if (-not $fixture.WaitForExit(50000)) { throw 'Installer async fixture exceeded its self-watchdog; no process was forced closed.' }
$report = Get-Content -LiteralPath $reportPath -Raw -Encoding UTF8 | ConvertFrom-Json
$report | Select-Object scenario,passed,timedOut,elapsedMilliseconds,workerMilliseconds,progressCalls,statusChanges,percentageChanges,progressInvalidations,progressPaints,heartbeats,maximumHeartbeatGapMilliseconds,errorRecovered,retried,lateProgressIgnored,error | Format-List
if ($fixture.ExitCode -ne 0 -and -not $BaselineSource) { throw ('Installer async fixture failed: ' + $fixture.ExitCode) }
