param(
    [string]$CacheDirectory = (Join-Path (Split-Path -Parent $PSScriptRoot) '.tmp\installer-downloads'),
    [bool]$DownloadMissing = $true,
    [bool]$IncludeGit = $true,
    [bool]$IncludeJava = $true,
    [switch]$OnlyJava
)
$ErrorActionPreference = 'Stop'
if ($OnlyJava) {
    & (Join-Path $PSScriptRoot 'prepare-windows-java.ps1') -CacheDirectory $CacheDirectory -DownloadMissing $DownloadMissing
    return
}
$cache = [System.IO.Path]::GetFullPath($CacheDirectory)
[void][System.IO.Directory]::CreateDirectory($cache)
Add-Type -AssemblyName System.Net.Http
Add-Type -AssemblyName System.IO.Compression.FileSystem
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$inputs = @(
    @{ name='node-v24.14.0-win-x64.zip'; sha256='313fa40c0d7b18575821de8cb17483031fe07d95de5994f6f435f3b345f85c66'; urls=@('https://nodejs.org/dist/v24.14.0/node-v24.14.0-win-x64.zip') },
    @{ name='python-3.13.7-embeddable-amd64.zip'; sha256='e201b2da753a88c1af29d87f9f48af4d64a0fc8522a204ae672bd2c382496701'; urls=@('https://www.python.org/ftp/python/3.13.7/python-3.13.7-embeddable-amd64.zip') },
    @{ name='Microsoft.WebView2.FixedVersionRuntime.156.0.4314.50.x64.cab'; sha256='fcf0cd55f7c6e8efced05114372de8b8ffc9032c0296d16e060d260637dfe5f2'; urls=@('https://msedge.sf.dl.delivery.mp.microsoft.com/filestreamingservice/files/bd9305d7-a7d7-44fe-b8bf-aefad5d6cdb5/Microsoft.WebView2.FixedVersionRuntime.156.0.4314.50.x64.cab') }
)
if ($IncludeGit) {
    # The mirror is a transport fallback only. Accept exactly the official
    # Git-for-Windows release hash; no mirror-provided version or hash is trusted.
    $inputs += @{ name='MinGit-2.56.0.4-64-bit.zip'; sha256='e3bf78351fe777fba47ddee5950082f8a98bb0f02604fdc7ba3ee2cb68ffa877'; urls=@('https://github.com/git-for-windows/git/releases/download/v2.56.0.windows.4/MinGit-2.56.0.4-64-bit.zip','https://mirrors.nju.edu.cn/github-release/git-for-windows/git/LatestRelease/MinGit-2.56.0.4-64-bit.zip') }
}

