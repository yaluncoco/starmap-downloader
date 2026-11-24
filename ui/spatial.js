"use strict";

const Spatial = {
  generation:0, version:0, timer:null, signature:"", layers:new Map(), groupLayers:new Map(), views:new Map(), fitted:new Set(), priorities:new Map(), touched:new Set(), hovered:null,
  summary:null, summaryKey:"", allocation:null, overlap:null, uncovered:null, point:null, popup:null, zooming:false, fitPending:false,
  colors:["#177d72","#477ab5","#a46c28","#8465ad","#ae5375","#577f44"], colorSignature:"", colorMap:new Map(),
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
    if(Monthly.enabled()&&Monthly.current())Monthly.current().priority=priority;
    this.touched.add(group?group.id:"primary");
    if(group)Batch.renderSettings();
    this.refresh();schedulePlan();
  },
  request(current) {
    if(Monthly.enabled()&&Monthly.query)return Monthly.spatialRequest(current);
    return {...Cities.request(),profile:current.profile,query_id:current.query.id,criteria:current.criteria,
      scene_keys:[...current.selected],options:{priority:this.priorityFor(current.id)},
      ...(Batch.query?{batch_id:Batch.query.id,group_id:current.id}:{})};
  },
  available(scene) {return scene.native?scene.native.available===true:scene.gedi?scene.gedi.available===true:scene.raw?scene.raw.available===true:true;},
  viewFor(id=this.current().id) {
    if(!this.views.has(id))this.views.set(id,{scope:"selected",detail:"overpass",date:"all",allocation:false});
    return this.views.get(id);
  },
  reset() {
    this.generation++;this.version++;clearTimeout(this.timer);this.touched.clear();this.summary=null;this.summaryKey="";this.signature="";
    this.layers.clear();this.groupLayers.clear();this.views.clear();this.fitted.clear();this.hovered=null;this.fitPending=false;this.colorSignature="";this.colorMap.clear();
    footprintLayer?.clearLayers();this.allocation?.clearLayers();this.overlap?.clearLayers();this.uncovered?.clearLayers();
    if(this.popup&&map)map.closePopup(this.popup);this.popup=null;this.point=null;
    $("coveragePanel").hidden=true;$("mapFootprintLegend").hidden=true;
  },
  initializeMap() {
    for(const [pane,z] of [["allocationPane",405],["overlapPane",407],["footprintPane",410],["aoiPane",420]]) {
      map.createPane(pane);map.getPane(pane).style.zIndex=String(z);
      if(pane!=="footprintPane")map.getPane(pane).style.pointerEvents="none";
    }
    this.allocation=L.featureGroup().addTo(map);this.overlap=L.featureGroup().addTo(map);this.uncovered=L.featureGroup().addTo(map);
    L.DomEvent.disableClickPropagation($("mapFootprintLegend"));L.DomEvent.disableScrollPropagation($("mapFootprintLegend"));
    map.on("popupclose",event=>{if(event.popup===this.popup){this.popup=null;this.point=null;}});
    map.on("zoomstart",()=>{this.zooming=true;});
    map.on("zoomend",()=>{this.zooming=false;if(this.fitPending){this.fitPending=false;this.fit();}});
  },
  initialize() {
    $("spatialPriority").addEventListener("change",()=>this.setPriority($("spatialPriority").value));
    $("recommendCoverage").addEventListener("click",()=>this.recommend());
    $("fitFootprints").addEventListener("click",()=>this.fit());
    for(const [id,field] of [["mapFootprintScope","scope"],["mapFootprintDetail","detail"],["mapFootprintDate","date"],["mapAllocation","allocation"]]) {
      $(id).addEventListener("change",()=>{
        const view=this.viewFor();view[field]=field==="allocation"?$(id).checked:$(id).value;
        if(field==="scope")view.date="all";
        if(this.popup&&map)map.closePopup(this.popup);this.popup=null;this.point=null;
        this.renderFootprints(this.current());this.renderAllocation(this.summary);this.availability();
      });
    }
    $("coverageBlockList").addEventListener("click",event=>{
      const button=event.target.closest("[data-spatial-key]");if(!button)return;
      if(button.dataset.move)this.move(button.dataset.spatialKey,Number(button.dataset.move));
      else this.toggle(button.dataset.spatialKey);
    });
  },
  refresh() {
    this.version++;clearTimeout(this.timer);
    const current=this.current();
    const summaryKey=JSON.stringify([this.generation,current.id,current.query?.id,[...current.selected],this.priorityFor(current.id)]);
    if(summaryKey!==this.summaryKey)this.summary=null;
    if(!state.aoi||!current.query||!current.scenes.some(s=>s.products?.some(p=>p.footprint))) {
      this.renderFootprints(current);$("coveragePanel").hidden=true;$("mapFootprintLegend").hidden=true;return;
    }
    this.renderFootprints(current);this.renderAllocation(this.summary);this.renderSummary(current,this.summary);
    if(this.summary||selectionLocked())return;
    const version=this.version, generation=this.generation;
    this.timer=setTimeout(async()=>{
      try {
        const result=await api("coverage",this.request(current));
        if(version!==this.version||generation!==this.generation)return;
        this.summary=result;this.summaryKey=summaryKey;this.renderFootprints(this.current());this.renderSummary(this.current(),result);this.renderAllocation(result);
        const fitKey=current.id+":"+current.query.id;
        if(result.selected_count&&!this.fitted.has(fitKey)){this.fitted.add(fitKey);this.fit();}
      } catch(error) {
        if(version!==this.version||generation!==this.generation)return;
        $("coverageNote").textContent=error.message;this.renderAllocation(null);
      }
    },120);
  },
  dateColors(current) {
    const signature=current.id+":"+(current.query?.id||"");
    if(signature===this.colorSignature)return this.colorMap;
    this.colorSignature=signature;
    const dates=[...new Set(current.scenes.map(s=>s.date).filter(Boolean))].sort();
    this.colorMap=new Map(dates.map((date,index)=>[date,index<this.colors.length?this.colors[index]:`hsl(${(index*137.508)%360} 45% 42%)`]));
    return this.colorMap;
  },
  style(scene,current,highlight=false) {
    const selected=current.selected.has(scene.key);
    return {pane:"footprintPane",color:this.dateColors(current).get(scene.date)||this.colors[0],dashArray:"6,5",
      weight:highlight?3:selected?2:1.2,opacity:this.available(scene)?selected?1:.65:.35,
      fillColor:this.dateColors(current).get(scene.date)||this.colors[0],fillOpacity:highlight?.12:this.viewFor(current.id).allocation?.02:selected?.16:.06,interactive:true};
  },
  renderControls(current) {
    const view=this.viewFor(current.id), source=current.scenes.filter(s=>view.scope!=="selected"||current.selected.has(s.key));
    const dates=[...new Set(source.map(s=>s.date).filter(Boolean))].sort();
    if(view.date!=="all"&&!dates.includes(view.date))view.date="all";
    $("mapFootprintScope").innerHTML=`<option value="selected">已选影像 (${current.selected.size})</option><option value="candidates">全部候选 (${current.scenes.length})</option>`;
    $("mapFootprintScope").value=view.scope;$("mapFootprintDetail").value=view.detail;
    $("mapFootprintDate").innerHTML='<option value="all">全部采集日期</option>'+dates.map(date=>`<option value="${escapeHtml(date)}">${escapeHtml(date)} (${source.filter(s=>s.date===date).length})</option>`).join("");
    $("mapFootprintDate").value=view.date;$("mapAllocation").checked=view.allocation;
    return source.filter(s=>view.date==="all"||s.date===view.date);
  },
  fit() {
    if(!map||!footprintLayer?.getBounds().isValid())return;
    if(this.zooming){this.fitPending=true;return;}
    const bounds=footprintLayer.getBounds();if(aoiLayer?.getBounds().isValid())bounds.extend(aoiLayer.getBounds());
    map.fitBounds(bounds,{paddingTopLeft:[65,55],paddingBottomRight:[260,55],maxZoom:16,animate:false});
  },
  renderFootprints(current) {
    if(!map||!footprintLayer)return;
    const signature=JSON.stringify([current.id,current.query?.id,current.scenes.map(s=>s.key)]);
    if(signature!==this.signature) {
      this.signature=signature;this.layers.clear();this.groupLayers.clear();this.hovered=null;footprintLayer.clearLayers();this.renderAllocation(null);
      if(this.popup)map.closePopup(this.popup);this.popup=null;this.point=null;
      for(const scene of current.scenes) {
        const group=L.featureGroup();
        for(const product of scene.products||[]) {
          if(!product.footprint)continue;
          const layer=L.geoJSON(product.footprint,{pane:"footprintPane",interactive:true,style:()=>this.style(scene,current)});
          layer.bindTooltip(`${escapeHtml(scene.tiles?.join(" / "))} · ${escapeHtml(scene.date)} ${escapeHtml(scene.time)} UTC · 云量 ${cloudLabel(scene.cloud_max)}`,{sticky:true});
          layer.on("click",event=>{if(selectionLocked())return;L.DomEvent.stopPropagation(event);this.openAt(event.latlng);});
          layer.addTo(group);
        }
        this.layers.set(scene.key,group);
      }
    }
    for(const scene of current.scenes)this.layers.get(scene.key)?.eachLayer(layer=>layer.setStyle(this.style(scene,current)));
    const rows=this.renderControls(current), view=this.viewFor(current.id), colors=this.dateColors(current), wanted=[];
    const grouped=view.detail==="overpass"&&!!this.summary?.footprint_groups;
    if(grouped) {
      const visible=new Set(rows.map(s=>s.key)), activeGroups=new Set();
      for(const group of this.summary.footprint_groups) {
        const keys=(view.scope==="selected"?group.selected_keys:group.scene_keys).filter(key=>visible.has(key));
        const geometry=view.scope==="selected"?group.selected_footprint:group.footprint;
        if(!keys.length||!geometry)continue;
        const layerKey=JSON.stringify([group.key,view.scope,keys]);activeGroups.add(layerKey);
        const members=keys.map(key=>current.scenes.find(s=>s.key===key));
        const selected=members.some(s=>current.selected.has(s.key));
        let layer=this.groupLayers.get(layerKey);
        if(!layer) {
          layer=L.geoJSON(geometry,{pane:"footprintPane",interactive:true});
          layer.on("click",event=>{if(selectionLocked())return;L.DomEvent.stopPropagation(event);this.openAt(event.latlng);});
          this.groupLayers.set(layerKey,layer);
        }
        layer.setStyle({color:colors.get(group.date)||this.colors[0],dashArray:"6,5",weight:selected?2:1.2,
          opacity:members.some(s=>this.available(s))?selected?1:.65:.35,
          fillColor:colors.get(group.date)||this.colors[0],fillOpacity:view.allocation?.02:selected?.16:.06});
        const times=members.map(s=>s.time).filter(Boolean).sort();
        layer.unbindTooltip();layer.bindTooltip(`${escapeHtml(group.date)} · ${escapeHtml(times[0]||"")} UTC · ${keys.length} 个源产品 · ${escapeHtml([...new Set(members.flatMap(s=>s.tiles||[]))].join(" / "))}`,{sticky:true});
        wanted.push(layer);
      }
      for(const key of this.groupLayers.keys())if(!activeGroups.has(key))this.groupLayers.delete(key);
    } else for(const scene of rows)if(this.layers.has(scene.key))wanted.push(this.layers.get(scene.key));
    const count=wanted.length;
    if(this.hovered&&this.layers.has(this.hovered)&&!wanted.includes(this.layers.get(this.hovered)))wanted.push(this.layers.get(this.hovered));
    const desired=new Set(wanted);
    for(const layer of footprintLayer.getLayers())if(!desired.has(layer))footprintLayer.removeLayer(layer);
    for(const layer of wanted)if(!footprintLayer.hasLayer(layer))footprintLayer.addLayer(layer);
    $("mapFootprintHeading").textContent=grouped?"过境组合 · 点击范围选片":"完整产品 · 点击范围选片";
    $("mapFootprintCount").textContent=rows.length?`地图显示 ${grouped?count+" 组 · ":""}${rows.length} 个产品`:"尚未选片，可切换全部候选或在列表勾选";
    $("mapFootprintNote").textContent=view.allocation?(view.date==="all"?"填色为优先分配；橙色为交叠，红色为未覆盖。":"显示该日期的优先分配，总体覆盖见下方。"):
      grouped?"同次过境合并真实产品范围；可展开逐产品选片。":"虚线框为完整产品范围，颜色区分采集日期。";
    $("mapFootprintDates").innerHTML=[...new Set(rows.map(s=>s.date).filter(Boolean))].sort().map(date=>`<span class="coverage-date"><i style="background:${colors.get(date)}"></i>${escapeHtml(date)}</span>`).join("");
    this.availability();
  },
  renderAllocation(summary) {
    this.allocation?.clearLayers();this.overlap?.clearLayers();this.uncovered?.clearLayers();
    if(!map||!summary)return;
    const current=this.current(), view=this.viewFor(current.id), colors=this.dateColors(current);
    if(!view.allocation)return;
    for(const block of summary.blocks)if(block.selected&&block.assigned&&(view.date==="all"||view.date===block.date))L.geoJSON(block.assigned,{pane:"allocationPane",interactive:false,
      style:{color:colors.get(block.date),weight:0,fillOpacity:.17}}).addTo(this.allocation);
    if(view.date==="all"&&summary.overlap)L.geoJSON(summary.overlap,{pane:"overlapPane",interactive:false,style:{color:"#bb812e",weight:1,fillColor:"#e5ae48",fillOpacity:.12}}).addTo(this.overlap);
    if(view.date==="all"&&summary.uncovered)L.geoJSON(summary.uncovered,{pane:"allocationPane",interactive:false,style:{color:"#be665d",dashArray:"3,4",weight:1,fillOpacity:.12}}).addTo(this.uncovered);
  },
  renderSummary(current,summary) {
    $("coveragePanel").hidden=false;$("mapFootprintLegend").hidden=!map;
    $("spatialPriority").value=this.priorityFor(current.id);
    const chosen=current.scenes.filter(s=>current.selected.has(s.key)), tiles=new Set(chosen.flatMap(s=>s.tiles||[]));
    const sentinel=current.scenes.some(s=>s.products?.some(p=>/^S2[A-Z]_MSIL2A_/.test(p.id)));
    $("coverageSummary").textContent=summary?`已选 ${summary.selected_count} 个产品${sentinel?` · ${summary.acquisition_count} 次过境`:""} · ${summary.tile_count} 个瓦片 · 覆盖 ${summary.coverage_percent.toFixed(2)}%`:
      `已选 ${chosen.length} 个产品 · ${tiles.size} 个瓦片 · 正在计算覆盖`;
    const dates=[...new Set(chosen.map(s=>s.date).filter(Boolean))].sort(), colors=this.dateColors(current);
    const chips=dates.map(date=>`<span class="coverage-date"><i style="background:${colors.get(date)}"></i>${escapeHtml(date)}<b>${chosen.filter(s=>s.date===date).length} 个产品</b></span>`).join("");
    $("coverageDates").innerHTML=dates.length?`<span>${dates.length} 个日期 · UTC</span>${chips}`:"尚未选中产品";
    const keys=summary?.ordered_keys||[...current.selected];
    $("coverageBlockList").innerHTML=keys.map((key,index)=>{
      const scene=current.scenes.find(s=>s.key===key), block=summary?.blocks.find(b=>b.key===key);if(!scene)return "";
      return `<div class="coverage-block"><span><strong>${index+1}. ${escapeHtml(scene.tiles?.join(" / "))} · ${escapeHtml(scene.date)}</strong><small>${escapeHtml(scene.time)} UTC · 云量 ${cloudLabel(scene.cloud_max)}${block?` · 优先分配 ${block.contribution_percent.toFixed(2)}%`:""}</small></span><button type="button" class="inline-link" data-spatial-key="${escapeHtml(key)}" data-move="-1" aria-label="提高 ${escapeHtml(scene.tiles?.join('/'))} ${escapeHtml(scene.date)} 的优先级">↑</button><button type="button" class="inline-link" data-spatial-key="${escapeHtml(key)}" data-move="1" aria-label="降低 ${escapeHtml(scene.tiles?.join('/'))} ${escapeHtml(scene.date)} 的优先级">↓</button><button type="button" class="inline-link danger" data-spatial-key="${escapeHtml(key)}">取消</button></div>`;
    }).join("")||'<span class="field-note">切换全部候选点击影像范围，或勾选下方产品。</span>';
    const regional=["s2-process","s2-earth-search","s2-cdse-s3"].includes(current.profile);
    $("coverageDetails").hidden=!regional;
    $("coverageNote").textContent=(summary?`几何交叠 ${summary.overlap_percent.toFixed(2)}% · 未覆盖 ${summary.uncovered_percent.toFixed(2)}%。`:"")+(!regional?
      "原始产品仍保留完整范围；地图选择控制下载哪些产品。":current.options.layout==="series"?
      "时间序列会逐产品导出，交叠区域会在多个文件中出现。":"已选产品拼成一份研究区影像；云与无数据位置由已选产品补齐，实际日期以来源图为准。覆盖不代表无云。");
    this.availability();
  },
  availability() {
    const locked=selectionLocked(), current=this.current();
    $("recommendCoverage").disabled=locked||!current.query;
    $("fitFootprints").disabled=locked||!map||!footprintLayer?.getLayers().length;
    for(const id of ["mapFootprintScope","mapFootprintDetail","mapFootprintDate","mapAllocation"])$(id).disabled=locked||!map||!this.current().query;
    $("spatialPriority").disabled=locked;
    const blocks=[...$("coverageBlockList").children];
    blocks.forEach((block,index)=>block.querySelectorAll("button").forEach(button=>{
      button.disabled=locked||button.dataset.move==="-1"&&index===0||button.dataset.move==="1"&&index===blocks.length-1;
    }));
    this.popup?.getElement()?.querySelectorAll("[data-map-key]").forEach(input=>{
      const scene=current.scenes.find(s=>s.key===input.dataset.mapKey);input.disabled=locked||!scene||!this.available(scene);
    });
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
    if(selectionLocked())return;
    const current=this.current(), scene=current.scenes.find(s=>s.key===key);if(!scene||!this.available(scene))return;
    if(current.selected.has(key))current.selected.delete(key);else current.selected.add(key);
    this.changed(current);
    const row=[...$("scenesBody").querySelectorAll("tr[data-key]")].find(row=>row.dataset.key===key);
    row?.scrollIntoView({block:"nearest"});
  },
  move(key,direction) {
    if(selectionLocked())return;
    const current=this.current(), keys=[...(this.summary?.ordered_keys||current.selected)], index=keys.indexOf(key), target=index+direction;
    if(index<0||target<0||target>=keys.length)return;
    [keys[index],keys[target]]=[keys[target],keys[index]];this.assign(current,keys);
    const group=Batch.results.get(current.id);
    if(group)Batch.settings(group.profile).options.priority="selection";else this.priorities.set("primary","selection");
    this.changed(this.current());
  },
  async recommend(id=Batch.active,automatic=false) {
    const current=this.current(id);if(!state.aoi||!current.query||selectionLocked())return;
    const generation=this.generation, query=current.query.id, version=this.version;
    try {
      const summary=await api("coverage",this.request(current));
      if(selectionLocked())return;
      if(generation!==this.generation||this.current(id).query?.id!==query||(automatic?this.touched.has(current.id):version!==this.version))return;
      if(summary.recommended_keys.length>500){if(!automatic)toast("推荐结果超过 500 个区块，请缩小日期或研究区后分批下载。",true);return;}
      this.assign(this.current(id),summary.recommended_keys);
      if(!automatic){this.touched.add(current.id);const view=this.viewFor(current.id);view.scope="selected";view.date="all";}
      if(current.id==="primary")renderScenes();else{Batch.renderResults();schedulePlan();this.refresh();}
      if(!automatic)toast(`已推荐 ${summary.recommended_keys.length} 个产品${summary.recommended_omitted_keys?.length?`，清理 ${summary.recommended_omitted_keys.length} 个重复覆盖产品`:""}；可继续手动补充影像。`);
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
    const scenes=this.pointScenes(point);
    if(!scenes.length)return;
    if(this.popup)map.closePopup(this.popup);
    this.point=point;
    this.popup=L.popup({maxWidth:340,minWidth:240,autoPanPadding:[55,55]}).setLatLng(point);
    this.popup.addTo(map);
    this.updatePopup();
  },
  pointScenes(point) {
    const current=this.current(), date=this.viewFor(current.id).date;
    return current.scenes.filter(scene=>(date==="all"||scene.date===date)&&scene.products?.some(p=>this.contains(p.footprint,point)))
      .sort((a,b)=>Number(current.selected.has(b.key))-Number(current.selected.has(a.key))||(a.cloud_max??Infinity)-(b.cloud_max??Infinity)||(b.datetime||"").localeCompare(a.datetime||""));
  },
  updatePopup() {
    if(!this.popup||!this.point)return;
    const current=this.current(), scenes=this.pointScenes(this.point);
    const content=document.createElement("div");content.className="footprint-popup";
    content.innerHTML=`<strong>此处覆盖 ${scenes.length} 个产品</strong><p>已选 ${scenes.filter(s=>current.selected.has(s.key)).length} 个；可勾选其他日期补云。地图与列表同步。</p>`+scenes.map(scene=>`<label><input type="checkbox" data-map-key="${escapeHtml(scene.key)}" aria-label="地图选择 ${escapeHtml(scene.tiles?.join('/'))} ${escapeHtml(scene.date)}" ${current.selected.has(scene.key)?"checked":""} ${!this.available(scene)||selectionLocked()?"disabled":""}><span><b>${escapeHtml(scene.tiles?.join(" / "))} · ${escapeHtml(scene.date)}</b><small>${escapeHtml(scene.time)} UTC · 云量 ${cloudLabel(scene.cloud_max)}${this.available(scene)?"":" · 无可用入口"}</small></span></label>`).join("");
    content.addEventListener("change",event=>{if(event.target.dataset.mapKey)this.toggle(event.target.dataset.mapKey);});
    L.DomEvent.disableClickPropagation(content);L.DomEvent.disableScrollPropagation(content);this.popup.setContent(content);
  },
  highlight(key,on) {
    const current=this.current(), scene=current.scenes.find(s=>s.key===key);if(!scene)return;
    const group=this.layers.get(key);if(!group)return;
    if(on) {
      if(this.hovered===key)return;
      if(this.hovered)this.highlight(this.hovered,false);
      this.hovered=key;if(!footprintLayer.hasLayer(group))footprintLayer.addLayer(group);
    } else {
      if(this.hovered!==key)return;this.hovered=null;
      const view=this.viewFor(current.id), grouped=view.detail==="overpass"&&!!this.summary?.footprint_groups;
      if(grouped||(view.scope==="selected"&&!current.selected.has(key))||(view.date!=="all"&&view.date!==scene.date))footprintLayer.removeLayer(group);
    }
    group.eachLayer(layer=>{layer.setStyle(this.style(scene,current,on));if(on)layer.bringToFront();});
  }
};
