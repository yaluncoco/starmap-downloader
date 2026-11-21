"use strict";

const $ = (id) => document.getElementById(id);
const appToken = document.querySelector('meta[name="app-token"]').content;
const state = {profile:"s2-process",collection:"",collections:[],gediCollections:[],gediFieldCollection:"",earthdataConnected:false,loadingCollections:false,rawConnected:false,s3Connected:false,connected:false,connecting:false,nativeDialogs:false,aoi:null,scenes:[],selected:new Set(),pickedFiles:new Set(),fileScene:"",busy:false,jobId:null,plan:null,planVersion:0,resultFolder:"",places:[],editing:false,query:null};
const updates = {configured:false, checking:false, working:false, release:null};
let map, aoiLayer, footprintLayer, baseLayer, drawHandler, editHandler, planTimer;
const modes = {
  research:"B02 / B03 / B04 / B08 / B05 / B06 / B07 / B8A / B11 / B12；配套保存 SCL、未掩膜反射率和逐像元来源。20 米波段最近邻对齐到所选网格。",
  rgbnir:"B04 / B03 / B02 / B08，Float32 派生反射率；仅保存这 4 个波段。",
  rgb:"B04 / B03 / B02，8 位 RGBA 浏览图，亮度增强 2.5 倍。",
  all:"L2A 的 12 个反射率波段；20/60 米原生波段会重采样。此模式只保存光谱 TIFF。",
  ndvi:"(B08 − B04) / (B08 + B04)，Float32，保留真实指数值。",
  custom:"勾选需要的波段；原生分辨率不同的波段会重采样。"
};
const profiles = {
  "s2-process":["Sentinel-2 L2A","Sentinel Hub · 区域裁剪","同一次过境的相邻瓦片可拼接。使用 Sentinel Hub OAuth，消耗处理额度。"],
  "s2-earth-search":["Sentinel-2 L2A","Earth Search · 公开裁剪","选择一个 Collection‑1 瓦片，按研究区导出。无需凭据，也不会自动替换成其他影像。"],
  "s2-l2a-cog":["Sentinel-2 L2A","Earth Search / AWS · 整瓦片 COG","可选地表反射率、场景分类、云雪概率、水汽、气溶胶、真彩和元数据。研究区只筛选瓦片；保留原始 DN，不裁剪、不重新打包成 SAFE。"],
  "s2-l3-quarterly":["Sentinel-2 L3 季度镶嵌","CDSE 目录 / Source Cooperative · 整瓦片 COG","按整季度与日期范围的重叠检索，不是单日过境。可选 B02、B03、B04、B08；保留原网格和 DN。镜像缺少精确记录时不会替换产品。"],
  "s2-l1c-aws":["Sentinel-2 L1C","Earth Search / AWS · 原始文件","选择同一瓦片的 JP2 波段及配套元数据。保持源像元和原始值，不按研究区裁剪。"],
  "landsat-pc-l2":["Landsat Collection 2 L2","USGS / Microsoft Planetary Computer","可选原始反射率、热红外、质量标识及元数据。按产品提供的文件下载，不裁剪，不需要个人密钥。"],
  "s1-aws-original":["Sentinel-1 GRD","Earth Search / AWS · 原始文件","按区域和日期检索；可下载所列测量文件和元数据，不裁剪、不重组完整 SAFE，也不替代 CDSE 产品。"],
  "s1-slc-seasia":["Sentinel-1 IW SLC","ASF DAAC / AWS · 东南亚历史存档","只下载存档中的精确原始 SAFE ZIP，保留复数 I/Q、子波束与极化。不裁剪，也不用 GRD 或 RTC 替代。"],
  "s1-rtc-pc":["Sentinel-1 RTC","Planetary Computer · 雷达区域裁剪","按区域导出地形校正后的线性 γ⁰ 后向散射，可选极化。程序请求临时读取授权；若源限制访问，不会换成 GRD 原件或别的景。"],
  "sen3-aws-original":["Sentinel-3","CDSE 目录 / MEEO 公开档案","选择 SLSTR、OLCI、SYNERGY 或 SRAL 原始数据。先核对精确版本和清单；缺失时显示原因，不会换版。整轨或完整产品可能需要数 GB 空间。"],
  "s5p-aws-l2":["Sentinel-5P TROPOMI L2","CDSE 目录 / MEEO 公开档案","核对产品版本并下载原始 NetCDF。各处理流日期覆盖不同；公开档案若缺该版本，不会换成别的条带或格式。"],
  "gedi-earthdata":["NASA GEDI","Earthdata · 激光足迹与森林参数","选择高度、覆盖度、垂直结构、生物量或栅格产品。保留精确版本的原始文件，可按研究区导出参数，并对齐之前下载的影像。"],
  "worldcover-s2":["ESA WorldCover · Sentinel-2","ESA / AWS · 年度光学合成 COG","原始 1° 瓦片，按瓦片勾选 RGBNIR、NDVI 百分位或 SWIR。保留原始 DN、NoData 与缩放；SWIR 约 20 米。"],
  "worldcover-s1":["ESA WorldCover · Sentinel-1","ESA / AWS · 年度雷达合成 COG","原始 1° Gamma0 瓦片，含 VV、VH、VH/VV。并非单次过境；缺少雷达层时不会用光学或其他年份替换。"],
  "worldcover-map":["ESA WorldCover · 土地覆盖","ESA / AWS · 年度分类图 COG","原始 3° 分类瓦片，约 10 米。保留 11 类编码、NoData 和调色板；2020 / 2021 使用不同算法版本。"],
  "modis-13Q1-061":["MODIS 植被指数 · 13Q1","NASA / Planetary Computer · 250 米","Terra / Aqua，16 天合成。可选 NDVI、EVI、反射率与 QA 的源 COG，或原始 HDF4。"],
  "modis-09Q1-061":["MODIS 地表反射率 · 09Q1","NASA / Planetary Computer · 250 米","Terra / Aqua，8 天合成。红光、近红外及质量位字段。保留完整瓦片。"],
  "modis-11A2-061":["MODIS 地表温度 · 11A2","NASA / Planetary Computer · 1 千米","Terra / Aqua，8 天合成。昼夜温度、发射率、观测时间与质量层。温度物理单位为开尔文。"],
  "modis-15A2H-061":["MODIS 叶面积指数 / FPAR · 15A2H","NASA / Planetary Computer · 500 米","Terra、Aqua 或两星合成，8 天。包含 LAI、FPAR、标准差及 QA。不裁剪。"],
  "modis-43A4-061":["MODIS NBAR · 43A4","NASA / Planetary Computer · 500 米","Terra + Aqua，按日发布的 16 天加权反射率合成。可选 7 个波段及对应 QA。"],
  "dem-30":["Copernicus DEM GLO-30","AWS · 公开数据","静态数字表面模型（DSM），含建筑和植被高度，并非裸地高程。可按研究区裁剪或取完整瓦片。"],
  "dem-90":["Copernicus DEM GLO-90","AWS · 公开数据","静态数字表面模型（DSM）。可按研究区裁剪或取完整瓦片。"],
  "cdse-original":["Copernicus 官方目录","CDSE · 原始产品","按区域检索，但下载保留完整覆盖范围。目录中部分产品没有此工具可用的下载入口。"]
};
const isOfficial = () => state.profile === "cdse-original";
const isRadar = () => state.profile === "s1-aws-original";
const isSlc = () => state.profile === "s1-slc-seasia";
const isRtc = () => state.profile === "s1-rtc-pc";
const rtcInputs = () => [...document.querySelectorAll("#rtcBands input")];
const isLandsat = () => state.profile === "landsat-pc-l2";
const isOpenOptical = () => isLandsat() || ["s2-l1c-aws","s2-l2a-cog"].includes(state.profile);
const isL3 = () => state.profile === "s2-l3-quarterly";
const isWorldCover = () => state.profile.startsWith("worldcover-");
const isWorldCoverS2 = () => state.profile === "worldcover-s2";
const isWorldCoverSingle = () => isWorldCover() && !isWorldCoverS2();
const isModis = () => state.profile.startsWith("modis-");
const isNbar = () => state.profile === "modis-43A4-061";
const isS5p = () => state.profile === "s5p-aws-l2";
const isSen3 = () => state.profile === "sen3-aws-original";
const isGedi = () => state.profile === "gedi-earthdata";
const gediProduct = () => state.gediCollections.find(item=>item.id===$("gediCollection").value);
const isGediRaster = () => isGedi() && gediProduct()?.kind==="raster";
const isRaw = () => isOfficial() || isRadar() || isSlc() || isOpenOptical() || isL3() || isS5p() || isSen3() || isWorldCover() || isModis();
const rawRoute = () => isOfficial()?$("rawTransport").value:(isLandsat()||isModis())?"pc-public":"public";
const isProcess = () => state.profile === "s2-process";
const isDem = () => state.profile.startsWith("dem-");
const primaryDateEnabled = () => !isDem() && !(isOfficial() && (Batch.enabled() ? Batch.isStatic(state.profile, state.collection) : $("allDates").checked));
const dateEnabled = () => primaryDateEnabled() || Batch.enabled();
const canSearch = () => state.aoi && !state.busy && !state.connecting && (!isProcess() || state.connected) && (!isOfficial() || state.collection) && (!isS5p() || $("s5pCollection").value) && (!isSen3() || $("sen3Collection").value) && (!isGedi() || $("gediCollection").value) && Batch.searchReady();
const transportLabel = id => ({odata:"CDSE OData","cdse-s3":"CDSE S3",earthdata:"NASA Earthdata","sentinel-hub":"Sentinel Hub OAuth",public:isL3()?"Source Cooperative":"公开 AWS","pc-public":"Microsoft Planetary Computer"}[id] || id);
const missingAccess = () => (state.plan?.requires || []).filter(id => id === "odata" ? !state.rawConnected : id === "cdse-s3" ? !state.s3Connected : id === "earthdata" ? !state.earthdataConnected : id === "sentinel-hub" ? !state.connected : true);
const hasDownloadAccess = () => state.plan?.kind === "batch" ? missingAccess().length === 0 : isProcess() ? state.connected : missingAccess().length === 0;
const selectable = (scene) => isGedi() ? scene.gedi?.available === true : !isRaw() || scene.raw?.available === true;
const allBands = ["B01","B02","B03","B04","B05","B06","B07","B08","B8A","B09","B11","B12"];
const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const icon = (name) => `<svg class="icon" aria-hidden="true"><use href="#i-${name}"/></svg>`;
const basename = (path) => String(path).split(/[\\/]/).pop();

async function api(route, data) {
  const headers = {"X-App-Token":appToken};
  const init = data === undefined ? {cache:"no-store",headers} : {method:"POST",headers:{...headers,"Content-Type":"application/json"},body:JSON.stringify(data)};
  const response = await fetch("/api/" + route, init);
  let value;
  try { value = await response.json(); } catch { throw new Error("本地服务没有响应，请重新启动软件。"); }
  if (!response.ok) throw new Error(value.error || "操作失败，请重试。");
  return value;
}

function toast(message, error=false, duration=7500) {
  const element = document.createElement("div");
  element.className = "toast" + (error ? " error" : "");
  element.innerHTML = `${icon(error?"info":"check")}<span>${escapeHtml(message)}</span><button class="icon-button" aria-label="关闭提示">${icon("close")}</button>`;
  element.querySelector("button").addEventListener("click",()=>element.remove());
  $("toastContainer").appendChild(element);
  setTimeout(()=>element.remove(),duration);
}

