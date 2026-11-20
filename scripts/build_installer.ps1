param(
    [Parameter(Mandatory=$true)][string]$Compiler,
    [string]$UpdateRepository
)
$ErrorActionPreference='Stop'
$project=(Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$compilerPath=(Resolve-Path -LiteralPath $Compiler).Path
Set-Location -LiteralPath $project
$python=Join-Path $project '.venv\Scripts\python.exe'
$dist=Join-Path $project 'dist'
$package=Join-Path $dist 'StarmapDownloader'
foreach ($target in @($dist,$package)) {
    if (Test-Path -LiteralPath $target) {
        $item=Get-Item -LiteralPath $target -Force
        if (!$item.FullName.StartsWith($project+'\',[StringComparison]::OrdinalIgnoreCase) -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw "Unsafe build target: $target"
        }
    }
}
$version=& $python -X utf8 -c 'from core import VERSION; print(VERSION)'
if ($LASTEXITCODE -ne 0 -or $version -notmatch '^[0-9]+\.[0-9]+(?:\.[0-9]+)?$') { throw 'Invalid application version' }
$env:PYTHONUTF8='1'
$env:STARMAP_CONSOLE='0'
$previousRepository=$env:STARMAP_RELEASE_REPOSITORY
try {
    $env:STARMAP_RELEASE_REPOSITORY=$UpdateRepository
    & $python -X utf8 -m PyInstaller --noconfirm --distpath $dist StarmapDownloader.spec
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed' }
} finally {
    $env:STARMAP_RELEASE_REPOSITORY=$previousRepository
}
$packageArgs=@((Join-Path $package 'StarmapDownloader.exe'),'--require-version',$version,'--require-gedi','--require-batch','--require-accounts')
if ($UpdateRepository) {
    $packageArgs+=@('--require-update-repository',$UpdateRepository)
} else {
    $packageArgs+=@('--require-no-update-channel','--expect-update-status','unconfigured')
}
& $python -X utf8 scripts\check_package.py @packageArgs
if ($LASTEXITCODE -ne 0) { throw 'Packaged startup check failed' }
foreach ($notice in @('installer\快速开始.txt', 'installer\ChineseSimplified.LICENSE.txt', 'THIRD_PARTY_NOTICES.md', 'docs\GEDI.md', 'docs\BATCH.md', 'docs\ACCOUNTS.md')) {
    Copy-Item -LiteralPath (Join-Path $project $notice) -Destination $package
}
$compilerArgs=@("/DAppRoot=$package","/DOutputRoot=$dist","/DAppVersion=$version")
if ($UpdateRepository) { $compilerArgs+=@("/DReleaseRepository=$UpdateRepository") }
& $compilerPath @compilerArgs (Join-Path $project 'installer\StarmapDownloader.iss')
if ($LASTEXITCODE -ne 0) { throw 'Windows installer build failed' }
$artifact=Join-Path $dist "StarmapDownloader-Setup-$version-win-x64.exe"
Get-Item -LiteralPath $artifact | Select-Object FullName,Length
$checksum=Get-FileHash -LiteralPath $artifact -Algorithm SHA256
"$($checksum.Hash.ToLowerInvariant()) *$([IO.Path]::GetFileName($artifact))" | Set-Content -LiteralPath ($artifact+'.sha256') -Encoding ascii
$checksum
