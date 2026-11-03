/* 115 转存工作台设置与扫码：配置由插件加密保存，登录复用原有接口。 */
(function () {
  "use strict";
  const BASE = "/api/v1/plugins/p115-import";
  const SETTINGS = BASE + "/settings";
  const $ = (id) => document.getElementById(id);
  const fields = ["app_id", "login_app", "offline_enabled", "offline_retry", "wecom_entry", "dry_run"];
  const switches = new Set(["offline_enabled", "offline_retry", "wecom_entry", "dry_run"]);
  let original = null;
  let originalView = null;
  let connection = null;
  let loading = false;
  let saving = false;
  let epochs = { web: 0, open: 0 };

  async function request(url, method, body) {
    const response = await fetch(url, {
      method: method || "GET", credentials: "same-origin",
      headers: { "Accept-Language": document.documentElement ? document.documentElement.lang : "zh-Hans",
        ...(body ? { "Content-Type": "application/json" } : {}) },
      body: body ? JSON.stringify(body) : undefined,
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || payload.success === false) {
      const detail = payload.detail || payload.message || ("HTTP " + response.status);
      throw new Error(typeof detail === "string" ? detail : "请求失败，请检查输入");
    }
    return payload.success === true && "data" in payload ? payload.data : payload;
  }
  function message(text, bad) {
    $("settings-message").textContent = text;
    $("settings-message").className = bad ? "small err" : "small muted";
  }
  function changesNow() {
    const current = {};
    fields.forEach((key) => {
      const el = $("setting-" + key);
      current[key] = switches.has(key) ? el.checked : el.value.trim();
    });
    return settingsChanges(original, current);
  }
  function updateDirty() {
    if (!original) return;
    const result = changesNow();
    const dirty = !!result.error || Object.keys(result.changes).length > 0;
    $("settings-save").disabled = saving || !originalView.editable || !dirty;
    $("settings-reset").hidden = !dirty;
    $("settings-reset").disabled = saving;
    $("settings-dirty-note").textContent = dirty ? "有未保存的修改" : "已保存";
    renderConnection();
  }
  function populate(view) {
    const values = view.values || {};
    original = { ...values };
    originalView = view;
    const select = $("setting-login_app");
    select.textContent = "";
    const choices = (((view.schema || {}).properties || {}).login_app || {}).enum || [];
    choices.forEach((choice) => {
      const option = document.createElement("option");
      option.value = choice; option.textContent = choice; select.appendChild(option);
    });
    fields.forEach((key) => {
      const el = $("setting-" + key);
      if (switches.has(key)) el.checked = !!values[key];
      else el.value = values[key] == null ? "" : String(values[key]);
    });
    $("settings-form").hidden = false;
    message(view.editable ? "" : (view.reason || "当前设置不可编辑。"), !view.editable);
    updateDirty();
  }
  function renderConnection() {
    const state = settingsConnections(connection, original && original.app_id, $("setting-app_id").value);
    const web = $("login-web-badge"), open = $("login-open-badge");
    const webLabels = { connected: "已连接", needs_login: "未登录", unknown: "状态未知" };
    const openLabels = { authorized: "已授权", needs_auth: "待授权", unconfigured: "未配置", unsaved: "待保存", unknown: "状态未知" };
    web.textContent = webLabels[state.web];
    web.className = "login-badge " + (state.web === "connected" ? "is-good" : state.web === "needs_login" ? "is-warn" : "");
    $("login-web-detail").textContent = state.web === "connected" && connection.account
      ? "当前账号：" + connection.account : "用于转存分享链接和文件改名。";
    $("login-web-start").textContent = state.web === "connected" ? "重新扫码" : "扫码登录";
    open.textContent = openLabels[state.open];
    open.className = "login-badge " + (state.open === "authorized" ? "is-good" : state.open === "unknown" ? "" : "is-warn");
    $("login-open-detail").textContent = state.open === "authorized" ? "官方接口已授权，可用于文件操作。"
      : state.open === "unsaved" ? "AppID 已修改，保存后再扫码授权。" : "授权后可使用官方接口管理文件。";
    $("login-open-start").textContent = state.open === "authorized" ? "重新授权" : "扫码授权";
  }
  async function refreshConnection() {
    try { connection = await request(BASE + "/status"); }
    catch (_) { connection = null; }
    renderConnection();
  }
  async function loadSettings() {
    if (loading) return;
    loading = true; message("正在读取设置…");
    try { populate(await request(SETTINGS)); await refreshConnection(); }
    catch (err) { original = null; $("settings-form").hidden = true; message("无法读取设置：" + err.message + "（请确认已用管理员账号登录）", true); }
    finally { loading = false; }
  }
  $("settings-panel").addEventListener("toggle", () => {
    if ($("settings-panel").open) { if (!original) loadSettings(); else refreshConnection(); }
  });
  $("settings-form").addEventListener("input", updateDirty);
  $("settings-form").addEventListener("change", updateDirty);
  $("settings-reset").onclick = () => { if (originalView) populate(originalView); };
  $("settings-form").onsubmit = async (event) => {
    event.preventDefault();
    if (!original) return;
    if (saving) return;
    const result = changesNow();
    if (result.error) { message(result.error, true); return; }
    if (!Object.keys(result.changes).length) { message("没有需要保存的修改。"); return; }
    if (!confirm("设置保存后立即生效，可能影响正在处理的入库任务。确定继续吗？")) return;
    saving = true; $("settings-controls").disabled = true; updateDirty(); message("正在保存设置…");
    try {
      const changedAppId = "app_id" in result.changes;
      const view = await request(SETTINGS, "PUT", { values: result.changes });
      populate(view);
      message(changedAppId ? "设置已保存。请按需重新授权开放平台。" : "设置已保存。");
      await refreshConnection();
    } catch (err) { message("保存失败：" + err.message, true); }
    finally { saving = false; $("settings-controls").disabled = false; updateDirty(); }
  };
  function setQr(kind, result) {
    const image = $("login-" + kind + "-qr");
    const link = $("login-" + kind + "-link");
    const src = result.qrcode_image || "";
    image.hidden = !/^data:image\/(?:svg\+xml|png);base64,[a-z0-9+/=]+$/i.test(src);
    if (!image.hidden) image.src = src; else image.removeAttribute("src");
    const url = result.scan_url || "";
    link.hidden = !image.hidden || !/^https:\/\//i.test(url);
    if (!link.hidden) link.href = url; else link.removeAttribute("href");
  }
  async function startLogin(kind) {
    const button = $("login-" + kind + "-start");
    const result = $("login-" + kind + "-result");
    const label = $("login-" + kind + "-status");
    const epoch = ++epochs[kind];
    result.hidden = false; label.textContent = "正在生成二维码…"; setQr(kind, {}); button.disabled = true;
    try {
      if (!original) throw new Error("请先加载设置，再生成二维码。");
      if (kind === "open" && (!original.app_id || $("setting-app_id").value.trim() !== original.app_id)) {
        throw new Error("请先填写并保存 AppID，再生成授权二维码。");
      }
      const started = await request(BASE + "/auth/" + kind + "/start", "POST", kind === "web" ? { app: $("setting-login_app").value } : {});
      if (epoch !== epochs[kind]) return;
      setQr(kind, started);
      label.textContent = started.qrcode_image ? "请使用 115 App 扫码。" : "二维码图片生成失败，请使用下方扫码链接。";
      let failures = 0;
      const poll = async () => {
        if (epoch !== epochs[kind]) return;
        try {
          const status = await request(BASE + "/auth/" + kind + "/status?login_id=" + encodeURIComponent(started.id) + "&wait_seconds=0");
          if (epoch !== epochs[kind]) return;
          failures = 0;
          if (status.status === "pending" || status.status === "scanned") {
            label.textContent = status.status === "scanned" ? "已扫码，请在手机上确认。" : "等待扫码…";
            setTimeout(poll, 2000); return;
          }
          label.textContent = status.message || (status.status === "done" ? "操作成功。" : "操作未完成，请重新生成二维码。");
          button.disabled = false;
          if (status.status === "done") { setQr(kind, {}); await refreshConnection(); window.dispatchEvent(new Event("p115-auth-changed")); }
        } catch (err) {
          if (epoch !== epochs[kind]) return;
          if (++failures < 3) { label.textContent = "状态查询暂时失败，正在重试…"; setTimeout(poll, 3000); }
          else { label.textContent = "状态查询失败：" + err.message + "，请重新生成二维码。"; button.disabled = false; }
        }
      };
      setTimeout(poll, 2000);
    } catch (err) {
      if (epoch === epochs[kind]) { label.textContent = err.message; button.disabled = false; }
    }
  }
  function closeQr(kind) {
    epochs[kind]++;
    $("login-" + kind + "-result").hidden = true;
    $("login-" + kind + "-start").disabled = false;
    setQr(kind, {});
  }
  $("login-web-start").onclick = () => startLogin("web");
  $("login-open-start").onclick = () => startLogin("open");
  $("login-web-close").onclick = () => closeQr("web");
  $("login-open-close").onclick = () => closeQr("open");
})();
