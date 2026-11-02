param(
    [string]$Root = (Split-Path -Parent $PSScriptRoot),
    [string]$OutputDirectory,
    [string]$CacheDirectory,
    [string]$RubyBuildTools,
    [switch]$Resume,
    [bool]$DownloadMissing = $true
)
$ErrorActionPreference = 'Stop'
$workspace = (Resolve-Path -LiteralPath $Root).ProviderPath
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $workspace ('.tmp\wp-' + [Guid]::NewGuid().ToString('N').Substring(0, 16)) }
$output = [System.IO.Path]::GetFullPath($OutputDirectory)
if ($Resume -and (-not (Test-Path -LiteralPath $output -PathType Container) -or @(Get-ChildItem -LiteralPath $output -Force).Count -eq 0)) { throw 'Resume requires an existing nonempty payload directory.' }
if (Test-Path -LiteralPath $output) {
    if (@(Get-ChildItem -LiteralPath $output -Force).Count -ne 0) {
        if (-not $Resume) { throw 'Payload output must be a new or empty directory.' }
        $priorManifestPath = Join-Path $output 'desktop-runtime.json'
        if (-not (Test-Path -LiteralPath $priorManifestPath)) { throw 'Resume requires an existing payload runtime manifest.' }
        $priorManifest = Get-Content -LiteralPath $priorManifestPath -Raw | ConvertFrom-Json
        $packageVersion = (Get-Content -LiteralPath (Join-Path $workspace 'package.json') -Raw | ConvertFrom-Json).version
        if ($priorManifest.schemaVersion -ne 1 -or $priorManifest.version -ne $packageVersion -or
            $priorManifest.node -ne 'runtimes/node/node.exe' -or $priorManifest.python -ne 'runtimes/python/python.exe' -or
            $priorManifest.webview2 -ne 'runtimes/webview2') { throw 'Resume payload identity does not match this build.' }
        foreach ($relativePath in @('runtimes/node/node.exe','runtimes/python/python.exe','runtimes/webview2/msedgewebview2.exe')) {
            if (-not (Test-Path -LiteralPath (Join-Path $output $relativePath) -PathType Leaf)) { throw 'Resume requires previously extracted runtimes.' }
        }
    }
} else { [void][System.IO.Directory]::CreateDirectory($output) }
if (-not $CacheDirectory) { $CacheDirectory = Join-Path $workspace '.tmp\installer-downloads' }
$cache = [System.IO.Path]::GetFullPath($CacheDirectory)
[void][System.IO.Directory]::CreateDirectory($cache)
& (Join-Path $PSScriptRoot 'download-windows-runtimes.ps1') -CacheDirectory $cache -DownloadMissing $DownloadMissing -IncludeGit $true
$scratch = Join-Path $workspace ('.tmp\payload-build-' + [Guid]::NewGuid().ToString('N'))
[void][System.IO.Directory]::CreateDirectory($scratch)
Write-Output ('PAYLOAD_DIRECTORY=' + $output)
Write-Output ('BUILD_DIRECTORY=' + $scratch)
$nodeVersion = '24.14.0'
$pythonVersion = '3.13.7'
$webViewVersion = '156.0.4314.50'
$nodeArchive = Join-Path $cache ('node-v' + $nodeVersion + '-win-x64.zip')
$pythonArchive = Join-Path $cache ('python-' + $pythonVersion + '-embeddable-amd64.zip')
$webViewDirectory = Join-Path $cache ('webview2-fixed\Microsoft.WebView2.FixedVersionRuntime.' + $webViewVersion + '.x64')
$gitDirectory = Join-Path $cache 'mingit'
$includeGit = Test-Path -LiteralPath (Join-Path $gitDirectory 'cmd\git.exe') -PathType Leaf
$expectedHashes = @{
    $nodeArchive = '313fa40c0d7b18575821de8cb17483031fe07d95de5994f6f435f3b345f85c66'
    $pythonArchive = 'e201b2da753a88c1af29d87f9f48af4d64a0fc8522a204ae672bd2c382496701'
}
foreach ($archive in @($nodeArchive, $pythonArchive)) {
    if (-not (Test-Path -LiteralPath $archive -PathType Leaf)) { throw ('Download prerequisite is missing: ' + $archive) }
    if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expectedHashes[$archive]) { throw ('Runtime archive verification failed: ' + $archive) }
}
if (-not (Test-Path -LiteralPath (Join-Path $webViewDirectory 'msedgewebview2.exe'))) { throw 'Verified fixed WebView2 runtime is missing.' }
$hostPython = Join-Path $workspace '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $hostPython)) { throw 'The build environment Python is missing.' }
$hostNode = (Get-Command node.exe -ErrorAction Stop).Source

