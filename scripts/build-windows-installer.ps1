param(
    [Parameter(Mandatory = $true)][string]$PayloadDirectory,
    [string]$OutputDirectory,
    [string]$Root,
    [string]$ReusePayloadArchive,
    [switch]$ForceAppendedPayload
)
$ErrorActionPreference = 'Stop'

# Archive reuse validation helpers. These have no installer or registry effects.
function Get-PayloadFileSha256 {
    param([string]$Path)
    $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read)
    $hasher = [System.Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($hasher.ComputeHash($stream)).Replace('-', '').ToLowerInvariant() }
    finally { $hasher.Dispose(); $stream.Dispose() }
}

function Add-AppendedPayload {
    param([string]$ExecutablePath, [string]$ArchivePath, [string]$ExpectedSha256)
    $destination = [System.IO.File]::Open($ExecutablePath, [System.IO.FileMode]::Open, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
    $source = $null; $hasher = $null; $copy = $null
    try {
        $source = [System.IO.File]::Open($ArchivePath, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read)
        [long]$offset = $destination.Length
        [long]$length = $source.Length
        if ($offset -lt 64 -or $length -lt 22 -or $offset -gt ([long]::MaxValue - $length - 64)) { throw 'Invalid appended payload size.' }
        $destination.Position = $offset
        $hasher = [System.Security.Cryptography.SHA256]::Create()
        $copy = [System.Security.Cryptography.CryptoStream]::new($destination, $hasher, [System.Security.Cryptography.CryptoStreamMode]::Write, $true)
        $source.CopyTo($copy, 1MB)
        $copy.FlushFinalBlock()
        $digest = $hasher.Hash
        $sha256 = [BitConverter]::ToString($digest).Replace('-', '').ToLowerInvariant()
        if ($destination.Position -ne $offset + $length -or ($ExpectedSha256 -and $ExpectedSha256 -ne $sha256)) { throw 'Appended payload differs from the verified archive.' }
        $footer = New-Object byte[] 64
        [System.Text.Encoding]::ASCII.GetBytes("CodeAgentZipV1`0`0").CopyTo($footer, 0)
        [BitConverter]::GetBytes([UInt64]$offset).CopyTo($footer, 16)
        [BitConverter]::GetBytes([UInt64]$length).CopyTo($footer, 24)
        $digest.CopyTo($footer, 32)
        $destination.Write($footer, 0, $footer.Length)
        $destination.Flush($true)
        return [pscustomobject]@{ offset = $offset; length = $length; sha256 = $sha256 }
    } finally {
        if ($copy) { $copy.Dispose() }
        if ($hasher) { $hasher.Dispose() }
        if ($source) { $source.Dispose() }
        $destination.Dispose()
    }
}

function Test-UnsafePayloadName {
    param([string]$PathName)
    $name = $PathName.Replace('\', '/')
    $parts = @($name.Split('/'))
    if (-not $name -or $PathName -ne $name -or $name.StartsWith('/') -or $name.Contains(':') -or
        $parts -contains '' -or $parts -contains '.' -or $parts -contains '..') { return $true }
    $leaf = $parts[-1]
    return $name -match '(^|/)(\.git|\.venv|transcripts|tool_results)(/|$)' -or $name -match '^logs(/|$)' -or
        ($name -match '(^|/)node_modules(/|$)' -and $name -notmatch '^runtimes/(node/node_modules|language-servers/node/node_modules|language-servers/sql/licenses)(/|$)') -or
        ($leaf -like '.env*' -and $leaf -ne '.env.example') -or $leaf -match '\.(db|sqlite|sqlite3|log)(-wal|-shm)?$' -or
        $leaf -eq 'installation-manifest.json' -or $name -match '^runtimes/python/Lib/site-packages/(bin|Scripts)/'
}

function Assert-JavaPayload {
    param([string]$PayloadRoot, $Runtime)
    $declared = @('java','jdtls','lombok' | Where-Object { $null -ne $Runtime.$_ })
    if ($declared.Count -eq 0) { return @() }
    if ($declared.Count -ne 3 -or $Runtime.java -ne 'runtimes/java/bin/java.exe' -or
        $Runtime.jdtls -ne 'runtimes/jdtls' -or $Runtime.lombok -ne 'runtimes/lombok/lombok.jar') {
        throw 'Bundled Java analysis runtime layout is incomplete or invalid.'
    }
    $critical = @('runtimes/java/bin/java.exe','runtimes/java/bin/javac.exe','runtimes/java/release','runtimes/java/NOTICE',
        'runtimes/jdtls/bin/jdtls','runtimes/jdtls/bin/jdtls.py','runtimes/jdtls/config_win/config.ini','runtimes/lombok/lombok.jar')
    foreach ($name in $critical) {
        if (-not (Test-Path -LiteralPath (Join-Path $PayloadRoot $name) -PathType Leaf)) { throw ('Bundled Java analysis input is missing: ' + $name) }
    }
    if (-not (Test-Path -LiteralPath (Join-Path $PayloadRoot 'runtimes/java/legal') -PathType Container)) { throw 'Bundled Java license notices are missing.' }
    $launchers = @(Get-ChildItem -LiteralPath (Join-Path $PayloadRoot 'runtimes/jdtls/plugins') -Filter 'org.eclipse.equinox.launcher_*.jar' -File)
    if (-not $launchers.Count) { throw 'Bundled Java language service engine is missing.' }
    foreach ($launcher in $launchers) { $critical += 'runtimes/jdtls/plugins/' + $launcher.Name }
    return $critical
}

function Assert-LanguagePayload {
    param([string]$PayloadRoot, $Runtime)
    if ($null -eq $Runtime.languageTools) { return @() }
    $buildNode = (Get-Command node.exe -ErrorAction Stop).Source
    $text = & $buildNode (Join-Path $PSScriptRoot 'validate-language-payload.mjs') $PayloadRoot
    if ($LASTEXITCODE -ne 0) { throw 'Bundled language analysis service or SDK verification failed.' }
    $paths = ($text -join "`n") | ConvertFrom-Json
    return @($paths)
}

function Assert-ReusablePayloadArchive {
    param(
        [Parameter(Mandatory = $true)][string]$ArchivePath,
        [Parameter(Mandatory = $true)][string]$PayloadRoot,
        [Parameter(Mandatory = $true)][object[]]$ExpectedFiles,
        [Parameter(Mandatory = $true)][string]$ExpectedVersion
    )
    if (-not (Test-Path -LiteralPath $ArchivePath -PathType Leaf)) { throw 'Reusable payload archive was not found.' }
    $archiveItem = Get-Item -LiteralPath $ArchivePath -Force
    if ($archiveItem -isnot [System.IO.FileInfo]) { throw 'Reusable payload must be a filesystem archive.' }
    $ancestor = $archiveItem
    while ($null -ne $ancestor) {
        if (($ancestor.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Reusable payload archive cannot be a link or traverse a linked directory.' }
        $ancestor = if ($ancestor -is [System.IO.FileInfo]) { $ancestor.Directory } else { $ancestor.Parent }
    }
    $rootPath = [System.IO.Path]::GetFullPath($PayloadRoot).TrimEnd('\')
    $runtime = Get-Content -LiteralPath (Join-Path $rootPath 'desktop-runtime.json') -Raw | ConvertFrom-Json
    $packageVersion = [string](Get-Content -LiteralPath (Join-Path $rootPath 'package.json') -Raw | ConvertFrom-Json).version
    if ($runtime.schemaVersion -ne 1 -or $runtime.version -ne $ExpectedVersion -or $packageVersion -ne $ExpectedVersion -or
        $runtime.node -ne 'runtimes/node/node.exe' -or $runtime.python -ne 'runtimes/python/python.exe' -or
        $runtime.webview2 -ne 'runtimes/webview2' -or ($runtime.git -and $runtime.git -ne 'runtimes/git/cmd/git.exe')) { throw 'Reusable payload runtime version or layout does not match the staged application.' }
    $javaCritical = @(Assert-JavaPayload -PayloadRoot $rootPath -Runtime $runtime)
    $languageCritical = @(Assert-LanguagePayload -PayloadRoot $rootPath -Runtime $runtime)
    $expected = New-Object 'System.Collections.Generic.Dictionary[string,object]' ([StringComparer]::OrdinalIgnoreCase)
    foreach ($file in $ExpectedFiles) {
        $filePath = [System.IO.Path]::GetFullPath($file.FullName)
        if (-not $filePath.StartsWith($rootPath + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Expected payload file escaped the staging directory.' }
        if (($file.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Staged payload cannot contain links.' }
        $name = $filePath.Substring($rootPath.Length + 1).Replace('\', '/')
        if (Test-UnsafePayloadName $name) { throw ('Unsafe staged payload input: ' + $name) }
        if ($expected.ContainsKey($name)) { throw ('Duplicate staged payload path: ' + $name) }
        $expected.Add($name, $file)
    }
    foreach ($critical in @('desktop-runtime.json','package.json','npm/release.json','bin/code-agent-desktop-service.mjs','npm/launcher-service.mjs',
        'web/dist/index.html','code_agent/server.py','code_agent/harness/node/runner.mjs','runtimes/node/node.exe',
        'runtimes/python/python.exe','runtimes/webview2/msedgewebview2.exe','Code-agent Uninstall.exe')) {
        if (-not $expected.ContainsKey($critical)) { throw ('Critical staged payload input is missing: ' + $critical) }
    }
    if (-not $expected.ContainsKey('Code-agent.exe') -and -not $expected.ContainsKey('Code-agent Launcher.exe')) { throw 'Critical staged payload input is missing: Code-agent.exe' }
    if ($runtime.git -and -not $expected.ContainsKey([string]$runtime.git)) { throw 'Bundled Git is missing from the staged payload.' }
    foreach ($critical in $javaCritical) { if (-not $expected.ContainsKey($critical)) { throw ('Bundled Java is missing from the staged payload: ' + $critical) } }
    foreach ($critical in $languageCritical) { if (-not $expected.ContainsKey($critical)) { throw ('Bundled language input is missing from the archive: ' + $critical) } }
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $stream = [System.IO.File]::Open($archiveItem.FullName, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read)
    $zip = New-Object System.IO.Compression.ZipArchive($stream, [System.IO.Compression.ZipArchiveMode]::Read, $false, [System.Text.Encoding]::UTF8)
    $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    $contentNames = New-Object 'System.Collections.Generic.List[string]'
    $contentPaths = New-Object 'System.Collections.Generic.List[string]'
    $contentSizes = New-Object 'System.Collections.Generic.List[long]'
    $totalBytes = 0L
    try {
        foreach ($entry in $zip.Entries) {
            $name = [string]$entry.FullName
            $unixType = ($entry.ExternalAttributes -shr 16) -band 0xF000
            if ((Test-UnsafePayloadName $name) -or $unixType -eq 0xA000 -or ($entry.ExternalAttributes -band 0x400) -ne 0) { throw ('Unsafe reusable archive input: ' + $name) }
            if (-not $seen.Add($name)) { throw ('Duplicate reusable archive path: ' + $name) }
            if (-not $expected.ContainsKey($name)) { throw ('Reusable archive has an unexpected input: ' + $name) }
            $file = $expected[$name]
            if ($entry.Length -ne $file.Length) { throw ('Reusable archive length differs from staging: ' + $name) }
            $contentNames.Add($name)
            $contentPaths.Add($file.FullName)
            $contentSizes.Add([long]$entry.Length)
            $totalBytes += $entry.Length
        }
        if ($seen.Count -ne $expected.Count) { throw 'Reusable archive membership differs from staged payload.' }
        if (-not ('CodeAgent.Build.PayloadArchiveVerifier' -as [type])) {
            $sourceRoot = if ($Root) { $Root } else { Split-Path -Parent $PSScriptRoot }
            $verifierSource = Join-Path $sourceRoot 'desktop/installer/PayloadArchiveVerifier.cs'
            Add-Type -Path $verifierSource -ReferencedAssemblies System.dll,System.Core.dll,System.IO.Compression.dll
        }
        try {
            $contentCheck = [CodeAgent.Build.PayloadArchiveVerifier]::Verify($archiveItem.FullName, $contentNames.ToArray(), $contentPaths.ToArray(), $contentSizes.ToArray(), 4)
        } catch {
            if ($_.Exception.InnerException) { throw $_.Exception.InnerException.Message }
            throw
        }
    } finally { $zip.Dispose(); $stream.Dispose() }
    return [pscustomobject]@{ archive = $archiveItem.FullName; files = $seen.Count; bytes = $totalBytes;
        verificationWorkers = $contentCheck.Workers; verificationMaxConcurrentFiles = $contentCheck.MaxConcurrentFiles;
        verificationComparedBytes = $contentCheck.ComparedBytes; verificationSeconds = $contentCheck.Seconds;
        verificationPeakWorkingSetBytes = $contentCheck.PeakWorkingSetBytes;
        sha256 = Get-PayloadFileSha256 $archiveItem.FullName }
}

# Build starts here.
if (-not $Root) { $Root = Split-Path -Parent $PSScriptRoot }
$workspace = (Resolve-Path -LiteralPath $Root).ProviderPath.TrimEnd('\')
$payload = (Resolve-Path -LiteralPath $PayloadDirectory).ProviderPath.TrimEnd('\')
if ($payload -eq $workspace -or $workspace.StartsWith($payload + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Payload must be a dedicated staging directory.' }
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $workspace 'dist\installers' }
$OutputDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)
if ($OutputDirectory -eq $payload -or $OutputDirectory.StartsWith($payload + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Installer output cannot be inside its payload.' }
[void][System.IO.Directory]::CreateDirectory($OutputDirectory)
$runtimePath = Join-Path $payload 'desktop-runtime.json'
if (-not (Test-Path -LiteralPath $runtimePath -PathType Leaf)) { throw 'Payload lacks desktop-runtime.json.' }
$runtime = Get-Content -LiteralPath $runtimePath -Raw | ConvertFrom-Json
$version = [string]$runtime.version
if ($runtime.schemaVersion -ne 1 -or $version -notmatch '^(\d+\.\d+\.\d+)(?:-[A-Za-z0-9.-]+)?$') { throw 'Invalid bundled runtime manifest.' }
$assemblyVersion = $Matches[1] + '.0'
$sourceVersion = [string](Get-Content -LiteralPath (Join-Path $workspace 'package.json') -Raw | ConvertFrom-Json).version
if ($sourceVersion -ne $version) { throw 'Payload and source versions differ.' }
[void](Assert-JavaPayload -PayloadRoot $payload -Runtime $runtime)
[void](Assert-LanguagePayload -PayloadRoot $payload -Runtime $runtime)

# Do not let a source checkout, user database, model secret or log accidentally
# become part of the offline installer.
$entries = @((Get-Item -LiteralPath $payload)) + @(Get-ChildItem -LiteralPath $payload -Recurse -Force)
foreach ($entry in $entries) {
    if (($entry.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw ('Payload cannot contain links: ' + $entry.FullName) }
}
$files = @($entries | Where-Object { -not $_.PSIsContainer })
foreach ($file in $entries | Where-Object { $_.FullName -ne $payload }) {
    $relative = $file.FullName.Substring($payload.Length + 1).Replace('\', '/')
    # Generated Python command wrappers are deliberately omitted below.
    if ($relative -match '^runtimes/python/Lib/site-packages/(bin|Scripts)/') { continue }
    if (Test-UnsafePayloadName $relative) {
        throw ('Unsafe or user-specific payload input: ' + $relative)
    }
}
$csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $csc -PathType Leaf)) { throw '.NET Framework 4.8 compiler was not found.' }
$scratch = Join-Path $workspace ('.tmp\windows-installer-build\' + [Guid]::NewGuid().ToString('N'))
[void][System.IO.Directory]::CreateDirectory($scratch)
$launcher = Join-Path $payload 'Code-agent.exe'
if ($ReusePayloadArchive -and -not (Test-Path -LiteralPath $launcher -PathType Leaf)) { $launcher = Join-Path $payload 'Code-agent Launcher.exe' }
if (-not $ReusePayloadArchive) {
    & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $workspace 'scripts\build-launcher-exe.ps1') -Root $workspace -OutputPath $launcher
    if ($LASTEXITCODE -ne 0) { throw 'Bundled launcher compilation failed.' }
} elseif (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) { throw 'Reusing payload requires its existing bundled launcher.' }

Add-Type -AssemblyName System.Drawing
$icon = [System.Drawing.Icon]::ExtractAssociatedIcon($launcher)
$iconPath = Join-Path $scratch 'installer.ico'
$iconStream = [System.IO.File]::Create($iconPath)
try { $icon.Save($iconStream) } finally { $iconStream.Dispose(); $icon.Dispose() }
$assemblyPath = Join-Path $scratch 'AssemblyInfo.cs'
$assemblySource = @"
using System.Reflection;
[assembly: AssemblyProduct("Code-agent")]
[assembly: AssemblyTitle("Code-agent Setup")]
[assembly: AssemblyVersion("$assemblyVersion")]
[assembly: AssemblyFileVersion("$assemblyVersion")]
[assembly: AssemblyInformationalVersion("$version")]
"@
[System.IO.File]::WriteAllText($assemblyPath, $assemblySource, (New-Object System.Text.UTF8Encoding($false)))
$manifest = Join-Path $workspace 'desktop\installer\app.manifest'
$uninstallSource = Join-Path $workspace 'desktop\installer\Uninstall.cs'
$setupSource = Join-Path $workspace 'desktop\installer\Program.cs'
$payloadSource = Join-Path $workspace 'desktop\installer\PayloadSource.cs'
$common = @('/nologo', '/target:winexe', '/platform:x64', '/optimize+', '/utf8output',
    ('/win32manifest:' + $manifest), ('/win32icon:' + $iconPath),
    '/reference:System.dll', '/reference:System.Core.dll', '/reference:System.Drawing.dll',
    '/reference:System.Windows.Forms.dll', '/reference:System.Web.Extensions.dll')
if (-not $ReusePayloadArchive) {
    $uninstallArguments = $common + @('/main:CodeAgent.Installer.UninstallProgram', ('/out:' + (Join-Path $payload 'Code-agent Uninstall.exe')), $uninstallSource, $assemblyPath)
    & $csc @uninstallArguments
    if ($LASTEXITCODE -ne 0) { throw 'Uninstaller compilation failed.' }
}

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$allFinalFiles = @(Get-ChildItem -LiteralPath $payload -File -Recurse -Force)
# pip may produce entry-point wrappers containing the build machine's Python
# path. Runtime code uses `python -m`; keep these files in staging but never
# distribute the exact generated bin/Scripts directories in site-packages.
$finalFiles = @($allFinalFiles | Where-Object {
    $_.FullName.Substring($payload.Length + 1).Replace('\', '/') -notmatch '^runtimes/python/Lib/site-packages/(bin|Scripts)/'
})
if ($ReusePayloadArchive) {
    $reuse = Assert-ReusablePayloadArchive -ArchivePath $ReusePayloadArchive -PayloadRoot $payload -ExpectedFiles $finalFiles -ExpectedVersion $version
    $archive = $reuse.archive
    Write-Output ('Verified existing payload archive: ' + $archive + ' (' + $reuse.files + ' files)')
} else {
    $archive = Join-Path $scratch 'payload.zip'
    $archiveStream = [System.IO.File]::Open($archive, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
    $zip = New-Object System.IO.Compression.ZipArchive($archiveStream, [System.IO.Compression.ZipArchiveMode]::Create, $false, [System.Text.Encoding]::UTF8)
    try {
        foreach ($file in $finalFiles) {
            $relative = $file.FullName.Substring($payload.Length + 1).Replace('\', '/')
            [void][System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $file.FullName, $relative, [System.IO.Compression.CompressionLevel]::Optimal)
        }
    } finally { $zip.Dispose(); $archiveStream.Dispose() }
}
$temporaryInstaller = Join-Path $scratch ('Code-agent-Setup-' + $version + '-win-x64.exe')
$appendedPayload = $ForceAppendedPayload -or (Get-Item -LiteralPath $archive).Length -ge 1GB
$setupArguments = $common + @('/main:CodeAgent.Installer.SetupProgram', ('/out:' + $temporaryInstaller),
    '/reference:System.IO.Compression.dll', '/reference:System.IO.Compression.FileSystem.dll',
    $setupSource, $payloadSource, $uninstallSource, $assemblyPath)
if (-not $appendedPayload) { $setupArguments += ('/resource:' + $archive + ',CodeAgent.Payload.zip') }
$maintenance = Join-Path $workspace 'bin\code-agent-maintenance.mjs'
if (Test-Path -LiteralPath $maintenance -PathType Leaf) { $setupArguments += ('/resource:' + $maintenance + ',CodeAgent.Maintenance.mjs') }
& $csc @setupArguments
if ($LASTEXITCODE -ne 0) { throw 'Installer compilation failed.' }
$overlay = $null
if ($appendedPayload) {
    $expectedArchiveSha = if ($ReusePayloadArchive) { $reuse.sha256 } else { $null }
    $overlay = Add-AppendedPayload -ExecutablePath $temporaryInstaller -ArchivePath $archive -ExpectedSha256 $expectedArchiveSha
    $archiveSha = $overlay.sha256
} else { $archiveSha = if ($ReusePayloadArchive) { $reuse.sha256 } else { Get-PayloadFileSha256 $archive } }
$installer = Join-Path $OutputDirectory ([System.IO.Path]::GetFileName($temporaryInstaller))
Copy-Item -LiteralPath $temporaryInstaller -Destination $installer -Force
$sha = Get-PayloadFileSha256 $installer
$metadata = [ordered]@{ installer = $installer; version = $version; architecture = 'win-x64'; size = (Get-Item -LiteralPath $installer).Length; sha256 = $sha; payloadFiles = $finalFiles.Count; payloadBytes = ($finalFiles | Measure-Object -Property Length -Sum).Sum; skippedPythonEntryPoints = $allFinalFiles.Count - $finalFiles.Count;
    reusedPayloadArchive = [bool]$ReusePayloadArchive; payloadArchive = $archive; payloadArchiveSha256 = $archiveSha; runtimeVersions = $runtime.runtimeVersions;
    payloadFormat = $(if ($appendedPayload) { 'appended-zip-v1' } else { 'embedded-resource' }); payloadOffset = $(if ($overlay) { $overlay.offset } else { $null }); payloadArchiveBytes = (Get-Item -LiteralPath $archive).Length }
$metadata | ConvertTo-Json | Set-Content -LiteralPath ($installer + '.json') -Encoding UTF8
($sha + '  ' + [System.IO.Path]::GetFileName($installer)) | Set-Content -LiteralPath ($installer + '.sha256') -Encoding ASCII
$metadata | ConvertTo-Json -Compress
