"use strict";

const MapView = {
  local:null, generation:0,
  async initialize() {
    if(!map)return;
    map.createPane("adminBasemapPane");map.getPane("adminBasemapPane").style.zIndex="180";
    const controller=new AbortController(), timer=setTimeout(()=>controller.abort(),15000);
    try {
      const response=await fetch("/map/admin-basemap.geojson",{signal:controller.signal});if(!response.ok)throw new Error();
      this.local=L.geoJSON(await response.json(),{pane:"adminBasemapPane",interactive:false,style:{color:"#91a9a0",weight:1,fillColor:"#e8eeeb",fillOpacity:.45},attribution:"行政边界：ChinaAdminDivisonSHP · MIT · 2024 · 已转为 WGS84"}).addTo(map);
    } catch {this.notice("离线行政底图读取失败，可通过城市列表或导入 SHP 设置区域。");}
    finally {clearTimeout(timer);}
  },
  notice(message) {$("mapOffline").hidden=false;$("mapOfflineMessage").textContent=message;},
  change(kind) {
    if(!map)return;
    const generation=++this.generation;if(baseLayer)map.removeLayer(baseLayer);
    const label=kind==="satellite"?"卫星底图":"标准底图";
    let failed=0, loaded=0;
    const Layer=L.TileLayer.extend({createTile(coords,done) {
      const tile=document.createElement("img");tile.alt="";tile.setAttribute("role","presentation");tile.width=tile.height=256;
      let attempts=0;
      const source=()=>`/map/tiles/${kind}/${coords.z}/${coords.x}/${coords.y}.png${attempts?"?retry=1":""}`;
      tile.onload=()=>{if(generation===MapView.generation){loaded++;if(!failed)$("mapOffline").hidden=true;}done(null,tile);};
      tile.onerror=()=>{
        if(generation!==MapView.generation){done(new Error("map changed"),tile);return;}
        if(attempts++<1){setTimeout(()=>{if(generation===MapView.generation)tile.src=source();},1500);return;}
        failed++;MapView.notice(`${label}连接不完整，已显示离线行政边界。可重试底图，城市选择和下载仍可使用。`);done(new Error("tile unavailable"),tile);
      };
      tile.src=source();return tile;
    }});
    baseLayer=new Layer("",{maxZoom:19,attribution:kind==="satellite"?"Tiles © Esri · Maxar · Earthstar Geographics":'© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors · © CARTO'});
    baseLayer.on("load",()=>{if(generation!==this.generation)return;if(failed)this.notice(`${loaded?"部分":""}${label}暂时无法加载，已显示离线行政边界；可点击重试。`);else if(loaded)$("mapOffline").hidden=true;});
    baseLayer.addTo(map);
    document.querySelectorAll("#basemapSwitch button").forEach(button=>button.classList.toggle("active",button.dataset.basemap===kind));
    $("retryBasemap").onclick=()=>this.change(kind);
  }
};
