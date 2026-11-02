param([string]$Root, [string]$OutputDirectory)
$ErrorActionPreference = 'Stop'
if (-not $Root) { $Root = Split-Path -Parent $PSScriptRoot }
$workspace = (Resolve-Path -LiteralPath $Root).ProviderPath
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $workspace '.tmp\installer-ui-verification' }
[void][System.IO.Directory]::CreateDirectory($OutputDirectory)
$csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$exe = Join-Path $OutputDirectory 'InstallerUiVerification.exe'
$arguments = @('/nologo', '/target:winexe', '/platform:x64', '/utf8output', '/main:CodeAgent.Installer.InstallerUiVerification',
    ('/out:' + $exe), ('/win32manifest:' + (Join-Path $workspace 'desktop\installer\app.manifest')),
    '/reference:System.dll', '/reference:System.Core.dll', '/reference:System.Drawing.dll', '/reference:System.Windows.Forms.dll',
    '/reference:System.Web.Extensions.dll', '/reference:System.IO.Compression.dll', '/reference:System.IO.Compression.FileSystem.dll',
    (Join-Path $workspace 'desktop\installer\Program.cs'), (Join-Path $workspace 'desktop\installer\Uninstall.cs'),
    (Join-Path $workspace 'desktop\installer\UiVerification.cs'))
& $csc @arguments
if ($LASTEXITCODE -ne 0) { throw 'Installer UI fixture compilation failed.' }
$fixture = Start-Process -FilePath $exe -ArgumentList ('"' + $OutputDirectory + '"') -WorkingDirectory $workspace -WindowStyle Hidden -PassThru
if (-not $fixture.WaitForExit(60000)) { throw 'Installer UI fixture did not finish; no process was forced closed.' }
if ($fixture.ExitCode -ne 0) { throw ('Installer UI fixture failed: ' + $fixture.ExitCode) }
$report = Get-Content -LiteralPath (Join-Path $OutputDirectory 'layout-report.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$report | ForEach-Object { [PSCustomObject]@{ State = $_.state; Dpi = $_.dpi; Failures = ($_.failures -join '; '); Screenshot = $_.screenshot } }