function friendlyArea(km2) {
  return km2 >= 1 ? `${km2.toLocaleString("zh-CN",{maximumFractionDigits:2})} km²` : `${Math.round(km2*1e6).toLocaleString("zh-CN")} m²`;
}
function sizeLabel(bytes) {
  if(bytes < 1024**2)return bytes < 1024 ? "小于 1 KB" : `${Math.round(bytes/1024)} KB`;
  if(bytes < 1024**3)return `${(bytes/1024**2).toFixed(1)} MB`;
  return `${(bytes/1024**3).toFixed(2)} GB`;
}
function updateButtons() {
  const available=updates.release?.status==="available", canDownload=available&&updates.release.installer_bytes>0;
  $("retryUpdate").disabled=updates.checking||updates.working;
  $("openUpdateRelease").hidden=!updates.configured||updates.checking;
  $("openUpdateRelease").disabled=updates.working;
  $("downloadUpdate").hidden=!canDownload;
  $("downloadUpdate").disabled=updates.working||state.busy||state.connecting;
  $("updateNote").hidden=!canDownload;
}
async function checkUpdates() {
  const dialog=$("updateDialog");
  if(!dialog.open)dialog.showModal();
  if(updates.checking||updates.working)return;
  updates.checking=true;updates.release=null;
  $("retryUpdate").hidden=true;
  $("updateMessage").textContent="正在检查 GitHub 稳定版…";updateButtons();
  try{
    const result=await api("check-update",{});updates.release=result;
    switch(result.status){
      case "unconfigured": $("updateMessage").textContent="此构建未配置 GitHub 更新渠道。";break;
      case "not_published": $("updateMessage").textContent="没有找到可用的公开稳定版，可能尚未发布。";break;
      case "current": $("updateMessage").textContent=result.ahead
        ?`已安装版本高于 GitHub 稳定版 v${result.latest_version}，无需更新。`:"当前已是最新稳定版。";break;
      case "available": $("updateMessage").textContent=result.installer_bytes
        ?`发现 v${result.latest_version} · 安装包 ${sizeLabel(result.installer_bytes)}`
        :`发现 v${result.latest_version}，但没有可校验的 Windows x64 安装包。`;break;
      default: throw new Error("无法识别更新状态，请稍后再试。");
    }
    $("retryUpdate").hidden=result.status==="unconfigured";
  }catch(error){
    $("updateMessage").textContent=error.message;$("retryUpdate").hidden=false;
  }finally{updates.checking=false;updateButtons();}
}
async function updateAction(download) {
  if(!updates.configured||updates.checking||updates.working||(download&&(!updates.release?.tag||state.busy||state.connecting)))return;
  updates.working=true;updateButtons();
  try{
    const result=await api(download?"download-update":"open-update-release",{tag:updates.release?.tag||""});
    if(download){
      $("updateDialog").close();state.jobId=result.job_id;setBusy(true);showDownloadProgress("update");pollJob(state.jobId);
    }
  }catch(error){$("updateMessage").textContent=error.message;}
  finally{updates.working=false;updateButtons();}
}
function displayPath(path) { return path.startsWith("\\\\?\\UNC\\") ? "\\\\" + path.slice(8) : path.startsWith("\\\\?\\") ? path.slice(4) : path; }
function criteria() {
  const result = {start:primaryDateEnabled()?$("startDate").value:null,end:primaryDateEnabled()?$("endDate").value:null};
  if(isOfficial())return {...result,collection:state.collection,radar_mode:state.collection==="sentinel-1-grd"?$("radarMode").value:""};
  if(isS5p()||isSen3())return {...result,collection:$(isSen3()?"sen3Collection":"s5pCollection").value};
  if(isGedi())return {...result,collection:$("gediCollection").value};
  if(isRadar()||isRtc())return {...result,radar_mode:$("radarMode").value};
  if(isDem()||isL3()||isWorldCover()||isModis()||isSlc())return result;
  return {...result,max_cloud:Number($("maxCloud").value)};
}
function options() {
  if(isGedi())return {mode:$("gediMode").value,fields:[...document.querySelectorAll("#gediFields input:checked")].map(input=>input.value),beams:[...document.querySelectorAll("#gediBeams input:checked")].map(input=>input.value),quality:$("gediQuality").value,nondegraded:$("gediNondegraded").checked,min_sensitivity:Number($("gediSensitivity").value),resolution:Number($("gediResolution").value),reference_path:$("gediReference").value.trim(),sample_reference:$("gediSampleReference").checked};
  if(isS5p()||isWorldCoverSingle()||isSlc())return {mode:"original",transport:"public"};
  if(isRaw())return {mode:$("rawMode").value,asset:$("rawAsset").value,assets:[...state.pickedFiles].sort(),transport:rawRoute()};
  if(isDem())return {mode:$("demMode").value};
  if(isRtc())return {mode:"rtc",resolution:Number($("rtcResolution").value),bands:rtcInputs().filter(input=>input.checked).map(input=>input.value)};
  return {mode:$("imageMode").value,resolution:Number($("resolution").value),max_cloud:Number($("maxCloud").value),mask_clouds:$("maskClouds").checked,bands:[...document.querySelectorAll("#customBands input:checked")].map(input=>input.value)};
}
function downloadRequest() {
  const result={geometry:state.aoi.geometry,profile:state.profile,criteria:criteria(),query_id:state.query?.id,scene_keys:[...state.selected],options:options(),output_dir:$("outputDirectory").value.trim()};
  return Batch.query?Batch.downloadRequest(result):result;
}
function renderCollectionPicker() {
  const term=$("collectionFilter").value.trim().toLocaleLowerCase().replaceAll("‑","-");
  const groups=new Map();
  for(const category of state.collections){
    if(!`${category.id} ${category.title} ${category.group}`.toLocaleLowerCase().includes(term)&&category.id!==state.collection)continue;
    if(!groups.has(category.group))groups.set(category.group,[]);
    groups.get(category.group).push(category);
  }
  $("rawCollection").innerHTML='<option value="">选择官方类别…</option>'+[...groups].map(([group,items])=>`<optgroup label="${escapeHtml(group)}">${items.map(item=>`<option value="${escapeHtml(item.id)}">${escapeHtml(item.title)} · ${escapeHtml(item.id)}</option>`).join("")}</optgroup>`).join("");
  $("rawCollection").value=state.collection;
  if(!state.loadingCollections&&state.collections.length){
    $("collectionsStatus").textContent=`显示 ${$("rawCollection").options.length-1} / ${state.collections.length} 个官方类别`;
  }
}
async function loadCollections(refresh=false) {
  if(state.loadingCollections||!refresh&&state.collections.length)return;
  state.loadingCollections=true;$("collectionsStatus").textContent="正在读取 Copernicus 官方分类…";updateAvailability();
  try{
    const data=await api("collections",{refresh});state.collections=data.collections;
    if(state.collection&&!state.collections.some(x=>x.id===state.collection)){state.collection="";clearScenes();}
    state.loadingCollections=false;renderCollectionPicker();
  }catch(error){$("collectionsStatus").textContent=`无法加载官方目录：${error.message} 可点击刷新重试。`;}
  finally{state.loadingCollections=false;updateAvailability();}
}
function renderOpenCollections(select, items, selected, includeStream = false) {
  const groups=new Map();
  for(const item of items){
    if(!groups.has(item.group))groups.set(item.group,[]);
    groups.get(item.group).push(item);
  }
  $(select).innerHTML=[...groups].map(([group,items])=>`<optgroup label="${escapeHtml(group)}">${items.map(item=>`<option value="${escapeHtml(item.id)}">${escapeHtml(item.title)}${includeStream?" · "+escapeHtml(item.id.split("-").at(-1).toUpperCase()):""}</option>`).join("")}</optgroup>`).join("");
  $(select).value=selected;
}
function renderProfile() {
  $("allDates").closest("label").hidden=Batch.enabled();
  const [dataset,source,description]=profiles[state.profile];
  $("headerDataset").textContent=dataset;$("headerSource").textContent=source;$("sourceDescription").textContent=isOfficial()&&state.collection==="sentinel-2-l2a"?"下载官方完整 Sentinel-2 L2A SAFE ZIP，保留全部波段、质量与大气图层、角度网格和产品元数据。研究区只筛选产品，整包不裁剪。":description;
  for(const id of ["rawCollectionFields","rawTransportField"])$(id).hidden=!isOfficial();
  $("s5pCollectionFields").hidden=!isS5p();
  $("sen3CollectionFields").hidden=!isSen3();
  for(const id of ["gediCollectionFields","gediOptions"])$(id).hidden=!isGedi();
  renderGediOptions();
  $("worldcoverInfo").hidden=!isWorldCover();$("modisInfo").hidden=!isModis();
  $("modisDateNote").textContent=isNbar()?"按 UTC 标称日筛选（16 天输入窗口的第 9 天）。并非当日单景；研究区只筛选整瓦片。":
    "按 UTC 标称日筛选（合成起始日），起止日期均包含。研究区只筛选整瓦片；不裁剪、不重写源数据。";
  $("sceneDateHeading").textContent=isGediRaster()?"汇总期起始":isGedi()?"文件观测起始":isModis()?"标称日":"采集时间";
  $("coverageHeading").textContent=isGedi()?"有效观测":"区域覆盖";
  $("slcInfo").hidden=!isSlc();
  $("rawOptions").hidden=!isRaw()||isS5p()||isWorldCoverSingle()||isSlc();$("demOptions").hidden=!isDem();
  $("rtcOptions").hidden=!isRtc();
  const whole = $("rawWholeProduct");
  whole.hidden=whole.disabled=!(isOfficial()||isSen3()||isModis());
  whole.textContent=isOfficial()&&state.collection==="sentinel-2-l2a"?"完整 Sentinel-2 SAFE ZIP（全部文件，不裁剪）":isModis()?"每个产品的原始 HDF4（全部层）":isSen3()?"完整 SEN3（清单 + 全部成员）":"整包或官方单主文件（不裁剪）";
  const noMetadata = isL3()||isWorldCover();
  $("rawMetadataOnly").hidden=$("rawMetadataOnly").disabled=noMetadata;
  $("rawMetadataOnly").textContent=isModis()?"每个产品的 NASA 配套 XML（如已列出）":"每个产品的一份元数据";
  if(isWorldCoverSingle()||isSlc())$("rawMode").value="original";
  else if((noMetadata&&$("rawMode").value==="metadata")||(!isOfficial()&&!isSen3()&&!isS5p()&&!isModis()&&$("rawMode").value==="original"&&isRaw()))$("rawMode").value=isRadar()?"metadata":"assets";
  Accounts.visibility();$("publicAuth").hidden=isOfficial()||isProcess()||isGedi();
  $("opticalOptions").hidden=isRaw()||isDem()||isRtc()||isGedi();$("cloudOptions").hidden=isOfficial()||isRadar()||isRtc()||isDem()||isS5p()||isSen3()||isL3()||isWorldCover()||isModis()||isSlc()||isGedi();$("dateFields").hidden=!dateEnabled();
  $("radarModeField").hidden=!(isRadar()||isRtc()||(isOfficial()&&state.collection==="sentinel-1-grd"));
  for(const option of $("radarMode").options){option.disabled=option.hidden=isRtc()&&["SM","WV"].includes(option.value);}
  for(const id of ["startDate","endDate"])$(id).min=isGedi()?"2019-04-04":isOfficial()||isWorldCover()?"1900-01-01":isModis()?"2000-01-01":isSlc()?"2014-10-06":isRadar()||isRtc()?"2014-04-03":isLandsat()?"1982-01-01":isS5p()?"2018-01-01":isSen3()?"2016-02-16":isL3()?"2017-01-01":"2015-06-23";
  $("tileHeading").textContent=isGediRaster()?"栅格参数 / 版本":isGedi()?"GEDI 足迹文件 / 版本":isS5p()?"原始条带 / 处理流":isSen3()?"原始产品 / 档案状态":isL3()?"镶嵌瓦片 / 镜像状态":isWorldCover()?"年度瓦片 / 档案状态":isModis()?"MODLAND 瓦片 / 合成窗口":isSlc()?"原始产品 / 极化":isRaw()?"原始产品 / 模式":isRtc()?"成像模式 / 极化":"瓦片";
  $("searchScenes").querySelector("span").textContent=isGedi()?"检索 GEDI 数据":isWorldCover()?"查找年度瓦片":isModis()?"查找 MODIS 瓦片":isRaw()?"检索原始产品":isDem()?"查找 DEM 瓦片":"查找可下载影像";
  $("downloadSelected").querySelector("span").textContent=Batch.enabled()?"下载主数据及附带数据":isGedi()?"下载选中 GEDI":"下载选中影像";
  if(Batch.enabled())$("searchScenes").querySelector("span").textContent="检索主数据及附带数据";
  Batch.renderChoices();
  renderAuth();
}

