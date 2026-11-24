"use strict";

const MapView = {
  local:null, generation:0, active:0, queue:[], tiles:new Map(),
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
  cancelTile(tile) {
    const job=this.tiles.get(tile);if(!job)return;
    job.cancelled=true;clearTimeout(job.retryTimer);job.controller?.abort();
    tile.onload=tile.onerror=null;
    if(job.objectUrl)URL.revokeObjectURL(job.objectUrl);
    this.tiles.delete(tile);
  },
  pump() {
    // A browser only opens six HTTP/1 connections to this local origin. Keep
    // three available for login, city selection and download progress/cancel.
    while(this.active<3&&this.queue.length) {
      const job=this.queue.shift();if(job.cancelled||job.generation!==this.generation)continue;
      this.active++;
      this.loadTile(job).finally(()=>{this.active--;this.pump();});
    }
  },
  failTile(job,error) {
    if(job.cancelled||job.generation!==this.generation)return;
    job.tile.onload=job.tile.onerror=null;
    if(job.objectUrl){URL.revokeObjectURL(job.objectUrl);job.objectUrl=null;}
    if(job.attempts++<1) {
      job.retryTimer=setTimeout(()=>{
        if(job.cancelled||job.generation!==this.generation)return;
        this.queue.push(job);this.pump();
      },1500);
    } else {this.tiles.delete(job.tile);job.finish(error);}
  },
  async loadTile(job) {
    const controller=job.controller=new AbortController(), timer=setTimeout(()=>controller.abort(),28000);
    try {
      const response=await fetch(job.source+(job.attempts?"?retry=1":""),{signal:controller.signal});
      if(!response.ok)throw new Error("tile unavailable");
      const body=await response.blob();
      if(job.cancelled||job.generation!==this.generation)return;
      job.objectUrl=URL.createObjectURL(body);
      job.tile.onload=()=>{
        if(job.cancelled||job.generation!==this.generation)return;
        job.tile.onload=job.tile.onerror=null;URL.revokeObjectURL(job.objectUrl);job.objectUrl=null;
        this.tiles.delete(job.tile);job.finish(null);
      };
      job.tile.onerror=()=>this.failTile(job,new Error("tile unavailable"));
      job.tile.src=job.objectUrl;
    } catch(error) {this.failTile(job,error);}
    finally {clearTimeout(timer);job.controller=null;}
  },
  change(kind) {
    if(!map)return;
    const generation=++this.generation;if(baseLayer)map.removeLayer(baseLayer);
    this.queue=[];for(const tile of [...this.tiles.keys()])this.cancelTile(tile);
    const label=kind==="satellite"?"卫星底图":"标准底图";
    let failed=0, loaded=0;
    const Layer=L.TileLayer.extend({createTile(coords,done) {
      const tile=document.createElement("img");tile.alt="";tile.setAttribute("role","presentation");tile.width=tile.height=256;
      const finish=error=>{
        if(error){failed++;MapView.notice(`${label}连接不完整，已显示离线行政边界。可重试底图，城市选择和下载仍可使用。`);}
        else {loaded++;if(!failed)$("mapOffline").hidden=true;}
        done(error,tile);
      };
      const job={tile,source:`/map/tiles/${kind}/${coords.z}/${coords.x}/${coords.y}.png`,generation,finish,attempts:0,cancelled:false};
      MapView.tiles.set(tile,job);MapView.queue.push(job);MapView.pump();return tile;
    }});
    baseLayer=new Layer("",{maxZoom:19,attribution:kind==="satellite"?"Tiles © Esri · Maxar · Earthstar Geographics":'© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors · © CARTO'});
    baseLayer.on("tileunload",event=>this.cancelTile(event.tile));
    baseLayer.on("load",()=>{if(generation!==this.generation)return;if(failed)this.notice(`${loaded?"部分":""}${label}暂时无法加载，已显示离线行政边界；可点击重试。`);else if(loaded)$("mapOffline").hidden=true;});
    baseLayer.addTo(map);
    document.querySelectorAll("#basemapSwitch button").forEach(button=>button.classList.toggle("active",button.dataset.basemap===kind));
    $("retryBasemap").onclick=()=>this.change(kind);
  }
};
