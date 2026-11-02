param([Parameter(Mandatory = $true)][string]$Root)
$ErrorActionPreference = 'Stop'
try {
    $workspace = (Resolve-Path -LiteralPath $Root).ProviderPath
    $nativeLauncher = Join-Path $workspace 'Code-agent.exe'
    if (-not (Test-Path -LiteralPath $nativeLauncher -PathType Leaf)) { $nativeLauncher = Join-Path $workspace 'Code-agent Launcher.exe' }
    if (-not (Test-Path -LiteralPath $nativeLauncher -PathType Leaf)) {
        throw 'Code-agent desktop application is missing. Run npm run build:launcher in the project directory.'
    }
    # User-requested interactive window. Backend helpers remain hidden.
    Start-Process -FilePath $nativeLauncher -WorkingDirectory $workspace
    exit 0
} catch {
    try {
        $logDirectory = Join-Path $Root 'logs'
        [void][System.IO.Directory]::CreateDirectory($logDirectory)
        Add-Content -LiteralPath (Join-Path $logDirectory 'launcher-desktop.log') -Value $_.Exception.Message -Encoding UTF8
    } catch { }
    [System.Console]::Error.WriteLine('Unable to open the native launcher. Check logs\launcher-desktop.log.')
    exit 1
}