function renderGediOptions() {
  if(!isGedi())return;
  const product=gediProduct();if(!product)return;
  const raster=product.kind==="raster", mode=$("gediMode");
  $("gediFootprintsMode").disabled=$("gediFootprintsMode").hidden=raster;
  if(raster&&mode.value==="footprints")mode.value="aligned";
  if(state.gediFieldCollection!==product.id){
    state.gediFieldCollection=product.id;
    $("gediFields").innerHTML=product.fields.map(field=>`<label class="asset-entry"><input type="checkbox" value="${escapeHtml(field.id)}" ${field.default?"checked":""} data-lock><span><strong>${escapeHtml(field.title)}</strong><small>${escapeHtml(field.id)}${field.units?" · "+escapeHtml(field.units):""}${["profile","waveform"].includes(field.kind)?" · 仅写入表格":""}</small></span></label>`).join("");
    $("gediResolution").value=raster?"1000":"10";
    if(product.level==="L1B")$("gediSensitivity").value="0";
  }
  $("gediProductNote").textContent=`${product.kind==="raster"?"全球汇总栅格；日期与汇总期重叠即匹配，不代表该日观测。":"约 25 米离散足迹；日期同时筛选文件和导出的实际足迹。"} 数据起始 ${product.first_date}${product.last_date?"，本版本截至 "+product.last_date:"；具体已发布日期以检索结果为准"}。`;
  $("gediDerivedOptions").hidden=mode.value==="original";
  $("gediFootprintFields").hidden=raster;
  $("gediGridFields").hidden=mode.value!=="aligned";
  const reference=Boolean($("gediReference").value.trim());
  $("clearGediReference").hidden=!reference;
  $("gediSampleField").hidden=raster||!reference;
  if(raster||!reference)$("gediSampleReference").checked=false;
  $("gediGridNote").textContent=raster?"原始栅格约 1 km。选择参考影像后完整沿用其坐标系、像元网格和范围，按研究区裁剪；最近邻对齐不会增加实际观测分辨率。":
    `${reference?"完整沿用参考影像的坐标系、像元网格与范围。":"无参考影像时，沿用本软件同研究区、同像元大小的 UTM 网格。"} 同一像元的足迹取参数均值，并另存各参数与总观测数量；无足迹处保留 NoData。完整波形和剖面写入表格，可按 shot_id 关联各级 GEDI 数据。`;
}

async function connectEarthdata() {
  if(state.busy||state.connecting||state.earthdataConnected)return;
  const method=$("earthdataLoginMode").value;
  const request=method==="token"?{method,token:$("earthdataToken").value.trim()}:{method,username:$("earthdataUsername").value.trim(),password:$("earthdataPassword").value};
  request.use_saved=Accounts.saved("earthdata");
  state.connecting=true;updateAvailability();
  try{
    const result=await api("earthdata-connect",request);state.earthdataConnected=result.connected;
    await Accounts.refresh();
    toast(result.saved?"Earthdata 登录信息已保存，下次打开自动恢复。":result.save_error,!result.saved);
  }catch(error){Accounts.setError("earthdata",error.message);toast(error.message,true);}
  finally{state.connecting=false;$("earthdataToken").value="";$("earthdataPassword").value="";renderAuth();renderEstimate();}
}

async function chooseGediReference() {
  if(state.busy||state.connecting)return;
  try{
    const path=await nativePath("choose_reference",{},()=>choosePath("选择参考 GeoTIFF","填写之前下载的 GeoTIFF 完整路径。GEDI 会沿用其网格；可选采样影像值。"));
    if(path){$("gediReference").value=path;renderGediOptions();schedulePlan();}
  }catch(error){toast(error.message,true);}
}
function modisLayerNote(asset) {
  const r=asset.raster;
  if(!isModis()||!r)return "";
  if(asset.id==="QC_Day"||asset.id==="QC_Night")return " · 原始 QA：0 可能有效，不可按 TIFF NoData 自动掩膜";
  if(asset.id.endsWith("_pixel_reliability"))return " · COG 255 对应 HDF4 的缺测值 −1";
  if(r.tiff_scale!==r.physical_scale)return ` · 物理值 = DN × ${r.physical_scale}（不是 TIFF 的 ${r.tiff_scale}）`;
  return r.physical_scale!==1||r.physical_offset?` · 物理值 = DN × ${r.physical_scale}${r.physical_offset?` ${r.physical_offset>0?"+":"−"} ${Math.abs(r.physical_offset)}`:""}${r.units?` · ${r.units}`:""}`:"";
}
const rawAssetTitle = a => isModis()||isWorldCover()?a.title:a.id;
function renderAssetPicker() {
  const ids=[...state.selected];
  const inOne=isRaw()&&!isS5p()&&!isWorldCoverSingle()&&!isSlc()&&ids.length===1;
  const assets=inOne?(state.scenes.find(s=>s.key===ids[0])?.raw?.assets||[]):[];
  const fileScene=inOne?`${state.profile}/${state.query?.id}/${ids[0]}`:"";
  if(fileScene!==state.fileScene){state.fileScene=fileScene;state.pickedFiles.clear();$("rawFileFilter").value="";}
  const route = rawRoute();
  const channels = {odata:"url","cdse-s3":"s3_url",public:"public_url","pc-public":"pc_url"};
  const supports = (asset, channel) => Boolean(asset[channels[channel]]);
  const allowed=assets.filter(asset=>route==="auto" || supports(asset,route));
  const allowedIds=new Set(allowed.map(a=>a.id));
  state.pickedFiles=new Set([...state.pickedFiles].filter(alias=>allowedIds.has(alias)));
  const pick=$("rawAsset"), current=pick.value;
  const prompt=ids.length!==1?"先在结果中单选产品":allowed.length?"选择文件…":"该产品在此通道没有可选文件";
  pick.innerHTML=`<option value="">${prompt}</option>`+allowed.map(a=>{
    const type = Object.keys(channels).filter(channel=>supports(a,channel)).map(transportLabel).join(" / ");
    return `<option value="${escapeHtml(a.id)}">${escapeHtml(rawAssetTitle(a))}${a.id===a.name?"":" · "+escapeHtml(a.name)}${a.bytes?" · "+sizeLabel(a.bytes):""} · ${escapeHtml(type+modisLayerNote(a))}</option>`;
  }).join("");
  if(allowed.some(a=>a.id===current))pick.value=current;
  $("rawAssetField").hidden=!isRaw()||$("rawMode").value!=="asset";
  const multi=isRaw()&&$("rawMode").value==="assets";
  $("rawMultiField").hidden=!multi;
  const query=$("rawFileFilter").value.trim().toLowerCase();
  const visible=multi?allowed.filter(a=>`${a.id} ${a.name} ${a.title||""}`.toLowerCase().includes(query)):[];
  $("rawMultiAssets").hidden=!visible.length;
  $("rawMultiAssets").innerHTML=visible.map(a=>`<label class="asset-entry"><input type="checkbox" value="${escapeHtml(a.id)}" ${state.pickedFiles.has(a.id)?"checked":""} ${state.busy?"disabled":""} data-lock><span><strong>${escapeHtml(rawAssetTitle(a))}</strong><small>${a.id!==a.name?escapeHtml(a.name):a.name==="xfdumanifest.xml"?"XFDU 清单":a.roles?.includes("metadata")?"源元数据":a.name.endsWith(".nc")?"NetCDF":"原始文件"}${a.bytes?" · "+sizeLabel(a.bytes):""}${escapeHtml(modisLayerNote(a))}</small></span></label>`).join("");
  updateMultiCount(allowed.length,visible.length);
}
function updateMultiCount(total,visible){
  const list=$("rawMultiAssets");
  if(total!==undefined){list.dataset.total=String(total);list.dataset.visible=String(visible);}
  const count=state.pickedFiles.size, available=Number(list.dataset.total||0), shown=Number(list.dataset.visible||0);
  $("rawMultiCount").textContent=state.selected.size!==1?"先在结果中单选一个产品。":
    !available?"所选通道没有可用文件。":`已选 ${count} 个 · 显示 ${shown} / ${available}${!shown?" · 没有匹配文件":""}`;
}
async function connectRaw() {
  if(state.busy||state.connecting||state.rawConnected)return;
  const method=$("rawLoginMode").value;
  const request=method==="token"?{method,token:$("rawToken").value.trim()}:{method,username:$("rawUsername").value.trim(),password:$("rawPassword").value,totp:$("rawTotp").value.trim()};
  request.use_saved=Accounts.saved("odata");
  state.connecting=true;updateAvailability();
  try{
    const result=await api("raw-connect",request);state.rawConnected=result.connected;
    await Accounts.refresh();
    toast(result.saved?"CDSE 登录信息已保存，下次打开自动恢复。":result.save_error,!result.saved);
  }catch(error){Accounts.setError("odata",error.message);toast(error.message,true);}
  finally{state.connecting=false;for(const id of ["rawPassword","rawTotp","rawToken"])$(id).value="";renderAuth();renderEstimate();}
}

async function connectS3() {
  if(state.busy||state.connecting||state.s3Connected)return;
  state.connecting=true;updateAvailability();
  try{
    const result=await api("s3-connect",{access_key:$("s3AccessKey").value,secret_key:$("s3SecretKey").value,use_saved:Accounts.saved("s3")});
    state.s3Connected=result.connected;
    await Accounts.refresh();
    toast(result.saved?"CDSE S3 密钥已保存，下次打开自动恢复。":result.save_error,!result.saved);
  }catch(error){Accounts.setError("s3",error.message);toast(error.message,true);}
  finally{state.connecting=false;$("s3AccessKey").value="";$("s3SecretKey").value="";renderAuth();renderEstimate();}
}

