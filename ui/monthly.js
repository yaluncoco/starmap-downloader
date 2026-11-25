"use strict";

const Monthly = {
  query:null, groups:new Map(), active:"",
  supported() {return ["s2-cdse-zip","s2-process","s2-earth-search","s2-cdse-s3"].includes(state.profile);},
  enabled() {return this.supported()&&$("timeMode").value==="monthly";},
  current() {return this.groups.get(this.active);},
  capture() {const group=this.current();if(group){group.selected=state.selected;group.priority=Spatial.priorities.get("primary")||group.priority||"cloud";}},
  count() {this.capture();return [...this.groups.values()].reduce((total,group)=>total+(group.include?group.selected.size:0),0);},
  clear() {this.query=null;this.groups.clear();this.active="";$("monthlyResults").hidden=true;},
  initialize() {
    const today=new Date().toISOString().slice(0,7);
    for(const id of ["startMonth","endMonth"]){$(id).min="2015-06";$(id).max=today;$(id).value=today;$(id).addEventListener("change",clearScenes);}
    $("timeMode").addEventListener("change",()=>{clearScenes();renderProfile();});
    $("monthResultSelect").addEventListener("change",()=>this.activate($("monthResultSelect").value));
    $("monthlyResultTabs").addEventListener("click",event=>{const button=event.target.closest("[data-month]");if(button&&!selectionLocked())this.activate(button.dataset.month);});
    $("monthlyResultTabs").addEventListener("change",event=>{
      const month=event.target.dataset.includeMonth;
      if(!month||selectionLocked())return;
      this.capture();this.groups.get(month).include=event.target.checked;this.renderResults();schedulePlan();
    });
    this.render();
  },
  render() {
    const supported=this.supported();
    $("timeModeField").hidden=!supported;
    if(!supported)$("timeMode").value="dates";
    const enabled=this.enabled();
    $("timeMode").querySelector('option[value="monthly"]').textContent=isZipOnly()?"按月下载 · 保存全部已选 ZIP":"按月下载 · 每月一份拼接影像";
    $("monthlyFields").querySelector("p").textContent=isZipOnly()?"支持跨年；每月独立选择产品，保存该月全部已选完整 ZIP。无匹配月份不生成文件。":"支持跨年；每个月独立选择来源，先下载整景，再本地拼接并裁剪完整研究区。无匹配月份不生成文件。";
    $("monthlyFields").hidden=!enabled;$("dateFields").hidden=enabled||!dateEnabled();
    $("companionSection").hidden=enabled;
    $("imageLayout").hidden=enabled||isNativeS3()||isZip();document.querySelector('label[for="imageLayout"]').hidden=enabled||isNativeS3()||isZip();
    if(enabled){$("imageLayout").value="mosaic";$("mosaicFallbackField").hidden=false;}
    $("mosaicDescription").textContent=enabled?"先下载本月全部已选整景，再本地拼接整月影像，最后按完整研究区裁剪。整景和整月影像保留；重叠位置默认云量较低优先，可在该月地图和列表中调整。":"在地图上选择影像块。交叠处默认云量较低优先；像元来源图保留实际产品和日期。未被选中影像覆盖的位置为 NoData。";
    if(enabled){$("searchScenes").querySelector("span").textContent="按月查找覆盖影像";$("downloadSelected").querySelector("span").textContent="下载已选月份";}
    this.renderResults();updateAvailability();
  },
  accept(query, restore=false) {
    this.query=query;this.groups.clear();
    for(const row of query.groups)this.groups.set(row.month,{...row,selected:new Set(row.recommended_keys||[]),include:true,priority:"cloud"});
    $("timeMode").value="monthly";$("startMonth").value=query.start_month;$("endMonth").value=query.end_month;
    this.active=query.groups.find(row=>row.status==="ready")?.month||query.groups[0]?.month;
    this.activate(this.active,true);this.render();
  },
  activate(month, initial=false) {
    if(!initial)this.capture();
    const group=this.groups.get(month);if(!group)return;
    this.active=month;state.scenes=group.scenes;state.query=group.query;state.selected=group.selected;
    Spatial.priorities.set("primary",group.priority);Spatial.summary=null;
    this.renderResults();renderScenes(!initial);renderEstimate();
    if(!group.scenes.length){$("scenesEmpty").querySelector("strong").textContent=group.status==="failed"?`${month} 检索失败`:`${month} 没有匹配影像`;$("scenesEmpty").querySelector(":scope > span").textContent=group.error||"该月不生成影像，不会使用相邻月份的产品补缺。";}
  },
  renderResults() {
    const visible=this.enabled()&&!!this.query;
    $("monthlyResults").hidden=!visible;if(!visible)return;
    const rows=[...this.groups.values()];
    $("monthlySelectionSummary").textContent=`${rows.filter(row=>row.include&&row.selected.size).length} 个月已选产品 · ${isZipOnly()?"保存全部已选 ZIP":"每月一份拼接影像"}`;
    $("monthResultSelect").innerHTML=rows.map(row=>`<option value="${escapeHtml(row.month)}">${escapeHtml(row.month)} · ${row.scenes.length} 个候选${row.status==="failed"?" · 检索失败":""}</option>`).join("");$("monthResultSelect").value=this.active;
    $("monthlyResultTabs").innerHTML=rows.map(row=>`<div class="monthly-month ${row.month===this.active?"active":""}"><label title="下载本月；无匹配月份不会生成文件"><input type="checkbox" data-include-month="${escapeHtml(row.month)}" aria-label="下载 ${escapeHtml(row.month)}" ${row.include?"checked":""} ${selectionLocked()?"disabled":""}></label><button type="button" data-month="${escapeHtml(row.month)}" ${selectionLocked()?"disabled":""}><strong>${escapeHtml(row.month)}</strong><span>${row.status==="failed"?"检索失败":row.scenes.length?`${row.selected.size} / ${row.scenes.length} 产品`:"无匹配"}</span></button></div>`).join("");
  },
  searchRequest() {return {...Cities.request(),profile:state.profile,start_month:$("startMonth").value,end_month:$("endMonth").value,max_cloud:Number($("maxCloud").value),options:{priority:"cloud"}};},
  request(base) {
    this.capture();const global=options();
    return {...Cities.request(),profile:state.profile,monthly_id:this.query.id,start_month:$("startMonth").value,end_month:$("endMonth").value,output_dir:base.output_dir,
      months:[...this.groups.values()].map(group=>({month:group.month,include:group.include,query_id:group.query.id,criteria:{...group.criteria,max_cloud:Number($("maxCloud").value)},scene_keys:[...group.selected],options:{...global,layout:"mosaic",priority:group.priority}}))};
  },
  spatialRequest(current) {return {...Cities.request(),profile:state.profile,monthly_id:this.query.id,month:this.active,query_id:current.query.id,criteria:current.criteria,scene_keys:[...current.selected],options:{priority:this.current().priority}};},
  planHtml(plan) {
    const zip=plan.archive_only;
    const rows=[["研究区域",plan.boundary_name+" · "+(plan.region?.scope||"完整所选边界")],["月份范围",`${plan.start_month} — ${plan.end_month} (UTC)`],["输出数量",zip?`${plan.scene_count} 个完整 ZIP · ${plan.output_count} 个月`:`${plan.output_count} 个月 · 每月一份城市裁剪影像`],["时间与来源",plan.time_note],["下载顺序",zip?"按月份依次执行，每月按所选并发下载全部 ZIP；已完成文件经校验后复用。":isZip()?"按月份依次执行，每月并发下载并校验全部 ZIP，再本地拼接与裁剪；各阶段可继续。":"按月份依次处理；每月先下载全部已选整景，再本地拼接，最后裁剪。各阶段可继续，已完成文件核验后复用"]];
    if(isZip()){
      const choices=plan.months.filter(row=>row.plan).map(row=>row.options);
      rows.push(["下载方式",[...new Set(choices.map(o=>zipEngineLabel(o.download_engine)))].join(" / ")],["同时下载",[...new Set(choices.map(o=>`最多 ${o.download_workers??2} 个完整产品`))].join(" / ")]);
    }
    if(!zip)rows.splice(3,0,["城市裁剪成果",`${sizeLabel(plan.estimated_bytes)}（未压缩，含配套图层）`]);
    if(plan.native_source_bytes)rows.push([state.profile==="s2-process"?"整景导出":isZip()?"完整 ZIP 体积估算":"整景原件",`约 ${sizeLabel(plan.native_source_bytes)}，下载后保留并可复用`]);
    if(plan.whole_mosaic_bytes)rows.push(["整月中间影像",`${sizeLabel(plan.whole_mosaic_bytes)}（未压缩，拼接后保留）`]);
    if(plan.staging_required_bytes)rows.push(["输出及临时空间",`按未压缩数据估算至少 ${sizeLabel(plan.staging_required_bytes)}`]);
    if(plan.estimated_pu)rows.push(["处理额度",`约 ${plan.estimated_pu.toFixed(2)} PU`]);
    return `<div class="plan-grid">${rows.map(([label,value])=>`<div><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`).join("")}</div><div class="monthly-plan-list">${plan.months.map(row=>`<div><strong>${escapeHtml(row.month)}</strong><span>${row.plan?row.plan.kind==="original"?`${row.selected_count} 个完整 ZIP · 产品覆盖 ${row.plan.coverage_percent.toFixed(2)}%`:`${row.selected_count} 个产品 · 覆盖 ${row.plan.coverage_percent.toFixed(2)}% · ${row.plan.resolution} 米`:row.status==="search_failed"?"检索失败，需要重新检索":row.status==="empty"?"无匹配，不生成文件":"未选择，不生成文件"}</span>${row.plan?`<small>${escapeHtml(row.options.label||"完整官方 SAFE ZIP")}</small>`:""}</div>`).join("")}</div><div class="plan-folder"><span>保存到</span><strong id="planPreviewFolder"></strong></div>`;
  },
  progress(groups=[]) {
    if(!groups.length)return;
    $("jobSources").hidden=false;
    const labels={pending:"等待",searching:"检索中",ready:"可选择",running:"下载中",completed:"已完成",empty:"无匹配",not_selected:"未选择",no_valid_pixels:"无有效像元",failed:"失败",search_failed:"检索失败",cancelled:"已取消"};
    const stages={download_scenes:"下载全部整景",local_mosaic:"本地整月拼接",clip_region:"裁剪完整研究区"};
    $("jobSources").innerHTML=groups.map(row=>`<div class="job-source ${row.status==="failed"?"failed":""}"><strong>${escapeHtml(row.month||row.id)}</strong><span>${escapeHtml(row.status==="running"&&stages[row.stage]?stages[row.stage]:labels[row.status]||row.status)}</span>${row.error?`<small>${escapeHtml(row.error)}</small>`:""}</div>`).join("");
  }
};
