param(
    [string]$Root = (Split-Path -Parent $PSScriptRoot),
    [string]$OutputPath,
    [string]$SdkDirectory
)
$ErrorActionPreference = 'Stop'
$workspace = (Resolve-Path -LiteralPath $Root).ProviderPath
if (-not $OutputPath) { $OutputPath = Join-Path $workspace 'Code-agent.exe' }
$OutputPath = [System.IO.Path]::GetFullPath($OutputPath)
$sdkVersion = '1.0.4258.31'
if (-not $SdkDirectory) { $SdkDirectory = Join-Path $env:USERPROFILE ('.nuget\packages\microsoft.web.webview2\' + $sdkVersion) }
if (-not (Test-Path -LiteralPath (Join-Path $SdkDirectory 'lib\net462\Microsoft.Web.WebView2.WinForms.dll'))) {
    $SdkDirectory = Join-Path $workspace ('.tmp\desktop-sdk\' + $sdkVersion)
    [void][System.IO.Directory]::CreateDirectory($SdkDirectory)
    $archive = Join-Path $SdkDirectory 'sdk.zip'
    Invoke-WebRequest -Uri ('https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/' + $sdkVersion + '/microsoft.web.webview2.' + $sdkVersion + '.nupkg') -OutFile $archive -UseBasicParsing
    Expand-Archive -LiteralPath $archive -DestinationPath $SdkDirectory -Force
}
$csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $csc)) { throw '.NET Framework 4.8 compiler was not found.' }
$scratch = Join-Path $workspace ('.tmp\desktop-build\' + [Guid]::NewGuid().ToString('N'))
[void][System.IO.Directory]::CreateDirectory($scratch)
$core = Join-Path $SdkDirectory 'lib\net462\Microsoft.Web.WebView2.Core.dll'
$forms = Join-Path $SdkDirectory 'lib\net462\Microsoft.Web.WebView2.WinForms.dll'
$loader = Join-Path $SdkDirectory 'runtimes\win-x64\native\WebView2Loader.dll'
foreach ($file in @($core, $forms, $loader)) { if (-not (Test-Path -LiteralPath $file)) { throw ('Missing SDK input: ' + $file) } }

# Convert the existing product mark to a multi-resolution Windows icon.
Add-Type -AssemblyName System.Drawing
$source = [System.Drawing.Image]::FromFile((Join-Path $workspace 'web\public\brand\code-agent-mark.png'))
$images = @()
try {
    foreach ($size in @(16, 24, 32, 48, 64, 128, 256)) {
        $bitmap = New-Object System.Drawing.Bitmap($size, $size)
        $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
        try {
            $graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
            $graphics.DrawImage($source, 0, 0, $size, $size)
            $memory = New-Object System.IO.MemoryStream
            try { $bitmap.Save($memory, [System.Drawing.Imaging.ImageFormat]::Png); $images += ,@($size, $memory.ToArray()) }
            finally { $memory.Dispose() }
        } finally { $graphics.Dispose(); $bitmap.Dispose() }
    }
} finally { $source.Dispose() }
$iconPath = Join-Path $scratch 'launcher.ico'
$iconStream = [System.IO.File]::Create($iconPath)
$writer = New-Object System.IO.BinaryWriter($iconStream)
try {
    $writer.Write([UInt16]0); $writer.Write([UInt16]1); $writer.Write([UInt16]$images.Count)
    $offset = 6 + 16 * $images.Count
    foreach ($image in $images) {
        $width = if ($image[0] -eq 256) { 0 } else { $image[0] }
        $writer.Write([byte]$width); $writer.Write([byte]$width); $writer.Write([byte]0); $writer.Write([byte]0)
        $writer.Write([UInt16]1); $writer.Write([UInt16]32); $writer.Write([UInt32]$image[1].Length); $writer.Write([UInt32]$offset)
        $offset += $image[1].Length
    }
    foreach ($image in $images) { $writer.Write([byte[]]$image[1]) }
} finally { $writer.Dispose(); $iconStream.Dispose() }

$temporaryExe = Join-Path $scratch 'Code-agent Launcher.exe'
$version = (Get-Content -LiteralPath (Join-Path $workspace 'package.json') -Raw | ConvertFrom-Json).version
if ($version -notmatch '^(\d+\.\d+\.\d+)(?:-[A-Za-z0-9.-]+)?$') { throw 'Invalid package version.' }
$assemblyVersion = $Matches[1] + '.0'
$assemblyInfo = Join-Path $scratch 'AssemblyInfo.cs'
$assemblySource = @"
using System.Reflection;
[assembly: AssemblyTitle("Code-agent Launcher")]
[assembly: AssemblyProduct("Code-agent")]
[assembly: AssemblyDescription("Code-agent desktop launcher")]
[assembly: AssemblyVersion("$assemblyVersion")]
[assembly: AssemblyFileVersion("$assemblyVersion")]
[assembly: AssemblyInformationalVersion("$version")]
"@
[System.IO.File]::WriteAllText($assemblyInfo, $assemblySource, (New-Object System.Text.UTF8Encoding($false)))
$arguments = @('/nologo', '/target:winexe', '/platform:x64', '/optimize+', '/utf8output',
    ('/out:' + $temporaryExe), ('/win32icon:' + $iconPath),
    '/reference:System.dll', '/reference:System.Core.dll', '/reference:System.Drawing.dll',
    '/reference:System.Windows.Forms.dll', '/reference:System.Web.Extensions.dll',
    ('/reference:' + $core), ('/reference:' + $forms),
    ('/resource:' + $core + ',CodeAgent.WebView2.Core.dll'),
    ('/resource:' + $forms + ',CodeAgent.WebView2.WinForms.dll'),
    ('/resource:' + $loader + ',CodeAgent.WebView2Loader.dll'),
    ('/resource:' + (Join-Path $SdkDirectory 'LICENSE.txt') + ',CodeAgent.WebView2.LICENSE.txt'),
    ('/resource:' + (Join-Path $SdkDirectory 'NOTICE.txt') + ',CodeAgent.WebView2.NOTICE.txt'))
$manifest = Join-Path $workspace 'desktop\launcher\app.manifest'
if (Test-Path -LiteralPath $manifest) { $arguments += '/win32manifest:' + $manifest }
$arguments += Join-Path $workspace 'desktop\launcher\Program.cs'
$arguments += $assemblyInfo
& $csc @arguments
if ($LASTEXITCODE -ne 0) { throw 'Native launcher compilation failed.' }
[void][System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($OutputPath))
Copy-Item -LiteralPath $temporaryExe -Destination $OutputPath -Force
$metadata = [ordered]@{
    executable = $OutputPath; size = (Get-Item -LiteralPath $OutputPath).Length
    sha256 = (Get-FileHash -LiteralPath $OutputPath -Algorithm SHA256).Hash.ToLowerInvariant()
    webView2Sdk = $sdkVersion; architecture = 'win-x64'; subsystem = 'Windows GUI'
}
$metadata | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $scratch 'build.json') -Encoding UTF8
$metadata | ConvertTo-Json -Compress