function rtcPolarizations() {
  const inputs=rtcInputs(), selected=state.scenes.filter(scene=>state.selected.has(scene.key));
  const common=selected.length?inputs.map(input=>input.value).filter(band=>selected.every(scene=>Object.hasOwn(scene.public?.assets||{},band))):null;
  const unavailable=common?inputs.filter(input=>input.checked&&!common.includes(input.value)).map(input=>input.value):[];
  const issue=!common?"":!common.length?"所选景没有共有极化，请分批下载。":unavailable.length?`所选景不同时提供 ${unavailable.join(" / ")}；请取消这些极化或更改所选景。`:
    !inputs.some(input=>input.checked)?"请勾选至少一个可用的雷达极化。":"";
  return {inputs,common,issue};
}
function updateAvailability() {
  const status=$("connectionStatus"), missing=missingAccess();
  const loaded=[...(state.rawConnected?["OData"]:[]),...(state.s3Connected?["S3"]:[])];
  status.classList.toggle("connected",isOfficial()?!!(state.plan?!missing.length:loaded.length):isGedi()?state.earthdataConnected:hasDownloadAccess());
  status.querySelector("span").textContent=isOfficial()?(missing.length?`需载入 ${missing.map(transportLabel).join(" + ")}`:
    loaded.length?`${loaded.join(" / ")} 凭据已载入`:"免登录检索 · 按通道认证"):
    isGedi()?(state.earthdataConnected?"Earthdata 凭据已载入 · 下载时验证":"GEDI 免登录检索 · 下载需 Earthdata 认证"):isProcess()?(state.connected?"API 已连接":"尚未连接 API"):isRtc()?"RTC · 下载时请求临时授权":isModis()?"MODIS · 无需个人密钥，自动请求临时读取":"公开数据 · 无需登录";
  document.querySelectorAll("[data-lock]").forEach(element=>{element.disabled=state.busy||state.connecting;});
  $("gediSensitivity").disabled=state.busy||state.connecting||isGedi()&&gediProduct()?.level==="L1B";
  $("gediResolution").disabled=state.busy||state.connecting||Boolean($("gediReference").value.trim());
  if(isRtc()){
    const {inputs,common}=rtcPolarizations();
    for(const input of inputs)input.disabled=state.busy||state.connecting||(common!==null&&!common.includes(input.value)&&!input.checked);
    $("rtcBandAvailability").textContent=common?(common.length?`所选景共有极化：${common.join(" / ")}`:"所选景没有共有极化。请分批下载。"):"选中影像后会显示各景共有的极化。";
  }
  $("clientId").readOnly=state.connected;
  $("clientSecret").disabled=state.connected||state.busy||state.connecting;
  $("toggleSecret").disabled=state.connected||state.busy||state.connecting;
  $("fitAoi").disabled=!state.aoi;
  $("clearAoi").disabled=!state.aoi||state.busy;
  $("exportBoundary").disabled=!state.aoi||state.busy;
  $("editBoundary").disabled=!state.aoi||state.busy;
  $("mapFitAoi").disabled=!state.aoi;
  $("mapDrawRectangle").disabled=state.busy;
  $("mapDrawPolygon").disabled=state.busy;
  $("rawCollection").disabled=state.busy||state.connecting||state.loadingCollections;
  $("refreshCollections").disabled=state.busy||state.connecting||state.loadingCollections;
  if(Batch.enabled()) {
    status.classList.toggle("connected",state.plan ? !missing.length : Batch.searchReady());
    status.querySelector("span").textContent=state.plan ? (missing.length ? "联合下载需 " + missing.map(transportLabel).join(" / ") : "联合下载 · 下载权限已就绪") : "附带 " + Batch.checked.size + " 个来源 · 可提前登录";
  }
  $("searchScenes").disabled=!canSearch();
  const companion=Batch.results.get(Batch.active), count=Batch.query?Batch.totalSelected():state.selected.size;
  $("selectAll").disabled=!(companion?companion.scenes.length:state.scenes.some(selectable))||state.busy;
  $("selectNone").disabled=!(companion?companion.selected.size:state.selected.size)||state.busy;
  $("downloadSelected").disabled=!hasDownloadAccess()||!state.aoi||!count||state.busy||state.connecting||!state.plan;
  Batch.availability();
  document.querySelectorAll("#companionResults input").forEach(element=>element.disabled=state.busy||element.dataset.unavailable==="1");
  document.querySelectorAll("#scenesBody input").forEach(element=>element.disabled=state.busy||element.dataset.unavailable==="1");
  updateButtons();
  Accounts.render();
}
function setBusy(value) {state.busy=value;updateAvailability();}
function renderAuth() {
  $("earthdataConnectedPanel").hidden=!state.earthdataConnected;
  $("earthdataAuthFields").hidden=state.earthdataConnected;
  $("rawConnectedPanel").hidden=!state.rawConnected;
  $("rawAuthFields").hidden=state.rawConnected;
  $("s3ConnectedPanel").hidden=!state.s3Connected;
  $("s3AuthFields").hidden=state.s3Connected;
  $("connectedPanel").hidden=!state.connected;
  $("authFields").hidden=state.connected;
  $("connectButton").hidden=state.connected;
  updateAvailability();
}
async function connect() {
  const id=$("clientId").value.trim(), secret=$("clientSecret").value.trim();
  if(state.busy||state.connected||state.connecting)return;
  if((!id||!secret)&&!Accounts.saved("oauth")){Accounts.setError("oauth","请同时填写 Client ID 和 Client Secret。");return;}
  $("authError").hidden=true;
  state.connecting=true;updateAvailability();const button=$("connectButton");button.querySelector("span").textContent="正在验证…";
  try{
    const result=await api("connect",{client_id:id,client_secret:secret,use_saved:Accounts.saved("oauth")});
    state.connected=result.connected;if(isProcess()||Batch.checked.has("s2-process"))clearScenes();$("clientSecret").value="";$("clientSecret").type="password";
    await Accounts.refresh();renderAuth();renderEstimate();
    toast(result.saved?"Sentinel Hub 登录信息已保存，下次打开自动恢复。":result.save_error,!result.saved);
  }catch(error){Accounts.setError("oauth",error.message);}
  finally{state.connecting=false;button.querySelector("span").textContent="验证并连接";updateAvailability();}
}

function configureMap() {
  if(!window.L){$("mapOffline").hidden=false;toast("地图组件未加载。仍可通过 SHP 或经纬度设置区域。",true);return;}
  L.drawLocal.draw.toolbar.actions={title:"取消绘制",text:"取消"};
  L.drawLocal.draw.toolbar.finish={title:"完成绘制",text:"完成"};
  L.drawLocal.draw.toolbar.undo={title:"移除上一个点",text:"撤销"};
  L.drawLocal.draw.handlers.polygon.tooltip={start:"点击地图放置第一个点",cont:"继续点击，添加顶点",end:"点击第一个点完成多边形"};
  L.drawLocal.draw.handlers.rectangle.tooltip={start:"按住鼠标拖动，画出矩形研究区"};
  L.drawLocal.edit.handlers.edit.tooltip={text:"拖动顶点修改边界",subtext:"再次点击编辑按钮以保存"};
  map=L.map("map",{center:[35.3,108.5],zoom:4,zoomControl:true,attributionControl:true});
  aoiLayer=L.featureGroup().addTo(map);footprintLayer=L.featureGroup().addTo(map);
  changeBasemap("standard");
  map.on(L.Draw.Event.CREATED,async(event)=>{
    $("drawHint").hidden=true;drawHandler=null;
    try{const result=await api("aoi",{geometry:event.layer.toGeoJSON(),name:"地图研究区"});setAoi(result,false);toast("研究区已设置，可查找可下载影像。");}
    catch(error){toast(error.message,true);}
  });
  map.on("draw:drawstop",()=>{$("drawHint").hidden=true;});
  setTimeout(()=>map.invalidateSize(),150);
  new ResizeObserver(()=>map.invalidateSize()).observe($("map"));
}
function changeBasemap(kind) {
  if(!map)return;
  if(baseLayer)map.removeLayer(baseLayer);
  if(kind==="satellite")baseLayer=L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",{maxZoom:19,attribution:"Tiles © Esri · Maxar · Earthstar Geographics"});
  else baseLayer=L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",{maxZoom:19,attribution:'© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors'});
  let errors=0;
  baseLayer.on("tileerror",()=>{errors++;if(errors>=4)$("mapOffline").hidden=false;});
  baseLayer.on("tileload",()=>{$("mapOffline").hidden=true;});
  baseLayer.addTo(map);
  document.querySelectorAll("#basemapSwitch button").forEach(button=>button.classList.toggle("active",button.dataset.basemap===kind));
}
function startDrawing(kind) {
  if(state.busy)return;
  if(!map){toast("地图暂不可用，请改用导入边界或输入坐标。",true);return;}
  if(state.editing){toast("请先点击编辑按钮，保存边界修改。",true);return;}
  if(drawHandler)drawHandler.disable();
  drawHandler=kind==="polygon"?new L.Draw.Polygon(map,{allowIntersection:false,showArea:true,metric:true,shapeOptions:{color:"#117b65",weight:2,fillOpacity:.15}}):new L.Draw.Rectangle(map,{shapeOptions:{color:"#117b65",weight:2,fillOpacity:.15}});
  drawHandler.enable();$("drawHint").textContent=kind==="polygon"?"按顺序点击放置顶点，点击第一个点完成。Esc 取消。":"按住鼠标拖动，画出研究区。Esc 取消。";$("drawHint").hidden=false;
  activateAoiTab("map");
}
async function toggleEditing() {
  if(!map||!state.aoi||state.busy)return;
  if(!state.editing){
    editHandler=new L.EditToolbar.Edit(map,{featureGroup:aoiLayer,selectedPathOptions:{maintainColor:true}});editHandler.enable();state.editing=true;$("editBoundary").classList.add("active");$("drawHint").textContent="拖动顶点修改边界，完成后再次点击编辑按钮。";$("drawHint").hidden=false;return;
  }
  editHandler.save();editHandler.disable();state.editing=false;$("editBoundary").classList.remove("active");$("drawHint").hidden=true;
  try{const result=await api("aoi",{geometry:aoiLayer.toGeoJSON(),name:state.aoi.name});setAoi(result,false);toast("边界修改已保存。");}
  catch(error){toast(error.message,true);renderAoiLayer(false);}
}
function renderAoiLayer(fit) {
  if(!aoiLayer)return;
  aoiLayer.clearLayers();footprintLayer.clearLayers();
  if(!state.aoi)return;
  L.geoJSON(state.aoi.geometry,{style:{color:"#08785d",weight:2,fillColor:"#239878",fillOpacity:.14}}).eachLayer(layer=>aoiLayer.addLayer(layer));
  if(fit&&aoiLayer.getBounds().isValid())map.fitBounds(aoiLayer.getBounds(),{padding:[75,75],maxZoom:16});
}
function setAoi(aoi,fit=true,keepScenes=false) {
  state.aoi=aoi;
  if(!keepScenes)clearScenes();
  $("aoiSummary").classList.toggle("empty",!aoi);
  $("aoiName").textContent=aoi?aoi.name:"尚未设置研究区";
  $("aoiStats").textContent=aoi?`${friendlyArea(aoi.area_km2)} · ${aoi.vertices} 个顶点 · WGS84`:"地图绘制、导入边界或填写坐标";
  $("mapAoiBadge").classList.toggle("has-aoi",!!aoi);
  $("mapAoiBadge").querySelector("span").textContent=aoi?`研究区 · ${friendlyArea(aoi.area_km2)}`:"绘制你的研究区";
  renderAoiLayer(fit);updateAvailability();
}
function activateAoiTab(name) {
  document.querySelectorAll("#aoiTabs button").forEach(button=>button.classList.toggle("active",button.dataset.tab===name));
  for(const tab of ["map","import","coordinates"])$("tab-"+tab).hidden=tab!==name;
}
async function applyCoordinates() {
  try{const result=await api("aoi",{coordinates:$("coordinates").value,name:"坐标研究区"});setAoi(result);toast("坐标已应用。");}
  catch(error){toast(error.message,true);}
}
async function choosePath(title,note,current="") {
  $("pathDialogTitle").textContent=title;$("pathDialogNote").textContent=note;$("pathDialogInput").value=current;
  $("pathDialog").showModal();
  return new Promise(resolve=>{
    let result="";
    const confirm=()=>{result=$("pathDialogInput").value.trim();$("pathDialog").close();};
    $("confirmPath").addEventListener("click",confirm,{once:true});
    $("pathDialog").addEventListener("close",()=>{$("confirmPath").removeEventListener("click",confirm);resolve(result);},{once:true});
    $("pathDialogInput").focus();
  });
}
async function nativePath(kind, options, fallback) {
  if(!state.nativeDialogs)return await fallback();
  const result=await api("dialog",{kind,...options});
  return result.path;
}
async function importBoundary() {
  try{
    const path=await nativePath("choose_boundary",{},()=>choosePath("选择边界文件","桌面版可使用文件选择器；开发浏览器模式请填写完整 SHP 或 GeoJSON 路径。"));
    if(!path)return;
    $("importFileName").textContent="正在读取边界…";
    const result=await api("aoi",{path,source_crs:$("sourceCrs").value.trim()});setAoi(result);$("importFileName").textContent=basename(path);toast("边界已导入，地图已定位到研究区。");
  }catch(error){$("importFileName").textContent="";toast(error.message,true);}
}
async function chooseFolder() {
  try{const path=await nativePath("choose_folder",{current:$("outputDirectory").value},()=>choosePath("选择输出文件夹","填写你希望保存影像的本地完整文件夹路径。",$("outputDirectory").value));if(path){$("outputDirectory").value=path;renderPlanFolder();await api("preferences",{output_dir:path});}}
  catch(error){toast(error.message,true);}
}
async function exportBoundary() {
  if(!state.aoi)return;
  const extension=$("exportFormat").value;$("exportDialog").close();
  try{
    const path=await nativePath("save_boundary",{extension},()=>choosePath("保存研究区边界","填写完整保存路径，文件名应以 ."+extension+" 结尾。"));
    if(!path)return;
    const result=await api("export-aoi",{geometry:state.aoi.geometry,name:state.aoi.name,path});toast("研究区已保存："+displayPath(result.path), false, 11000);
  }catch(error){toast(error.message,true);}
}
async function searchPlaces(event) {
  event.preventDefault();
  const query=$("placeQuery").value.trim();if(!query)return;
  $("placeSearchButton").disabled=true;$("placeSearchButton").textContent="搜索中";$("placeResults").hidden=true;
  try{
    const result=await api("geocode",{query});state.places=result.places;
    if(!result.places.length){toast("没有找到该地点，可以试试上级行政区或输入经纬度。",true);return;}
    $("placeResults").innerHTML=result.places.map((place,index)=>`<button class="place-result" data-index="${index}">${escapeHtml(place.name)}</button>`).join("");$("placeResults").hidden=false;
  }catch(error){toast(error.message,true);}
  finally{$("placeSearchButton").disabled=false;$("placeSearchButton").textContent="搜索";}
}

