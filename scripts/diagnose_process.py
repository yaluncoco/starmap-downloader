import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")

import rasterio
from rasterio.io import MemoryFile
from core.catalogue import find_scenes
from core.geometry import parse_coordinates
from core.http import AuthManager, HttpClient, PROCESS_URL

credentials = json.loads(sys.stdin.read())
auth = AuthManager()
auth.connect(credentials["client_id"], credentials["client_secret"])
credentials.clear()
geometry = parse_coordinates("116.35,39.90,116.355,39.905")
scenes = find_scenes(geometry, "2024-06-01", "2024-06-10", 100)
print(json.dumps({"scenes": [{"key": s["key"], "coverage": s["coverage_percent"], "cloud": s["cloud_max"], "tiles": s["tiles"]} for s in scenes]}), flush=True)
http = HttpClient()
payload = {
    "input": {"bounds": {"bbox": [116.35,39.90,116.355,39.905], "properties": {"crs": "http://www.opengis.net/def/crs/OGC/1.3/CRS84"}}, "data": [{"type": "sentinel-2-l2a", "dataFilter": {"timeRange": {"from": "2024-06-01T00:00:00Z", "to": "2024-06-11T00:00:00Z"}, "maxCloudCoverage": 100}}]},
    "output": {"width": 64, "height": 64, "responses": [{"identifier": "default", "format": {"type": "image/tiff"}}]},
    "evalscript": '//VERSION=3\nfunction setup(){return {input:["B04","dataMask"],output:{bands:2,sampleType:"FLOAT32"}};}\nfunction evaluatePixel(s){return [s.B04,s.dataMask];}'
}
response = http.request("POST", PROCESS_URL, json=payload, headers={"Authorization": "Bearer " + auth.token(), "Accept": "image/tiff"})
print(json.dumps({"status": response.status_code, "mime": response.headers.get("Content-Type"), "bytes": len(response.content)}), flush=True)
if response.status_code == 200:
    with MemoryFile(response.content) as memory:
        with memory.open() as ds:
            values = ds.read()
            print(json.dumps({"red_range": [float(values[0].min()),float(values[0].max())], "data_mask_range": [float(values[1].min()),float(values[1].max())], "valid": int((values[1]>0).sum()), "crs": str(ds.crs)}), flush=True)
else:
    print("PROCESS_REQUEST_FAILED")
auth.clear()
http.close()