# Ship only the explicit application/runtime whitelist, never local user data.
$copyScript = Join-Path $scratch 'copy-assets.mjs'
$copyProgram = @'
import {cp,copyFile,mkdir,readdir,lstat,writeFile,readFile} from 'node:fs/promises';
import {join,relative,dirname,resolve,sep} from 'node:path';
import {pathToFileURL} from 'node:url';
const [root,out]=process.argv.slice(2,4).map(value=>resolve(value));
const includeGit=process.argv[4]==='true';
const {payloadFiles}=await import(pathToFileURL(join(root,'npm/payload.mjs')).href);
const exact=['package.json','pyproject.toml','.env.example','npm/release.json','scripts/backend_idle_check.py','scripts/launcher_process.py',
 'desktop/launcher/LICENSE-WebView2.txt','desktop/launcher/NOTICE-WebView2.txt'];
const files=[...await payloadFiles(root),...exact.map(p=>join(root,p))];
for(const directory of ['bin','npm'])for(const name of await readdir(join(root,directory)))
 if(/\.(?:mjs|ps1)$/.test(name))files.push(join(root,directory,name));
for(const source of files){const name=relative(root,source);if(name.startsWith('..')||name.includes('node_modules'))throw Error('Invalid application input');
 const target=join(out,name);await mkdir(dirname(target),{recursive:true});await copyFile(source,target);}
for(const directory of ['npm/launcher-ui','web/dist'])await cp(join(root,directory),join(out,directory),{recursive:true,dereference:false,filter:async p=>{
 const name=relative(root,p);if(name.split(sep).some(x=>['__pycache__','node_modules'].includes(x)))return false;
 const info=await lstat(p);if(info.isSymbolicLink())throw Error('Symlink in frontend assets');return info.isDirectory()||!name.endsWith('.map');}});
await writeFile(join(out,'desktop-runtime.json'),JSON.stringify({schemaVersion:1,version:JSON.parse(await readFile(join(root,'package.json'),'utf8')).version,
 node:'runtimes/node/node.exe',python:'runtimes/python/python.exe',webview2:'runtimes/webview2',
 ...(includeGit?{git:'runtimes/git/cmd/git.exe'}:{}),
 java:'runtimes/java/bin/java.exe',jdtls:'runtimes/jdtls',lombok:'runtimes/lombok/lombok.jar',
 runtimeVersions:{node:'24.14.0',python:'3.13.7',webview2:'156.0.4314.50',...(includeGit?{git:'2.56.0.windows.4'}:{}),
 java:'21.0.12.1+1',jdtls:'1.61.0-202609031315',lombok:'1.18.48'}},null,2)+'\n');
console.log(JSON.stringify({applicationFiles:files.length,root:out}));
'@
[System.IO.File]::WriteAllText($copyScript, $copyProgram, (New-Object System.Text.UTF8Encoding($false)))
& $hostNode $copyScript $workspace $output $includeGit.ToString().ToLowerInvariant()
if ($LASTEXITCODE -ne 0) { throw 'Application whitelist copy failed.' }