function clearScenes() {
  state.scenes=[];state.selected.clear();state.plan=null;state.query=null;state.planVersion++;Batch.clearResults();
  $("sceneCount").textContent="0";$("scenesEmpty").hidden=false;$("scenesTableWrap").hidden=true;$("scenesLoading").hidden=true;
  $("scenesEmpty").querySelector("strong").textContent="让影像覆盖你的研究区";
  $("scenesEmpty").querySelector(":scope > span").textContent="设置影像类别、研究区和筛选条件，再点击检索。";
  $("scenesSubtitle").textContent=isGedi()?"按明确版本检索 NASA 文件。目录相交不代表研究区有通过质量筛选的足迹。":isProcess()?"按实际过境时间整理，跨瓦片自动拼接。":isRaw()?"按类别检索产品。没有安全可用入口的结果会标明。":"只裁剪所选公开瓦片，不静默替换产品。";
  $("scenesBody").innerHTML="";
  if(footprintLayer)footprintLayer.clearLayers();renderAssetPicker();renderEstimate();updateAvailability();
}
function firstSelection() {
  const first=state.scenes.find(selectable);
  return new Set(first?[first.key]:[]);
}
function renderScenes() {
  $("sceneCount").textContent=String(state.scenes.length);$("scenesLoading").hidden=true;$("scenesEmpty").hidden=!!state.scenes.length;$("scenesTableWrap").hidden=!state.scenes.length;
  if(!state.scenes.length){$("scenesEmpty").querySelector("strong").textContent="该条件下没有找到影像";$("scenesEmpty").querySelector(":scope > span").textContent=isDem()?"公开 DEM 索引没有覆盖该研究区的瓦片。":isWorldCover()?"只发布 2020 和 2021 年度产品。请检查日期范围或调整为有覆盖的研究区。":isSlc()?"没有符合条件的精确历史存档 ZIP。此入口不覆盖全球，不提供 2024-04-04 之后的采集数据。":"请调整日期、影像类别、筛选条件或研究区。";}
  $("scenesBody").innerHTML=state.scenes.map(scene=>{
    const cloud=scene.cloud_max??0, coverage=scene.coverage_percent;
    const available=selectable(scene), checked=state.selected.has(scene.key);
    const title=scene.title||scene.key;
    const description=isGedi()?`<span class="scene-product">${escapeHtml(title)}</span>${escapeHtml(available?scene.tiles.join(' / ')+(isGediRaster()?" · 汇总至 "+scene.gedi.end.slice(0,10):""):scene.gedi?.unavailable_reason||"无可用下载入口")}`:isRaw()?`<span class="scene-product">${escapeHtml(title)}</span>${available?escapeHtml(scene.tiles.join(' / ')+(scene.raw?.source_note?" · "+scene.raw.source_note:"")):(isS5p()||isSen3()||isL3()||isWorldCover()||isModis())?escapeHtml(scene.raw?.unavailable_reason||'无可用下载入口'):'无可用下载入口'}`:
      (isProcess()?`${scene.tile_count} 块`:escapeHtml(scene.tiles.join(' / ')));
    return `<tr data-key="${escapeHtml(scene.key)}" class="${checked?"selected":""} ${available?"":"scene-unavailable"}"><td><input type="checkbox" aria-label="选择 ${escapeHtml(title)}" ${checked?"checked":""} ${available?"":'disabled data-unavailable="1"'}></td><td class="scene-datetime">${escapeHtml(scene.date)}<span>${escapeHtml(scene.time)}</span></td><td><span class="platform-tag">${escapeHtml(scene.platform)}</span></td><td title="${escapeHtml(title+(scene.tiles.length?" · "+scene.tiles.join(', '):""))}">${description}</td><td><span class="cloud-cell"><span class="cloud-bar"><i style="width:${Math.max(0,Math.min(100,cloud))}%"></i></span>${scene.cloud_max==null?"—":scene.cloud_max+"%"}</span></td><td class="${coverage!=null&&coverage<95?"coverage-low":""}">${isGedi()?"导出后统计":coverage==null?"—":coverage+"%"}</td></tr>`;
  }).join("");
  if(state.query){
    const q=state.query, dates=q.start?`${q.start} — ${q.end}`:isDem()?"静态 DSM":"不限日期";
    $("scenesSubtitle").textContent=isGedi()?`${gediProduct()?.title||q.collection} · ${dates} · ${isGediRaster()?"按完整汇总期重叠匹配":"导出时精确筛选足迹日期与边界"}`:isRaw()?`${isOfficial()||isS5p()||isSen3()?q.collection:profiles[state.profile][0]} · ${dates}${q.radar_mode?" · "+q.radar_mode:""}${isOpenOptical()?" · 云量 ≤ "+q.max_cloud+"%":isL3()?" · 日期与季度重叠":isWorldCover()?" · 日期与年度重叠":isModis()?" · 按标称日筛选":""} · 不裁剪`:
      isDem()?`${dates} · 可裁剪或下载整瓦片`:isRtc()?`${dates}${q.radar_mode?" · "+q.radar_mode:""} · 每景单独裁剪 · 线性 γ⁰`:
      `${dates} · 云量 ≤ ${q.max_cloud}% · 每${isProcess()?"次过境":"个瓦片"}一个输出`;
  }
  Batch.renderResults();renderAssetPicker();updateAvailability();schedulePlan();
}
function renderEstimate() {
  const selected=Batch.query?Batch.totalSelected():state.selected.size;
  $("selectedCount").hidden=!selected;$("selectedCount").textContent=String(selected);
  const label=$("downloadEstimate"), headline=label.querySelector("strong"), body=label.querySelector("span");
  const p=state.plan, count=selected;
  if(!count){headline.textContent="尚未选择影像";body.textContent="选择后查看文件类型、预计体积和输出信息。";}
  else if(!p){headline.textContent=`已选择 ${count} 项`;body.textContent=(isRtc()&&rtcPolarizations().issue)||"正在检查下载计划…";}
  else if(p.kind==="batch"){
    const missing=missingAccess();
    headline.textContent=`联合下载 · ${p.groups.length} 个来源 · ${p.scene_count} 个结果`;
    body.textContent=`同区域 / 同 UTC 日期 · 约 ${sizeLabel(p.estimated_bytes)}${p.unknown_size?" + 部分体积待确定":""}${missing.length?" · 先登录 "+missing.map(transportLabel).join(" / "):" · 登录已就绪"}`;
  }
  else if(p.kind==="gedi"){
    const mode=p.options.mode, missing=missingAccess();
    headline.textContent=`GEDI · ${p.scene_count} 个源文件${mode==="original"?" · 保留原始格式":p.data_kind==="raster"?" · 对齐栅格":mode==="aligned"?" + 足迹表格 / 对齐栅格":" + 足迹表格"}`;
    body.textContent=`原始文件约 ${p.estimated_bytes?sizeLabel(p.estimated_bytes):"由服务确定"}${p.grid?` · ${p.grid.width} × ${p.grid.height} 像元`:""}${missing.length?" · 请先连接 Earthdata":" · 不消耗 Sentinel Hub 额度"}`;
  }else if(p.kind==="original"){
    headline.textContent=p.sen3_complete?`${p.scene_count} 个完整 SEN3 · ${p.requests} 个文件`:`${p.requests} 个原始文件 · 不裁剪`;
    const missing=missingAccess();
    body.textContent=`${p.transports.map(transportLabel).join(" + ")} · ${p.estimated_bytes?sizeLabel(p.estimated_bytes):"体积由服务确定"}${missing.length?" · 请先载入 "+missing.map(transportLabel).join(" + ")+" 凭据":""}`;
  }else{
    headline.textContent=`${count} 景 · ${p.resolution} 米 · ${p.options.output_bands.length} 波段`;
    body.textContent=`未压缩约 ${sizeLabel(p.bytes_per_scene*count)} · ${p.kind==="public"?`${p.source_objects} 个源文件 · 无处理额度`:`预计 ${p.estimated_pu} PU · ${p.requests} 次请求`}`;
  }
  updateAvailability();
}
function schedulePlan() {
  clearTimeout(planTimer);state.plan=null;renderEstimate();
  const version=++state.planVersion;
  if(!state.aoi||!(Batch.query?Batch.totalSelected():state.selected.size)||state.busy||(isRtc()&&rtcPolarizations().issue))return;
  planTimer=setTimeout(async()=>{
    try{
      const result=await api(Batch.query?"batch-plan":"plan",downloadRequest());if(version!==state.planVersion)return;
      state.plan=result;Batch.updateReferences(result);
      if(missingAccess().includes("cdse-s3"))$("s3Access").open=true;
      if(missingAccess().includes("odata"))$("rawODataAccess").open=true;
      renderEstimate();
    }
    catch(error){if(version!==state.planVersion)return;$("downloadEstimate").querySelector("span").textContent=error.message;updateAvailability();}
  },180);
}
async function searchScenes() {
  if(!canSearch())return;
  if(state.editing){toast("请先点击编辑按钮保存边界。",true);return;}
  if(dateEnabled()&&(!$("startDate").value||!$("endDate").value)){toast("请选择开始和结束日期。",true);return;}
  if(dateEnabled()&&$("startDate").value>$("endDate").value){toast("开始日期不能晚于结束日期。",true);return;}
  if(drawHandler)drawHandler.disable();
  clearScenes();setBusy(true);showSearchProgress();
  try{const request={geometry:state.aoi.geometry,profile:state.profile,...criteria()};const result=await api(Batch.enabled()?"batch-search":"search",Batch.enabled()?Batch.searchRequest(request):request);state.jobId=result.job_id;$("cancelSearch").disabled=false;pollJob(result.job_id);}
  catch(error){setBusy(false);$("scenesLoading").hidden=true;$("scenesEmpty").hidden=false;toast(error.message,true);}
}
function renderPlanFolder() {
  const field=$("planPreviewFolder");
  if(field&&state.plan)field.textContent=$('outputDirectory').value.trim().replace(/[\\/]$/,"")+"\\"+(state.plan.folder_name||"S2L2A_"+state.plan.fingerprint);
}
function gediPlanItems(p) {
  const mode=p.options.mode, o=p.options;
  const items=[["GEDI 类别",p.collection_title],["原始文件",`${p.scene_count} 个 · 约 ${p.estimated_bytes?sizeLabel(p.estimated_bytes):"由服务确定"}`],
    ["下载与导出",mode==="original"?"保留原始文件":p.data_kind==="raster"?"原始栅格 + 对齐 GeoTIFF":mode==="aligned"?"原始 HDF5 + CSV / GeoJSON + 均值 / 数量 GeoTIFF":"原始 HDF5 + CSV / GeoJSON"],
    ["日期范围",`${p.criteria.start} — ${p.criteria.end}（UTC）`],["原始文件缓存",p.root_name+"/original（同一修订可复用）"]];
  if(p.data_kind==="footprint"&&mode!=="original")items.push(["质量标志",o.quality==="recommended"?p.quality_paths.join(" / ")+" = "+(state.gediCollections.find(item=>item.id===o.collection)?.quality_value??1):"不按质量标志筛选，仍保留质量列"],
    ["附加筛选",`${o.nondegraded?"degrade = 0":"保留定位降级"} · sensitivity ${o.min_sensitivity?"≥ "+o.min_sensitivity:"不额外筛选"} · ${o.beams.length} 个波束`],["导出参数",p.fields.map(f=>f.id+(f.units?" ("+f.units+")":"")).join(" / ")]);
  if(p.grid)items.push(["输出网格",`${p.grid.width.toLocaleString()} × ${p.grid.height.toLocaleString()} 像元`],["坐标系",p.grid.crs_label],
    ["像元大小",p.grid.pixel_size.map(x=>Number(x).toPrecision(7)).join(" × ")+" "+p.grid.pixel_unit],
    ["网格来源",p.grid.reference?displayPath(p.grid.reference.path):"沿用同研究区、同像元大小的 UTM 输出规则"]);
  if(o.sample_reference)items.push(["影像采样","足迹中心所在像元 · 应用参考文件的缩放与偏移"]);
  return items;
}
function showDownloadPlan() {
  if(!state.plan||state.busy||!hasDownloadAccess())return;
  if(state.editing){toast("请先保存边界修改。",true);return;}
  if(!$("outputDirectory").value.trim()){toast("请先选择输出文件夹。",true);$("outputDirectory").focus();return;}
  try{
    const p=state.plan;
    if(p.kind==="batch"){
      $("planDetails").innerHTML=Batch.planHtml(p);
      $("planNote").textContent="各来源共用研究区和 UTC 日期范围，保留各自的采集或汇总时间。独立来源可同时下载；跟随主影像的 GEDI 等待所选 GeoTIFF 完成。无匹配、失败和取消会分别记录，已完成的文件保留。";
      renderPlanFolder();if(!$("downloadDialog").open)$("downloadDialog").showModal();return;
    }
    const raw=p.kind==="original", pub=p.kind==="public", gedi=p.kind==="gedi";
    const items=gedi?gediPlanItems(p):raw?[["产品类别",p.options.collection],["文件数量",`${p.requests} 个`],["下载范围","原产品范围，不按研究区裁剪"],
      ["目录提供的体积",p.estimated_bytes?sizeLabel(p.estimated_bytes):"下载时确定"],["通道",p.transports.map(transportLabel).join(" + ")],
      ["文件形式",p.options.mode==="original"?(isDem()?"完整 DSM GeoTIFF":isModis()?"原始 HDF4":isSlc()?"完整原始 SAFE ZIP":"官方整包或单主文件"):p.options.mode==="metadata"?"目录元数据":"目录所列原始资产"]]:
      [["影像数量",`${p.scene_count} 景`],["输出网格",`${p.width.toLocaleString()} × ${p.height.toLocaleString()} 像元`],["像元大小",`${p.resolution} 米`],["输出内容",p.options.label],["坐标系",`${p.crs_label} · EPSG:${p.epsg}`],["预计文件体积",`${sizeLabel(p.bytes_per_scene*p.scene_count)}（未压缩）`],
       [pub?"需读取的公开源":"预计请求数量",pub?`${p.source_objects} 个源文件`:`最多 ${p.requests} 次 · 每景 ${p.tiles_per_scene} 区块`],["处理额度",pub?"无需 Sentinel Hub 额度":`约 ${p.estimated_pu} PU`]];
    const rows=items.map(([name,value])=>`<div class="plan-item"><span>${escapeHtml(name)}</span><strong>${escapeHtml(value)}</strong></div>`).join("");
    const files=raw||gedi? p.targets.map(t=>t.name).slice(0,6).join(" / ")+(p.targets.length>6?`（另 ${p.targets.length-6} 项）`:"") :p.options.output_bands.join(" / ");
    $("planDetails").innerHTML=rows+`<div class="plan-item wide"><span>${raw||gedi?"原始文件/产品":"波段顺序"}</span><strong class="band-list">${escapeHtml(files)}</strong></div><div class="plan-item wide"><span>保存位置</span><strong id="planPreviewFolder" class="plan-path"></strong></div>`;
    $("planNote").textContent=gedi?(p.options.mode==="original"?"下载精确产品版本的原始文件。有 NASA 目录校验值时进行核对；所有文件记录本地 SHA-256。缺少目录校验值的原始文件另做完整解码检查。研究区只用于检索。":p.data_kind==="raster"?"保留原始约 1 km 栅格，按研究区裁剪并最近邻对齐。日期匹配整个汇总期，无法拆分为某一天的观测。":
      "只导出研究区与 UTC 日期内通过所选条件的足迹。约 25 米离散观测按足迹中心入格，无观测处为 NoData，不插值。参数均值和观测数量分别保存；标准误及区间均值不能当作合并不确定性。高程默认是椭球高，RH 是相对高度。"):
      isSen3()?(p.sen3_complete?"不裁剪。仅当清单和所有原始成员验证通过，才发布完整 .SEN3 目录。":"不裁剪。选中的原始成员会保留文件名，存入单独目录；不是完整 .SEN3 产品。"):
      isSlc()?"只保存完整原始 ZIP。分段接收并固定对象版本，核对 ASF 全文件 MD5 和 SAFE 全部文件 CRC；逐一校验清单成员及复数 I/Q 测量。":
      isModis()?"保留源 HDF4 / XML / COG 原字节。校验源版本及完整 MD5；COG 逐块解码、核对图层和 MODLAND 网格。HDF4 若有配套 XML，则额外校验 NASA 的文件长度和 MD5。物理缩放与 TIFF 标签分开记入下载记录。":
      isWorldCover()?"下载原始年度整瓦片，不裁剪或转换 DN。校验源 ETag、文件长度、栅格网格及产品标识；保留 NoData、缩放与偏移。":
      isL3()?"保留季度整瓦片。有效像元 DN × 0.0001 = 反射率；−32768 为 NoData，TIFF 未内嵌缩放。用目录网格与长度、镜像对象版本和本地 SHA‑256 核对；没有官方逐文件校验值时不视为官方字节等同。":
      isOpenOptical()&&state.profile==="s2-l2a-cog"?"保留源 COG 的像元、缩放和偏移，不裁剪、不重建 SAFE。发布前核对瓦片索引，并逐文件验证目录 SHA‑256。":
      raw?"原始产品不裁剪。通过版本、格式和完整性检查才保存。下载记录区分目录校验值与本地 SHA‑256。":
      isRtc()?"导出线性 γ⁰（不转 dB）。各极化保留自己的 NoData；仅读取所需字节区间，校验每段后再裁剪。":
      p.options.mode==="research"?"保存分析影像、未掩膜 10 波段备份、SCL 和像元来源图及来源表。全云时仍保留备份；各文件必须网格一致并通过完整性校验才完成。20 米源波段的细节不会因输出 10 米而增加。完整角度与大气辅助层请使用 SAFE 整包。":
      "区域外及无效像元以 NoData 或透明显示。全空影像不保存；已有文件通过本地 SHA‑256 校验才跳过。";
    renderPlanFolder();$("downloadDialog").showModal();
  }catch(error){toast(error.message,true);}
}
async function beginDownload() {
  $("confirmDownload").disabled=true;
  try{
    const request={...downloadRequest(),plan_fingerprint:state.plan?.fingerprint};const result=await api(state.plan?.kind==="batch"?"batch-download":"download",request);
    $("downloadDialog").close();setBusy(true);state.jobId=result.job_id;
    showDownloadProgress();pollJob(result.job_id);
  }catch(error){toast(error.message,true);}
  finally{$("confirmDownload").disabled=false;}
}
function showSearchProgress(canCancel=false) {
  $("scenesEmpty").hidden=true;$("scenesLoading").hidden=false;$("searchMessage").textContent=Batch.enabled()?"连接主数据及附带来源目录…":isGedi()?"连接 NASA CMR 目录…":"连接官方影像目录…";
  $("cancelSearch").disabled=!canCancel;$("cancelSearch").textContent="取消检索";
}
function showDownloadProgress(kind="download") {
  state.resultFolder="";
  $("jobTitle").textContent=kind==="update"?"软件更新":Batch.query?"主数据及附带数据":isGedi()?"GEDI 数据下载":"下载队列";
  Batch.progress([]);
  $("jobPanel").hidden=false;$("jobFiles").hidden=true;$("jobFiles").innerHTML="";$("openResultFolder").hidden=true;
  $("closeJobPanel").hidden=true;$("cancelJob").hidden=false;$("cancelJob").disabled=false;$("cancelJob").textContent="取消任务";
  $("jobLogs").innerHTML="";$("jobStatus").className="job-status";$("jobStatus").textContent="正在下载";
  $("jobProgress").style.width="0%";$("jobFraction").textContent="0%";$("jobMessage").textContent="正在准备下载…";
}
async function cancelActive(button) {
  if(!state.jobId||button.disabled)return;
  button.disabled=true;
  try{await api("cancel",{job_id:state.jobId});button.textContent="正在取消…";}
  catch(error){button.disabled=false;toast(error.message,true);}
}
async function pollJob(id) {
  let retries=0;
  const poll=async()=>{
    if(state.jobId!==id)return;
    try{
      const job=await api("job?id="+encodeURIComponent(id));retries=0;
      const searching=job.kind==="search"||job.kind==="batch-search";
      if(searching)$("searchMessage").textContent=job.message;
      else{
        $("jobMessage").textContent=job.message;$("jobProgress").style.width=(job.progress*100).toFixed(1)+"%";$("jobFraction").textContent=Math.round(job.progress*100)+"%";
        $("jobLogs").innerHTML=job.logs.map(log=>`<div class="job-log"><time>${new Date(log.time*1000).toLocaleTimeString("zh-CN",{hour12:false})}</time><span>${escapeHtml(log.message)}</span></div>`).join("");
        $("jobLogs").scrollTop=$("jobLogs").scrollHeight;
        Batch.progress(job.detail?.batch_groups);
        if(job.detail?.folder)state.resultFolder=job.detail.folder;
      }
      if(job.status==="running"||job.status==="cancelling"){if(job.status==="cancelling"){const button=$(searching?"cancelSearch":"cancelJob");button.disabled=true;button.textContent="正在取消…";}setTimeout(poll,900);return;}
      state.jobId=null;setBusy(false);
      if(searching){
        $("scenesLoading").hidden=true;
        if(job.status==="completed"){state.scenes=job.result.scenes;state.query=job.result.query;state.selected=firstSelection();if(job.result.batch)Batch.accept(job.result.batch);renderScenes();if(job.result.batch)toast(job.message+"，请分别查看各来源结果。",job.result.batch.groups.some(g=>g.error));else if(state.scenes.length)toast(`找到 ${state.scenes.length} 个结果${state.selected.size?"，已选中首个可下载结果。":"，但没有可用下载入口。"}`);}
        else{$("scenesEmpty").hidden=false;toast(job.error||job.message,job.status==="failed");}
      }else{
        $("closeJobPanel").hidden=false;$("cancelJob").hidden=true;
        $("jobStatus").className="job-status "+(job.status==="failed"?"failed":job.status==="partial"?"partial":job.status==="cancelled"?"cancelled":"");
        const isUpdate=job.kind==="update";
        if(job.result?.sources)Batch.progress(job.result.sources);
        $("jobStatus").textContent=job.status==="completed"?(isUpdate?"校验通过":job.result.files.length?"下载完成":"无有效输出"):job.status==="partial"?"部分完成":job.status==="failed"?"下载失败":"已取消";
        if(job.status==="completed"||job.status==="partial"){
          state.resultFolder=job.result.folder;$("jobFiles").innerHTML=job.result.files.map(file=>`<div class="job-file">${icon("check")}<span title="${escapeHtml(displayPath(file.path))}">${file.status==="skipped"?"已有 · ":""}${escapeHtml(basename(file.path))}</span></div>`).join("");$("jobFiles").hidden=false;
          toast(isUpdate?job.message:`${job.message}。来源记录已保存。`,job.status==="partial",12000);
        }else toast(job.error||job.message,job.status==="failed",12000);
        $("openResultFolder").hidden=!state.resultFolder;if(!isUpdate)schedulePlan();
      }
    }catch(error){
      retries++;if(retries<=5){setTimeout(poll,1500);return;}
      toast("暂时无法读取任务进度。任务可能仍在后台运行，请保持软件打开。",true,15000);retries=0;setTimeout(poll,4000);
    }
  };
  poll();
}

