"use strict";

const Spatial = {
  generation:0, version:0, timer:null, signature:"", layers:new Map(), priorities:new Map(), touched:new Set(),
  summary:null, allocation:null, overlap:null, uncovered:null, point:null, popup:null,
  colors:["#177d72","#477ab5","#a46c28","#8465ad","#ae5375","#577f44"],
  current(id=Batch.active) {
    const group=Batch.results.get(id);
    return group ? {id:group.id, profile:group.profile, query:group.query, criteria:group.criteria, scenes:group.scenes,
      selected:group.selected, options:Batch.optionsFor(group.profile)} : {id:"primary",profile:state.profile,query:state.query,
      criteria:criteria(),scenes:state.scenes,selected:state.selected,options:options()};
  },
  priorityFor(id="primary") {
    const group=Batch.results.get(id);
    return group ? Batch.settings(group.profile).options.priority||"cloud" : this.priorities.get(id)||"cloud";
  },
  setPriority(priority,id=Batch.active) {
    const group=Batch.results.get(id);
    if(group)Batch.settings(group.profile).options.priority=priority;
    else this.priorities.set("primary",priority);
    this.touched.add(group?group.id:"primary");
    if(group)Batch.renderSettings();
    this.refresh();schedulePlan();
  },
  request(current) {
    return {geometry:state.aoi.geometry,profile:current.profile,query_id:current.query.id,criteria:current.criteria,
      scene_keys:[...current.selected],options:{priority:this.priorityFor(current.id)},
      ...(Batch.query?{batch_id:Batch.query.id,group_id:current.id}:{})};
  },
  available(scene) {return scene.native?scene.native.available===true:scene.gedi?scene.gedi.available===true:scene.raw?scene.raw.available===true:true;},
  reset() {
    this.generation++;this.version++;clearTimeout(this.timer);this.touched.clear();this.summary=null;this.signature="";
    this.layers.clear();footprintLayer?.clearLayers();this.allocation?.clearLayers();this.overlap?.clearLayers();this.uncovered?.clearLayers();
    if(this.popup&&map)map.closePopup(this.popup);this.popup=null;this.point=null;
    $("coveragePanel").hidden=true;$("mapFootprintLegend").hidden=true;
  },
  initializeMap() {
    for(const [pane,z] of [["allocationPane",405],["overlapPane",407],["footprintPane",410],["aoiPane",420]]) {
      map.createPane(pane);map.getPane(pane).style.zIndex=String(z);
      if(pane!=="footprintPane")map.getPane(pane).style.pointerEvents="none";
    }
    this.allocation=L.featureGroup().addTo(map);this.overlap=L.featureGroup().addTo(map);this.uncovered=L.featureGroup().addTo(map);
  },
  initialize() {
    $("spatialPriority").addEventListener("change",()=>this.setPriority($("spatialPriority").value));
    $("recommendCoverage").addEventListener("click",()=>this.recommend());
    $("fitFootprints").addEventListener("click",()=>{if(footprintLayer?.getBounds().isValid())map.fitBounds(footprintLayer.getBounds(),{padding:[65,65],maxZoom:16});});
    $("coverageBlockList").addEventListener("click",event=>{
      const button=event.target.closest("[data-spatial-key]");if(!button)return;
      if(button.dataset.move)this.move(button.dataset.spatialKey,Number(button.dataset.move));
      else this.toggle(button.dataset.spatialKey);
    });
  },
  refresh() {
    this.version++;clearTimeout(this.timer);this.summary=null;
    const current=this.current();
    if(!state.aoi||!current.query||!current.scenes.some(s=>s.products?.some(p=>p.footprint))) {
      this.renderFootprints(current);$("coveragePanel").hidden=true;$("mapFootprintLegend").hidden=true;return;
    }
    this.renderFootprints(current);this.renderSummary(current,null);
    if(state.busy||state.aoiPending)return;
    const version=this.version, generation=this.generation;
    this.timer=setTimeout(async()=>{
      try {
        const result=await api("coverage",this.request(current));
        if(version!==this.version||generation!==this.generation)return;
        this.summary=result;this.renderSummary(this.current(),result);this.renderAllocation(result);
      } catch(error) {
        if(version!==this.version||generation!==this.generation)return;
        $("coverageNote").textContent=error.message;this.renderAllocation(null);
      }
    },120);
  },
  dateColors(current) {
    const dates=[...new Set(current.scenes.map(s=>s.date).filter(Boolean))].sort();
    return new Map(dates.map((date,index)=>[date,this.colors[index%this.colors.length]]));
  },
  style(scene,current,highlight=false) {
    const selected=current.selected.has(scene.key);
    return {pane:"footprintPane",color:this.dateColors(current).get(scene.date)||this.colors[0],dashArray:"6,5",
      weight:highlight?3:selected?2:1.2,opacity:this.available(scene)?selected?1:.65:.35,
      fillColor:this.dateColors(current).get(scene.date)||this.colors[0],fillOpacity:selected?.045:.012,interactive:true};
  },
  renderFootprints(current) {
    if(!map||!footprintLayer)return;
    const signature=JSON.stringify([current.id,current.query?.id,current.scenes.map(s=>s.key)]);
    if(signature!==this.signature) {
      this.signature=signature;this.layers.clear();footprintLayer.clearLayers();this.renderAllocation(null);
      if(this.popup)map.closePopup(this.popup);this.popup=null;this.point=null;
      for(const scene of current.scenes) {
        const group=L.featureGroup();
        for(const product of scene.products||[]) {
          if(!product.footprint)continue;
          const layer=L.geoJSON(product.footprint,{pane:"footprintPane",interactive:true,style:()=>this.style(scene,current)});
          layer.bindTooltip(`${escapeHtml(scene.tiles?.join(" / "))} · ${escapeHtml(scene.date)} ${escapeHtml(scene.time)} UTC · 云量 ${cloudLabel(scene.cloud_max)}`,{sticky:true});
          layer.on("click",event=>{if(state.busy||state.editing||state.aoiPending)return;L.DomEvent.stopPropagation(event);this.openAt(event.latlng);});
          layer.addTo(group);
        }
        group.addTo(footprintLayer);this.layers.set(scene.key,group);
      }
    }
    for(const scene of current.scenes)this.layers.get(scene.key)?.eachLayer(layer=>layer.setStyle(this.style(scene,current)));
  },
  renderAllocation(summary) {
    this.allocation?.clearLayers();this.overlap?.clearLayers();this.uncovered?.clearLayers();
    if(!map||!summary)return;
    const current=this.current(), colors=this.dateColors(current);
    for(const block of summary.blocks)if(block.selected&&block.assigned)L.geoJSON(block.assigned,{pane:"allocationPane",interactive:false,
      style:{color:colors.get(block.date),weight:0,fillOpacity:.17}}).addTo(this.allocation);
    if(summary.overlap)L.geoJSON(summary.overlap,{pane:"overlapPane",interactive:false,style:{color:"#bb812e",weight:1,fillColor:"#e5ae48",fillOpacity:.12}}).addTo(this.overlap);
    if(summary.uncovered)L.geoJSON(summary.uncovered,{pane:"allocationPane",interactive:false,style:{color:"#be665d",dashArray:"3,4",weight:1,fillOpacity:.12}}).addTo(this.uncovered);
  },
  renderSummary(current,summary) {
    $("coveragePanel").hidden=false;$("mapFootprintLegend").hidden=!map;
    $("spatialPriority").value=this.priorityFor(current.id);
    const chosen=current.scenes.filter(s=>current.selected.has(s.key)), tiles=new Set(chosen.flatMap(s=>s.tiles||[]));
    $("coverageSummary").textContent=summary?`已选 ${summary.selected_count} 块 · ${summary.tile_count} 个瓦片 · 覆盖 ${summary.coverage_percent.toFixed(2)}%`:
      `已选 ${chosen.length} 块 · ${tiles.size} 个瓦片 · 正在计算覆盖`;
    const dates=[...new Set(chosen.map(s=>s.date).filter(Boolean))].sort(), colors=this.dateColors(current);
    const chips=dates.map(date=>`<span class="coverage-date"><i style="background:${colors.get(date)}"></i>${escapeHtml(date)}<b>${chosen.filter(s=>s.date===date).length} 块</b></span>`).join("");
    $("coverageDates").innerHTML=dates.length?`<span>${dates.length} 个日期 · UTC</span>${chips}`:"尚未选中影像块";
    $("mapFootprintDates").innerHTML=[...new Set(current.scenes.map(s=>s.date).filter(Boolean))].sort().map(date=>`<span class="coverage-date"><i style="background:${colors.get(date)}"></i>${escapeHtml(date)}</span>`).join("");
    const keys=summary?.ordered_keys||[...current.selected];
    $("coverageBlockList").innerHTML=keys.map((key,index)=>{
      const scene=current.scenes.find(s=>s.key===key), block=summary?.blocks.find(b=>b.key===key);if(!scene)return "";
      return `<div class="coverage-block"><span><strong>${index+1}. ${escapeHtml(scene.tiles?.join(" / "))} · ${escapeHtml(scene.date)}</strong><small>${escapeHtml(scene.time)} UTC · 云量 ${cloudLabel(scene.cloud_max)}${block?` · 优先分配 ${block.contribution_percent.toFixed(2)}%`:""}</small></span><button type="button" class="inline-link" data-spatial-key="${escapeHtml(key)}" data-move="-1" aria-label="提高 ${escapeHtml(scene.tiles?.join('/'))} ${escapeHtml(scene.date)} 的优先级" ${index===0||state.busy?"disabled":""}>↑</button><button type="button" class="inline-link" data-spatial-key="${escapeHtml(key)}" data-move="1" aria-label="降低 ${escapeHtml(scene.tiles?.join('/'))} ${escapeHtml(scene.date)} 的优先级" ${index===keys.length-1||state.busy?"disabled":""}>↓</button><button type="button" class="inline-link danger" data-spatial-key="${escapeHtml(key)}" ${state.busy?"disabled":""}>取消</button></div>`;
    }).join("")||'<span class="field-note">点击地图中的虚线框，或勾选下方产品。</span>';
    const regional=["s2-process","s2-earth-search","s2-cdse-s3"].includes(current.profile);
    $("coverageDetails").hidden=!regional;
    $("coverageNote").textContent=(summary?`几何交叠 ${summary.overlap_percent.toFixed(2)}% · 未覆盖 ${summary.uncovered_percent.toFixed(2)}%。`:"")+(!regional?
      "原始产品仍保留完整范围；地图选择控制下载哪些产品。":current.options.layout==="series"?
      "时间序列会逐产品导出，交叠区域会在多个文件中出现。":"填色为优先分配范围；云与无数据位置可由已选影像补齐，实际日期以来源图为准。覆盖不代表无云。");
    this.availability();
  },
  availability() {
    const locked=state.busy||state.connecting||state.editing||state.aoiPending;
    $("recommendCoverage").disabled=locked||!this.current().query;
    $("fitFootprints").disabled=locked||!map||!this.layers.size;
    $("spatialPriority").disabled=locked;
    $("coverageBlockList").querySelectorAll("button").forEach(button=>{if(locked)button.disabled=true;});
  },
  assign(current,keys) {
    if(current.id==="primary")state.selected=new Set(keys);else Batch.results.get(current.id).selected=new Set(keys);
  },
  changed(current) {
    this.touched.add(current.id);
    if(current.id==="primary")renderScenes();else{Batch.renderResults();schedulePlan();this.refresh();}
    if(this.popup&&this.point)this.updatePopup();
  },
  toggle(key) {
    if(state.busy||state.editing||state.aoiPending)return;
    const current=this.current(), scene=current.scenes.find(s=>s.key===key);if(!scene||!this.available(scene))return;
    if(current.selected.has(key))current.selected.delete(key);else current.selected.add(key);
    this.changed(current);
    const row=[...$("scenesBody").querySelectorAll("tr[data-key]")].find(row=>row.dataset.key===key);
    row?.scrollIntoView({block:"nearest"});
  },
  move(key,direction) {
    if(state.busy||state.editing||state.aoiPending)return;
    const current=this.current(), keys=this.summary?.ordered_keys||[...current.selected], index=keys.indexOf(key), target=index+direction;
    if(index<0||target<0||target>=keys.length)return;
    [keys[index],keys[target]]=[keys[target],keys[index]];this.assign(current,keys);
    const group=Batch.results.get(current.id);
    if(group)Batch.settings(group.profile).options.priority="selection";else this.priorities.set("primary","selection");
    this.changed(this.current());
  },
  async recommend(id=Batch.active,automatic=false) {
    const current=this.current(id);if(!state.aoi||!current.query||state.busy||state.aoiPending)return;
    const generation=this.generation, query=current.query.id, version=this.version;
    try {
      const summary=await api("coverage",this.request(current));
      if(generation!==this.generation||this.current(id).query?.id!==query||(automatic?this.touched.has(current.id):version!==this.version))return;
      if(summary.recommended_keys.length>500){if(!automatic)toast("推荐结果超过 500 个区块，请缩小日期或研究区后分批下载。",true);return;}
      this.assign(this.current(id),summary.recommended_keys);
      if(current.id==="primary")renderScenes();else{Batch.renderResults();schedulePlan();this.refresh();}
      if(!automatic)toast(`已推荐 ${summary.recommended_keys.length} 个影像块；可继续在地图上调整。`);
    } catch(error) {if(generation===this.generation&&!automatic)toast(error.message,true);}
  },
  autoSelect() {
    const all=[this.current("primary"),...[...Batch.results.keys()].map(id=>this.current(id))];
    for(const current of all)if(["s2-process","s2-earth-search","s2-cdse-s3"].includes(current.profile)&&current.options.layout==="mosaic")this.recommend(current.id,true);
  },
  contains(geometry,point) {
    const inRing=ring=>{
      let inside=false;
      for(let i=0,j=ring.length-1;i<ring.length;j=i++) {
        const a=ring[i],b=ring[j];
        if((a[1]>point.lat)!==(b[1]>point.lat)&&point.lng<(b[0]-a[0])*(point.lat-a[1])/(b[1]-a[1])+a[0])inside=!inside;
      }
      return inside;
    };
    const inPolygon=rings=>rings.length&&inRing(rings[0])&&!rings.slice(1).some(inRing);
    return geometry?.type==="Polygon"?inPolygon(geometry.coordinates):geometry?.type==="MultiPolygon"?geometry.coordinates.some(inPolygon):false;
  },
  openAt(point) {
    const current=this.current(), scenes=current.scenes.filter(scene=>scene.products?.some(p=>this.contains(p.footprint,point)));
    if(!scenes.length)return;
    this.point=point;
    if(scenes.length===1&&this.available(scenes[0]))this.toggle(scenes[0].key);
    this.popup=L.popup({maxWidth:340,minWidth:240,autoPanPadding:[55,55]}).setLatLng(point).addTo(map);
    this.updatePopup();
  },
  updatePopup() {
    if(!this.popup||!this.point)return;
    const current=this.current(), scenes=current.scenes.filter(scene=>scene.products?.some(p=>this.contains(p.footprint,this.point)));
    const content=document.createElement("div");content.className="footprint-popup";
    content.innerHTML=`<strong>此处覆盖 ${scenes.length} 个产品</strong><p>勾选所需日期，地图与下方列表同步。</p>`+scenes.map(scene=>`<label><input type="checkbox" data-map-key="${escapeHtml(scene.key)}" aria-label="地图选择 ${escapeHtml(scene.tiles?.join('/'))} ${escapeHtml(scene.date)}" ${current.selected.has(scene.key)?"checked":""} ${!this.available(scene)||state.busy?"disabled":""}><span><b>${escapeHtml(scene.tiles?.join(" / "))} · ${escapeHtml(scene.date)}</b><small>${escapeHtml(scene.time)} UTC · 云量 ${cloudLabel(scene.cloud_max)}${this.available(scene)?"":" · 无可用入口"}</small></span></label>`).join("");
    content.addEventListener("change",event=>{if(event.target.dataset.mapKey)this.toggle(event.target.dataset.mapKey);});
    L.DomEvent.disableClickPropagation(content);L.DomEvent.disableScrollPropagation(content);this.popup.setContent(content);
  },
  highlight(key,on) {
    const current=this.current(), scene=current.scenes.find(s=>s.key===key);if(!scene)return;
    this.layers.get(key)?.eachLayer(layer=>{layer.setStyle(this.style(scene,current,on));if(on)layer.bringToFront();});
  }
};