Add-Type -AssemblyName System.IO.Compression.FileSystem
$nodePayload = Join-Path $output 'runtimes\node'
$pythonPayload = Join-Path $output 'runtimes\python'
$webViewPayload = Join-Path $output 'runtimes\webview2'
if (-not $Resume) {
    $nodeExpanded = Join-Path $scratch 'node'
    [System.IO.Compression.ZipFile]::ExtractToDirectory($nodeArchive, $nodeExpanded)
    [void][System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($nodePayload))
    Copy-Item -LiteralPath (Join-Path $nodeExpanded ('node-v' + $nodeVersion + '-win-x64')) -Destination $nodePayload -Recurse
    [System.IO.Compression.ZipFile]::ExtractToDirectory($pythonArchive, $pythonPayload)
    Copy-Item -LiteralPath $webViewDirectory -Destination $webViewPayload -Recurse
}
if ($includeGit) {
    $gitArchive = Join-Path $cache 'MinGit-2.56.0.4-64-bit.zip'
    if (-not (Test-Path -LiteralPath $gitArchive) -or (Get-FileHash -LiteralPath $gitArchive -Algorithm SHA256).Hash.ToLowerInvariant() -ne 'e3bf78351fe777fba47ddee5950082f8a98bb0f02604fdc7ba3ee2cb68ffa877') { throw 'Git archive verification failed.' }
    $gitPayload = Join-Path $output 'runtimes\git'
    if (-not (Test-Path -LiteralPath (Join-Path $gitPayload 'cmd\git.exe'))) { Copy-Item -LiteralPath $gitDirectory -Destination $gitPayload -Recurse }
}
& (Join-Path $PSScriptRoot 'prepare-windows-java.ps1') -CacheDirectory $cache -PayloadDirectory $output -PythonExecutable $hostPython -DownloadMissing $DownloadMissing
if ($LASTEXITCODE -ne 0) { throw 'Bundled Java analysis preparation failed.' }
foreach ($name in @('msvcp140.dll','msvcp140_codecvt_ids.dll','vcruntime140.dll','vcruntime140_1.dll')) {
    $redistributable = Join-Path $webViewDirectory $name
    if (Test-Path -LiteralPath $redistributable -PathType Leaf) {
        foreach ($destination in @($output, $nodePayload, $pythonPayload)) { Copy-Item -LiteralPath $redistributable -Destination (Join-Path $destination $name) -Force }
    }
}
# Keep staged application sources ahead of the dependency wheel's older copy.
# The embedded Windows path resolver treats ../.. differently from ..\..;
# use native separators and verify the actual imported module origins below.
$pth = @('python313.zip', '.', '..\..', 'Lib\site-packages', 'import site') -join "`r`n"
[System.IO.File]::WriteAllText((Join-Path $pythonPayload 'python313._pth'), $pth + "`r`n", (New-Object System.Text.UTF8Encoding($false)))

# Constraints preserve versions already proven in the local runtime; optional
# office/build packages not currently installed are resolved and then recorded.
$constraints = Join-Path $scratch 'constraints.txt'
$freezeProgram = 'import importlib.metadata as m,sys; from pathlib import Path; rows=sorted({d.metadata["Name"]+"=="+d.version for d in m.distributions() if d.metadata.get("Name") and d.metadata["Name"].lower().replace("_","-")!="code-agent"}); Path(sys.argv[1]).write_text("\n".join(rows)+"\n",encoding="utf-8")'
$freezeScript = Join-Path $scratch 'freeze-build-runtime.py'
[System.IO.File]::WriteAllText($freezeScript, $freezeProgram, (New-Object System.Text.UTF8Encoding($false)))
& $hostPython -X utf8 $freezeScript $constraints
if ($LASTEXITCODE -ne 0) { throw 'Could not record runtime dependency constraints.' }
$wheels = Join-Path $scratch 'wheels'
[void][System.IO.Directory]::CreateDirectory($wheels)
& $hostPython -X utf8 -m pip --disable-pip-version-check wheel --no-deps --wheel-dir $wheels $workspace
if ($LASTEXITCODE -ne 0) { throw 'Backend wheel build failed.' }
$projectWheel = @(Get-ChildItem -LiteralPath $wheels -Filter 'code_agent-*.whl')
if ($projectWheel.Count -ne 1) { throw 'Backend wheel output is ambiguous.' }
$wheelRequirement = $projectWheel[0].FullName + '[office]'
& $hostPython -X utf8 -m pip --disable-pip-version-check download --only-binary=:all: --python-version 3.13 --platform win_amd64 --implementation cp --abi cp313 --constraint $constraints --dest $wheels $wheelRequirement pip setuptools wheel
if ($LASTEXITCODE -ne 0) { throw 'Offline dependency wheel collection failed.' }
$sitePackages = Join-Path $pythonPayload 'Lib\site-packages'
& $hostPython -X utf8 -m pip --disable-pip-version-check install --no-index --find-links $wheels --no-compile --target $sitePackages $wheelRequirement pip setuptools wheel
if ($LASTEXITCODE -ne 0) { throw 'Offline backend dependency installation failed.' }

