"use strict";
window.MovieFriendsDisplayPolicy = (() => {
  let bridge, epoch = 0, timer = null, busy = false;
  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; };
  const help = (label,text) => window.MovieFriendsHelp.create(label,text);
  const names = { movieclaw: "MovieClaw", emby: "Emby", plex: "Plex" };
  function action(label, fn, cls = "button-outline") {
    const n = el("button", `button ${cls}`, label); n.type = "button";
    n.onclick = async () => { n.disabled = true; const session = bridge.sessionVersion(); try { await fn(); } catch (e) { if (!e.staleSession && session === bridge.sessionVersion()) bridge.toast(bridge.safeError(e), true); } finally { n.disabled = false; } }; return n;
  }
  const alive = (version, session) => version === epoch && bridge.user() && session === bridge.sessionVersion();
  async function nativeCatalog() {
    const version = epoch, session = bridge.sessionVersion();
    const rows = await bridge.hostApi("/libraries");
    if (!alive(version,session)) {const error = new Error("账号已变化"); error.staleSession=true; throw error;}
    if (!Array.isArray(rows)) throw new Error("主程序媒体库列表暂不可用。");
    const libraries = rows.filter(row => Number.isSafeInteger(row.id) && row.id > 0 && row.viewer_access !== false && typeof row.name === "string").map(row => ({ id: String(row.id), name: row.name.slice(0,120) }));
    await bridge.api("/display-policy/movieclaw", { method: "PUT", body: { libraries } });
  }
  function render(data) {
    const root = document.getElementById("display-policy-settings"); root.replaceChildren();
    const panel = el("section", "panel display-policy-panel");
    const head = el("div", "section-heading"); head.append(el("h3", "", "媒体库展示范围"), action("刷新媒体库与来源", refresh)); panel.append(head);
    head.querySelector("h3").append(help("媒体库展示范围","勾选要排除的媒体库。对外分享始终排除这些作品；原始私人记录不会删除。设置排除库后，来源未确认的内容也会排除。取消排除后，已有公开授权可能恢复展示；不会修改主程序或 Emby/Plex 的库权限，无法收回他人已保存的内容。"));
    const excluded=data.groups.reduce((count,group)=>count+group.libraries.filter(item=>item.excluded).length,0);
    panel.append(el("p","scope-count",excluded?`已排除 ${excluded} 个媒体库`:"未排除媒体库"));
    const reading = data.scan?.status === "reading";
    const form = el("form", "display-policy-form"); const choices = [];
    // 隐藏已断开的来源卡片，但保存时保留其旧排除规则，避免重新连接后意外公开。
    for (const group of data.groups.filter(group=>group.active===false)) for (const library of group.libraries.filter(library=>library.excluded)) choices.push({source_id:group.source_id,library_id:library.id,box:{checked:true}});
    const groups = data.groups.filter(group=>group.active!==false).sort((a,b) => (["movieclaw","plex","emby"].indexOf(a.platform) - ["movieclaw","plex","emby"].indexOf(b.platform)) || (a.account_name||"").localeCompare(b.account_name||""));
    const grid = el("div", "display-source-grid");grid.style.setProperty("--source-columns",Math.min(3,Math.max(1,groups.length)));
    for (const group of groups) {
      const section = el("fieldset", "display-library-group"); section.append(el("legend", "", `${names[group.platform] || "数据来源"}${group.account_name ? ` · ${group.account_name}` : ""}`));
      for (const library of group.libraries) {
        const label = el("label", "display-library-choice"), box = el("input"); box.type = "checkbox"; box.name=`excluded:${group.source_id}:${library.id}`;box.checked = library.excluded; box.disabled = reading;
        label.append(box, el("span", "", library.name), el("small", "field-help", "排除")); section.append(label); choices.push({ source_id: group.source_id, library_id: library.id, box });
      }
      section.dataset.platform=group.platform;grid.append(section);
    }
    form.append(grid);
    if (!groups.length) form.append(el("p", "field-help", "尚未读取媒体库。点击刷新，读取当前账号可访问的主程序及已连接 Emby/Plex 媒体库。"));
    const controls = el("fieldset", "display-library-group personal-display-options"); controls.append(el("legend", "", "我的观看足迹与统计"), help("足迹与统计范围","以下两项只控制个人主页，互相独立；对勾选的排除库及来源未确认的作品生效，不改变对外分享限制。"));
    function option(title, detail, checked) {
      const label = el("label", "setting-row"), text = el("span"), box = el("input");
      box.type = "checkbox"; box.setAttribute("role", "switch"); box.checked = checked; box.disabled = reading;
      text.append(el("strong", "", title), el("small", "", detail)); label.append(text,box); controls.append(label); return box;
    }
    const hide = option("从观看足迹中隐藏", "个人主页不显示这些记录，原始数据仍保留。", data.hide_private);
    const stats = option("不计入观看统计", "电影、电视剧、单集数量及待匹配计数均不包含这些记录。", data.exclude_statistics ?? data.hide_private);
    hide.name="hide_private";stats.name="exclude_statistics";
    const save = el("button", "button button-primary", "保存展示范围"); save.type = "submit"; save.disabled = reading; form.append(controls, save);
    window.MovieFriendsSettings.bind(form,{key:"display-policy",session:bridge.sessionVersion()});
    form.onsubmit = async event => {
      event.preventDefault(); save.disabled = true; const version = epoch, session = bridge.sessionVersion();
      try {
        const result = await bridge.api("/display-policy", { method: "PUT", body: { excluded: choices.filter(row => row.box.checked).map(({ source_id, library_id }) => ({ source_id, library_id })), hide_private: hide.checked, exclude_statistics: stats.checked } });
        if (!alive(version,session)) return; window.MovieFriendsSettings.clear("display-policy",session);render(result); await bridge.onSaved?.(); bridge.toast("展示范围已保存，旧公开内容也按新规则过滤。私人原始记录仍保留。");
      } catch(e) { if (!e.staleSession && alive(version,session)) bridge.toast(bridge.safeError(e),true); } finally {save.disabled=false;}
    };
    panel.append(form);
    if (data.scan) {
      const scan = data.scan;
      if(scan.status==="reading")panel.append(el("p","watch-status",`正在核对来源 ${scan.processed} 条…`));
      else if(scan.status==="complete")head.append(help("来源核对",`上次核对完成，共处理 ${scan.processed} 条。`));
      else panel.append(el("p","watch-status","上次来源核对已中断，请重新刷新。"));
      for (const error of scan.errors || []) panel.append(el("p", "field-help", error));
      if (scan.status === "reading") { clearTimeout(timer); const version = epoch, session = bridge.sessionVersion(); timer = setTimeout(() => { if (alive(version,session)) load(false).catch(e => bridge.toast(bridge.safeError(e),true)); }, 1500); }
    }
    root.append(panel);
  }
  async function load(syncNative = true) {
    if (!bridge.user() || busy) return; busy = true; const version = epoch, session = bridge.sessionVersion();
    try { if (syncNative) await nativeCatalog().catch(() => {}); const data = await bridge.api("/display-policy"); if (alive(version,session)) render(data); } finally { busy = false; }
  }
  async function refresh() { const version = epoch, session = bridge.sessionVersion(); await nativeCatalog().catch(e => {if (!e.staleSession) bridge.toast(bridge.safeError(e),true);}); if (!alive(version,session)) return; await bridge.api("/display-policy/refresh", { method: "POST" }); await load(false); }
  function reset() { epoch++; clearTimeout(timer); busy=false; document.getElementById("display-policy-settings")?.replaceChildren(); }
  return { configure(value) {bridge=value;}, load, reset };
})();
