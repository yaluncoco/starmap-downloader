"use strict";

const Accounts = {
  active: "earthdata", status: null, errors: {},
  initialize() {
    $("manageAccounts").addEventListener("click", () => this.open(Batch.recommendedAccount()));
    $("accountTabs").addEventListener("click", event => {
      const button = event.target.closest("[data-account]");
      if (button) { this.active = button.dataset.account; this.visibility(); }
    });
    $("accountTabs").addEventListener("keydown", event => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      const buttons = [...$("accountTabs").querySelectorAll("button")];
      const current = buttons.findIndex(button => button.dataset.account === this.active);
      const index = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1 :
        (current + (event.key === "ArrowRight" ? 1 : -1) + buttons.length) % buttons.length;
      event.preventDefault(); this.active = buttons[index].dataset.account; this.visibility(); buttons[index].focus();
    });
    $("clearSavedAccounts").addEventListener("click", async () => {
      if (state.busy || state.connecting) return;
      try {
        this.apply(await api("accounts-clear", {})); this.errors = {};
        for (const id of ["clientId", "clientSecret", "earthdataUsername", "earthdataPassword", "earthdataToken", "rawUsername", "rawPassword", "rawTotp", "rawToken", "s3AccessKey", "s3SecretKey"]) $(id).value = "";
        if (isProcess()) clearScenes();
        renderAuth(); renderEstimate(); toast("已退出所有账号并删除本机保存的登录信息。");
      } catch (error) { toast(error.message, true); }
    });
  },
  open(account = "earthdata") {
    this.active = account;
    this.visibility(); this.render();
    if (!$("accountsDialog").open) $("accountsDialog").showModal();
  },
  visibility() {
    $("processAuth").hidden = this.active !== "oauth";
    $("gediAuth").hidden = this.active !== "earthdata";
    $("rawAuth").hidden = this.active !== "cdse";
    for (const button of $("accountTabs").querySelectorAll("button")) {
      const active = button.dataset.account === this.active;
      button.classList.toggle("active", active); button.setAttribute("aria-selected", String(active));
      button.tabIndex = active ? 0 : -1;
    }
  },
  saved(provider) { return Boolean(this.status?.providers?.[provider]?.saved); },
  setError(provider, message) { this.errors[provider] = message; this.render(); },
  apply(data, prefill = false) {
    this.status = data;
    const providers = data.providers || {};
    state.connected = Boolean(providers.oauth?.connected);
    state.rawConnected = Boolean(providers.odata?.connected);
    state.s3Connected = Boolean(providers.s3?.connected);
    state.earthdataConnected = Boolean(providers.earthdata?.connected);
    for (const [provider, values] of Object.entries(providers)) if (values.connected) delete this.errors[provider];
    if (prefill) {
      for (const [provider, field, mode] of [["oauth", "clientId", null], ["odata", "rawUsername", "rawLoginMode"], ["earthdata", "earthdataUsername", "earthdataLoginMode"]]) {
        const values = providers[provider];
        if (values?.identifier) $(field).value = values.identifier;
        if (mode && values?.saved) { $(mode).value = values.method; $(mode).dispatchEvent(new Event("change")); }
      }
    }
    this.render();
  },
  render() {
    if (!this.status) return;
    $("manageAccounts").setAttribute("aria-busy", String(this.status.restoring));
    const providers = this.status.providers || {};
    for (const [provider, statusId, errorId, passwordId] of [
      ["oauth", "oauthAuthState", "authError", "clientSecret"],
      ["earthdata", "earthdataAuthState", "earthdataAuthError", "earthdataPassword"],
      ["odata", "rawAuthState", "rawAuthError", "rawPassword"],
      ["s3", "s3AuthState", "s3AuthError", "s3SecretKey"]
    ]) {
      const values = providers[provider] || {};
      $(statusId).textContent = values.connected ? (values.saved ? "已连接并保存 · 下次自动恢复" : "已连接 · 本次运行有效") +
        (values.verified ? "" : " · 下载时验证") : "尚未连接";
      const message = this.errors[provider] || values.error || "";
      $(errorId).textContent = message; $(errorId).hidden = !message;
      $(passwordId).placeholder = values.saved ? "已保存，留空沿用；填写可更新" : provider === "oauth" ? "粘贴客户端 Secret" : "";
    }
    for (const [provider, id] of [["earthdata", "earthdataToken"], ["odata", "rawToken"]]) {
      $(id).placeholder = providers[provider]?.saved && providers[provider].method === "token" ? "已保存，留空沿用；填写可更新" : "";
    }
    for (const button of $("accountTabs").querySelectorAll("button")) {
      const names = button.dataset.account === "cdse" ? ["odata", "s3"] : [button.dataset.account];
      const connected = names.some(name => providers[name]?.connected);
      const saved = names.some(name => providers[name]?.saved);
      button.classList.toggle("account-connected", connected);
      button.querySelector("small").textContent = connected ? "已连接" : saved ? "待验证" : "未登录";
    }
    const status = $("accountRestoreStatus");
    status.textContent = this.status.restoring ? "正在恢复已保存的账号，请稍候…" : this.status.storage_error ||
      (Object.values(providers).some(value => value.error) ? "部分账号需要重新验证，请检查对应平台。" : "");
    status.hidden = !status.textContent;
    status.classList.toggle("account-status-error", !this.status.restoring);
    $("accountComplete").textContent = this.status.has_connected_accounts ? "完成，选择数据" : "暂不登录，选择数据";
    $("clearSavedAccounts").disabled = state.busy || state.connecting || !(this.status.has_saved_accounts || this.status.storage_error);
    this.visibility();
  },
  async refresh() {
    this.apply(await api("accounts"));
    this.render();
  },
  async startup(data) {
    if (!data) { this.open(Batch.recommendedAccount()); return; }
    this.apply(data, true);
    if (data.restoring) {
      state.connecting = true; updateAvailability();
      try {
        while (this.status.restoring) {
          await new Promise(resolve => setTimeout(resolve, 250));
          this.apply(await api("accounts"), true);
        }
      } catch (error) {
        state.connecting = false; renderAuth(); this.open(Batch.recommendedAccount()); toast(error.message, true); return;
      }
    }
    state.connecting = false; renderAuth(); renderEstimate();
    const publicResume = downloads.tasks.some(task => Array.isArray(task.requires) && task.requires.length === 0);
    if (this.status.login_required && !publicResume) {
      const failed = Object.entries(this.status.providers).find(([, value]) => value.error)?.[0];
      this.open(failed === "odata" || failed === "s3" ? "cdse" : failed || Batch.recommendedAccount());
    }
  }
};
