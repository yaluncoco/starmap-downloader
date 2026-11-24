"use strict";

const Batch = {
  sources: [], checked: new Set(), configurations: new Map(), query: null,
  results: new Map(), active: "primary", primaryReference: "", primaryStamp: "",
  enabled() { return this.checked.size > 0 && !Monthly.enabled(); },
  totalSelected() { return state.selected.size + [...this.results.values()].reduce((n, group) => n + group.selected.size, 0); },
  sharedDates() { return {start: $("startDate").value, end: $("endDate").value}; },
  isStatic(profile, collection = "") { return profile.startsWith("dem-") || profile === "cdse-original" && /^(cop-dem-|s2glc)/.test(collection); },
  source(profile) { return this.sources.find(item => item.id === profile); },
  settings(profile) {
    if (!this.configurations.has(profile)) {
      const spec = this.source(profile);
      this.configurations.set(profile, {options: structuredClone(spec.default_options), alignment: profile === "gedi-earthdata" && this.primaryMayHaveRaster() ? "primary" : "aoi", sample: false, open: true});
    }
    return this.configurations.get(profile);
  },
  primaryMayHaveRaster() {
    return ["s2-process", "s2-earth-search", "s2-cdse-s3", "s2-l2a-cog", "s2-l3-quarterly", "landsat-pc-l2", "s1-rtc-pc", "s1-aws-original", "dem-30", "dem-90", "worldcover-s2", "worldcover-s1", "worldcover-map"].includes(state.profile) || state.profile.startsWith("modis-") || isGedi();
  },
  initialize(sources) {
    this.sources = sources;
    $("companionFilter").addEventListener("input", () => this.renderChoices());
    $("companionSources").addEventListener("change", event => {
      const input = event.target.closest("[data-companion]");
      if (!input) return;
      if (input.checked) { this.checked.add(input.dataset.companion); this.settings(input.dataset.companion); }
      else this.checked.delete(input.dataset.companion);
      clearScenes(); renderProfile(); this.renderSettings();
      if(input.checked && input.dataset.companion === "cdse-original") loadCollections().then(() => this.renderSettings());
    });
    $("companionSettings").addEventListener("change", event => this.changeSetting(event));
    $("companionSettings").addEventListener("click", async event => {
      const remove = event.target.closest("[data-remove-companion]");
      if (remove) {
        event.preventDefault(); this.checked.delete(remove.dataset.removeCompanion);
        clearScenes(); renderProfile(); this.renderSettings(); return;
      }
      const picker = event.target.closest("[data-choose-companion-reference]");
      if (!picker) return;
      try {
        const path = await nativePath("choose_reference", {}, () => choosePath("选择附带 GEDI 的参考 GeoTIFF", "填写已有 GeoTIFF 的完整路径。"));
        if (path) { this.settings(picker.dataset.chooseCompanionReference).options.reference_path = path; this.renderSettings(); schedulePlan(); }
      } catch (error) { toast(error.message, true); }
    });
    $("companionPrimaryReference").addEventListener("change", () => { this.primaryReference = $("companionPrimaryReference").value; schedulePlan(); });
    $("batchResultTabs").addEventListener("click", event => {
      const button = event.target.closest("[data-result-source]");
      if (button) { this.active = button.dataset.resultSource; this.renderResults(); updateAvailability(); }
    });
    $("companionResults").addEventListener("change", event => this.changeResult(event));
    this.renderChoices(); this.renderSettings();
  },
  primaryChanged() {
    this.checked.delete(state.profile); this.primaryReference = ""; this.primaryStamp = "";
    this.renderChoices(); this.renderSettings();
  },
  clearResults() {
    this.query = null; this.results.clear(); this.active = "primary"; this.primaryReference = "";
    $("batchResultTabs").hidden = true; $("companionResults").hidden = true;
    $("companionReferenceField").hidden = true;
    document.querySelector(".scenes-panel").classList.remove("has-companions");
  },
  criteriaFor(profile) {
    const options = this.settings(profile).options;
    const value = this.isStatic(profile, options.collection) ? {start: null, end: null} : this.sharedDates();
    if (options.collection) value.collection = options.collection;
    if (Object.hasOwn(options, "max_cloud")) value.max_cloud = Number(options.max_cloud);
    if (Object.hasOwn(options, "radar_mode")) value.radar_mode = options.radar_mode;
    return value;
  },
  searchReady() {
    if(Monthly.enabled())return true;
    return (!this.checked.has("s2-process") || state.connected) && (!this.checked.has("s2-cdse-s3") || state.s3Connected);
  },
  recommendedAccount() {
    if ((isGedi() || this.checked.has("gedi-earthdata")) && !state.earthdataConnected) return "earthdata";
    if ((isProcess() || this.checked.has("s2-process")) && !state.connected) return "oauth";
    if (isZip() || isOfficial() || isNativeS3() || this.checked.has("s2-cdse-zip") || this.checked.has("cdse-original") || this.checked.has("s2-cdse-s3")) return "cdse";
    return isProcess() ? "oauth" : "earthdata";
  },
  renderChoices() {
    const term = $("companionFilter").value.trim().toLocaleLowerCase();
    $("companionSources").innerHTML = this.sources.filter(source => !term || `${source.title} ${source.group} ${source.id}`.toLocaleLowerCase().includes(term) || this.checked.has(source.id)).map(source => {
      const primary = source.id === state.profile;
      return `<label class="asset-entry"><input type="checkbox" data-companion="${escapeHtml(source.id)}" aria-label="附带 ${escapeHtml(source.title)}" ${this.checked.has(source.id) ? "checked" : ""} ${primary || state.busy || state.connecting ? "disabled" : ""} ${primary ? 'data-primary="1"' : ""} data-lock><span><strong>${escapeHtml(source.title)}</strong><small>${primary ? "当前主来源" : source.auth === "earthdata" ? "需要 NASA Earthdata" : source.auth === "sentinel-hub" ? "需要 Sentinel Hub OAuth" : source.auth === "cdse-s3" ? "需要 CDSE S3 密钥" : source.auth === "cdse" ? "按下载通道使用 CDSE 凭据" : "无需个人登录"}</small></span></label>`;
    }).join("");
    $("companionCount").textContent = this.checked.size ? `${this.checked.size} 个来源` : "可选";
    $("companionDateNote").hidden = !this.enabled();
  },
  collectionOptions(source, selected) {
    const items = source.id === "cdse-original" ? state.collections : source.collections || [];
    if (!items.length) return `<option value="${escapeHtml(selected || "")}">${source.id === "cdse-original" ? "正在载入官方类别…" : "选择类别…"}</option>`;
    return items.map(item => `<option value="${escapeHtml(item.id)}" ${selected === item.id ? "selected" : ""}>${escapeHtml(item.title)}</option>`).join("");
  },
  renderSettings() {
    const h = escapeHtml;
    const select = (key, title, values, current) => `<label>${h(title)}<select data-option="${key}" data-lock>${values.map(([value, label]) => `<option value="${h(value)}" ${String(current) === String(value) ? "selected" : ""}>${h(label)}</option>`).join("")}</select></label>`;
    const check = (key, title, current) => `<label class="checkbox-label"><input type="checkbox" data-option="${key}" ${current ? "checked" : ""} data-lock>${h(title)}</label>`;
    const arrays = (key, values, selected) => `<div class="band-grid">${values.map(([id, title]) => `<label class="band-choice"><input type="checkbox" data-array="${key}" value="${h(id)}" ${selected?.includes(id) ? "checked" : ""} data-lock>${h(title)}</label>`).join("")}</div>`;
    $("companionSettings").innerHTML = [...this.checked].map(profile => {
      const source = this.source(profile), config = this.settings(profile), o = config.options;
      let fields = "";
      if (Object.hasOwn(o, "collection")) fields += `<label>数据产品与版本<select data-option="collection" data-lock>${this.collectionOptions(source, o.collection)}</select></label>`;
      if (Object.hasOwn(o, "max_cloud")) fields += `<label>此来源整景云量上限（%）<input type="number" data-option="max_cloud" min="0" max="100" value="${h(o.max_cloud)}" data-lock></label>`;
      if (Object.hasOwn(o, "radar_mode")) fields += select("radar_mode", "雷达成像模式", [["", "全部"], ["IW", "IW"], ["EW", "EW"], ...(profile === "s1-rtc-pc" ? [] : [["SM", "SM"], ["WV", "WV"]])], o.radar_mode);
      if (profile === "cdse-original") fields += select("transport", "下载通道", [["auto", "按目录选择"], ["odata", "CDSE OData"], ["cdse-s3", "CDSE S3"]], o.transport || "auto");
      if (source.kind === "gedi") {
        const product = source.collections.find(item => item.id === o.collection);
        if (!product) return "";
        if (config.fieldsCollection !== product.id) {
          if (config.fieldsCollection || !Array.isArray(o.fields)) o.fields = product.fields.filter(field => field.default).map(field => field.id);
          config.fieldsCollection = product.id;
        }
        if (!o.beams) o.beams = ["BEAM0000", "BEAM0001", "BEAM0010", "BEAM0011", "BEAM0101", "BEAM0110", "BEAM1000", "BEAM1011"];
        if (product.kind === "raster" && o.mode === "footprints") o.mode = "aligned";
        fields += select("mode", "下载与导出", [["aligned", "原始文件 + 对齐 GeoTIFF"], ...(product.kind === "footprint" ? [["footprints", "原始文件 + 足迹 CSV / GeoJSON"]] : []), ["original", "仅原始文件"]], o.mode);
        if (o.mode !== "original") {
          if (product.kind === "footprint") {
            fields += `<label>GEDI 导出参数</label><div class="asset-checklist">${product.fields.map(field => `<label class="asset-entry"><input type="checkbox" data-array="fields" value="${h(field.id)}" ${o.fields.includes(field.id) ? "checked" : ""} data-lock><span><strong>${h(field.title)}</strong><small>${h(field.id)}${field.units ? " · " + h(field.units) : ""}</small></span></label>`).join("")}</div>`;
            fields += select("quality", "足迹质量筛选", [["recommended", "此版本的官方质量标志"], ["all", "保留全部，仍导出质量列"]], o.quality);
            fields += check("nondegraded", "另行去除定位降级足迹", o.nondegraded);
            if (product.level !== "L1B") fields += `<label>最低敏感度（0 表示不额外筛选）<input type="number" data-option="min_sensitivity" min="0" max="1" step="0.01" value="${h(o.min_sensitivity || 0)}" data-lock></label>`;
            else o.min_sensitivity = 0;
            fields += `<details><summary>波束选择</summary>${arrays("beams", ["BEAM0000", "BEAM0001", "BEAM0010", "BEAM0011", "BEAM0101", "BEAM0110", "BEAM1000", "BEAM1011"].map(b => [b, b]), o.beams)}</details>`;
          }
          fields += `<label>GEDI 网格与参考<select data-alignment data-lock>${[["primary", "跟随本次主 GeoTIFF"], ["aoi", "同研究区的 UTM 网格"], ["reference", "指定已有 GeoTIFF"]].map(([value, label]) => `<option value="${value}" ${config.alignment === value ? "selected" : ""}>${label}</option>`).join("")}</select></label>`;
          if (config.alignment === "reference") fields += `<label>参考 GeoTIFF<div class="input-with-action"><input data-option="reference_path" value="${h(o.reference_path || "")}" placeholder="已有影像的完整路径" data-lock><button type="button" class="icon-button" data-choose-companion-reference="${profile}" aria-label="选择附带 GEDI 参考影像" data-lock>${icon("folder")}</button></div></label>`;
          if (config.alignment === "aoi" && o.mode === "aligned") fields += select("resolution", "输出像元大小", [10, 20, 30, 40, 60, 90, 100, 250, 500, 1000].map(n => [n, n + " 米"]), o.resolution);
          if (config.alignment !== "aoi" && product.kind === "footprint") fields += `<label class="checkbox-label"><input type="checkbox" data-sample-reference ${config.sample ? "checked" : ""} data-lock>在足迹表格中附带参考影像像元值</label>`;
          fields += `<p class="field-note">${config.alignment === "primary" ? "先下载主影像，再完整读取其实际网格；可以在检索结果下方选择参考文件。" : "无足迹处保留 NoData，不在轨道空隙插值。"}</p>`;
        }
      } else if (source.kind === "optical") {
        fields += select("mode", "导出内容", [["research", "研究分析包：10 波段 + SCL + 备份 + 来源"], ["rgbnir", "RGB + 近红外反射率"], ["ndvi", "NDVI"], ["rgb", "自然色浏览图"], ["all", "全部 12 波段"], ["custom", "自选波段"]], o.mode);
        if (o.mode === "custom") { if (!o.bands) o.bands = ["B04", "B03", "B02", "B08"]; fields += arrays("bands", allBands.map(b => [b, b]), o.bands); }
        fields += select("resolution", "输出像元大小", [10, 20, 60].map(n => [n, n + " 米"]), o.resolution);
        fields += check("mask_clouds", "去除云、薄云和云阴影", o.mask_clouds);
        if (profile === "s2-cdse-s3") fields += '<p class="field-note">官方原件完整校验后拼接；首次会保存较大的原始波段与 XML 缓存。</p>';
        else fields += select("layout", "区域输出方式", [["mosaic", "跨日期拼接 · 一份研究区影像"], ["series", "时间序列 · 每个产品分别导出"]], o.layout || "mosaic");
        if ((o.layout || "mosaic") === "mosaic") fields += check("fill_gaps", "用已选影像补齐云和无数据位置", o.fill_gaps !== false);
      } else if (source.kind === "rtc") {
        fields += arrays("bands", ["VV", "VH", "HH", "HV"].map(b => [b, b]), o.bands);
        fields += select("resolution", "输出像元大小", [10, 20, 40, 60].map(n => [n, n + " 米"]), o.resolution);
      } else {
        fields += select("mode", "下载内容", source.modes.map(mode => [mode, {original: profile === "sen3-aws-original" ? "完整 SEN3（全部成员）" : profile.startsWith("modis-") ? "原始 HDF4（全部层）" : "原始主文件 / 整包", assets: "分别勾选各产品的原始文件", metadata: "各产品的一份元数据", dem: "按研究区裁剪 DSM"}[mode] || mode]), o.mode);
        if (o.mode === "assets") fields += `<p class="field-note">检索后在此来源的结果页勾选各产品文件。${source.default_assets.length ? "默认参数：" + source.default_assets.map(h).join(" / ") + "。" : "默认勾选可用的数据文件。"}</p>`;
      }
      const timeNote = this.isStatic(profile, o.collection) ? "静态产品；仅共用研究区，不表示所选日期的观测。" : source.time_note;
      return `<details class="source-settings" data-source-settings="${profile}" ${config.open ? "open" : ""}><summary><span>${h(source.title)}</span><button type="button" class="inline-link remove-companion" data-remove-companion="${profile}" data-lock>移除</button></summary>${fields}<p class="field-note">${h(timeNote)}</p></details>`;
    }).join("");
    for (const element of $("companionSettings").querySelectorAll("[data-source-settings]")) element.addEventListener("toggle", () => { this.settings(element.dataset.sourceSettings).open = element.open; });
    this.renderAuthentication();
  },
  renderAuthentication() {
    const needs = [];
    if (this.checked.has("gedi-earthdata")) needs.push(state.earthdataConnected ? "Earthdata 已载入" : "GEDI 需要 Earthdata");
    if (this.checked.has("s2-process")) needs.push(state.connected ? "Sentinel Hub 已连接" : "Sentinel Hub 需要 OAuth");
    if (this.checked.has("cdse-original")) needs.push(state.rawConnected || state.s3Connected ? "CDSE 凭据已载入" : "CDSE 需按通道登录");
    if (this.checked.has("s2-cdse-s3")) needs.push(state.s3Connected ? "S3 密钥已载入" : "官方像元读取需要 S3 密钥");
    $("companionAuthNote").hidden = !needs.length; $("companionAuthNote").textContent = needs.join(" · ");
    Accounts.visibility();
  },
  availability() {
    for (const input of $("companionSources").querySelectorAll("input")) input.disabled = !!input.dataset.primary || state.busy || state.connecting;
    this.renderAuthentication();
  },
  changeSetting(event) {
    const root = event.target.closest("[data-source-settings]");
    if (!root) return;
    const profile = root.dataset.sourceSettings, config = this.settings(profile), field = event.target;
    let newQuery = false;
    if (field.dataset.array) {
      const key = field.dataset.array;
      config.options[key] = [...root.querySelectorAll(`[data-array="${key}"]:checked`)].map(input => input.value);
    } else if (field.hasAttribute("data-alignment")) {
      config.alignment = field.value;
    } else if (field.hasAttribute("data-sample-reference")) {
      config.sample = field.checked;
    } else if (field.dataset.option) {
      const key = field.dataset.option;
      config.options[key] = field.type === "checkbox" ? field.checked : field.type === "number" || key === "resolution" ? Number(field.value) : field.value;
      newQuery = ["collection", "max_cloud", "radar_mode"].includes(key);
    }
    if (newQuery) clearScenes();
    if (["collection", "mode", "layout"].includes(field.dataset.option) || field.hasAttribute("data-alignment")) this.renderSettings();
    if (profile === "cdse-original" && !state.collections.length) loadCollections().then(() => this.renderSettings());
    if (!newQuery) { this.renderResults(); schedulePlan(); Spatial.refresh(); }
    updateAvailability();
  },
  optionsFor(profile) {
    const config = this.settings(profile), result = structuredClone(config.options);
    if (profile === "s2-cdse-s3") result.layout = "mosaic";
    if (profile === "gedi-earthdata") {
      result.reference_path = config.alignment === "reference" && result.mode !== "original" ? result.reference_path || "" : "";
      result.sample_reference = config.alignment === "reference" && config.sample && result.mode !== "original";
    }
    return result;
  },
  searchRequest(base) {
    return {...base, shared_dates: this.sharedDates(), options: options(), companions: [...this.checked].map(profile => ({id: profile, profile, criteria: this.criteriaFor(profile), options: this.optionsFor(profile)}))};
  },
  downloadRequest(base) {
    const stamp = JSON.stringify([base.scene_keys, base.options]);
    if (stamp !== this.primaryStamp) { this.primaryStamp = stamp; this.primaryReference = ""; }
    return {...base, batch_id: this.query?.id, shared_dates: this.sharedDates(), primary_reference: this.primaryReference || undefined,
      companions: [...this.checked].map(profile => {
        const config = this.settings(profile), result = this.results.get(profile), opts = this.optionsFor(profile);
        const aligned = profile === "gedi-earthdata" && config.alignment === "primary" && opts.mode !== "original";
        return {id: profile, profile, criteria: this.criteriaFor(profile), scene_keys: [...(result?.selected || [])], options: opts,
          scene_assets: opts.mode === "assets" ? Object.fromEntries([...(result?.selected || [])].map(key => [key, [...(result.assets.get(key) || [])]])) : {},
          align_to_primary: aligned, sample_primary: aligned && config.sample};
      })};
  },
  accept(batch, restore = false) {
    this.query = batch;
    if (restore) {
      this.checked = new Set(batch.groups.slice(1).map(group => group.profile));
      for (const group of batch.groups.slice(1)) {
        const config = this.settings(group.profile);
        config.options = {...config.options, ...group.options, ...group.criteria};
      }
    }
    this.results.clear();
    for (const group of batch.groups.slice(1)) {
      const source = this.source(group.profile);
      const selected = new Set(group.scenes.filter(scene => Spatial.available(scene)).map(scene => scene.key));
      const assets = new Map(group.scenes.map(scene => {
        const available = this.assetsFor(scene);
        const defaults = source.default_assets.length ? available.filter(asset => source.default_assets.includes(asset.id)) : available.filter(asset => !/\.(xml|json|txt|safe)$/i.test(asset.name));
        return [scene.key, new Set(defaults.map(asset => asset.id))];
      }));
      this.results.set(group.id, {...group, selected, assets});
    }
    this.renderChoices(); this.renderSettings(); this.renderResults();
  },
  assetsFor(scene) {
    return (scene.raw?.assets || []).filter(asset => /\.(tif|tiff|jp2|nc|hdf|h5|xml|json|txt|safe|zip)$/i.test(asset.name));
  },
  renderResults() {
    if (!this.query) return;
    const primary = this.active === "primary";
    const tabs = [{id: "primary", title: "主数据", count: state.scenes.length, selected: state.selected.size}, ...[...this.results.values()].map(group => ({id: group.id, title: group.profile === "gedi-earthdata" ? "GEDI" : profiles[group.profile][0], count: group.scenes.length, selected: group.selected.size}))];
    $("batchResultTabs").hidden = false;
    $("batchResultTabs").innerHTML = tabs.map(tab => `<button type="button" role="tab" data-result-source="${escapeHtml(tab.id)}" aria-selected="${this.active === tab.id}">${escapeHtml(tab.title)} · ${tab.selected}/${tab.count}</button>`).join("");
    document.querySelector(".scenes-panel").classList.add("has-companions");
    $("scenesTableWrap").hidden = !primary || !state.scenes.length;
    $("scenesEmpty").hidden = !primary || !!state.scenes.length;
    $("companionResults").hidden = primary;
    Spatial.refresh();
    if (primary) return;
    const group = this.results.get(this.active);
    if (!group) { this.active = "primary"; this.renderResults(); return; }
    const source = this.source(group.profile), config = this.settings(group.profile);
    $("companionResults").innerHTML = `<p class="companion-result-note">${escapeHtml(source.title)} · ${escapeHtml(this.query.shared_dates.start)} — ${escapeHtml(this.query.shared_dates.end)} UTC · 已选 ${group.selected.size} 个。${escapeHtml(group.time_note || source.time_note)}</p>` + (group.error ? `<p class="companion-error">检索失败：${escapeHtml(group.error)}</p>` : !group.scenes.length ? '<p class="companion-result-note">相同研究区和日期没有匹配数据；计划会记录该结果。</p>' : "") + group.scenes.map(scene => {
      const available = Spatial.available(scene);
      const selected = group.selected.has(scene.key), assets = this.assetsFor(scene);
      const assetList = config.options.mode === "assets" && selected ? `<details><summary>原始文件 · 已选 ${(group.assets.get(scene.key) || new Set()).size} / ${assets.length}</summary><div class="asset-checklist">${assets.map(asset => `<label class="asset-entry"><input type="checkbox" data-companion-asset="${escapeHtml(asset.id)}" data-scene="${escapeHtml(scene.key)}" ${group.assets.get(scene.key)?.has(asset.id) ? "checked" : ""} ${state.busy ? "disabled" : ""}><span><strong>${escapeHtml(asset.title || asset.id)}</strong><small>${escapeHtml(asset.name)}${asset.bytes ? " · " + sizeLabel(asset.bytes) : ""}</small></span></label>`).join("")}</div></details>` : "";
      return `<div class="companion-scene"><label class="companion-scene-label"><input type="checkbox" data-companion-scene="${escapeHtml(scene.key)}" ${selected ? "checked" : ""} ${!available || state.busy ? "disabled" : ""} ${!available ? 'data-unavailable="1"' : ""}><span><strong>${escapeHtml(scene.title || scene.key)}</strong><small>${escapeHtml(scene.date)} ${escapeHtml(scene.time)} UTC · ${escapeHtml(scene.tiles?.join(" / "))}${scene.gedi?.estimated_bytes ? " · " + sizeLabel(scene.gedi.estimated_bytes) : ""}${!available ? " · " + escapeHtml(scene.gedi?.unavailable_reason || scene.raw?.unavailable_reason || "无可用入口") : ""}</small></span></label>${assetList}</div>`;
    }).join("");
  },
  changeResult(event) {
    const group = this.results.get(this.active), input = event.target;
    if (!group || selectionLocked()) return;
    Spatial.touched.add(group.id);
    if (input.hasAttribute("data-companion-scene")) {
      const key = input.dataset.companionScene;
      if (input.checked) group.selected.add(key); else group.selected.delete(key);
    } else if (input.hasAttribute("data-companion-asset")) {
      const picked = group.assets.get(input.dataset.scene);
      if (input.checked) picked.add(input.dataset.companionAsset); else picked.delete(input.dataset.companionAsset);
    }
    this.renderResults(); schedulePlan(); Spatial.refresh();
  },
  selectAll(clear = false) {
    const group = this.results.get(this.active);
    if (!group) return false;
    if (selectionLocked()) return true;
    group.selected = clear ? new Set() : new Set(group.scenes.filter(scene => Spatial.available(scene)).map(scene => scene.key));
    this.renderResults(); schedulePlan(); Spatial.refresh(); return true;
  },
  updateReferences(plan) {
    if (plan?.kind !== "batch") return;
    const references = plan.reference_candidates || [];
    if (!this.primaryReference) this.primaryReference = plan.primary_reference || "";
    $("companionReferenceField").hidden = !references.length || !plan.groups.some(group => group.align_to_primary && group.selected_count);
    $("companionPrimaryReference").innerHTML = references.map(item => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.title)}</option>`).join("");
    $("companionPrimaryReference").value = this.primaryReference;
  },
  planHtml(plan) {
    const h = escapeHtml;
    const dates = `${plan.shared_dates.start} — ${plan.shared_dates.end}（UTC）`;
    let html = `<div class="plan-item"><span>共同日期</span><strong>${h(dates)}</strong></div><div class="plan-item"><span>研究区</span><strong>${h(state.aoi.name)} · ${friendlyArea(state.aoi.area_km2)}</strong></div><div class="plan-item"><span>来源 / 选中结果</span><strong>${plan.groups.length} 个来源 / ${plan.scene_count} 项</strong></div><div class="plan-item"><span>预计下载 / 未压缩输出</span><strong>约 ${sizeLabel(plan.estimated_bytes)}${plan.unknown_size ? "，部分原文件体积由服务确定" : ""}</strong></div>`;
    for (const [index, group] of plan.groups.entries()) {
      html += `<div class="plan-source-title">${index ? "附带" : "主数据"} · ${h(group.title)}</div>`;
      if (!group.selected_count) {
        html += `<div class="plan-source-summary ${group.error ? "error" : ""}">${h(group.error ? "检索失败：" + group.error : group.status === "empty" ? "相同区域和日期没有可用匹配；记录为空。" : "本来源未选择下载结果。")}</div>`; continue;
      }
      html += `<div class="plan-source-summary">${group.selected_count} / ${group.matched_count} 个结果 · 约 ${sizeLabel(group.estimated_bytes)}<br>${h(group.time_note)}</div>`;
      for (const p of group.plans.slice(0, 3)) {
        const detail = p.kind === "gedi" ? gediPlanItems(p) : p.kind === "original" ? [["下载内容", p.options.mode === "metadata" ? "元数据" : `${p.requests} 个原始文件`], ["文件", p.targets.slice(0, 6).map(t => t.name).join(" / ")]] : [["输出网格", `${p.width} × ${p.height} · ${p.resolution} 米`], ["输出内容", p.options.label], ["坐标系", p.crs_label]];
        if (p.kind === "mosaic") detail.unshift(["拼接产品", `${p.source_count} 个区块 / 1 份区域输出`],
          ["选中采集日期（UTC）", p.dates.join(" / ")], ["产品边界覆盖", `${p.coverage.coverage_percent.toFixed(2)}% · 未覆盖 ${p.coverage.uncovered_percent.toFixed(2)}%`],
          ["交叠优先级", p.coverage.priority_label]);
        if (p.transport === "cdse-s3" && p.kind === "mosaic") detail.push(["官方原件缓存", `${p.source_objects} 个原件 · 约 ${sizeLabel(p.native_source_bytes)}`]);
        for (const [label, value] of detail) html += `<div class="plan-item"><span>${h(label)}</span><strong class="plan-path">${h(value)}</strong></div>`;
      }
      if (group.plans.length > 3) html += `<div class="plan-source-summary">另 ${group.plans.length - 3} 个产品的文件选择已计入计划。</div>`;
      if (group.align_to_primary) html += `<div class="plan-source-summary">GEDI 跟随参考：${h(group.reference_source)}${group.reference_grid_pending ? "。主文件下载完成后读取实际网格。" : "。完整采用主影像网格。"}${group.sample_primary ? "足迹表格附带该影像像元值。" : ""}</div>`;
    }
    return html + '<div class="plan-item wide"><span>共同输出目录（含研究区 SHP 和每个来源的记录）</span><strong id="planPreviewFolder" class="plan-path"></strong></div>';
  },
  progress(groups) {
    $("jobSources").hidden = !groups?.length;
    const labels = {pending: "等待", searching: "检索中", ready: "可下载", empty: "无匹配", not_selected: "未选择", running: "下载中", completed: "完成", failed: "失败", partial: "部分完成", search_failed: "检索失败", dependency_failed: "参考未生成", cancelled: "已取消"};
    $("jobSources").innerHTML = (groups || []).map(group => `<div class="job-source-row ${["failed", "partial", "search_failed", "dependency_failed"].includes(group.status) ? "failed" : group.status}"><span>${escapeHtml(group.title)}</span><span title="${escapeHtml(group.error || "")}">${labels[group.status] || group.status}</span></div>`).join("");
  }
};
