# 星图影像下载器 1.4.3

Windows 10/11 x64 桌面工具。按研究区和 UTC 日期检索影像与 NASA GEDI，可导出区域 GeoTIFF、GEDI 足迹参数与对齐栅格，也可保存指定的整瓦片、元数据或官方原始产品。可在绘制区域前登录所需平台，勾选附带来源后，主数据与附带数据共用研究区和 UTC 日期。GEDI 可跟随本次选中的主 GeoTIFF 网格，并附带采样其像元值。登录与保存规则见 [账号说明](docs/ACCOUNTS.md)，详细用法见 [联合下载](docs/BATCH.md) 和 [GEDI 参数与影像对齐](docs/GEDI.md)。

## 安装与首次试用

1. 从 [GitHub 稳定版发布页](https://github.com/yaluncoco/starmap-downloader/releases/tag/v1.4.3) 下载 `StarmapDownloader-Setup-1.4.3-win-x64.exe`；源码构建产物位于 `dist/`，也可直接运行 `dist/StarmapDownloader/StarmapDownloader.exe`。按当前用户安装，无需 Python 或管理员权限；需要 Microsoft Edge WebView2 Evergreen Runtime。安装包未签名，可对照同目录的 `.sha256` 文件核对完整性。卸载不删除另存的输出和配置。
2. 首次打开自动弹出「登录账号」，在同一窗口按需填写 NASA Earthdata、Sentinel Hub、CDSE OData / S3 的完整信息。登录成功后默认加密保存，下次启动自动恢复。可选择「暂不登录，选择数据」使用公开数据；未登录时，每次启动仍会弹出登录窗口。左侧只保留数据、日期、研究区和保存位置。再在「数据来源与下载方式」选择主路线：

   | 路线 | 文件 | 认证 |
   | --- | --- | --- |
   | Sentinel‑2 L2A · Sentinel Hub | 区域 GeoTIFF；可拼接同一次过境的瓦片 | 同一个 Sentinel Hub OAuth 客户端的 Client ID + Client Secret |
   | Sentinel‑2 L2A · 公开区域裁剪 | Earth Search Collection‑1；每个所选瓦片一个区域 GeoTIFF | 无需登录 |
   | Sentinel‑2 L2A · 整瓦片 COG | Earth Search Collection‑1；勾选源波段、分类、云雪、水汽、气溶胶、真彩或元数据；按 STAC SHA‑256 校验 | 无需登录 |
   | Sentinel‑2 L3 · 季度镶嵌 COG | CDSE 筛选与 Source Cooperative 镜像交叉核验；4 个 10 m 波段，保留整瓦片和 DN | 无需登录；镜像不匹配不下载 |
   | Sentinel‑2 L1C · 原始 JP2 | Earth Search / AWS；选取原始波段、瓦片级元数据，不裁剪 | 无需登录 |
   | Landsat Collection 2 Level‑2 | USGS / Microsoft Planetary Computer；原始反射率、温度、质量掩膜或元数据，不裁剪 | 程序取得公开临时读取授权，无需个人密钥 |
   | Sentinel‑3 · SLSTR / OLCI / SYNERGY / SRAL | MEEO/AWS 同版 SEN3：勾选原始 NetCDF 或下载完整 `.SEN3` 目录；不裁剪 | 无需登录 |
   | Sentinel‑5P TROPOMI L2 | CDSE STAC 按区域和 UTC 时段检索，MEEO/AWS 原始 NetCDF 条带；选大气变量及处理流，不裁剪 | 无需登录 |
   | Sentinel‑1 GRD · 公开雷达文件 | AWS 所列的测量 TIFF、标定、噪声和清单等文件；不裁剪，不重组 SAFE | 无需登录 |
   | Sentinel‑1 IW SLC · 东南亚历史存档 | ASF 目录筛选 + AWS 精确原始 SAFE ZIP；复数 I/Q、子波束和极化不变，不裁剪 | 无需登录；存档中没有同版 ZIP 时不替换 |
   | Sentinel‑1 RTC · 雷达区域 GeoTIFF | Planetary Computer 地形校正雷达；IW/EW、VV/VH/HH/HV，区域 Float32 线性 γ⁰ | 本版尝试取得临时读取授权；拒绝时停止，不更换数据来源 |
   | ESA WorldCover · Sentinel-2 年度合成 | 2020 / 2021：1° RGBNIR、NDVI p90/p50/p10、SWIR 约 20 m COG；不裁剪 | 无需登录 |
   | ESA WorldCover · Sentinel-1 年度 Gamma0 | 2020 / 2021：1° VV、VH、VH/VV COG；非单次过境 | 无需登录 |
   | ESA WorldCover · 土地覆盖分类 | 2020 v100 / 2021 v200：3° 分类瓦片，保留分类编码、NoData 和调色板 | 无需登录 |
   | NASA MODIS Collection 6.1（五类） | 13Q1 NDVI/EVI、09Q1 反射率、11A2 地表温度、15A2H LAI/FPAR、43A4 NBAR；整瓦片 HDF4、可用 XML 或分层 COG | 无需登录；程序自动申请短期只读授权 |
   | NASA GEDI · L1B / L2A / L2B / L3 / L4A / L4B / L4C | 明确选择 V003 或历史版本；原始 HDF5/GeoTIFF、足迹 CSV/GeoJSON、参数均值与观测数量栅格；可完整对齐已有影像网格 | 检索免登录；下载使用 NASA Earthdata 用户名密码或 User Token，首次可能需数据中心授权 |
   | Copernicus DEM GLO-30 / GLO-90 | 区域 GeoTIFF 或完整源瓦片，不改变完整瓦片网格 | 无需登录 |
   | Copernicus 官方目录 | 整包、可确定的单一数据主文件、元数据或指定资产；不裁剪 | 检索免登录；下载按计划使用 CDSE OData 账号/临时访问令牌，或 CDSE S3 Access Key + Secret Key |

3. 设置研究区：按地点搜索地图后画矩形/多边形；导入面状 SHP/GeoJSON；或输入 WGS84 经纬度。四点请沿边界顺序，每行 `经度,纬度`；矩形也可输入 `西,南,东,北`。SHP 的 `.shp/.shx/.dbf/.prj` 放在同一目录；没有 `.prj` 时手动填写源坐标系（例如 `EPSG:32650`）。可以把研究区另存为 SHP 或 GeoJSON。
4. 选择共同日期、类别和内容；需要其他数据时，在「附带下载」勾选来源并设置参数。一次检索后，在「主数据」和各附带来源页签分别勾选产品与原始文件，确认完整联合计划后下载。同一来源失败或无匹配会单独记录，已完成的文件保留。选「勾选多个文件」时，先单选一个产品，再勾选需要的波段和配套文件（最多 100 个）。文件筛选不清空已勾选项目。原始产品的研究区只参与检索；多个文件不等于完整产品包。各类 Secret、密码、S3 密钥与访问令牌默认以当前 Windows 用户绑定的方式加密保存，不写入普通配置、日志或下载记录；一次性验证码不保存。不要在聊天中提交这些凭据。

### 检查更新

点击右上角「检查更新」。配置了公开更新渠道的安装版只查询 GitHub 稳定版，不会在后台自动检查，不需要 GitHub 登录，也不会发送影像账号或密钥。有新版时，可打开发布页或点击「下载并校验」。校验通过后，在保存文件夹中找到安装包；先等影像任务结束并关闭软件，再手动安装。校验失败不会保存安装包，也不会替换上一次成功下载的文件。

当前稳定版为 1.4.3，可覆盖安装公开 1.4.2 / 1.4.1 / 1.1 以及本地 1.2.0 / 1.3.0 / 1.4.0。用过本地预览版的用户，可手动运行稳定版安装包覆盖安装。无更新渠道的本地开发构建会明确提示。版本变化见 [CHANGELOG.md](CHANGELOG.md)。本版测试范围与实传结果见 [验证记录](docs/VALIDATION.md)。

### Sentinel-2 研究分析包

两条 L2A 区域裁剪路线默认保存 **10 个常用波段＋SCL＋未掩膜备份＋逐像元来源**，附带下载也采用相同默认值。波段为 B02、B03、B04、B08、B05、B06、B07、B8A、B11、B12。主 TIFF 用于分析，`.unmasked.tif` 保留云区及负反射率，`.scl.tif` 和 `.source.tif` 供重新筛选与追溯，`.sources.json` 和 `.starmap.json` 保存来源及完整性记录；整组文件缺失时不会报告下载完整。

原有 RGB、RGB＋近红外、12 波段、NDVI 和自选模式继续可用。研究包仍是区域派生数据，20 米波段对齐至 10 米不增加源空间细节；完整角度、AOT/WVP、云概率和 SAFE 元数据请使用 **「下载完整 Sentinel-2 SAFE」** 入口。完整的文件、质量规则与来源限制见 [研究分析包说明](docs/S2_RESEARCH.md)。

### Sentinel-2 + GEDI 联合下载

选择 Sentinel-2 L2A 公开裁剪 →「附带下载」勾选 GEDI → 可先连接 Earthdata → 绘制或导入一片区域 → 选择共同日期 → 分别勾选影像和 GEDI 参数 → 确认联合计划。选择「跟随本次主 GeoTIFF」后，先完成选中的主影像，再读取实际网格导出 GEDI；有多景或多个原始 TIFF 时可选择具体参考。年度、季度与 GEDI 汇总产品保留完整周期含义，DEM 等静态产品仅共用区域。新图标用于 Windows 桌面与开始菜单快捷方式、窗口、EXE、安装包和界面。

### GEDI 快速开始

选择 NASA GEDI → L2A 高度或 L4A 生物量 → 设置研究区与实际观测日期 → 勾选参数 → 在顶部「登录账号」连接 Earthdata。需要与已有影像配对时，选择「对齐已有 GeoTIFF」，可附带读取各足迹所在的影像像元。约 25 米离散足迹不会填满细网格；无观测处保留 NoData。

公开目录与本机实传检查分别使用：

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts\live_gedi.py
# 私密交互输入令牌，只下载一个精确文件，再独立核对校验值、观测数量与像元均值。
.\.venv\Scripts\python.exe -X utf8 scripts\live_gedi.py --download
# 通过打包程序检查真实 NASA 目录；不会输入凭据或下载受保护文件。
.\.venv\Scripts\python.exe -X utf8 scripts\live_gedi.py --exe .\dist\StarmapDownloader\StarmapDownloader.exe
```

没有 Earthdata 凭据时，只验证公共目录；不要把目录结果或模拟测试当作认证实传结果。完整产品、字段、质量和垂直基准说明见 [GEDI.md](docs/GEDI.md)。

### 不用密钥的验证样例

- 北京研究区：`116.350,39.900,116.355,39.905`。日期设为 **2024-06-10 至 2024-06-10（UTC）**，选择 **Sentinel-2 L2A · 公开区域裁剪**，云量上限 **100%**、分辨率 **10 米**、自选 **B04**，保留去云勾选。核对 `S2A_MSIL2A_20240610T030531_N0510_R075_T50TMK_20240610T081949.SAFE`。输出 `44 × 57`，其中 `2,337` 个有效像元。
- 同一区域与同一天选择 **Sentinel-2 L1C · 原始 JP2**，可勾选 B01、B10 和 `metadata.xml`；这是大气顶 L1C，不是 L2A 地表反射率。L1C 原 JP2 保持源 DN，不用区域裁剪的比例因子自动转换。
- 同一区域，日期 **2024-06-11（UTC）**，选择 **Landsat Collection 2 L2**，核对 `LC08_L2SP_123032_20240611_20240628_02_T1`。可选 `red`、`qa_pixel` 与 `mtl.json` 一并保存；原始 DN 和 QA 位标识保持不变，科学分析时按所选产品元数据处理。
- 同一区域选 **Copernicus DEM 30 米 / 90 米**。不设置日期，选裁剪或完整瓦片。DSM 表示含植被/建筑在内的数字表面模型，不是裸地 DTM；海域可能无瓦片。
- 北京同一区域选 **Sentinel-1 GRD · 公开雷达文件**，日期 **2024-06-12（UTC）**，模式 IW。选「每个产品的一份元数据」可下载产品号末尾为 `BEF9` 的 `manifest.safe`。如需复现测量 TIFF 下载，可使用下方 `-RadarMeasurement`，测试样例约 132 MiB。

### Sentinel‑1 RTC 区域裁剪

- 北京同一区域 `116.350,39.900,116.355,39.905`，日期 **2024-06-12（UTC）**，选 **Sentinel‑1 RTC · 雷达区域 GeoTIFF**、IW、VV 和 VH、10 米。选择来源版本末尾为 `9D5E` 的景，而非上面 GRD 原件的 `BEF9`。两个波段的输出网格为 44 × 57，均有 2,337 个有效像元。
- RTC 每个所选景单独裁剪，不跨景拼接。批量导出请选各景共有的极化；如没有共有极化，按景分批。输出是 **线性 γ⁰ 后向散射**，不是 dB 或原始 GRD DN。每个波段保留自己的 NoData（`-32768`），不应用光学云掩膜。10／20／40／60 米是输出像元大小，不代表实际源分辨率。
- 软件只读取所需字节区间，要求区间、总长、强 ETag 与 Azure 区间 MD5 均匹配；输出记录包含未签名对象路径、精确源版本、每波段校验区间数和本地 SHA‑256。这不是整景源对象 MD5 校验。Planetary Computer 的临时授权及数据可用性由服务决定，不能保证任何会话均能访问，亦不保证 EW 全域覆盖。

### Sentinel‑2 整瓦片与季度镶嵌

- **L2A（单次过境）**：例如 `15.60,78.22,15.67,78.25`、**2024-06-05（UTC）**，将整景云量上限设为 100%（此样本用于校验，不是晴空影像），选择 `S2B_T33XWG_20240605T130710_L2A` 对应的 `T33XWG` 瓦片。此样本有 21 个文件（12 个反射率波段、SCL、AOT、WVP、CLD、SNW、三通道 TCI、三份元数据）。有效文件按目录供应，非每景保证 21 个。保留 10/20/60 米源网格以及原始像元、缩放和偏移；0 为该公开来源的 NoData。影像只覆盖瓦片的一部分时，仍保留整瓦片的 NoData。此路线不是 SAFE 包，也不是「公开区域裁剪」。
- **L3（季度范围而非单日过境）**：例如北京 `116.350,39.900,116.355,39.905`、**2024-06-10（UTC）**，结果为 2024 Q2（2024-04-01 至 2024-06-30）的 `50TMK` 镶嵌。提供 B02/B03/B04/B08，不含官方包的观测次数栅格。GeoTIFF 原始 Int16：有效 DN × `0.0001` = 反射率，`-32768` 为 NoData；缩放系数未写在源 TIFF 中，下载不改写文件。
- **镜像边界**：目录与镜像需要同一产品、季度、网格与各波段大小。镜像文档覆盖 2017 Q1–2025 Q4；这不是逐季度、逐瓦片下载保证。2026 Q2 北京样本的 CDSE 记录可查，但验收时缺少同版镜像 STAC 索引，界面标为不可下载，不用其他季度或瓦片代替。L3 对象通过长度、强 ETag、栅格全块解码与本地 SHA‑256 校验；没有官方逐文件摘要的对象不声称字节与 CDSE 完全相同。Multipart ETag 不是整文件 MD5。

### ESA WorldCover 年度整瓦片

- 试用：研究区 `-109.42,-27.16,-109.39,-27.13`（复活节岛），日期 **2021-06-15 至 2021-06-15**。选择「Sentinel-2 年度合成」，单选 `S28W110` 瓦片，勾选 RGBNIR、NDVI、SWIR。匹配的是 **2021 全年**，不是 6 月 15 日的过境；该位置的 Sentinel-1 年度合成缺失时会明确显示不可用。
- 研究区只选择整瓦片，不裁剪。S1 和分类图可批量勾选多个年度瓦片。分类图 2020 和 2021 使用不同的算法版本，直接比较有把算法差异误判为变化的风险。
- 输出保持原始编码和 TIFF 缩放。例如 NDVI 有效像元 `DN × 0.008 − 1`，NoData 为 `255`；SWIR 约 20 米。写入最终位置前核对年份、图层、网格、字节数、源 ETag 和栅格像元块。同名已有文件发现损坏时不覆盖；请先移出 TIFF 及同名 `.starmap.json`，再重新下载。
- 数据来源：[ESA WorldCover](https://esa-worldcover.org/en/data-access)，CC BY 4.0；引用时保留项目和数据贡献者署名。

### NASA MODIS Collection 6.1 · 整瓦片

1. 在「数据来源与下载方式」选择五个 MODIS 入口之一，再设置地图边界、导入 SHP/GeoJSON 或输入坐标。
2. 开始和结束日期都包含，只按 **UTC 标称日** 筛选，而不是「合成周期有重叠就纳入」。13Q1/09Q1/11A2/15A2H 按合成开始日；逐日发布的 MCD43A4 按 16 天加权窗口中的第 9 天。例如 `A2024182` 标称 **2024-06-30**，核对的 HDF4/COG 中的观测窗口为 **2024-06-22 至 2024-07-07**；目录与 XML 可能从标称日开始。文件记录将三种时间分开，不将 STAC 中点当作采集日。
3. 可批量选产品保存各自的 HDF4；若要指定一个或多个层，请单选同一个产品，再勾选 COG、HDF4 或目录已列出的 XML。配套 XML 缺失时不会构造替代文件。产品身份包含 Terra/Aqua、MODLAND `hXXvYY`、版本 `061`、标称日和精确文件名。研究区仅选择与原始瓦片相交的产品，不裁剪，也不保证每个像元有效。
4. 原始文件不转格式、不重写。下载记录含 Azure 文件版本、字节数、源 MD5、本地 SHA‑256；COG 还逐块完整解码并核对真实正弦网格、分层标识和观测日期。HDF4 若有目录配套 XML，另对照 NASA 发布的长度和 MD5；没有 XML 时只验证源字节、HDF4 文件头和精确对象身份，软件不解码 HDF4 的 SDS。

**保留原始 DN 与标签。** 13Q1 的 NDVI、EVI 和反射率有效 DN 乘 `0.0001`，角度乘 `0.01`；不要乘源 TIFF 的 `10000` 或 `100`。COG 的 `pixel_reliability=255` 是 HDF4 的 `-1` 缺测；13Q1 其他层不能套用这条规则。11A2 的 `QC_Day`/`QC_Night` 应以原始整数读取，并结合对应 LST 核验；TIFF 标为 NoData 的 `0` 可能是有效 QA。11A2 温度为 `DN × 0.02` K（先排除 LST 自身的 NoData）。每个层的物理系数与标记分开写入 JSON。

来源是 Microsoft Planetary Computer 的公开 NASA MODIS C6.1 档案，不使用 CDSE 或个人密钥；临时授权不可用、源对象缺失或校验失败时停止当前文件，不切换日期、瓦片、卫星或版本。

### CDSE 官方原始产品

- 类别选择器实时读取 CDSE STAC，可按中文分组、英文名称或集合 ID 筛选。包含 Sentinel‑1 多种雷达产品、Sentinel‑2 L1C/L2A 和合成产品、Sentinel‑3 OLCI/SLSTR、Sentinel‑5P、Sentinel‑6、Landsat、CLMS、MODIS、Copernicus DEM 等。**有目录结果不代表有可用下载入口、账户权限或区域裁剪支持。**
- 自动通道：对同一个所选产品，按目录优先使用 OData；无 OData 入口时使用满足身份核验的 S3 对象。也可选「仅 CDSE OData」或「仅 CDSE S3」。这是规划规则，不会在传输错误后静默换通道，更不会换成另一产品。确认计划会列出实际所需凭据。
- S3 密钥从界面里的 **CDSE S3 Keys Manager** 取得，与 Sentinel Hub OAuth 客户端无关。点击载入只代表密钥格式通过检查；有效期及数据权限要在真实下载时验证。
- S3 的 `AccessDenied` 表示所选对象访问被拒绝，程序会区分签名不匹配、Access Key 失效和系统时间问题。同一密钥可访问 Sentinel-2，但仍可能被某个 DEM 对象拒绝；有目录记录不保证各对象都获授权。
- 多对象只列为可选资产，不会猜测整包。原始 ZIP 大小以实际响应为准。区域裁剪支持公开 Sentinel‑2 L2A、公开 DEM、Sentinel‑1 RTC（临时读取授权）和 Sentinel Hub L2A；AOI 跨度超过 8°或跨 180° 经线请分区，UTM 区域导出支持 80°S～84°N。

## Sentinel‑3 原始产品

- 可选 **11 个产品／处理流组合**：SLSTR LST（NTC、NRT）、RBT（NTC）、AOD（NRT）、FRP（NTC）、WST 海表温度（NTC）；OLCI EFR（NTC）、LFR（NTC）、WFR（NRT）；SYNERGY AOD（NTC）；SRAL SRA‑A 高度计测量（NTC）。研究区仅筛选覆盖范围，保留源观测网格和量化值。NTC 为非实时产品，NRT 为近实时产品。
- 北京样例：`116.350,39.900,116.355,39.905`，开始和结束日期均设为 **2024-03-05 UTC**，选 **SLSTR L2 地表温度 LST · NTC**，勾选轨道 `30526` 的 `S3B_SL_2_LST…PS2_O_NT_004.SEN3`。单选该产品后，可勾选温度 `LST_in.nc`、经纬度 `geodetic_in.nc`、质量标记 `flags_in.nc` 和时间 `time_in.nc`；也可选「完整 SEN3」，保存原始 XFDU 清单及其声明的 11 个文件。
- 试用新增类别时，下面的日期同时填开始和结束。多个目录结果中按末尾的处理版本选精确样本：

  | 类别 | WGS84 矩形（西,南,东,北） | UTC 日期 | 样本版本末尾 |
  | --- | --- | --- | --- |
  | SYNERGY · L2 AOD · NTC | `136.42569,-11.25161,136.44569,-11.23161` | 2026-01-07 | `2637_134_330______PS1_O_NT_002.SEN3` |
  | SLSTR · L2 WST · NTC | `114.54271,5.43159,114.56271,5.45159` | 2024-02-28 | `6059_109_273______MAR_O_NT_003.SEN3` |
  | SRAL · L1A SRA‑A · NTC | `-37.10585,0.60653,-37.08585,0.62653` | 2024-03-05 | `3029_090_223______PS2_O_NT_005.SEN3` |
- 精确版本是否公开按**每个产品**显示。较长日期范围请拆分（单次最多核对 48 个结果）。找不到同版时，不下载其他过境、处理流或版本；可切换到「Copernicus 官方目录」检查有凭据的官方下载路线。「仅部分可用」只能选已核实的成员，不能下载完整 SEN3。
- 「完整 SEN3」仅指原始 `xfdumanifest.xml` 与全部声明成员，不包括独立预览图或其他目录附加资产。先验证清单，多成员产品最多同时传输 4 个成员；所有成员的摘要及代表性数组通过校验后，才一次性发布该产品目录。每个成员用清单长度和 MD5 校验，有 CDSE 文件级摘要时再核对一次；Multipart ETag 只用来锁定对象版本。任一成员失败或取消时，未完成的产品不发布，已完成的其他产品不删除。
- WST 原件没有通用的 `product_name` 字段，必须同时匹配 CDSE 与 XFDU 的 MD5，并核对 GHRSST 文件名、卫星、观测时段和处理版本；不会因此降低其他产品的身份校验要求。WST 与 SRAL 样本可达数百 MB 或数 GB，请先看计划中的大小并预留磁盘空间。
- 选下载部分文件时保留原名，但存入 `_selected_members` 目录。已有结果复核失败时请把产品目录和同名 `.starmap.json` 移走后重试，工具不会覆盖。

## Sentinel‑1 IW SLC 原始 ZIP（东南亚历史存档）

- 日期按**采集开始的 UTC 日**筛选；研究区只与原产品覆盖相交，不代表每个像元都有效。存档日期范围 **2014-10-06 至 2024-04-04**，不保证日期连续、区域完整或全球覆盖。单次最多查 62 个存档日；ASF 有目录而 AWS 没有同名同大小 ZIP 时，不加入下载结果。
- 选择「Sentinel-1 IW SLC · 东南亚历史 SAFE ZIP」，输入 `123.70,-8.145,123.74,-8.10`、两端均填 **2014-11-01**，可查 VV 样本 `S1A_IW_SLC__1SSV_20141101T100821_20141101T100834_003086_00388F_357C`（966,228,993 字节）。VV+VH 样本用 `125.03,-4.89,125.055,-4.86`、**2023-09-10**（1,536,693,925 字节）。
- 保存同名原始 ZIP，不解包、不裁剪、不转换强度。每文件最多 4 条并行连接，按固定版本传输 32 MiB 区间；中断时只补当前区间的剩余字节。不跨程序重启保留未完成文件。发布前比对 ASF 全文件 MD5、SAFE 采集身份、每个 ZIP 文件的 CRC、清单成员的 MD5，以及 IW1/IW2/IW3 的 CInt16 复数测量和配套注释。下载记录同时写入本地 SHA‑256；已有包复核失败不会覆盖。

## Sentinel‑5P 原始条带

- 可选 NO₂、CH₄、CO、SO₂、HCHO、总柱 O₃、紫外气溶胶指数、气溶胶层高度和云参数；处理流为 OFFL（离线）、NRTI（近实时）和 RPRO（再处理）。例如：北京坐标 `116.350,39.900,116.355,39.905`，两端日期均为 **2024-06-10 UTC**，选 **CH₄ · OFFL**。
- 以 CDSE 条带的观测覆盖区和时段筛选，不是浓度图裁剪。NRTI 样本约 5 分钟，不等于整轨。多选按条带保存；不会重投影、重新打包或代为应用 QA 筛选。覆盖相交不保证研究区内有有效观测。
- 软件核对 CDSE 与 MEEO 的完整原始文件名、大小及可校验摘要；在公开索引找不到同一处理版本时，该行不可选。不同日期、变量和处理流并不都公开；找不到时可改回「Copernicus 官方目录」按需认证，不自动换版本。

## 如何判断下载正确

- 区域输出检查网格、坐标系、波段类型、掩膜与有效像元，并记录源产品及波段。通常不发布全 NoData 影像；研究包主影像全云但存在未掩膜光谱时，仍保存整组文件。公开裁剪按所需字节范围读取，比较 ETag、区间和总长；使用最近邻，浮点反射率应用 STAC scale/offset。自然色为 8 位 RGBA；反射率、NDVI 与 DSM 为 Float32，NoData `-9999`。输出网格间距不等于源分辨率。
- OData 核对产品 UUID 与名称。S3 签名限定官方对象地址；续传必须满足区间、长度与版本，S3 使用 `If-Match`，强 ETag 不可用时重下当前文件。拒绝不可信跳转，不关闭 TLS 验证。ZIP 检查结构、成员与 CRC；原始 TIFF/JP2 解码像元块，公开 DEM 另核对整瓦片位置与网格。AWS 雷达 SAFE 清单还核对采集身份和产品编号中的 CRC‑16。公开 Sentinel‑5P NetCDF 除 CDSE 的全文件摘要外，还核对产品号、轨道、观测起止和维度，并完整解码经纬度、`qa_value` 和所选变量的主要数组；不宣称解码了全部辅助变量或验证了科学精度。Sentinel‑3 校验 SEN3 清单身份和成员摘要，按产品核对 NetCDF 内嵌产品号或 WST 专用身份字段，并完整解码报告所列的代表数组。其余通用 NetCDF/HDF 原始路线仍仅核对容器头、长度及可用的提供方校验值。
- 下载记录区分提供方校验值和本地 SHA‑256。适用时验证目录多哈希编码的 SHA‑256／SHA3‑256／MD5，AWS 单部分对象的 ETag/MD5，或 Azure 发布的对象 MD5；缺少提供方校验值时不声称已做全文件源散列匹配。L1C 额外检查 AWS 瓦片索引里的 SAFE 标识、时刻和路径，并在传输后确认索引 ETag 未变。Landsat 按精确处理版本绑定文件，MTL JSON 核对产品编号和 WRS 轨道。已有文件通过大小、来源、本地 SHA‑256 及可用的源摘要验证才跳过；重复下载记录保留原件大小、ETag、提供方摘要结果、格式验证信息和原校验记录引用。发现损坏请移走文件和对应 `.starmap.json` 后重试。不跨软件重启保留未完成下载；取消时已完成的文件不删除。
- Sentinel Hub 的 `safe_id` 是完整产品标识匹配，`mgrs_and_utc` 是瓦片与采集秒唯一匹配，后者不能证明提供方处理版本相同。云量是整瓦片估计；目录交叠比例不代表裁剪后有多少有效像元。

## 源码、构建和复测

用 PowerShell 7 执行。公开发布请按[发布步骤](docs/RELEASING.md)在构建时传入 GitHub 仓库；不传 `-UpdateRepository` 会构建无更新渠道的测试包。第三方来源见[声明](THIRD_PARTY_NOTICES.md)。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -X utf8 -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -X utf8 app.py
.\.venv\Scripts\python.exe -X utf8 -m pytest -q

# 使用可信的 Inno Setup 6 编译器，传入 ISCC.exe 的实际完整路径
.\scripts\build_installer.ps1 -Compiler 'C:\Tools\Inno Setup 6\ISCC.exe'
# 同一 SHP 和 UTC 日期检索 Sentinel-2、GEDI、DEM，并实传公开 Sentinel-2 + DEM。
.\.venv\Scripts\python.exe -X utf8 scripts\live_batch.py --exe .\dist\StarmapDownloader\StarmapDownloader.exe --output .\verification\batch\packaged-live
# 安装后复测联合下载；需在干净测试用户中运行。
.\scripts\check_installer.ps1 -Installer .\dist\StarmapDownloader-Setup-1.4.3-win-x64.exe -Batch
# 可选：在干净测试用户中验证从上版升级、保留输出设置以及卸载。
# -Slc 实传 VV 样本；同时加 -SlcDual 会再实传 VV+VH 样本（共约 2.50 GB）。
.\scripts\check_installer.ps1 -Installer .\dist\StarmapDownloader-Setup-1.4.3-win-x64.exe -Slc -SlcDual -ModisSmoke
# 若有旧的本地预览版 1.12.0，复测编号重置下的覆盖安装时加 -ReplacePreview -UpgradeFrom 'C:\备份\StarmapDownloader-Setup-1.12.0-win-x64.exe'。
# 独立 MODIS 参考需要可选 pyhdf；--fetch 下载缺失的已锁定的源对象。
.\.venv\Scripts\python.exe -X utf8 scripts\check_modis_reference.py .\verification\modis-next-source --fetch
.\scripts\check_installer.ps1 -Installer .\dist\StarmapDownloader-Setup-1.4.3-win-x64.exe -Modis
# 安装后复测 ESA WorldCover 三类数据、独立源文件、真实缺层、损坏重下与界面。
.\scripts\check_installer.ps1 -Installer .\dist\StarmapDownloader-Setup-1.4.3-win-x64.exe -WorldCover
# 安装后复测 L2A 整瓦片和 L3 季度镶嵌，再验证卸载保留。
.\scripts\check_installer.ps1 -Installer .\dist\StarmapDownloader-Setup-1.4.3-win-x64.exe -S2Tiles
# 安装、复测 RTC 的 IW 两组极化 + 覆盖边缘；-RtcEw 还要求一份指定 EW 样本。
.\scripts\check_installer.ps1 -Installer .\dist\StarmapDownloader-Setup-1.4.3-win-x64.exe -RtcEw
# 雷达测量 TIFF、Landsat 红波段是可选大样本；-Live 也运行新增光学路线、RTC IW 和整 DEM
.\scripts\check_installer.ps1 -Installer .\dist\StarmapDownloader-Setup-1.4.3-win-x64.exe -Live -RadarMeasurement -LandsatMeasurement -S5pAllNrti
# 复测 9 类 Sentinel-3 样本、3 条 Sentinel-5P 处理流和卸载保留
.\scripts\check_installer.ps1 -Installer .\dist\StarmapDownloader-Setup-1.4.3-win-x64.exe -Sen3 -S5p
# -Sen3LargeFiles 也实传 WST（约 647 MB）与 SRAL（约 2.52 GB）。
# -Sen3FullRbt 覆盖所有 11 类样本，并另外验证 RBT 的全部 98 个文件。两种模式都需要较长时间。
.\scripts\check_installer.ps1 -Installer .\dist\StarmapDownloader-Setup-1.4.3-win-x64.exe -Sen3FullRbt
```

本机已保存账号的 Sentinel-2 完整性可用以下脚本验证。它使用北京 2024-06-10 的同一 50TMK 产品，保存并恢复输出设置；运行时请关闭其他软件会话。`--source-zip` 可复用已下载的同一官方完整 SAFE，`--regional-only` 只测试两条区域路线、完整包对照和真实云区，不重新测试 S3 / GEDI。缺省不加这两个参数时，还会使用保存账号实传完整 SAFE、S3 与 GEDI。各平台的失败分别记录，凭据不会写入报告。

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts\live_completeness.py --exe 'C:\实际安装路径\StarmapDownloader.exe' --research --source-zip 'D:\已保存的同一完整产品.zip' --regional-only --output-dir .\verification\completeness-new
```

单独核对保存的 CDSE S3 密钥和 Sentinel-2 原件，可运行下列专项脚本，避免先重复其他平台的大文件传输。样本仍为上述精确 SAFE；默认检查十个研究波段、SCL、产品/瓦片 XML 和清单。`--extended` 增加 B01/B09、AOT/WVP、云雪概率和另外两份元数据，共 22 个官方目录文件。脚本将每个文件与完整 SAFE 中的同名成员比较 SHA-256，并解码全部原生 JP2 像元块；这些选定文件不代表完整 SAFE 的全部成员。省略 `--require-transfer` 可复核已有缓存。

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts\live_s3.py --exe 'C:\实际安装路径\StarmapDownloader.exe' --source-zip 'D:\已保存的同一完整产品.zip' --extended --require-transfer --require-cache-evidence --output-dir .\verification\s3-completeness-new
```

本地 `verification/` 保存安装和实传测试产物，不随源码提交。目录检索或模拟测试通过不代表实下载链路已验收。Playwright 页面测试需要 Edge，Evalscript 测试需要 Node.js。已安装二进制的认证链路可用独立的本机交互脚本复测。传入**安装后的程序**（或同版打包目录中的程序），不是安装器。每个模式使用新输出目录，不要把凭据放进命令行、脚本或聊天：

```powershell
$exe = 'C:\实际安装路径\StarmapDownloader.exe'
# 加 --preflight 只核对缺凭据处理；不输入密钥，也不尝试认证下载。
.\.venv\Scripts\python.exe -X utf8 scripts\live_auth_packaged.py --exe $exe --service s3 --preflight --output-dir .\verification\preflight-s3-new
.\.venv\Scripts\python.exe -X utf8 scripts\live_auth_packaged.py --exe $exe --service process --output-dir .\verification\private-process-new
.\.venv\Scripts\python.exe -X utf8 scripts\live_auth_packaged.py --exe $exe --service odata --odata-token --output-dir .\verification\private-odata-new
.\.venv\Scripts\python.exe -X utf8 scripts\live_auth_packaged.py --exe $exe --service s3 --output-dir .\verification\private-s3-new
# 仅复测 SRAL 大文件：指定新输出目录及更长的下载时限（秒）。
.\.venv\Scripts\python.exe -X utf8 scripts\live_sen3.py --exe $exe --only sr-1-sra-a-ntc --download-timeout 7200 --output .\verification\sral-retest-new
```

单独复测一份 RTC 产品和覆盖边缘（命令行支持 `--only`，可省略 `--exe` 测试源码）：

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts\live_rtc.py --exe $exe --only beijing-iw-edge --output .\verification\rtc-edge-retest-new
.\.venv\Scripts\python.exe -X utf8 scripts\live_rtc.py --exe $exe --only svalbard-iw-cross-zone --output .\verification\rtc-cross-zone-retest-new
```

## 官方来源

- [CDSE STAC](https://documentation.dataspace.copernicus.eu/APIs/STAC.html) · [CDSE OData](https://documentation.dataspace.copernicus.eu/APIs/OData.html) · [CDSE Access Token](https://documentation.dataspace.copernicus.eu/APIs/Token.html) · [CDSE S3](https://documentation.dataspace.copernicus.eu/APIs/S3.html)
- [Sentinel Hub OAuth](https://documentation.dataspace.copernicus.eu/APIs/SentinelHub/Overview/Authentication.html) · [Sentinel‑2 L2A Process](https://documentation.dataspace.copernicus.eu/APIs/SentinelHub/Process/Examples/S2L2A.html)
- [CDSE Sentinel‑2 季度镶嵌算法](https://documentation.dataspace.copernicus.eu/Data/Others/Sentinel2_Mosaic_Algorithm.html) · [Source Cooperative 季度数据镜像](https://data.source.coop/tge-labs/sentinel-2-quarterly-cloudless-mosaics/README.md)
- [Earth Search](https://github.com/Element84/earth-search) · [AWS Copernicus DEM](https://registry.opendata.aws/copernicus-dem/) · [Microsoft Edge WebView2](https://developer.microsoft.com/microsoft-edge/webview2/)
- [Sentinel‑3 产品说明](https://sentiwiki.copernicus.eu/web/s3-products) · [MEEO / AWS Sentinel‑3 档案](https://registry.opendata.aws/sentinel-3/)
- [Sentinel‑5P 产品说明](https://sentiwiki.copernicus.eu/web/s5p-products) · [MEEO / AWS Sentinel‑5P Level 2 原始 NetCDF 档案](https://registry.opendata.aws/sentinel5p/)
- [USGS Landsat Collection 2 Level‑2](https://www.usgs.gov/landsat-missions/landsat-collection-2-level-2-science-products) · [Planetary Computer Landsat 数据集](https://planetarycomputer.microsoft.com/dataset/landsat-c2-l2) · [STAC File 扩展](https://github.com/stac-extensions/file)
- [Planetary Computer Sentinel‑1 RTC STAC 集合](https://planetarycomputer.microsoft.com/api/stac/v1/collections/sentinel-1-rtc) · [Azure Get Blob 字节区间与校验值](https://learn.microsoft.com/en-us/rest/api/storageservices/get-blob)
