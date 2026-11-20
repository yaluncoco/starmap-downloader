import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")
from core.http import AuthManager,HttpClient
c=json.loads(sys.stdin.read());a=AuthManager();a.connect(c["client_id"],c["client_secret"]);c.clear();h=HttpClient()
r=h.request("POST","https://sh.dataspace.copernicus.eu/catalog/v1/search",headers={"Authorization":"Bearer "+a.token()},json={"bbox":[116.35,39.90,116.355,39.905],"datetime":"2024-06-01T00:00:00Z/2024-06-11T00:00:00Z","collections":["sentinel-2-l2a"],"limit":20})
print(json.dumps({"status":r.status_code}),flush=True)
if r.status_code==200:
    d=r.json();print(json.dumps({"context":d.get("context"),"features":[{"id":f["id"],"properties":f["properties"]} for f in d.get("features",[])]}),flush=True)
else:print(r.text[:600])
a.clear();h.close()
