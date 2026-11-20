# GEDI 官方目录夹具

这些 JSON 是 2026-11-20 从公共 NASA CMR `granules.umm_json` 取得的单条完整响应，不含账号、令牌或受保护数据文件。

请求为 `https://cmr.earthdata.nasa.gov/search/granules.umm_json`，参数 `collection_concept_id` 见 `core/gedi_catalogue.py`；时间段通常为 `2020-06-01T00:00:00Z,2020-06-02T00:00:00Z`，`page_size=1`。L3 V3 使用 2024-06-01 至 2024-06-11 与汇总期重叠的检索。

夹具覆盖 LP DAAC 的 L1B/L2A/L2B V003、L2A/L2B V002，以及 ORNL DAAC 的 L3 V3、L4A V3/V2.1、L4B V2.1、L4C V3/V2。保留粒度标识、修订、官方 URL、时间、边界与目录校验字段。

L2A/L2B V002 的这些记录只有估算 MB，没有精确字节数和目录校验值；ORNL 的历史 L4A/L4C 使用 SHA-256，V003 的部分足迹记录使用 SHA-512。测试必须保留这些区别。

端到端测试中的小型 HDF5 和 GeoTIFF 是按数据字典构造的合成样本，用于验证算法、参数和协议。它们不代表实际 NASA 足迹观测，也不作为已完成认证实传的证据。
