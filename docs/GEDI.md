# GEDI 下载与影像对齐

在「数据来源与下载方式」选择 **NASA GEDI**。研究区、UTC 日期、输出目录、任务进度和取消操作与现有影像功能共用。

GEDI 数据免费。**目录检索不需要登录，受保护的原始文件下载需要 NASA Earthdata 认证。** Copernicus 的账号、OAuth Client Secret 和 S3 密钥不能用来下载 GEDI。

## 连接 Earthdata

1. 在右上角「登录账号」选择 NASA Earthdata。没有账号时，点击「免费注册」，在 [Earthdata Login](https://urs.earthdata.nasa.gov/users/new) 注册。
2. 推荐在 Earthdata 官网的个人资料页生成 **User Token**，在本软件顶部「登录账号」的 NASA Earthdata 页签中粘贴并连接。也可填写 Earthdata **用户名**和密码；用户名通常不是邮箱。联合登录或多因素认证账号可使用官网生成的令牌。
3. 首次下载如果被要求授权数据中心，在软件中打开「所选 NASA 产品说明」，进入 Earthdata Search，以该账号完成首次下载/授权。L1B/L2A/L2B 使用 **LP DAAC**，L3/L4A/L4B/L4C 使用 **ORNL DAAC**。
4. 登录成功后默认加密保存所选用户名密码或 User Token，下次打开自动恢复。普通配置、CSV、GeoJSON、下载记录、接口返回和错误提示均不写入密码或令牌；可在同一窗口「退出并删除」。详见 [账号说明](ACCOUNTS.md)。

「已载入」表示软件接收了令牌；数据中心会在实际下载时验证权限。令牌过期、首次授权未完成或网络异常时会明确报错。

## 产品与参数

以 2026-11-20 核验的 NASA CMR 官方目录为准。每个类别都固定到明确的集合与版本，不用其他版本填补缺失结果。

| 类别 | 提供的版本 | 主要内容 | 软件导出 |
| --- | --- | --- | --- |
| L1B | V003 / V002 | 定位、发射与接收波形 | 原始 HDF5；足迹位置、窗口高程、采样数；可选完整波形数组 |
| L2A | V003 / V002 | 地面高程与相对高度 RH | RH25/50/75/90/95/98/100、最低模态与最高回波椭球高、敏感度、完整 RH 剖面、geoid 与可选 EGM2008 高程 |
| L2B | V003 / V002 | 冠层覆盖与垂直结构 | cover、PAI、FHD、敏感度、cover_z、pai_z、pavd_z 剖面 |
| L3 | V3 / V2 | 约 1 km 地表高程、冠层高度与观测数量 | 选择目录所列的原始参数栅格，或对齐导出 |
| L4A | V3 / V2.1 | 足迹地上生物量密度 | AGBD、预测标准误和预测区间，单位 Mg/ha |
| L4B | V2.1 | 约 1 km 生物量与不确定性 | 选择目录所列的原始参数栅格，或对齐导出 |
| L4C | V3 / V2 | 波形结构复杂度 | WSCI 及其预测区间 |

原始 HDF5 保留产品中的全部变量；参数勾选只决定本软件的派生导出。完整波形和剖面是数组，保存在 CSV/GeoJSON 中，不能当作一个标量波段直接平均。

## 三种下载方式

- **仅原始文件**：保留 NASA 原始 HDF5/GeoTIFF，研究区和日期只参与文件检索。
- **原始文件 + 足迹 CSV/GeoJSON**：精确筛选研究区内的足迹中心、实际 UTC 观测时间、所选波束和质量条件，导出参数与标识。
- **原始文件 + 对齐 GeoTIFF**：足迹产品同时生成 CSV、GeoJSON、参数均值栅格和观测数量栅格；L3/L4B 生成对齐后的原始参数栅格。

原始文件缓存在 `<输出目录>/<类别与版本>/original/<粒度号与修订号>/`。参数、研究区或网格不同的派生结果存入 `exports/<指纹>/`。同一修订的原始文件通过 SHA-256 检查后可复用。

NASA CMR 提供 SHA-512、SHA-256 等目录校验值时，软件核对完整原始文件。部分历史 LP DAAC 记录没有逐文件目录校验值，此时核对传输长度、固定对象版本（如提供）、HDF5 完整解码，并记录本地 SHA-256；本地 SHA-256 不等于 NASA 提供的校验值。

## 与之前的影像对齐

最直接的方法是选择 **「对齐已有 GeoTIFF」**，指定之前下载的影像。输出完整沿用其坐标系、仿射变换、像元大小、行列数与范围，支持非整数网格起点。足迹表格中的 `grid_row` / `grid_col` 从 **0** 开始，表示该足迹中心所在的参考像元。

需要配对影像与 GEDI 参数时，勾选 **「在足迹表格中附带该影像的像元值」**。导出的 `reference_band_1` 等列使用足迹中心所在像元，应用参考文件自己的 scale/offset，掩膜与缺测像元为 null。它表示像元配对，不是 25 米足迹范围内的面积平均，也不会自动保证 GEDI 和影像来自同一天。

未选参考文件时，足迹栅格沿用本软件现有的 UTM 规则。相同研究区与像元大小可与现有 Sentinel-2/RTC 区域输出对齐。原始整瓦片通常使用自己的网格，此时应选择实际 GeoTIFF 作参考。

GEDI 足迹直径约 25 米，相邻足迹与轨道之间有间隔。输出 10 米网格不会产生 10 米连续观测：

- 同一像元内的有效标量参数取算术均值；没有有效参数的像元为 `-9999`。
- `gedi_counts.tif` 第一波段是 `shot_count`，之后是各参数的有效足迹数量。0 表示没有对应观测；各参数缺测时数量可能不同。
- 不对足迹间空隙插值。标准误和预测区间的均值只是描述性汇总，不能作为合并后的统计不确定性。
- L3/L4B 为约 1 km 汇总栅格，使用最近邻对齐；不会因参考影像较细而增加实际观测分辨率。

聚合使用磁盘上的稀疏单元与分块写出，避免按参考影像总像元数分配所有参数的内存。单个参考网格仍受现有 20 亿像元输出上限约束。

## 关联各级 GEDI 与质量控制

各级足迹数据可按 `shot_number` 精确关联；GeoJSON 中它始终为字符串，避免超过 2^53 后丢失精度。CSV 中请按文本导入该列。表格同时提供 `shot_id`（如 `GEDI:10000000000000001`），便于 Excel 等软件保留完整关联键。软件对同一批次重复的足迹编号去重，并记录重复数量。

默认使用所选版本的官方质量标志。V003 的 `l2a_quality_flag_rel3`、`l2b_quality_flag_rel3`、`l4a_quality_flag_rel3`、`l4c_quality_flag_rel3` 与历史版本的字段不同。选择「保留全部」时仍保留质量列，便于后续筛选；定位降级与最低敏感度条件是独立选项，需单独关闭。敏感度 0 表示不额外筛选，官方标志本身仍可能包含敏感度门槛。

高程默认是 WGS84 椭球高，RH 是相对高度。L2A 可选择 `elev_lowestmode_egm2008 = elev_lowestmode − geoid`；它使用产品中的 EGM2008 geoid。水平网格对齐本身不会统一 GEDI 与 DSM 的垂直基准，也不能把冠层高程当作裸地高程。

## 日期与观测范围

足迹主要分布在约 51.6°S–51.6°N。2023 年 3 月至 2024 年 4 月暂停观测，发布可能滞后于采集。目录中一个轨道文件与研究区相交，不代表区域内必有通过筛选的足迹。

L3/L4B 的日期表示整段汇总期；与检索范围重叠即可匹配，不能拆成某一天的观测。L4B V2.1 汇总至 2023-03-16，不能用它代替 2024 年的足迹生物量数据。历史版本的已发布截止期在类别下方显示。

结果中的 `manifest.json` 保存精确来源、修订号、参数、质量筛选、网格、参考文件 SHA-256 与统计；`aoi.geojson` 保存研究区；足迹导出的 `fields.json` 说明参数单位、数组和关联列。

## 验证

自动测试覆盖当前及历史官方 CMR 记录、Earthdata 重定向、凭据隔离、SHA 校验、缺少目录校验值时的完整解码与未选参数损坏拒绝、日期与带孔边界、版本质量字段与缺测标志、64 位足迹编号、剖面转置、波形 1 起始索引、稀疏聚合、已有影像采样、空结果、取消、缓存与页面流程。真实 CMR 检索已验证；受保护原始文件的实传需要用户自己的 Earthdata 授权，目录检索与模拟下载不替代实传验收。

可使用 `scripts/live_gedi.py` 进行公共目录检查，或加 `--download` 后在本机交互输入令牌做实传与像元核对。加 `--exe dist/StarmapDownloader/StarmapDownloader.exe` 可通过打包程序的本机 API 验证。不要把密码或令牌放入命令行、脚本文件或聊天。

## 官方依据

- [GEDI 数据产品](https://gedi.umd.edu/data/products/)
- [Earthdata User Token API](https://urs.earthdata.nasa.gov/documentation/for_users/user_token)
- [L2A V003 数据字典](https://lpdaac.usgs.gov/documents/2439/gedi_l2a_v3_product_data_dictionary.html)
- [L2B V003 数据字典](https://lpdaac.usgs.gov/documents/2438/gedi_l2b_v3_product_data_dictionary.html)
- [L1B V003 数据字典](https://lpdaac.usgs.gov/documents/2437/gedi_l1b_v3_product_data_dictionary.html)
- [L4A V3 说明](https://daac.ornl.gov/GEDI/guides/GEDI_L4A_AGB_Density_V3.html)
- [L4C V3 说明](https://daac.ornl.gov/GEDI/guides/GEDI_L4C_WSCI_V3.html)
- [L4B V2.1 官方集合](https://search.earthdata.nasa.gov/search/granules?p=C2792577683-ORNL_CLOUD)
