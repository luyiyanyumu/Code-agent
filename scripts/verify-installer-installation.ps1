param(
    [Parameter(Mandatory=$true)][string]$Installer,
    [Parameter(Mandatory=$true)][string]$Target,
    [Parameter(Mandatory=$true)][string]$OutputDirectory
)
$ErrorActionPreference = 'Stop'
$workspace = Split-Path -Parent $PSScriptRoot
$installerPath = (Resolve-Path -LiteralPath $Installer).ProviderPath
if (-not [IO.Path]::IsPathRooted($Target)) { throw 'Target must be an explicit absolute installation directory.' }
$targetPath = [IO.Path]::GetFullPath($Target)
$outputPath = [IO.Path]::GetFullPath($OutputDirectory)
if (Test-Path -LiteralPath (Join-Path $outputPath 'install-result.json')) { throw 'Use a fresh verification output directory.' }
[void][IO.Directory]::CreateDirectory($outputPath)
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$fixture = Join-Path $outputPath 'InstallerInstallationVerification.exe'
& $compiler /nologo /target:winexe /platform:x64 /utf8output (('/out:' + $fixture)) (('/win32manifest:' + (Join-Path $workspace 'desktop\installer\app.manifest'))) /reference:System.dll /reference:System.Core.dll /reference:System.Drawing.dll /reference:System.Windows.Forms.dll /reference:System.Web.Extensions.dll (Join-Path $workspace 'desktop\installer\InstallationVerification.cs')
if ($LASTEXITCODE -ne 0) { throw 'Real installer fixture compilation failed.' }
$fixtureArgs = '"' + $installerPath + '" "' + $targetPath + '" "' + $outputPath + '"'
$process = Start-Process -FilePath $fixture -ArgumentList $fixtureArgs -WorkingDirectory $workspace -WindowStyle Hidden -PassThru
while (-not $process.WaitForExit(1000)) { }
$reportPath = Join-Path $outputPath 'report.json'
Get-Content -LiteralPath $reportPath -Raw -Encoding UTF8
if ($process.ExitCode -ne 0) { throw ('Real GUI installation verification failed: ' + $process.ExitCode) }
