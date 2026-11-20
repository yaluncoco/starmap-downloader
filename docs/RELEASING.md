# 发布 Windows 稳定版

发布前确认 GitHub `owner/repo` 和可见性。仅**公开 Release** 可由安装版匿名检查；私有仓库分发请走 GitHub 自身的登录下载，不要在程序或构建配置中嵌入凭据。已打包的更新渠道绑定到指定的仓库，换仓库后需要重新打包。

在干净的 PowerShell 7 环境使用可信的 Python、Inno Setup 编译器和本项目的 `.venv`。将示例中的 `OWNER/REPO`、编译器和基线安装包路径换成实际值。

```powershell
$repo = 'OWNER/REPO'
$version = .\.venv\Scripts\python.exe -X utf8 -c 'from core import VERSION; print(VERSION)'
$tag = "v$version"
$asset = ".\dist\StarmapDownloader-Setup-$version-win-x64.exe"
.\.venv\Scripts\python.exe -X utf8 -m pytest -q
.\scripts\build_installer.ps1 -Compiler 'C:\Inno Setup 6\ISCC.exe' -UpdateRepository $repo
.\scripts\check_installer.ps1 -Installer $asset -UpgradeFrom 'C:\备份\上一版完整安装包.exe' -UpdateRepository $repo
```

1.4.1 的构建脚本会强制检查 GEDI、联合下载、统一账号界面与图标资源（`--require-gedi --require-batch --require-accounts`）。打包后还应验证同一区域与共同日期的真实联网流程：

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts\check_package.py .\dist\StarmapDownloader\StarmapDownloader.exe --require-version $version --require-gedi --require-batch --require-accounts --require-update-repository $repo
.\.venv\Scripts\python.exe -X utf8 scripts\live_batch.py --exe .\dist\StarmapDownloader\StarmapDownloader.exe --output .\verification\batch\packaged-live
```

`live_batch.py` 使用同一 SHP 和 UTC 日期检索 Sentinel-2、GEDI 与 DEM，检查缺少 Earthdata 连接时整批下载会被阻止，再实际下载公开 Sentinel-2 和 DEM 并核对像元、网格及校验记录。它不执行受保护的 NASA 下载；该部分需用户在本机界面完成 Earthdata 认证后另行验证。安装回归也可加 `-Batch` 运行同一联网检查；已安装正式版时继续使用独立测试用户，避免影响正式安装。

首个公开版本为 `v1.1`。若从旧的本地预览版回归，预览版本号可能大于公开版，测试命令需另外指定 `-ReplacePreview`。正常发布不要使用此参数。

1. 提交前检查暂存内容，只保留源码、说明、测试和必要夹具。不要提交凭据、用户配置、影像或构建输出。若本机已安装正式版，请在另外的测试用户下运行安装回归。
2. 发布与安装包相同版本的源代码标签。仓库分支设为 `main`、远端设为 `origin` 的情况下，可以用 `git push -u origin main` 及 `git push origin $tag`。发布前核对远端标签引用的是本次提交。
3. 先创建 GitHub Release 草稿，上传 `$asset` 和 `$asset.sha256`，再核对安装包名称、大小和 GitHub 记录的 `sha256:…` 摘要。正文用 `CHANGELOG.md` 中对应小节；正式发布前不要标成 pre-release。建议为仓库启用不可变发布，并将这一版标为 **Latest**。不要用同一个已发布的版本号替换二进制，有修订就升补丁版本号。
4. 发布后从未登录 GitHub 的已安装程序点击「检查更新」。本版应显示「当前已是最新稳定版」；测试旧版可检索的升级时，应显示新版版本号且不自动下载。也可用以下命令冒烟验证公开 API 与实际安装版：

```powershell
.\scripts\check_installer.ps1 -Installer $asset -UpdateRepository $repo -ExpectUpdateStatus current
```

安装包必须命名为 `StarmapDownloader-Setup-<版本>-win-x64.exe`，标签使用 `v<主版本>.<次版本>` 或 `v<主版本>.<次版本>.<补丁>`；省略补丁按 0 比较，但安装包名称必须与标签去掉 `v` 后完全一致。校验文件用于用户手工比对；应用内从 GitHub REST API 获取安装包的大小、资产 ID 与 SHA-256，不会将同名的校验文本当作可信摘要回退。