function bindEvents() {
  $("updateButton").addEventListener("click",checkUpdates);
  $("retryUpdate").addEventListener("click",checkUpdates);
  $("openUpdateRelease").addEventListener("click",()=>updateAction(false));
  $("downloadUpdate").addEventListener("click",()=>updateAction(true));
  let datesBeforeWorldCover;
  $("sourceProfile").addEventListener("change",()=>{
    const wasWorldCover=isWorldCover();
    state.profile=$("sourceProfile").value;Batch.primaryChanged();
    if(!wasWorldCover&&isWorldCover()){
      const start=$("startDate").value,end=$("endDate").value;
      datesBeforeWorldCover=[start,end];
      if(!start||!end||start>end||start>"2021-12-31"||end<"2020-01-01"){
        $("startDate").value="2021-01-01";$("endDate").value="2021-12-31";
      }
    }else if(wasWorldCover&&!isWorldCover()&&datesBeforeWorldCover){
      [$("startDate").value,$("endDate").value]=datesBeforeWorldCover;
    }
    for(const id of ["clientSecret","rawPassword","rawToken","rawTotp","s3AccessKey","s3SecretKey","earthdataToken","earthdataPassword"])$(id).value="";
    $("radarMode").value="";$("rawMode").value=isRadar()?"metadata":isOpenOptical()||isL3()||isWorldCoverS2()||isModis()?"assets":"original";$("rawTransport").value="auto";
    clearScenes();renderProfile();if(isOfficial())loadCollections();
  });
  $("s2SafeShortcut").addEventListener("click",async()=>{
    if(state.busy||state.connecting||state.editing)return;
    state.profile="cdse-original";state.collection="sentinel-2-l2a";$("sourceProfile").value=state.profile;
    $("collectionFilter").value="";$("allDates").checked=false;$("radarMode").value="";
    $("rawMode").value="original";$("rawTransport").value="odata";
    Batch.primaryChanged();clearScenes();renderProfile();
    await loadCollections();renderCollectionPicker();renderProfile();
  });
  for(const id of ["s5pCollection","sen3Collection"])$(id).addEventListener("change",clearScenes);
  $("gediCollection").addEventListener("change",()=>{clearScenes();renderProfile();});
  $("gediMode").addEventListener("change",()=>{renderGediOptions();schedulePlan();});
  for(const id of ["gediFields","gediBeams","gediQuality","gediNondegraded","gediSensitivity","gediResolution","gediSampleReference"])$(id).addEventListener("change",schedulePlan);
  $("gediReference").addEventListener("change",()=>{renderGediOptions();schedulePlan();});
  $("chooseGediReference").addEventListener("click",chooseGediReference);
  $("clearGediReference").addEventListener("click",()=>{$("gediReference").value="";renderGediOptions();schedulePlan();});
  $("earthdataLoginMode").addEventListener("change",()=>{
    const token=$("earthdataLoginMode").value==="token";
    $("earthdataTokenField").hidden=!token;$("earthdataAccountFields").hidden=token;
    $("earthdataToken").value="";$("earthdataPassword").value="";
  });
  $("earthdataConnect").addEventListener("click",connectEarthdata);
  for(const id of ["earthdataToken","earthdataPassword"])$(id).addEventListener("keydown",event=>{if(event.key==="Enter")connectEarthdata();});
  $("earthdataDisconnect").addEventListener("click",async()=>{
    try{await api("earthdata-disconnect",{});delete Accounts.errors.earthdata;for(const id of ["earthdataUsername","earthdataPassword","earthdataToken"])$(id).value="";await Accounts.refresh();renderAuth();renderEstimate();}catch(e){toast(e.message,true);}
  });
  for(const [id,name] of [["getEarthdataToken","earthdata-token"],["earthdataRegister","earthdata-register"],["earthdataAuthDocs","earthdata-auth"]])$(id).addEventListener("click",()=>api("open-link",{name}).catch(e=>toast(e.message,true)));
  $("gediDocs").addEventListener("click",()=>api("open-link",{name:"gedi:"+$("gediCollection").value}).catch(e=>toast(e.message,true)));
  $("collectionFilter").addEventListener("input",renderCollectionPicker);
  $("refreshCollections").addEventListener("click",()=>loadCollections(true));
  $("rawCollection").addEventListener("change",()=>{
    state.collection=$("rawCollection").value;
    $("allDates").checked=!!state.collections.find(c=>c.id===state.collection)?.static;
    $("radarMode").value="";
    clearScenes();renderProfile();
  });
  $("radarMode").addEventListener("change",clearScenes);
  $("rtcBands").addEventListener("change",schedulePlan);
  $("rtcResolution").addEventListener("change",schedulePlan);
  $("allDates").addEventListener("change",()=>{clearScenes();renderProfile();});
  $("demMode").addEventListener("change",()=>{
    $("demModeNote").textContent=$("demMode").value==="original"?"下载完整的源 GeoTIFF；研究区只用于挑选瓦片。":"每个所选瓦片独立裁剪。";
    schedulePlan();
  });
  $("rawMode").addEventListener("change",()=>{renderAssetPicker();schedulePlan();});
  $("rawTransport").addEventListener("change",()=>{
    const route=$("rawTransport").value;
    if(route==="cdse-s3")$("s3Access").open=true;
    if(route==="odata")$("rawODataAccess").open=true;
    renderAssetPicker();schedulePlan();
  });
  $("rawAsset").addEventListener("change",schedulePlan);
  $("rawFileFilter").addEventListener("input",renderAssetPicker);
  $("rawMultiAssets").addEventListener("change",event=>{
    const field=event.target;
    if(!field.matches('input[type="checkbox"]'))return;
    if(field.checked){
      if(state.pickedFiles.size>=100){field.checked=false;toast("一次最多选 100 个文件。",true);return;}
      state.pickedFiles.add(field.value);
    }else state.pickedFiles.delete(field.value);
    updateMultiCount();schedulePlan();
  });
  $("rawLoginMode").addEventListener("change",()=>{
    const token=$("rawLoginMode").value==="token";
    $("rawTokenField").hidden=!token;$("rawAccountFields").hidden=token;
    for(const id of ["rawToken","rawPassword","rawTotp"])$(id).value="";
  });
  $("rawAuthDocs").addEventListener("click",()=>api("open-link",{name:"raw-auth"}).catch(e=>toast(e.message,true)));
  $("rawConnect").addEventListener("click",connectRaw);
  $("rawDisconnect").addEventListener("click",async()=>{
    try{await api("raw-disconnect",{});delete Accounts.errors.odata;for(const id of ["rawUsername","rawPassword","rawTotp","rawToken"])$(id).value="";await Accounts.refresh();renderAuth();renderEstimate();}catch(e){toast(e.message,true);}
  });

  $("getS3KeyButton").addEventListener("click",()=>api("open-link",{name:"s3-keys"}).catch(e=>toast(e.message,true)));
  $("s3AuthDocs").addEventListener("click",()=>api("open-link",{name:"s3-docs"}).catch(e=>toast(e.message,true)));
  $("s3Connect").addEventListener("click",connectS3);
  $("s3SecretKey").addEventListener("keydown",event=>{if(event.key==="Enter")connectS3();});
  $("s3Disconnect").addEventListener("click",async()=>{
    try{await api("s3-disconnect",{});delete Accounts.errors.s3;$("s3AccessKey").value="";$("s3SecretKey").value="";await Accounts.refresh();renderAuth();renderEstimate();}catch(e){toast(e.message,true);}
  });
  $("modisDocs").addEventListener("click",()=>api("open-link",{name:state.profile}).catch(e=>toast(e.message,true)));
  $("worldcoverDocs").addEventListener("click",()=>api("open-link",{name:"worldcover"}).catch(e=>toast(e.message,true)));
  $("getKeyButton").addEventListener("click",()=>api("open-link",{name:"dashboard"}).catch(e=>toast(e.message,true)));
  $("registerButton").addEventListener("click",()=>api("open-link",{name:"register"}).catch(e=>toast(e.message,true)));
  $("connectButton").addEventListener("click",connect);
  $("clientSecret").addEventListener("keydown",event=>{if(event.key==="Enter")connect();});
  $("toggleSecret").addEventListener("click",()=>{$("clientSecret").type=$("clientSecret").type==="password"?"text":"password";});
  $("disconnectButton").addEventListener("click",async()=>{try{await api("disconnect",{});delete Accounts.errors.oauth;$("clientId").value="";$("clientSecret").value="";await Accounts.refresh();clearScenes();renderAuth();$("clientId").focus();}catch(error){toast(error.message,true);}});
  $("helpButton").addEventListener("click",()=>$("helpDialog").showModal());
  $("openAuthDocs").addEventListener("click",()=>api("open-link",{name:"auth"}).catch(e=>toast(e.message,true)));
  $("openQuotaDocs").addEventListener("click",()=>api("open-link",{name:"quotas"}).catch(e=>toast(e.message,true)));
  document.querySelectorAll("[data-close]").forEach(button=>button.addEventListener("click",()=>$(button.dataset.close).close()));
  $("maxCloud").addEventListener("input",()=>{const v=$("maxCloud").value;$("cloudValue").textContent=v+"%";$("maxCloud").style.background=`linear-gradient(to right,var(--green) 0%,var(--green) ${v}%,#e5ecee ${v}%,#e5ecee 100%)`;});
  for(const id of ["startDate","endDate","maxCloud"])$(id).addEventListener("change",()=>{if(state.query)clearScenes();});
  $("imageMode").addEventListener("change",()=>{$("modeDescription").textContent=modes[$("imageMode").value];$("customBands").hidden=$("imageMode").value!=="custom";schedulePlan();});
  $("resolution").addEventListener("change",schedulePlan);$("maskClouds").addEventListener("change",schedulePlan);$("customBands").addEventListener("change",schedulePlan);
  document.querySelectorAll("#aoiTabs button").forEach(button=>button.addEventListener("click",()=>activateAoiTab(button.dataset.tab)));
  for(const id of ["drawRectangle","mapDrawRectangle"])$(id).addEventListener("click",()=>startDrawing("rectangle"));
  for(const id of ["drawPolygon","mapDrawPolygon"])$(id).addEventListener("click",()=>startDrawing("polygon"));
  $("editBoundary").addEventListener("click",toggleEditing);
  for(const id of ["fitAoi","mapFitAoi"])$(id).addEventListener("click",()=>{if(aoiLayer?.getBounds().isValid())map.fitBounds(aoiLayer.getBounds(),{padding:[60,60],maxZoom:16});});
  $("clearAoi").addEventListener("click",async()=>{
    try{
      await api("aoi",{clear:true});
      if(state.editing){editHandler.disable();state.editing=false;$("editBoundary").classList.remove("active");$("drawHint").hidden=true;}
      setAoi(null);
    }catch(error){toast(error.message,true);}
  });
  $("applyCoordinates").addEventListener("click",applyCoordinates);$("importBoundary").addEventListener("click",importBoundary);$("chooseFolder").addEventListener("click",chooseFolder);
  $("outputDirectory").addEventListener("change",()=>api("preferences",{output_dir:$("outputDirectory").value}).catch(()=>{}));
  $("placeSearchForm").addEventListener("submit",searchPlaces);
  $("placeResults").addEventListener("click",event=>{const button=event.target.closest("[data-index]");if(!button)return;const place=state.places[Number(button.dataset.index)];if(map){const [south,north,west,east]=place.bounds;map.fitBounds([[south,west],[north,east]],{padding:[50,50],maxZoom:15});}$("placeQuery").value=place.name.split(",")[0];$("placeResults").hidden=true;});
  document.addEventListener("click",event=>{if(!$("placeSearchForm").contains(event.target))$("placeResults").hidden=true;});
  document.querySelectorAll("#basemapSwitch button").forEach(button=>button.addEventListener("click",()=>changeBasemap(button.dataset.basemap)));
  $("exportBoundary").addEventListener("click",()=>$("exportDialog").showModal());$("saveBoundary").addEventListener("click",exportBoundary);
  $("searchScenes").addEventListener("click",searchScenes);
  $("selectAll").addEventListener("click",()=>{if(Batch.selectAll())return;state.selected=new Set(state.scenes.filter(selectable).map(scene=>scene.key));renderScenes();});
  $("selectNone").addEventListener("click",()=>{if(Batch.selectAll(true))return;state.selected.clear();renderScenes();});
  $("scenesBody").addEventListener("click",event=>{if(state.busy)return;const row=event.target.closest("tr[data-key]");if(!row||row.classList.contains("scene-unavailable"))return;const key=row.dataset.key;if(state.selected.has(key))state.selected.delete(key);else state.selected.add(key);row.classList.toggle("selected",state.selected.has(key));row.querySelector("input").checked=state.selected.has(key);renderAssetPicker();schedulePlan();});
  $("scenesBody").addEventListener("mouseover",event=>{const row=event.target.closest("tr[data-key]");if(!row||!footprintLayer)return;const scene=state.scenes.find(scene=>scene.key===row.dataset.key);footprintLayer.clearLayers();for(const product of scene?.products||[])L.geoJSON(product.footprint,{style:{color:"#699cac",weight:1,fillOpacity:0,dashArray:"5,5",interactive:false}}).addTo(footprintLayer);});
  $("scenesBody").addEventListener("mouseleave",()=>footprintLayer?.clearLayers());
  $("downloadSelected").addEventListener("click",showDownloadPlan);$("changePlanFolder").addEventListener("click",chooseFolder);$("confirmDownload").addEventListener("click",beginDownload);
  $("toggleJobLogs").addEventListener("click",()=>{$("jobLogs").hidden=!$("jobLogs").hidden;$("toggleJobLogs").textContent=$("jobLogs").hidden?"查看记录":"收起记录";});
  $("closeJobPanel").addEventListener("click",()=>$("jobPanel").hidden=true);
  for(const id of ["cancelJob","cancelSearch"])$(id).addEventListener("click",()=>cancelActive($(id)));
  $("openResultFolder").addEventListener("click",()=>api("open-folder",{path:state.resultFolder}).catch(e=>toast(e.message,true)));
}

