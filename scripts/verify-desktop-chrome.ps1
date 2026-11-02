param([string]$Root, [string]$OutputDirectory)
$ErrorActionPreference = 'Stop'
if (-not $Root) { $Root = Split-Path -Parent $PSScriptRoot }
$workspace = (Resolve-Path -LiteralPath $Root).ProviderPath
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $workspace ('.tmp\native-chrome-verification\' + [Guid]::NewGuid().ToString('N')) }
[void][IO.Directory]::CreateDirectory($OutputDirectory)
$sdk = Join-Path $env:USERPROFILE '.nuget\packages\microsoft.web.webview2\1.0.4258.31\lib\net462'
if (-not (Test-Path -LiteralPath $sdk)) { $sdk = Join-Path $workspace '.tmp\desktop-sdk\1.0.4258.31\lib\net462' }
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$exe = Join-Path $OutputDirectory 'WindowChromeVerification.exe'
$compile = @('/nologo','/target:winexe','/platform:x64','/utf8output','/main:CodeAgent.DesktopLauncher.WindowChromeVerification',('/out:'+$exe),('/win32manifest:'+(Join-Path $workspace 'desktop\launcher\app.manifest')),
    '/reference:System.dll','/reference:System.Core.dll','/reference:System.Drawing.dll','/reference:System.Windows.Forms.dll','/reference:System.Web.Extensions.dll',
    ('/reference:'+(Join-Path $sdk 'Microsoft.Web.WebView2.Core.dll')),('/reference:'+(Join-Path $sdk 'Microsoft.Web.WebView2.WinForms.dll')),
    (Join-Path $workspace 'desktop\launcher\Program.cs'),(Join-Path $workspace 'desktop\launcher\WindowChromeVerification.cs'))
& $compiler @compile
if ($LASTEXITCODE -ne 0) { throw 'Native chrome fixture compilation failed.' }
$fixture = Start-Process -FilePath $exe -ArgumentList ('"' + $OutputDirectory + '" "' + $sdk + '"') -WorkingDirectory $workspace -WindowStyle Hidden -PassThru
if (-not $fixture.WaitForExit(30000)) { throw 'Native chrome fixture exceeded its bounded wait.' }
Get-Content -LiteralPath (Join-Path $OutputDirectory 'report.json') -Raw -Encoding UTF8
if ($fixture.ExitCode -ne 0) { throw ('Native chrome fixture failed: ' + $fixture.ExitCode) }
