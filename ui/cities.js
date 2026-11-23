"use strict";

const Cities = {
  catalogue:null,
  request() { return state.aoi?.region?.city_id ? {city_id:state.aoi.region.city_id} : {geometry:state.aoi.geometry}; },
  async initialize() {
    $("cityProvince").addEventListener("change",()=>this.render());
    $("cityFilter").addEventListener("input",()=>{if($("cityFilter").value.trim())$("cityProvince").value="";this.render();});
    $("cityChoice").addEventListener("change",()=>this.describe());
    $("applyCity").addEventListener("click",()=>this.apply($("cityChoice").value));
    $("reloadCities").addEventListener("click",()=>this.load());
    await this.load();
  },
  async load() {
    $("cityStatus").textContent="读取本地完整行政边界…";
    try {
      this.catalogue=await api("cities");
      $("cityProvince").innerHTML='<option value="">全部省级行政区</option>'+this.catalogue.provinces.map(row=>`<option value="${escapeHtml(row.id)}">${escapeHtml(row.name)}</option>`).join("");
      $("cityProvince").value="110000";
      $("reloadCities").hidden=true;this.render();
    } catch(error) {$("cityStatus").textContent=error.message;$("reloadCities").hidden=false;}
    updateAvailability();
  },
  render() {
    if(!this.catalogue)return;
    const province=$("cityProvince").value, term=$("cityFilter").value.trim(), previous=$("cityChoice").value;
    const rows=this.catalogue.cities.filter(row=>(!province||row.province_id===province)&&`${row.province}${row.name}`.includes(term));
    $("cityChoice").innerHTML=rows.length?rows.map(row=>`<option value="${escapeHtml(row.id)}">${escapeHtml(row.name)}${province?"":" · "+escapeHtml(row.province)}</option>`).join(""):'<option value="">没有匹配的行政区域</option>';
    if(rows.some(row=>row.id===previous))$("cityChoice").value=previous;
    this.describe();
  },
  describe() {
    const row=this.catalogue?.cities.find(row=>row.id===$("cityChoice").value);
    $("cityStatus").textContent=row?`${row.scope} · ${friendlyArea(row.area_km2)} · WGS84 · 边界版本 ${this.catalogue.source.version||this.catalogue.source.date||"2024"}`:"可切换省级行政区或调整城市名称。";
    $("applyCity").disabled=!row||state.busy||state.connecting||state.aoiPending;
  },
  async apply(id) {
    if(!id||state.busy||state.connecting||state.aoiPending)return;
    try {
      const result=await mutateAoi({city_id:id});
      if(!result)return;
      activateAoiTab("city");
      if(Monthly.supported()){$("timeMode").value="monthly";Monthly.render();}
      $("cityProvince").value=this.catalogue?.cities.find(row=>row.id===id)?.province_id||"";
      $("cityFilter").value="";this.render();$("cityChoice").value=id;this.describe();
      toast(`${result.name}的完整行政区域已设置，可导出 SHP 并选择下载月份。`);
    } catch(error) {toast(error.message,true);}
  }
};