function Get-VerifiedArchive {
    param($InputRecord)
    $destination = [System.IO.Path]::GetFullPath((Join-Path $cache $InputRecord.name))
    if (-not $destination.StartsWith($cache + [System.IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid runtime cache path.' }
    if ((Test-Path -LiteralPath $destination -PathType Leaf) -and (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash.ToLowerInvariant() -eq $InputRecord.sha256) {
        return [pscustomobject]@{ name=$InputRecord.name; path=$destination; sha256=$InputRecord.sha256; cached=$true; url=$InputRecord.urls[0]; bytes=(Get-Item -LiteralPath $destination).Length }
    }
    if (-not $DownloadMissing) { throw ('Missing or invalid pinned runtime: ' + $InputRecord.name) }
    foreach ($url in $InputRecord.urls) {
        $temporary = $destination + '.' + [Guid]::NewGuid().ToString('N') + '.partial'
        $handler = New-Object System.Net.Http.HttpClientHandler
        $handler.AllowAutoRedirect = $true
        $client = New-Object System.Net.Http.HttpClient($handler)
        $client.Timeout = [TimeSpan]::FromSeconds(45)
        $cancel = New-Object System.Threading.CancellationTokenSource
        $cancel.CancelAfter([TimeSpan]::FromMinutes(15))
        $response = $null; $stream = $null; $file = $null
        try {
            Write-Host ('Downloading pinned runtime: ' + $InputRecord.name)
            $response = $client.GetAsync($url, [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead, $cancel.Token).GetAwaiter().GetResult()
            $response.EnsureSuccessStatusCode() | Out-Null
            $stream = $response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
            $file = [System.IO.File]::Create($temporary)
            $buffer = New-Object byte[] (1024 * 1024)
            $bytes = 0L; $lastReport = [DateTime]::UtcNow
            while ($true) {
                $read = $stream.ReadAsync($buffer, 0, $buffer.Length, $cancel.Token).GetAwaiter().GetResult()
                if ($read -eq 0) { break }
                $file.Write($buffer, 0, $read); $bytes += $read
                if (([DateTime]::UtcNow - $lastReport).TotalSeconds -ge 8) {
                    Write-Host ($InputRecord.name + ': ' + $bytes + ' bytes received')
                    $lastReport = [DateTime]::UtcNow
                }
            }
            $file.Dispose(); $file = $null
            if ((Get-FileHash -LiteralPath $temporary -Algorithm SHA256).Hash.ToLowerInvariant() -ne $InputRecord.sha256) { throw 'Pinned runtime SHA256 mismatch.' }
            Copy-Item -LiteralPath $temporary -Destination $destination -Force
            Remove-Item -LiteralPath $temporary -Force
            return [pscustomobject]@{ name=$InputRecord.name; path=$destination; sha256=$InputRecord.sha256; cached=$false; url=$InputRecord.urls[0]; transportUrl=$url; bytes=$bytes }
        } catch {
            # Retain incomplete downloads for diagnosis. They never become inputs.
            Write-Warning ($InputRecord.name + ': transport failed; trying the next pinned source if available.')
        } finally {
            if ($file) { $file.Dispose() }
            if ($stream) { $stream.Dispose() }
            if ($response) { $response.Dispose() }
            $cancel.Dispose(); $client.Dispose(); $handler.Dispose()
        }
    }
    throw ('Could not download and verify runtime: ' + $InputRecord.name)
}

$records = @()
foreach ($inputRecord in $inputs) { $records += Get-VerifiedArchive $inputRecord }
$webViewBase = Join-Path $cache 'webview2-fixed'
$webView = Join-Path $webViewBase 'Microsoft.WebView2.FixedVersionRuntime.156.0.4314.50.x64'
if (-not (Test-Path -LiteralPath (Join-Path $webView 'msedgewebview2.exe'))) {
    [void][System.IO.Directory]::CreateDirectory($webViewBase)
    & (Join-Path $env:SystemRoot 'System32\expand.exe') '-F:*' (Join-Path $cache 'Microsoft.WebView2.FixedVersionRuntime.156.0.4314.50.x64.cab') $webViewBase *> (Join-Path $cache 'webview2-expand.log')
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath (Join-Path $webView 'msedgewebview2.exe'))) { throw 'Fixed WebView2 extraction failed.' }
}
if ($IncludeGit -and -not (Test-Path -LiteralPath (Join-Path $cache 'mingit\cmd\git.exe'))) {
    $gitDirectory = Join-Path $cache ('mingit-extract-' + [Guid]::NewGuid().ToString('N'))
    [System.IO.Compression.ZipFile]::ExtractToDirectory((Join-Path $cache 'MinGit-2.56.0.4-64-bit.zip'), $gitDirectory)
    $gitTarget = Join-Path $cache 'mingit'
    [void][System.IO.Directory]::CreateDirectory($gitTarget)
    foreach ($entry in Get-ChildItem -LiteralPath $gitDirectory -Force) { Copy-Item -LiteralPath $entry.FullName -Destination $gitTarget -Recurse -Force }
}
$signature = Get-AuthenticodeSignature -LiteralPath (Join-Path $webView 'msedgewebview2.exe')
if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'Microsoft Corporation') { throw 'Fixed WebView2 publisher signature is invalid.' }
$records | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $cache 'pinned-runtime-inputs.json') -Encoding UTF8
if ($IncludeJava) { & (Join-Path $PSScriptRoot 'prepare-windows-java.ps1') -CacheDirectory $cache -DownloadMissing $DownloadMissing }
Write-Output ('Pinned runtimes ready: ' + $cache)
