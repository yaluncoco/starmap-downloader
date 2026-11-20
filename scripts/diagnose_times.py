import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")
from rasterio.io import MemoryFile
from core.http import AuthManager, HttpClient, PROCESS_URL
from core.geometry import parse_coordinates
from core.planning import normalize_options, build_plan

c=json.loads(sys.stdin.read());auth=AuthManager();auth.connect(c["client_id"],c["client_secret"]);c.clear()
http=HttpClient()
g=parse_coordinates("116.35,39.90,116.355,39.905");p,tiles,_=build_plan(g,normalize_options({"max_cloud":100,"mask_clouds":False}))
cases=[("WGS84_NARROW","http://www.opengis.net/def/crs/OGC/1.3/CRS84",list(g.bounds),"2024-06-10T02:55:31Z","2024-06-10T03:15:31Z"),("UTM_DAY","http://www.opengis.net/def/crs/EPSG/0/32650",list(tiles[0].bounds),"2024-06-10T00:00:00Z","2024-06-11T00:00:00Z")]
for name,crs,bbox,begin,end in cases:
    payload={"input":{"bounds":{"bbox":bbox,"properties":{"crs":crs}},"data":[{"type":"sentinel-2-l2a","dataFilter":{"timeRange":{"from":begin,"to":end},"maxCloudCoverage":100}}]},"output":{"width":64,"height":64,"responses":[{"identifier":"default","format":{"type":"image/tiff"}}]},"evalscript":'//VERSION=3\nfunction setup(){return {input:["B04","dataMask"],output:{bands:2,sampleType:"FLOAT32"}};}\nfunction evaluatePixel(s){return [s.B04,s.dataMask];}'}
    r=http.request("POST",PROCESS_URL,json=payload,headers={"Authorization":"Bearer "+auth.token(),"Accept":"image/tiff"})
    if r.status_code==200:
        with MemoryFile(r.content) as mem:
            with mem.open() as ds:
                a=ds.read();print(json.dumps({"case":name,"valid":int((a[1]>0).sum()),"red_range":[float(a[0].min()),float(a[0].max())]}),flush=True)
    else:print(json.dumps({"case":name,"status":r.status_code}),flush=True)
auth.clear();http.close()