async function initialize() {
  $("customBands").innerHTML=allBands.map(band=>`<label class="band-choice"><input type="checkbox" value="${band}" ${["B02","B03","B04","B08"].includes(band)?"checked":""} data-lock>${band}</label>`).join("");
  $("gediBeams").innerHTML=["BEAM0000","BEAM0001","BEAM0010","BEAM0011","BEAM0101","BEAM0110","BEAM1000","BEAM1011"].map(beam=>`<label class="band-choice"><input type="checkbox" value="${beam}" checked data-lock>${beam}</label>`).join("");
  const today=new Date(), before=new Date(today);before.setUTCDate(before.getUTCDate()-30);
  const dateString=(date)=>date.toISOString().slice(0,10);
  $("startDate").value=dateString(before);$("endDate").value=dateString(today);
  for(const id of ["startDate","endDate"]){$(id).min="2015-06-23";$(id).max=dateString(today);}
  configureMap();bindEvents();Accounts.initialize();
  try{
    const data=await api("bootstrap");state.connected=data.connected;state.rawConnected=data.raw_connected;state.s3Connected=data.s3_connected;state.earthdataConnected=Boolean(data.earthdata_connected);state.gediCollections=data.gedi_collections||[];state.nativeDialogs=data.native_dialogs;
    Batch.initialize(data.batch_sources||[]);
    updates.configured=Boolean(data.update_repository);
    $("updateCurrent").textContent=`当前版本 v${data.version}`;
    $("updateButton").disabled=false;
    $("clientId").value=data.preferences.client_id||"";$("outputDirectory").value=data.preferences.output_dir;
    if(data.aoi)setAoi(data.aoi,true,true);
    const q=data.query||data.active_job?.detail?.criteria;
    renderOpenCollections("s5pCollection", data.s5p_collections||[], data.s5p_default||"", true);
    renderOpenCollections("sen3Collection", data.sen3_collections||[], data.sen3_default||"");
    renderOpenCollections("gediCollection", state.gediCollections, data.gedi_default||"");
    if(q){
      state.query=data.query;
      state.profile=q.profile||data.active_job?.detail?.search_profile||state.profile;
      $("sourceProfile").value=state.profile;
      if(isS5p()||isSen3())$(isSen3()?"sen3Collection":"s5pCollection").value=q.collection;
      else if(isGedi())$("gediCollection").value=q.collection;
      else state.collection=q.collection||"";
      $("radarMode").value=q.radar_mode||"";
      $("allDates").checked=isOfficial()&&!q.start&&!q.end;
      if(q.start)$("startDate").value=q.start;
      if(q.end)$("endDate").value=q.end;
      if(q.max_cloud!==undefined){$("maxCloud").value=q.max_cloud;$("maxCloud").dispatchEvent(new Event("input"));}
    }
    if(data.batch)Batch.accept(data.batch,true);
    renderProfile();
    if(isOfficial()||Batch.checked.has("cdse-original"))loadCollections().then(()=>Batch.renderSettings());
    if(data.query){state.scenes=data.scenes||[];state.selected=firstSelection();renderScenes();}
    if(data.active_job){state.jobId=data.active_job.id;setBusy(true);if(["search","batch-search"].includes(data.active_job.kind))showSearchProgress(true);else showDownloadProgress(data.active_job.kind);pollJob(state.jobId);}
    await Accounts.startup(data.accounts);
  }catch(error){toast(error.message,true);}
  updateAvailability();
}

initialize();
