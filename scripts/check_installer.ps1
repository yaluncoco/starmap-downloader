param(
    [Parameter(Mandatory=$true)][string]$Installer,
    [string]$UpgradeFrom,
    [switch]$ReplacePreview,
    [string]$UpdateRepository,
    [ValidateSet('unconfigured','not_published','current','available')][string]$ExpectUpdateStatus,
    [switch]$Live,
    [switch]$Batch,
    [switch]$Accounts,
    [switch]$RadarMeasurement,
    [switch]$LandsatMeasurement,
    [switch]$S2Tiles,
    [switch]$WorldCover,
    [switch]$Slc,
    [switch]$SlcDual,
    [string]$SlcReference,
    [switch]$Modis,
    [switch]$ModisSmoke,
    [string]$ModisReference,
    [string]$WorldCoverReference,
    [ValidateSet('svalbard-l2a','beijing-l2a','rapa-nui-l3','beijing-l3')][string[]]$S2Cases,
    [switch]$Rtc,
    [switch]$RtcEw,
    [switch]$S5p,
    [switch]$S5pAllNrti,
    [switch]$Sen3,
    [switch]$Sen3FullRbt,
    [switch]$Sen3LargeFiles
)
$ErrorActionPreference='Stop'
if ($ReplacePreview -and !$UpgradeFrom) { throw '-ReplacePreview requires -UpgradeFrom' }
if ($SlcReference -and !$Slc -and !$SlcDual) { throw '-SlcReference requires -Slc or -SlcDual' }
if ($ModisReference -and !$Modis -and !$ModisSmoke) { throw '-ModisReference requires -Modis or -ModisSmoke' }
if ($Modis -and $ModisSmoke) { throw 'Choose -Modis or -ModisSmoke' }
if ($WorldCoverReference -and !$WorldCover -and !$Live) { throw '-WorldCoverReference requires -WorldCover or -Live' }
if ($S2Cases -and !$S2Tiles -and !$Live) { throw '-S2Cases requires -S2Tiles or -Live' }
if (($RadarMeasurement -or $LandsatMeasurement) -and !$Live) { throw '-RadarMeasurement and -LandsatMeasurement require -Live' }
$project=(Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$setup=(Resolve-Path -LiteralPath $Installer).Path
if (!(Test-Path -LiteralPath $setup -PathType Leaf)) { throw 'Installer not found' }
$priorSetup=$null
if ($UpgradeFrom) {
    $priorSetup=(Resolve-Path -LiteralPath $UpgradeFrom).Path
    if ($priorSetup -eq $setup -or !(Test-Path -LiteralPath $priorSetup -PathType Leaf)) { throw 'Upgrade baseline installer must be a different file' }
}
$registry='HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{64D676BE-9FB7-4DC8-BA41-F498E21BC854}_is1'
if (Test-Path -LiteralPath $registry) { throw 'The app is already registered for this Windows user. Use a clean test user to avoid replacing that installation.' }
$tag=[Guid]::NewGuid().ToString('N').Substring(0,8)
$root=(New-Item -Path (Join-Path $project "verification\install-$tag") -ItemType Directory).FullName
$app=Join-Path $root '程序\StarmapDownloader'
$startMenu=Join-Path ([Environment]::GetFolderPath('Programs')) '星图影像下载器'
if (Test-Path -LiteralPath $startMenu) { throw 'The app Start menu group already exists; use a clean test user.' }
$userAppData=(New-Item -Path (Join-Path $root 'LocalAppData') -ItemType Directory).FullName
$userOutput=(New-Item -Path (Join-Path $root '下载输出') -ItemType Directory).FullName
$python=Join-Path $project '.venv\Scripts\python.exe'
$installLog=Join-Path $root 'install.log'
$uninstallLog=Join-Path $root 'uninstall.log'
$check=@{Installer=$setup; SHA256=(Get-FileHash -LiteralPath $setup -Algorithm SHA256).Hash; LogDirectory=$root; IsolatedInstall=$app}
$oldAppData=$env:LOCALAPPDATA
$oldUtf=$env:PYTHONUTF8
$uninstaller=$null
$desktop=$null
try {
    if ($priorSetup) {
        $baselineLog=Join-Path $root 'install-baseline.log'
        $priorOptions=@('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/SP-','/LANG=chinesesimp',"/DIR=`"$app`"","/LOG=`"$baselineLog`"")
        $process=Start-Process -FilePath $priorSetup -ArgumentList $priorOptions -WindowStyle Hidden -Wait -PassThru
        if ($process.ExitCode -ne 0) { throw "Baseline setup exit code $($process.ExitCode)" }
        $prior=Get-ItemProperty -LiteralPath $registry
        if ($prior.InstallLocation.TrimEnd('\') -ne $app) { throw 'Wrong baseline registration path' }
        $check.UpgradeFromVersion=$prior.DisplayVersion
        $check.UpgradeFromSHA256=(Get-FileHash -LiteralPath $priorSetup -Algorithm SHA256).Hash
        $env:LOCALAPPDATA=$userAppData
        $env:PYTHONUTF8='1'
        try {
            & $python -X utf8 (Join-Path $project 'scripts\check_package.py') (Join-Path $app 'StarmapDownloader.exe') --set-output $userOutput
            if ($LASTEXITCODE -ne 0) { throw 'Could not save verification preferences in baseline app' }
        } finally {
            if ($null -eq $oldAppData) { Remove-Item Env:LOCALAPPDATA -ErrorAction SilentlyContinue } else { $env:LOCALAPPDATA=$oldAppData }
        }
        $upgradePref=Join-Path $userAppData 'CopernicusDownloader\preferences.json'
        $upgradeExample=Join-Path $userOutput '升级保留的文件.txt'
        [System.IO.File]::WriteAllText($upgradeExample,'version '+$check.UpgradeFromVersion+' user test data')
        $upgradePrefHash=(Get-FileHash -LiteralPath $upgradePref -Algorithm SHA256).Hash
        $upgradeExampleHash=(Get-FileHash -LiteralPath $upgradeExample -Algorithm SHA256).Hash
    }
    $installOptions=@('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/SP-','/LANG=chinesesimp',"/DIR=`"$app`"","/LOG=`"$installLog`"")
    $process=Start-Process -FilePath $setup -ArgumentList $installOptions -WindowStyle Hidden -Wait -PassThru
    if ($process.ExitCode -ne 0) { throw "Setup exit code $($process.ExitCode). Log: $installLog" }
    $check.InstallExitCode=$process.ExitCode
    $registration=Get-ItemProperty -LiteralPath $registry
    if ($registration.InstallLocation.TrimEnd('\') -ne $app) { throw 'Wrong registration path' }
    $check.RegisteredVersion=$registration.DisplayVersion
    $exe=Join-Path $app 'StarmapDownloader.exe'
    $check.InstalledExecutableSHA256=(Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash
    $uninstaller=(Get-ChildItem -LiteralPath $app -File -Filter 'unins*.exe' | Select-Object -First 1).FullName
    if (!(Test-Path -LiteralPath (Join-Path $startMenu '星图影像下载器.lnk') -PathType Leaf)) { throw 'Start menu entry missing' }
    $check.StartMenuShortcut=$true
    if ([version]$check.RegisteredVersion -ge [version]'1.4.0') {
        $taskIcon=Join-Path $app '_internal\ui\assets\globe-orbit.ico'
        $taskShell=New-Object -ComObject WScript.Shell
        $taskStartShortcut=$taskShell.CreateShortcut((Join-Path $startMenu '星图影像下载器.lnk'))
        if ($taskStartShortcut.IconLocation.Split(',')[0] -ne $taskIcon) { throw 'Start menu icon does not reference supplied ICO' }
        if ((Get-FileHash -LiteralPath $taskIcon -Algorithm SHA256).Hash -ne (Get-FileHash -LiteralPath (Join-Path $project 'ui\assets\globe-orbit.ico') -Algorithm SHA256).Hash) { throw 'Installed ICO differs from supplied icon' }
        $taskDesktopPath=Join-Path ([Environment]::GetFolderPath('DesktopDirectory')) '星图影像下载器.lnk'
        if (Test-Path -LiteralPath $taskDesktopPath) {
            $taskDesktopShortcut=$taskShell.CreateShortcut($taskDesktopPath)
            if ($taskDesktopShortcut.TargetPath -ne $exe -or $taskDesktopShortcut.IconLocation.Split(',')[0] -ne $taskIcon) { throw 'Desktop shortcut target or icon mismatch' }
            $check.DesktopShortcutSuppliedIcon=$true
        }
        $check.StartMenuSuppliedIcon=$true
    }
    $env:LOCALAPPDATA=$userAppData
    $env:PYTHONUTF8='1'
    $pkgArgs=@($exe,'--require-version',$check.RegisteredVersion)
    if ($UpdateRepository) { $pkgArgs+=@('--require-update-repository',$UpdateRepository) }
    if ($ExpectUpdateStatus) { $pkgArgs+=@('--expect-update-status',$ExpectUpdateStatus) }
    if ($priorSetup) {
        $oldVersion=[version]$check.UpgradeFromVersion
        $newVersion=[version]$check.RegisteredVersion
        if ($newVersion -eq $oldVersion -or (!$ReplacePreview -and $newVersion -lt $oldVersion)) { throw 'Upgrade target did not register the expected different version' }
        if ($ReplacePreview) { $check.ReplacedLocalPreview=$true }
        if ((Get-FileHash -LiteralPath $upgradePref -Algorithm SHA256).Hash -ne $upgradePrefHash -or
            (Get-FileHash -LiteralPath $upgradeExample -Algorithm SHA256).Hash -ne $upgradeExampleHash) { throw 'Upgrade modified the old preference or data file' }
        $check.UpgradePreservedPreferencesAndOutput=$true
        $pkgArgs+=@('--require-output',$userOutput)
    }
    & $python -X utf8 (Join-Path $project 'scripts\check_package.py') @pkgArgs
    if ($LASTEXITCODE -ne 0) { throw 'Installed binary portable smoke check failed' }
    $check.InstalledApiAndShapefile=$true

    $listener=[System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback,0)
    $listener.Start(); $port=$listener.LocalEndpoint.Port; $listener.Stop()
    $desktop=Start-Process -FilePath $exe -ArgumentList @('--port',"$port") -WorkingDirectory $root -WindowStyle Hidden -PassThru
    $origin="http://127.0.0.1:$port"
    $webView=$null
    for ($i=0;$i -lt 80;$i++) {
        if ($desktop.HasExited) { throw 'Installed native desktop quit early' }
        $webView=Get-CimInstance -ClassName Win32_Process -Filter "ParentProcessId = $($desktop.Id) AND Name = 'msedgewebview2.exe'" | Select-Object -First 1
        if ($webView) { break }
        Start-Sleep -Milliseconds 250
    }
    if (!$webView) { throw 'Installed native desktop did not start Edge WebView2' }
    $homeResponse=Invoke-WebRequest -UseBasicParsing -Uri $origin -TimeoutSec 5
    if ($homeResponse.Content -notmatch '<meta name="app-token" content="([^"<>]+)"') { throw 'Installed native desktop did not serve its UI' }
    $headers=@{'X-App-Token'=$Matches[1]}
    $bootstrap=Invoke-RestMethod -Uri "$origin/api/bootstrap" -Headers $headers -TimeoutSec 5
    if (!$bootstrap.native_dialogs -or $bootstrap.version -ne $check.RegisteredVersion) { throw 'Installed native API mismatch' }
    if ($UpdateRepository -and $bootstrap.update_repository -ne $UpdateRepository) { throw 'Installed native API update channel mismatch' }
    if ($bootstrap.PSObject.Properties.Name -contains 'gedi_collections') {
        if ($bootstrap.gedi_collections.Count -ne 13 -or $bootstrap.gedi_default -ne 'gedi02-a-003') { throw 'Installed GEDI collections mismatch' }
        if ((Get-Content -LiteralPath (Join-Path $app '快速开始.txt') -Raw -Encoding UTF8) -notmatch 'NASA GEDI' -or
            !(Test-Path -LiteralPath (Join-Path $app 'GEDI.md') -PathType Leaf)) { throw 'Installer did not include GEDI instructions' }
        $check.InstalledGediInterfaceAndInstructions=$true
    }

    if ($bootstrap.PSObject.Properties.Name -contains 'batch_sources') {
        if ($bootstrap.batch_sources.Count -ne 23 -or !(Test-Path -LiteralPath (Join-Path $app 'BATCH.md') -PathType Leaf)) {
            throw 'Installed batch sources or instructions are incomplete'
        }
        if ((Get-Content -LiteralPath (Join-Path $app '快速开始.txt') -Raw -Encoding UTF8) -notmatch '联合下载') {
            throw 'Installed quick-start guide omitted batch downloads'
        }
        foreach ($asset in @('batch.js','assets/globe-orbit.svg','assets/globe-orbit.ico')) {
            Invoke-WebRequest -UseBasicParsing -Uri "$origin/$asset" -TimeoutSec 5 | Out-Null
        }
        $check.InstalledBatchInterfaceAndInstructions=$true
    }

    # Before the public version numbering reset, infer support from the legacy preview version.
    $publicSeries=$bootstrap.PSObject.Properties.Name -contains 'update_repository'
    function Supports-PreviewFeature([version]$Introduced) {
        return $publicSeries -or [version]$check.RegisteredVersion -ge $Introduced
    }
    if (Supports-PreviewFeature '1.8.0') {
        $guide=Join-Path $app '快速开始.txt'
        if (!(Test-Path -LiteralPath $guide -PathType Leaf) -or (Get-Content -LiteralPath $guide -Raw -Encoding UTF8) -notmatch 'Sentinel-1 RTC') {
            throw 'Installer did not include the RTC quick-start guide'
        }
        $check.RtcQuickStartGuide=$true
    }
    if ($ExpectUpdateStatus) { $check.ExpectedUpdateStatus=$ExpectUpdateStatus }
    Invoke-RestMethod -Uri "$origin/api/preferences" -Method Post -Headers $headers -ContentType 'application/json; charset=utf-8' -Body (@{output_dir=$userOutput} | ConvertTo-Json -Compress) -TimeoutSec 5 | Out-Null
    $check.NativeWebView2=$true
    if ([version]$check.RegisteredVersion -ge [version]'1.4.0') {
        if (@($bootstrap.accounts.providers.PSObject.Properties).Count -ne 4) { throw 'Installed account platforms missing' }
        if (!(Test-Path -LiteralPath (Join-Path $app 'ACCOUNTS.md'))) { throw 'Installed account instructions missing' }
        Invoke-WebRequest -UseBasicParsing -Uri "$origin/accounts.js" -TimeoutSec 5 | Out-Null
        $check.InstalledAccountInterfaceAndInstructions=$true
    }
    if (!$desktop.CloseMainWindow()) { Stop-Process -Id $desktop.Id -ErrorAction SilentlyContinue }
    if (!$desktop.WaitForExit(6000)) { Stop-Process -Id $desktop.Id }
    $desktop=$null

    if ($Accounts) {
        $taskAccountOutput=Join-Path $root 'accounts'
        & $python -X utf8 (Join-Path $project 'scripts\check_accounts_package.py') --exe $exe --output $taskAccountOutput
        if ($LASTEXITCODE -ne 0) { throw 'Installed startup/login/persistence verification failed' }
        $check.AccountsReport=Join-Path $taskAccountOutput 'accounts-package-report.json'
    }
    if ($Batch) {
        $batchOutput=Join-Path $userOutput 'batch'
        & $python -X utf8 (Join-Path $project 'scripts\live_batch.py') --exe $exe --output $batchOutput
        if ($LASTEXITCODE -ne 0) { throw 'Installed shared-area batch verification failed' }
        $check.BatchReport=Join-Path $batchOutput 'batch-live-report.json'
    }
    if ($Slc -or $SlcDual) {
        if (!(Supports-PreviewFeature '1.12.0')) { throw 'IW SLC acceptance requires 1.12.0 or later' }
        if ((Get-Content -LiteralPath (Join-Path $app '快速开始.txt') -Raw -Encoding UTF8) -notmatch 'Sentinel-1 IW SLC') {
            throw 'Installer did not include the IW SLC quick-start instructions'
        }
        $slcOutput=Join-Path $userOutput 'slc'
        $slcArgs=@('--exe',$exe,'--output',$slcOutput)
        if ($SlcReference) { $slcArgs+=@('--reference-dir',(Resolve-Path -LiteralPath $SlcReference).Path) }
        if ($Slc -and $SlcDual) { $slcArgs+='--both' }
        elseif ($SlcDual) { $slcArgs+='--dual' }
        & $python -X utf8 (Join-Path $project 'scripts\live_slc.py') @slcArgs
        if ($LASTEXITCODE -ne 0) { throw 'Installed IW SLC byte or I/Q verification failed' }
        $check.SlcReport=Join-Path $slcOutput 'slc-live-report.json'
    }
    if ($Modis -or $ModisSmoke) {
        if (!(Supports-PreviewFeature '1.11.0')) { throw 'MODIS acceptance requires 1.11.0 or later' }
        if ((Get-Content -LiteralPath (Join-Path $app '快速开始.txt') -Raw -Encoding UTF8) -notmatch 'MODIS Collection 6.1') {
            throw 'Installer did not include the MODIS quick-start instructions'
        }
        $modisOutput=Join-Path $userOutput 'modis'
        $modisArgs=@('--exe',$exe,'--output',$modisOutput)
        if ($ModisReference) { $modisArgs+=@('--reference-dir',(Resolve-Path -LiteralPath $ModisReference).Path) }
        if ($ModisSmoke) { $modisArgs+='--smoke' }
        & $python -X utf8 (Join-Path $project 'scripts\live_modis.py') @modisArgs
        if ($LASTEXITCODE -ne 0) { throw 'Installed MODIS reference or transfer verification failed' }
        $check.ModisReport=Join-Path $modisOutput 'modis-live-report.json'
    }
    if ($WorldCover -or ($Live -and (Supports-PreviewFeature '1.10.0'))) {
        if (!(Supports-PreviewFeature '1.10.0')) { throw 'ESA annual verification needs 1.10.0 or later' }
        if ((Get-Content -LiteralPath (Join-Path $app '快速开始.txt') -Raw -Encoding UTF8) -notmatch 'ESA WorldCover') {
            throw 'Installer did not include the ESA WorldCover quick-start instructions'
        }
        $wcOutput=Join-Path $userOutput 'worldcover'
        $wcArgs=@('--exe',$exe,'--output',$wcOutput)
        if ($WorldCoverReference) { $wcArgs+=@('--reference-dir',(Resolve-Path -LiteralPath $WorldCoverReference).Path) }
        & $python -X utf8 (Join-Path $project 'scripts\live_worldcover.py') @wcArgs
        if ($LASTEXITCODE -ne 0) { throw 'Installed ESA WorldCover transfer, independent reference or browser verification failed' }
        $check.WorldCoverReport=Join-Path $wcOutput 'worldcover-live-report.json'
    }
    if ($S2Tiles -or ($Live -and (Supports-PreviewFeature '1.9.0'))) {
        if (!(Supports-PreviewFeature '1.9.0')) { throw 'Sentinel-2 L2A COG and L3 verification needs 1.9.0 or later' }
        $s2Output=Join-Path $userOutput 's2-tiles'
        $s2Args=@('--exe',$exe,'--output',$s2Output)
        foreach ($selectedCase in $S2Cases) { $s2Args+=@('--only',$selectedCase) }
        & $python -X utf8 (Join-Path $project 'scripts\live_s2_tiles.py') @s2Args
        if ($LASTEXITCODE -ne 0) { throw 'Installed Sentinel-2 whole tile transfer, source pixel or browser verification failed' }
        $check.S2NativeReport=Join-Path $s2Output 's2-native-live-report.json'
    }
    if ($Rtc -or $RtcEw -or ($Live -and (Supports-PreviewFeature '1.8.0'))) {
        if (!(Supports-PreviewFeature '1.8.0')) { throw 'Sentinel-1 RTC verification needs 1.8.0 or later' }
        $rtcOutput=Join-Path $userOutput 'rtc'
        $rtcArgs=@('--exe',$exe,'--output',$rtcOutput)
        if ($RtcEw) { $rtcArgs+='--include-ew' }
        & $python -X utf8 (Join-Path $project 'scripts\live_rtc.py') @rtcArgs
        if ($LASTEXITCODE -ne 0) { throw 'Installed Sentinel-1 RTC download and independent exact-pixel comparison failed' }
        $check.RtcReport=Join-Path $rtcOutput 'rtc-live-report.json'
    }
    if ($Sen3 -or $Sen3FullRbt -or $Sen3LargeFiles -or ($Live -and (Supports-PreviewFeature '1.6.0'))) {
        if (!(Supports-PreviewFeature '1.6.0')) { throw 'Sentinel-3 verification needs 1.6.0 or later' }
        if ($Sen3LargeFiles -and !(Supports-PreviewFeature '1.7.0')) { throw '-Sen3LargeFiles needs 1.7.0 or later' }
        $sen3Args=@('--exe',$exe,'--output',$userOutput)
        if ($Sen3FullRbt) { $sen3Args+='--full-rbt' }
        if (!$Sen3LargeFiles -and !$Sen3FullRbt) { $sen3Args+='--skip-large' }
        & $python -X utf8 (Join-Path $project 'scripts\live_sen3.py') @sen3Args
        if ($LASTEXITCODE -ne 0) { throw 'Installed Sentinel-3 original member and SEN3 product verification failed' }
        $check.Sen3Report=Join-Path $userOutput 'sen3-live-report.json'
        & $python -X utf8 (Join-Path $project 'scripts\check_sen3_reference.py') $check.Sen3Report
        if ($LASTEXITCODE -ne 0) { throw 'Independent Beijing geolocation, LST and flag comparison failed' }
        $check.Sen3PixelCheck=Join-Path $userOutput 'sen3-lst-geolocation-check.json'
    }
    if ($S5p -or $S5pAllNrti -or ($Live -and (Supports-PreviewFeature '1.5.0'))) {
        if (!(Supports-PreviewFeature '1.5.0')) { throw 'Sentinel-5P verification needs 1.5.0 or later' }
        $s5pArgs=@('--exe',$exe,'--output',$userOutput)
        if ($S5pAllNrti) { $s5pArgs+='--all-nrti' }
        & $python -X utf8 (Join-Path $project 'scripts\live_s5p.py') @s5pArgs
        if ($LASTEXITCODE -ne 0) { throw 'Installed Sentinel-5P original NetCDF verification failed' }
        $check.S5pReport=Join-Path $userOutput 's5p-live-report.json'
    }
    if ($Live) {
        $liveArgs=@('--exe',$exe,'--output',$userOutput,'--native-catalogue')
        if ($RadarMeasurement) { $liveArgs+='--radar-measurement' }
        & $python -X utf8 (Join-Path $project 'scripts\live_public.py') @liveArgs
        if ($LASTEXITCODE -ne 0) { throw 'Installed live public data smoke check failed' }
        $check.LiveReport=Join-Path $userOutput 'live-report.json'
        & $python -X utf8 (Join-Path $project 'scripts\check_reference_pixels.py') $check.LiveReport
        if ($LASTEXITCODE -ne 0) { throw 'Independent live COG pixel comparison failed' }
        $check.PixelCheck=Join-Path $userOutput 'pixel-reference-check.json'
        if (Supports-PreviewFeature '1.4.0') {
            $nativeArgs=@('--exe',$exe,'--output',$userOutput)
            if ($LandsatMeasurement) { $nativeArgs+='--landsat-measurement' }
            & $python -X utf8 (Join-Path $project 'scripts\live_open_native.py') @nativeArgs
            if ($LASTEXITCODE -ne 0) { throw 'Installed L1C and Landsat multi-file verification failed' }
            $check.OpenNativeReport=Join-Path $userOutput 'open-native-report.json'
        } elseif ($LandsatMeasurement) { throw '-LandsatMeasurement needs a 1.4.0 or later installer' }
    } elseif (!$S2Tiles -and !$S5p -and !$S5pAllNrti -and !$Sen3 -and !$Sen3FullRbt -and !$Sen3LargeFiles -and !$Rtc -and !$RtcEw) {
        $samplePath=Join-Path $userOutput '卸载不会删除此文件.txt'
        [System.IO.File]::WriteAllText($samplePath,'test data outside the app directory')
    }
    $appPref=Join-Path $userAppData 'CopernicusDownloader\preferences.json'
    $beforePreferences=(Get-FileHash -LiteralPath $appPref -Algorithm SHA256).Hash
    $beforeOutput=@(Get-ChildItem -LiteralPath $userOutput -Recurse -File | ForEach-Object {
        Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256
    })
    if (!$beforeOutput.Count) { throw 'No verification data files found' }
} finally {
    if ($desktop -and !$desktop.HasExited) { Stop-Process -Id $desktop.Id -ErrorAction SilentlyContinue }
    if ($null -eq $oldAppData) { Remove-Item Env:LOCALAPPDATA -ErrorAction SilentlyContinue } else { $env:LOCALAPPDATA=$oldAppData }
    if ($null -eq $oldUtf) { Remove-Item Env:PYTHONUTF8 -ErrorAction SilentlyContinue } else { $env:PYTHONUTF8=$oldUtf }
    if ((Test-Path -LiteralPath $registry) -and !$uninstaller) {
        $entry=Get-ItemProperty -LiteralPath $registry
        if ($entry.InstallLocation.TrimEnd('\') -eq $app) {
            $uninstaller=(Get-ChildItem -LiteralPath $app -File -Filter 'unins*.exe' | Select-Object -First 1).FullName
        }
    }
    if ($uninstaller) {
        $resolved=(Resolve-Path -LiteralPath $uninstaller).Path
        if ((Split-Path -LiteralPath $resolved) -ne $app -or !$resolved.StartsWith($root+'\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Unexpected uninstall target' }
        $uninstallOptions=@('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART',"/LOG=`"$uninstallLog`"")
        $process=Start-Process -FilePath $resolved -ArgumentList $uninstallOptions -WindowStyle Hidden -Wait -PassThru
        if ($process.ExitCode -ne 0) { throw "Uninstaller exit code $($process.ExitCode)" }
        $check.UninstallExitCode=$process.ExitCode
    }
}
if ((Test-Path -LiteralPath $registry) -or (Test-Path -LiteralPath $exe) -or (Test-Path -LiteralPath $startMenu)) { throw 'Uninstall left application files, registry, or Start menu entry' }
if ((Get-FileHash -LiteralPath $appPref -Algorithm SHA256).Hash -ne $beforePreferences) { throw 'Uninstall modified preferences' }
foreach ($item in $beforeOutput) {
    if ((Get-FileHash -LiteralPath $item.Path -Algorithm SHA256).Hash -ne $item.Hash) { throw 'Uninstall modified user output' }
}
$check.PreservedOutputFileCount=$beforeOutput.Count
$check.PreservedUserPreferences=$true
$check.PreservedUserOutput=$true
$check | ConvertTo-Json -Depth 4 | Tee-Object -FilePath (Join-Path $root 'installer-check.json')
