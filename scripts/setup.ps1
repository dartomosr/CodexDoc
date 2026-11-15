$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root
$uv = Get-Command uv -ErrorAction SilentlyContinue
if (!$uv) {
    Write-Output 'Installing uv for the current user...'
    $installer = Join-Path ([IO.Path]::GetTempPath()) ('codexdoc-uv-' + [guid]::NewGuid().ToString() + '.ps1')
    try {
        Invoke-WebRequest -UseBasicParsing -Uri 'https://astral.sh/uv/0.13.0/install.ps1' -OutFile $installer
        & $installer
        if ($LASTEXITCODE -ne 0) { throw 'uv installation failed' }
    } finally {
        Remove-Item -LiteralPath $installer -ErrorAction SilentlyContinue
    }
    $uvPath = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
} else { $uvPath = $uv.Source }
& $uvPath python install 3.13.9
if ($LASTEXITCODE -ne 0) { throw 'Python installation failed' }
& $uvPath sync --locked
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
& (Join-Path $PSScriptRoot 'install-renderer.ps1')
if ($LASTEXITCODE -ne 0) { throw 'Renderer installation failed' }
& (Join-Path $root '.venv\Scripts\python.exe') -m codexdoc doctor
if ($LASTEXITCODE -ne 0) { throw 'Environment check failed' }
Write-Output 'CodexDoc is ready. Start it using CodexDoc.cmd.'
