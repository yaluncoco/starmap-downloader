# Sentinel-2 test snapshots

- `s2-l2a-item.json`: selected fields of Element 84 Earth Search Collection‑1 item `S2B_T33XWG_20240605T130710_L2A`. `s2-l2a-tileInfo.json` is its exact public index, with the original SHA-256 in the pruned item.
- `s2-l3-cdse-item.json`: selected fields of CDSE STAC `Sentinel-2_mosaic_2024_Q2_50TMK_0_0`. `s2-l3-mirror-item.json` is the corresponding Taylor Geospatial Engine / Source Cooperative index.
- Tests are offline and mutate public metadata or build synthetic imagery in pytest temporary directories; no credentials. Live reference checks are in `scripts/live_s2_tiles.py`.
