$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$root = Split-Path -Parent $PSScriptRoot
$lock = Get-Content -LiteralPath (Join-Path $root 'runtime.lock.json') -Raw | ConvertFrom-Json
$runtime = Join-Path $root '.runtime'
$target = Join-Path $runtime 'libreoffice'
$marker = Join-Path $target 'runtime.json'
if (Test-Path -LiteralPath $marker) {
    $record = Get-Content -LiteralPath $marker -Raw | ConvertFrom-Json
    if ($record.sha256 -eq $lock.libreoffice.sha256 -and (Test-Path -LiteralPath (Join-Path $target $record.executable))) {
        Write-Output 'Managed LibreOffice is ready.'
        exit 0
    }
}
New-Item -ItemType Directory -Force -Path $runtime | Out-Null
$msi = Join-Path $runtime 'libreoffice.msi'
if (!(Test-Path -LiteralPath $msi) -or (Get-FileHash -LiteralPath $msi -Algorithm SHA256).Hash.ToLower() -ne $lock.libreoffice.sha256) {
    Write-Output 'Downloading the pinned document renderer (about 330 MB)...'
    Invoke-WebRequest -UseBasicParsing -Uri $lock.libreoffice.url -OutFile $msi
}
if ((Get-FileHash -LiteralPath $msi -Algorithm SHA256).Hash.ToLower() -ne $lock.libreoffice.sha256) {
    throw 'Renderer checksum mismatch. Installation stopped.'
}
Write-Output 'Extracting the private renderer; desktop Office settings are not changed...'
$log = Join-Path $runtime 'libreoffice-extract.log'
$process = Start-Process -FilePath 'msiexec.exe' -ArgumentList @('/a', ('"' + $msi + '"'), '/qn', ('TARGETDIR="' + $target + '"'), '/L*v', ('"' + $log + '"')) -WindowStyle Hidden -PassThru -Wait
if ($process.ExitCode -ne 0) { throw "Renderer extraction failed ($($process.ExitCode)). See $log" }
$exe = Get-ChildItem -LiteralPath $target -Filter soffice.com -Recurse | Select-Object -First 1
if (!$exe) { throw 'Extracted renderer executable not found.' }
$relative = $exe.FullName.Substring($target.Length + 1)
@{ version = $lock.libreoffice.version; sha256 = $lock.libreoffice.sha256; executable = $relative } | ConvertTo-Json | Set-Content -LiteralPath $marker -Encoding UTF8
Write-Output "Managed renderer ready: $($exe.FullName)"