# Local wheel direct_url entries describe the build machine, not runtime input.
foreach ($file in @(Get-ChildItem -LiteralPath $sitePackages -Filter direct_url.json -Recurse -File)) {
    $fullName = [System.IO.Path]::GetFullPath($file.FullName)
    if (-not $fullName.StartsWith($sitePackages + [System.IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw 'Unexpected wheel metadata path.' }
    Remove-Item -LiteralPath $fullName -Force
}
# --target script wrappers are not relocatable and are excluded by the installer
# archive whitelist (runtimes/python/Lib/site-packages/bin and Scripts). Runtime
# tools use python -m; no source-project executable is used after installation.
$runtimeInventory = Join-Path $output 'runtime-packages.json'
$inventoryProgram = 'import importlib.metadata as m,json,sys; from pathlib import Path; packages=sorted(({"name":d.metadata["Name"],"version":d.version} for d in m.distributions(path=[sys.argv[1]]) if d.metadata.get("Name")),key=lambda x:x["name"].lower()); Path(sys.argv[2]).write_text(json.dumps(packages,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")'
$inventoryScript = Join-Path $scratch 'record-runtime-packages.py'
[System.IO.File]::WriteAllText($inventoryScript, $inventoryProgram, (New-Object System.Text.UTF8Encoding($false)))
& $hostPython -X utf8 $inventoryScript $sitePackages $runtimeInventory
if ($LASTEXITCODE -ne 0) { throw 'Runtime inventory creation failed.' }
Copy-Item -LiteralPath $constraints -Destination (Join-Path $output 'runtime-build-constraints.txt')

$envBefore = @{}
foreach ($name in @('CODE_AGENT_HOME','CODE_AGENT_NODE','CODE_AGENT_HARNESS_NODE_DIR','CODE_AGENT_WEB_DIST','CODE_AGENT_PROVIDER','PYTHONPATH','PYTHONHOME','PYTHONDONTWRITEBYTECODE')) { $envBefore[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
try {
    $env:CODE_AGENT_HOME = Join-Path $scratch 'validation-home'
    $env:CODE_AGENT_NODE = Join-Path $nodePayload 'node.exe'
    $env:CODE_AGENT_HARNESS_NODE_DIR = Join-Path $output 'code_agent\harness\node'
    $env:CODE_AGENT_WEB_DIST = Join-Path $output 'web\dist'
    $env:CODE_AGENT_PROVIDER = 'offline'
    $env:PYTHONPATH = $null
    $env:PYTHONHOME = $null
    $env:PYTHONDONTWRITEBYTECODE = '1'
    Push-Location $scratch
    try {
        $validateScript = Join-Path $scratch 'validate-imports.py'
        $validateProgram = @'
import sys
from pathlib import Path
import code_agent, code_agent.server, code_agent.harness
import code_agent.codeintel.native_dialog, code_agent.api.native_dialog_api
import httpx, fastapi, pydantic, winpty, docx, pptx, pip
application = Path(sys.argv[1]).resolve() / 'code_agent'
for module in (code_agent, code_agent.server, code_agent.harness,
               code_agent.codeintel.native_dialog, code_agent.api.native_dialog_api):
    if not Path(module.__file__).resolve().is_relative_to(application):
        raise RuntimeError('Bundled backend imported a dependency wheel copy instead of staged application sources: ' + module.__name__)
print('Bundled application source origins, backend and office imports passed')
'@
        [System.IO.File]::WriteAllText($validateScript, $validateProgram, (New-Object System.Text.UTF8Encoding($false)))
        & (Join-Path $pythonPayload 'python.exe') -I -B -X utf8 $validateScript $output
        if ($LASTEXITCODE -ne 0) { throw 'Bundled backend import validation failed.' }
        & (Join-Path $pythonPayload 'python.exe') -I -B -X utf8 -m code_agent.harness --prepare
        if ($LASTEXITCODE -ne 0) { throw 'Bundled Harness validation failed.' }
    } finally { Pop-Location }
} finally {
    foreach ($name in $envBefore.Keys) { [Environment]::SetEnvironmentVariable($name, $envBefore[$name], 'Process') }
}
# Pick up application changes made while the dependency wheels were assembling.
& $hostNode $copyScript $workspace $output $includeGit.ToString().ToLowerInvariant()
if ($LASTEXITCODE -ne 0) { throw 'Final application whitelist synchronization failed.' }
# This runs after copy-assets rewrites desktop-runtime.json. Resume reuses the
# group fragments already staged, while a fresh payload prepares pinned tools.
& (Join-Path $PSScriptRoot 'prepare-windows-languages.ps1') -PayloadDirectory $output -CacheDirectory (Join-Path $cache 'languages') -PythonExecutable $hostPython -NodeExecutable $hostNode -RubyBuildTools $RubyBuildTools -Resume:$Resume -DownloadMissing $DownloadMissing
if ($LASTEXITCODE -ne 0) { throw 'Bundled language service preparation failed.' }
$report = [ordered]@{ payloadDirectory = $output; buildDirectory = $scratch; python = $pythonVersion; node = $nodeVersion; webview2 = $webViewVersion; languageServers = 28; validated = $true; nativeExecutablesPending = $true }
$report | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $scratch 'payload-report.json') -Encoding UTF8
$report | ConvertTo-Json -Compress
