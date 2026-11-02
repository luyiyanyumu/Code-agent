param(
    [string]$CacheDirectory = (Join-Path (Split-Path -Parent $PSScriptRoot) '.tmp\installer-downloads'),
    [string]$PayloadDirectory,
    [string]$PythonExecutable = (Join-Path (Split-Path -Parent $PSScriptRoot) '.venv\Scripts\python.exe'),
    [bool]$DownloadMissing = $true
)
$ErrorActionPreference = 'Stop'
$cache = [IO.Path]::GetFullPath($CacheDirectory)
[void][IO.Directory]::CreateDirectory($cache)
Add-Type -AssemblyName System.Net.Http
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
$inputs = @(
    @{name='OpenJDK21U-jdk_x64_windows_hotspot_21.0.12.1_1.zip';sha256='f9d6e191ab098c0d416e7d588a24420a8621cd2f4720dab2459b8b7b2d2d8b4e';url='https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.12.1%2B1/OpenJDK21U-jdk_x64_windows_hotspot_21.0.12.1_1.zip'},
    @{name='jdt-language-server-1.61.0-202609031315.tar.gz';sha256='338e7e73d61836651ba2453919a0d34fa763eb4e7c03342092309bffb8934c64';url='https://download.eclipse.org/jdtls/milestones/1.61.0/jdt-language-server-1.61.0-202609031315.tar.gz'},
    @{name='lombok-1.18.48.jar';sha256='85477a4655ebb2c074a9099cfb749be454449fee564d4282610df1b85f7c508b';url='https://repo.maven.apache.org/maven2/org/projectlombok/lombok/1.18.48/lombok-1.18.48.jar'}
)
foreach ($inputRecord in $inputs) {
    $destination = Join-Path $cache $inputRecord.name
    if ((Test-Path -LiteralPath $destination -PathType Leaf) -and (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash.ToLowerInvariant() -eq $inputRecord.sha256) { continue }
    if (-not $DownloadMissing) { throw ('Missing or invalid pinned Java input: ' + $inputRecord.name) }
    $temporary = $destination + '.' + [Guid]::NewGuid().ToString('N') + '.partial'
    $client = New-Object Net.Http.HttpClient
    $client.Timeout = [TimeSpan]::FromSeconds(45)
    $cancel = New-Object Threading.CancellationTokenSource
    $cancel.CancelAfter([TimeSpan]::FromMinutes(15))
    $response = $null; $stream = $null; $file = $null
    try {
        Write-Host ('Downloading pinned Java input: ' + $inputRecord.name)
        $response = $client.GetAsync($inputRecord.url, [Net.Http.HttpCompletionOption]::ResponseHeadersRead, $cancel.Token).GetAwaiter().GetResult()
        $response.EnsureSuccessStatusCode() | Out-Null
        $stream = $response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
        $file = [IO.File]::Create($temporary)
        $stream.CopyToAsync($file, (1024 * 1024), $cancel.Token).GetAwaiter().GetResult()
        $file.Dispose(); $file = $null
        if ((Get-FileHash -LiteralPath $temporary -Algorithm SHA256).Hash.ToLowerInvariant() -ne $inputRecord.sha256) { throw ('Pinned Java input SHA256 mismatch: ' + $inputRecord.name) }
        Copy-Item -LiteralPath $temporary -Destination $destination -Force
        Remove-Item -LiteralPath $temporary -Force
    } finally {
        if ($file) { $file.Dispose() }; if ($stream) { $stream.Dispose() }; if ($response) { $response.Dispose() }; $cancel.Dispose(); $client.Dispose()
    }
}
if (-not (Test-Path -LiteralPath $PythonExecutable -PathType Leaf)) { throw 'Java preparation needs the build environment Python executable.' }
$arguments = @('-B','-X','utf8',(Join-Path $PSScriptRoot 'prepare_windows_java.py'),'--cache',$cache)
if ($PayloadDirectory) { $arguments += @('--payload',[IO.Path]::GetFullPath($PayloadDirectory)) }
& $PythonExecutable @arguments
if ($LASTEXITCODE -ne 0) { throw 'Pinned Java runtime preparation failed.' }
