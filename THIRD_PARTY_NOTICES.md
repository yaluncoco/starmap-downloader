# 第三方声明

- 地图界面使用 Leaflet 和 Leaflet.draw，保留各自的版权和许可文件：`ui/vendor/leaflet/LICENSE`、`ui/vendor/leaflet-draw/LICENSE`。
- 安装器由 Inno Setup 构建。简体中文消息基于 [Inno Setup Chinese Simplified Translation](https://github.com/kira-96/Inno-Setup-Chinese-Simplified-Translation)，许可文本见 `installer/ChineseSimplified.LICENSE.txt`。
- 随应用附带的 ESA WorldCover 瓦片索引是派生元数据，不是影像。数据来源、署名和许可见 `core/data/WorldCover.txt`。软件获取的影像仍受数据提供方各自的条款约束。
- 内置完整城市行政边界和定位底图由 [GaryBikini/ChinaAdminDivisonSHP](https://github.com/GaryBikini/ChinaAdminDivisonSHP) 的 `v24.02.06` 数据派生，固定提交 `ae72417fee2a63f453e6b2717e40655e87e26887`，Copyright (c) 2020 GaryBikini，MIT 许可。完整许可、原始文件摘要和 WGS84 转换说明见 `core/data/cities/cities-LICENSE.txt`、`cities-source.json`；引用 [doi:10.5281/zenodo.10624971](https://doi.org/10.5281/zenodo.10624971)。
- 在线标准底图使用 CARTO / OpenStreetMap，卫星底图使用 Esri World Imagery；界面保留各提供方署名。在线底图用于定位，研究影像仍来自所选卫星数据路线。
- Windows 安装目录中的 `_internal` 保留 Python 运行时及所用 Python 包的发行元数据和随附许可。Microsoft Edge WebView2 Runtime 需由用户在系统上安装，不由本安装器下载。
