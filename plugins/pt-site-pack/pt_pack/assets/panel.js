"use strict";
(() => {
  const base = "/api/v1/plugins/pt-site-pack";
  const $ = (id) => document.getElementById(id);
  let snapshot = null, loading = false, noticeText = "", noticeKind = "";
  const retries = new Set();
  const pendingResults = new Map();
  const expanded = new Set();
  const reports = new Map(), probeKeywords = new Map();
  let probing = null;
  const failedIcons = new Set();
  const uaDrafts = new Map();
  let workspace=null, catalog=null, mutationBusy=false;
  async function syncTheme() {
    try {
      const response = await fetch("/api/v1/ui/preferences", {credentials: "same-origin", cache: "no-store"});
      if (!response.ok) return;
      const body = await response.json(), theme = (body.data || body).theme;
      if (["silver", "netflix"].includes(theme)) document.documentElement.dataset.theme = theme;
    } catch { /* Use the host's silver settings style if preferences are unavailable. */ }
  }
  const formatter = new Intl.DateTimeFormat("zh-CN", {timeZone: "Asia/Shanghai", dateStyle: "short", timeStyle: "medium"});
  const date = (s) => { if (!s) return "从未"; const d = new Date(s); return Number.isNaN(d.valueOf()) ? "未知" : formatter.format(d); };
  function element(tag, text, cls) { const e = document.createElement(tag); if (text != null) e.textContent = text; if (cls) e.className = cls; return e; }
  function notice(text, kind = "") { noticeText = text; noticeKind = kind; $("notice").textContent = text; $("notice").className = "notice " + kind; }
  function link(text, href, cls) { const e = element("a", text, cls); e.href = href; if (href.startsWith("/settings/")) e.target = "_top"; return e; }
  function website(value) {
    try { const url = new URL(value); return ["https:", "http:"].includes(url.protocol) && !url.username && !url.password && url.pathname === "/" && !url.search && !url.hash ? url.origin : null; }
    catch { return null; }
  }
  function externalLink(text, url, cls, label) {
    const a = link(text, url, cls); a.target = "_blank"; a.rel = "noopener noreferrer"; a.referrerPolicy = "no-referrer";
    if (label) a.setAttribute("aria-label", label); return a;
  }
  function avatar(site, url) {
    const logo = url ? externalLink(null, url, "site-logo", "访问 " + site.name + "（新标签页）") : element("div", null, "site-logo");
    const fallback = element("span", site.name.slice(0,1).toUpperCase(), "logo-fallback"); logo.append(fallback);
    const icon = site.icon_url;
    // Do not relax CSP or send the user's site list to an external favicon service.
    if (icon === base + "/icons/" + encodeURIComponent(site.site_id) && !failedIcons.has(site.site_id)) {
      const img = element("img"); img.alt = ""; img.width = 28; img.height = 28; img.decoding = "async"; img.referrerPolicy = "no-referrer";
      img.addEventListener("load", () => { fallback.hidden = true; });
      img.addEventListener("error", () => { failedIcons.add(site.site_id); img.remove(); fallback.hidden = false; });
      img.src = icon; logo.append(img);
    }
    return logo;
  }
  async function api(path, options = {}) {
    const response = await fetch(base + path, {credentials: "same-origin", cache: "no-store", ...options});
    let data; try { data = await response.json(); } catch { throw new Error("服务器响应暂不可用，请稍后刷新"); }
    if (!response.ok) {
      if (response.status === 401 || response.status === 403) throw new Error("登录已失效或没有权限，请回到 MovieClaw 重新登录管理员账号");
      const message = data.detail || data.message || data.error?.message;
      throw new Error(typeof message === "string" ? message : "请求失败（HTTP " + response.status + "）");
    }
    return data.data || data;
  }
  function fact(list, key, value) { const wrap = element("div"); wrap.append(element("dt", key), element("dd", value)); list.append(wrap); }
  function card(site) {
    const root = element("article", null, "site " + site.state); root.dataset.siteId = site.site_id;
    const head = element("div", null, "site-head"), title = element("div", null, "site-heading"), titleLine = element("div", null, "title-line");
    const url = website(site.website_url || site.base_url), name = element("h2");
    name.append(url ? externalLink(site.name, url, "site-name", "打开 " + site.name + "（新标签页）") : element("span", site.name));
    head.append(avatar(site, url));
    titleLine.append(name, element("span", site.verification_label, "badge " + site.verification));
    if (site.configured && !site.enabled) titleLine.append(element("span", "已暂停", "badge"));
    const subtitle = element("div", null, "identifier");
    subtitle.append(element("span", site.site_id), element("span", site.source_label, "badge"), element("span", site.connection_label));
    if (!site.adapter_available) subtitle.append(element("span", site.adapter_label, "badge pending"));
    title.append(titleLine, subtitle); if (site.last_error) title.append(element("p", site.last_error, "reason")); head.append(title);
    const actions = element("div", null, "row-actions");
    if (url) actions.append(externalLink("访问站点 ↗", url, "button secondary", "访问 " + site.name + " 网站（新标签页）"));
    const busy = retries.has(site.site_id) || site.verification === "verifying";
    const button = element("button", busy ? "验证中…" : site.verification === "failed" ? "重试验证" : "重新验证", "button");
    button.type = "button"; button.disabled = !site.retry_allowed || busy; button.title = site.retry_reason || "仅重新验证，不下载媒体";
    button.addEventListener("click", () => retry(site));
    if (site.configured) actions.append(button); else actions.append(link("配置接入", "/settings/sites", "button secondary"));
    const detail = element("div", null, "site-detail"); detail.id = "detail-" + site.site_id; detail.hidden = !expanded.has(site.site_id);
    const toggle = element("button", detail.hidden ? "⌄" : "⌃", "detail-toggle"); toggle.type = "button";
    toggle.setAttribute("aria-label", site.name + " 展开或收起详情"); toggle.setAttribute("aria-controls", detail.id); toggle.setAttribute("aria-expanded", String(!detail.hidden));
    toggle.addEventListener("click", () => { detail.hidden = !detail.hidden; if (detail.hidden) expanded.delete(site.site_id); else expanded.add(site.site_id); toggle.textContent = detail.hidden ? "⌄" : "⌃"; toggle.setAttribute("aria-expanded", String(!detail.hidden)); });
    actions.append(toggle); head.append(actions); root.append(head);
    const index = element("div", null, "detail-section"), indexFacts = element("dl", null, "facts");
    fact(indexFacts, "已缓存种子", site.torrent_count == null ? "尚无统计" : site.torrent_count.toLocaleString());
    fact(indexFacts, "上次同步", date(site.last_sync_at)); fact(indexFacts, "下次同步", site.next_sync_at ? date(site.next_sync_at) : site.torrent_count != null ? "即将开始" : "尚无计划信息");
    fact(indexFacts, "同步间隔", site.sync_interval_seconds == null ? "尚无统计" : site.sync_interval_seconds + " 秒");
    const indexContent = element("div", null, "detail-content"); indexContent.append(indexFacts); if (site.sync_error) indexContent.append(element("p", site.sync_error, "reason"));
    index.append(element("h3", "索引"), indexContent); detail.append(index);
    const auth = element("div", null, "detail-section"), authFacts = element("dl", null, "facts");
    fact(authFacts, "授权方式", ({cookie:"Cookie",apikey:"API Key",credential:"账号密码"})[site.auth_type] || "未配置"); fact(authFacts, "最近验证尝试", date(site.last_checked_at)); fact(authFacts, "最近验证成功", date(site.last_verified_at)); fact(authFacts, "适配状态", site.adapter_label);
    auth.append(element("h3", "授权"), authFacts); detail.append(auth);
    const identity = element("div",null,"detail-section"), identityContent = element("div",null,"identity-editor");
    const uaLabel=element("label","本站 User-Agent（留空继承默认设置）"), uaInput=element("input",null,"ua-input");
    uaInput.type="text"; uaInput.maxLength=512; uaInput.autocomplete="off"; uaInput.setAttribute("aria-label",site.name+" User-Agent");
    uaInput.value=uaDrafts.get(site.site_id) ?? site.user_agent ?? "";
    uaInput.addEventListener("input",()=>uaDrafts.set(site.site_id,uaInput.value)); uaLabel.append(uaInput);
    const uaSave=element("button","保存本站设置","button secondary"); uaSave.type="button";
    uaSave.addEventListener("click",()=>saveIdentity(site.site_id,uaInput.value));
    identityContent.append(uaLabel,uaSave); identity.append(element("h3","浏览器"),identityContent); detail.append(identity);
    if(site.source==="custom") {
      const section=element("div",null,"detail-section"), actions=element("div",null,"actions");
      for(const [label,callback,disabled] of [["编辑适配",()=>editCustom(site.site_id),false],
        [site.custom_enabled ? "停用适配":"启用适配",()=>customAction(site.site_id,site.custom_enabled ? "disable":"enable"),false],
        ["恢复上一版",()=>customAction(site.site_id,"rollback"),!site.has_history],
        ["归档适配",()=>customAction(site.site_id,"archive"),false]]) {
        const b=element("button",label,"button secondary"); b.type="button"; b.disabled=disabled; b.addEventListener("click",callback); actions.append(b);
      }
      const exported=link("导出 YAML",base+"/workspace/export/"+encodeURIComponent(site.site_id),"button secondary"); exported.download=site.site_id+".yaml";actions.append(exported);
      section.append(element("h3","适配"),actions);detail.append(section);
    }
    if (["plugin","custom"].includes(site.source)) {
      const section = element("div", null, "detail-section"), content = element("div", null, "detail-content");
      const controls = element("div", null, "actions"), keyword = element("input", null, "probe-keyword");
      keyword.type = "text"; keyword.maxLength = 100; keyword.placeholder = "输入本站有资源的片名";
      keyword.setAttribute("aria-label", site.name + " 诊断关键词"); keyword.value = probeKeywords.get(site.site_id) || "";
      keyword.addEventListener("input", () => probeKeywords.set(site.site_id, keyword.value));
      const button = element("button", probing === site.site_id ? "测试中…" : "测试搜索 / 详情", "button secondary");
      button.type = "button"; button.disabled = !site.usable || probing !== null;
      button.addEventListener("click", () => probe(site, keyword.value)); controls.append(keyword, button); content.append(controls);
      const report = reports.get(site.site_id);
      if (report) {
        const facts = element("dl", null, "facts");
        for (const [key, name] of [["search", "搜索"], ["detail", "详情"]]) {
          const check = report[key] || {}; fact(facts, name, check.ok ? "通过" : check.error || "未完成");
        }
        fact(facts, "测试时间", date(report.checked_at)); content.append(facts);
        if (report.sample) content.append(element("p", "测试样本：" + report.sample.title, "site-note"));
        if (report.torrent?.ok) content.append(element("p", "种子 bencode 校验通过 · " + report.torrent.file_count + " 个文件 · " + report.torrent.bytes.toLocaleString() + " 字节种子元数据", "site-note"));
      }
      content.append(element("p", "只读取搜索与详情页，不获取种子文件，不添加下载任务。", "site-note"));
      section.append(element("h3", "功能测试"), content); detail.append(section);
    }
    const info = element("div", null, "detail-section"), infoContent = element("div", null, "detail-content"), infoActions = element("div", null, "actions");
    infoActions.append(link(site.configured ? "查看 / 更新账号配置 ↗" : "配置接入 ↗", "/settings/sites", "button secondary")); infoContent.append(infoActions);
    if (site.retry_reason) infoContent.append(element("p", site.retry_reason, "site-note"));
    if (site.configured && !site.enabled) infoContent.append(element("p", "暂停状态保留；重新验证不会自动启用站点。", "site-note"));
    infoContent.append(element("p", "验证结果来自主程序，搜索、详情、下载仍需分别验收。页面不读取或展示 Cookie。", "site-note"));
    info.append(element("h3", "说明"), infoContent); detail.append(info); root.append(detail); return root;
  }
  function render() {
    if (!snapshot) return;
    const q = $("search").value.trim().toLocaleLowerCase(), source = $("source").value, state = $("state").value;
    const matches = snapshot.sites.filter(s => (!q || (s.name + " " + s.site_id).toLocaleLowerCase().includes(q)) &&
      (source === "all" || source === s.source) && (state === "all" ||
       state === "configured" && s.configured || state === "unconfigured" && !s.configured ||
       state === "active" && s.usable || !["configured", "unconfigured", "active"].includes(state) && state === s.state));
    const fragment = document.createDocumentFragment();
    for (const group of [{name: "已接入站点", entries: matches.filter(s => s.configured)}, {name: "待配置账号", entries: matches.filter(s => !s.configured)}]) {
      if (!group.entries.length) continue;
      const section = element("section", null, "site-group"), heading = element("h2", null, "group-title"), list = element("div", null, "site-list");
      heading.append(element("span", group.name), element("span", String(group.entries.length), "count"));
      if (!list.hidden) group.entries.forEach(s => list.append(card(s)));
      section.append(heading, list); fragment.append(section);
    }
    $("sites").replaceChildren(fragment);
    $("empty").hidden = matches.length !== 0; $("result-count").textContent = "匹配 " + matches.length + " / " + snapshot.sites.length + " 站";
    for (const name of ["supported", "configured", "usable", "failed"]) $(name).textContent = snapshot.counts[name === "supported" ? "total" : name];
    $("sources").textContent = "官方 " + snapshot.counts.official + " · 插件 " + snapshot.counts.plugin + " · 本站自定义 " + snapshot.counts.custom;
    $("updated").textContent = "更新于 " + date(snapshot.updated_at);
  }
  async function load(manual = false) {
    if (loading || (!manual && document.activeElement?.matches(".probe-keyword,.ua-input"))) return;
    loading = true; $("refresh").disabled = true;
    try {
      const data = await Promise.all([api("/sites"), api("/diagnostics"), readWorkspace()]);
      snapshot = data[0]; reports.clear(); for (const report of data[1].reports) reports.set(report.site_id, report); render();
      if (snapshot.warning) notice(snapshot.warning);
      else if (manual || !noticeText || noticeKind === "error" || noticeText === "正在读取服务器状态…") notice("已读取最新状态。页面刷新不触发 PT 站验证。");
      for (const [id, previousCheck] of pendingResults) {
        const site = snapshot.sites.find(s => s.site_id === id);
        if (!site || site.verification === "verifying" || !site.last_checked_at || site.last_checked_at === previousCheck) continue;
        if (site.verification === "active") { notice(site.name + "：本次验证通过。", "success"); pendingResults.delete(id); }
        else if (site.verification === "failed") { notice(site.name + "：验证失败。" + site.last_error, "error"); pendingResults.delete(id); }
      }
    } catch (error) { notice(error.message + (snapshot ? "；当前显示的是上次成功读取的数据。" : ""), "error"); }
    finally { loading = false; $("refresh").disabled = false; }
  }
  async function retry(site) {
    if (retries.has(site.site_id) || !site.retry_allowed) return;
    retries.add(site.site_id); render(); notice("正在为 " + site.name + " 发起原生验证…");
    try {
      const result = await api("/sites/" + encodeURIComponent(site.site_id) + "/retry", {method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"});
      pendingResults.set(site.site_id, site.last_checked_at);
      notice(site.name + "：" + result.message, "success");
    } catch (error) { notice(site.name + "：" + error.message, "error"); }
    finally { retries.delete(site.site_id); await load(); render(); }
  }
  async function probe(site, value) {
    const keyword = value.trim();
    if (!keyword) { notice("请先输入本站有资源的片名关键词。", "error"); return; }
    if (probing !== null) return;
    probing = site.site_id; probeKeywords.set(site.site_id, keyword); expanded.add(site.site_id); render();
    notice("正在测试 " + site.name + " 的搜索与详情…");
    try {
      const report = await api("/sites/" + encodeURIComponent(site.site_id) + "/probe?keyword=" + encodeURIComponent(keyword), {method: "POST"});
      reports.set(site.site_id, report);
      const ok = ["search", "detail"].every(k => report[k]?.ok);
      notice(site.name + (ok ? "：搜索和详情检查通过，未提交下载任务。" : "：有项目未通过，请查看功能测试结果。"), ok ? "success" : "error");
    } catch (error) { notice(site.name + "：" + error.message, "error"); }
    finally { probing = null; render(); }
  }
  let assistantLoading = false, currentJobId = "", currentJob = null, addMode = "import";
  async function readWorkspace() {
    workspace = await api("/workspace");
    if (document.activeElement !== $("default-user-agent")) $("default-user-agent").value = workspace.default_user_agent;
    return workspace;
  }
  async function write(path, body, reload = true) {
    if (mutationBusy) throw new Error("正在保存，请稍候");
    mutationBusy = true;
    try {
      await readWorkspace();
      const result = await api(path, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({...body,revision:workspace.revision})});
      await readWorkspace();
      if (reload) { await load(); if (!$("assistant-pane").hidden) await loadAdd(); }
      return result;
    } finally { mutationBusy = false; }
  }
  async function saveIdentity(siteId, value) {
    try {
      await write("/workspace/identity", {site_id:siteId || null,user_agent:value});
      if (siteId) uaDrafts.delete(siteId);
      $("identity-message").textContent = "已保存，后续请求使用新设置。";
      notice("浏览器身份已保存。","success"); render();
    } catch(error) { notice(error.message,"error"); }
  }
  function download(text, filename) {
    const url = URL.createObjectURL(new Blob([text],{type:"application/yaml"}));
    const a = element("a"); a.href=url; a.download=filename; a.click(); setTimeout(()=>URL.revokeObjectURL(url),1000);
  }
  async function customAction(siteId, action) {
    try { const result = await write("/workspace/custom/"+encodeURIComponent(siteId)+"/"+action,{}); notice(result.message,"success"); }
    catch(error) { notice(error.message,"error"); }
  }
  function editCustom(siteId) {
    const record = workspace?.custom[siteId]; if (!record) return;
    $("custom-yaml").value = JSON.stringify(record.config,null,2);
    $("editor-message").textContent = "正在编辑："+record.config.display_name+" · "+record.config.base_url+"。保存后可回退上一版。";
    setMode("import"); selectPane(true); $("custom-yaml").focus();
  }
  function renderCatalog() {
    if (!catalog) return;
    const q = $("catalog-search").value.trim().toLowerCase();
    const fragment = document.createDocumentFragment();
    const rows = catalog.sites.filter(s=>s.adapter_available && (!q || (s.name+" "+s.site_id+" "+s.base_url).toLowerCase().includes(q)));
    for(const site of rows) {
      const row = element("div",null,"catalog-item"), text=element("div");
      text.append(element("strong",site.name),element("p",site.source_label+" · "+(site.website_url || site.base_url || site.site_id)));
      const action=element("button",site.added ? "已添加" : "添加到本站","button secondary");
      action.type="button"; action.disabled=site.added || mutationBusy;
      action.addEventListener("click",async()=>{
        action.disabled=true;
        try { await write("/workspace/select",{site_id:site.site_id}); selectPane(false); notice(site.name+" 已添加，请配置自己的账号。","success"); }
        catch(error) { notice(error.message,"error"); action.disabled=false; }
      });
      row.append(text,action); fragment.append(row);
    }
    if(!rows.length) fragment.append(element("p","目录未找到匹配项，可在下方导入或生成适配。","site-note"));
    $("catalog-results").replaceChildren(fragment);
  }
  async function loadAdd() {
    try {
      await readWorkspace(); catalog=await api("/catalog"); renderCatalog();
      const archive=document.createDocumentFragment();
      for(const [id,record] of Object.entries(workspace.custom).filter(([,r])=>r.archived)) {
        const row=element("div",null,"catalog-item"), button=element("button","恢复到本站目录","button secondary");
        button.type="button"; button.addEventListener("click",()=>customAction(id,"enable"));
        row.append(element("strong",record.config.display_name),button); archive.append(row);
      }
      if(!archive.childNodes.length) archive.append(element("p","暂无归档。","site-note"));
      $("archived-sites").replaceChildren(archive);
      const options=[new Option("选择生成记录","")];
      for(const job of workspace.jobs) options.push(new Option((job.site_url || job.site_id)+" · "+job.state,job.id));
      $("assistant-jobs").replaceChildren(...options); $("assistant-jobs").value=currentJobId;
    } catch(error) { notice(error.message,"error"); }
  }
  async function loadAssistant() {
    if (assistantLoading || !currentJobId) return;
    assistantLoading=true;
    try {
      const id=currentJobId, job=await api("/assistant/jobs/"+encodeURIComponent(id));
      if(id!==currentJobId) return;
      currentJob=job;
      $("assistant-status").textContent=(job.site_url || "")+"："+job.message;
      $("load-job-draft").disabled=!job.has_draft;
      $("generate-draft").disabled=["queued","checking","generating"].includes(job.state);
    } catch(error) { $("assistant-status").textContent=error.message; }
    finally { assistantLoading=false; }
  }
  function setMode(mode) {
    addMode=mode;
    for(const name of ["import","template","ai"]) $("mode-"+name).setAttribute("aria-pressed",String(mode===name));
    $("site-basics").hidden=mode==="import";
    $("template-controls").hidden=mode!=="template";
    $("ai-controls").hidden=mode!=="ai";
    $("import-file-label").hidden=mode!=="import";
    $("import-guidance").hidden=mode!=="import";
  }
  function selectPage(name, changeAddress=true, statistics=false) {
    if (!statistics) window.PTStatistics?.leave();
    for (const [page,pane,tab] of [["sites","sites-pane","tab-sites"],["add","assistant-pane","tab-assistant"],["overview","overview-pane","tab-overview"]]) {
      $(pane).hidden=name!==page; $(tab).setAttribute("aria-pressed",String(name===page));
    }
    if(changeAddress) history.pushState(null,"","#"+name);
    if(name==="add") { $("site-add-flow").hidden=false; $("statistics-assistant").hidden=true; loadAdd(); }
    if(name==="overview") window.PTOverview?.open();
  }
  function selectPane(adding, changeAddress=true) { selectPage(adding ? "add":"sites",changeAddress); }
  function pageFromHash() { return location.hash==="#overview" ? "overview" : ["#assistant","#add"].includes(location.hash) ? "add":"sites"; }
  window.PTPanel={showStatistics:()=>{selectPage("add",true,true);$("site-add-flow").hidden=true;$("statistics-assistant").hidden=false;},overview:()=>selectPage("overview")};
  $("tab-sites").addEventListener("click",()=>selectPage("sites"));
  $("tab-assistant").addEventListener("click",()=>selectPage("add"));
  $("tab-overview").addEventListener("click",()=>selectPage("overview"));
  $("add-site").addEventListener("click",()=>selectPage("add"));
  window.addEventListener("popstate",()=>selectPage(pageFromHash(),false));
  $("save-default-ua").addEventListener("click",()=>saveIdentity("",$("default-user-agent").value));
  $("catalog-search").addEventListener("input",renderCatalog);
  for(const name of ["import","template","ai"]) $("mode-"+name).addEventListener("click",()=>setMode(name));
  $("import-file").addEventListener("change",async(event)=>{
    const file=event.currentTarget.files[0]; if(!file) return;
    if(file.size>100000) { notice("适配文件不能超过100 KB","error"); return; }
    $("custom-yaml").value=await file.text(); $("editor-message").textContent="已载入文件："+file.name+"，请检查后加入本站目录。";
  });
  $("make-template").addEventListener("click",async()=>{
    try {
      const result=await api("/workspace/template",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({site_url:$("new-site-url").value,site_id:$("new-site-id").value,display_name:$("new-site-name").value})});
      $("custom-yaml").value=result.yaml; $("editor-message").textContent="标准 NexusPHP 模板，请按本站页面检查分类与选择器。";
    } catch(error) { notice(error.message,"error"); }
  });
  $("generate-draft").addEventListener("click",async()=>{
    $("generate-draft").disabled=true; currentJob=null; currentJobId=""; $("load-job-draft").disabled=true;
    $("custom-yaml").value=""; $("editor-message").textContent="本次正在生成，上一份草稿不会作为本次结果使用。";
    try {
      const result=await api("/assistant/generate",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({site_url:$("new-site-url").value,list_html:$("list-html").value,detail_html:$("detail-html").value,allow_model:$("allow-model").checked})});
      currentJobId=result.id; await loadAdd(); await loadAssistant();
    } catch(error) { $("assistant-status").textContent=error.message; $("generate-draft").disabled=false; }
  });
  $("assistant-jobs").addEventListener("change",async()=>{currentJobId=$("assistant-jobs").value;currentJob=null;$("load-job-draft").disabled=true;await loadAssistant();});
  $("load-job-draft").addEventListener("click",()=>{
    if(!currentJob?.draft || currentJob.id!==currentJobId) return;
    $("custom-yaml").value=JSON.stringify(currentJob.draft,null,2);
    $("editor-message").textContent="草稿来源："+currentJob.site_url+"。请审核后加入本站目录。";
  });
  $("import-config").addEventListener("click",async()=>{
    try {
      const result=await write("/workspace/import",{yaml:$("custom-yaml").value});
      $("editor-message").textContent=result.message;
      selectPane(false); expanded.add(result.site_id); $("search").value=result.site_id; render(); notice(result.message,"success");
    } catch(error) { $("editor-message").textContent=error.message; notice(error.message,"error"); }
  });
  $("download-editor").addEventListener("click",async()=>{
    try { const result=await api("/workspace/validate",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({yaml:$("custom-yaml").value})}); download(JSON.stringify(result.config,null,2),result.config.site_id+".yaml"); }
    catch(error) { notice(error.message,"error"); }
  });
  $("refresh").addEventListener("click",async()=>{if(!$("overview-pane").hidden) await window.PTOverview?.refresh();else if($("assistant-pane").hidden) await load(true);else {await loadAdd();await loadAssistant();}});
  for(const id of ["search","source","state"]) $(id).addEventListener(id==="search" ? "input":"change",render);
  function refreshVisible() {
    if(document.hidden) return;
    if(!$("overview-pane").hidden) window.PTOverview?.poll();
    else if($("assistant-pane").hidden) load();
    else if(addMode==="ai") loadAssistant();
  }
  document.addEventListener("visibilitychange",refreshVisible);
  selectPage(pageFromHash(),false);
  syncTheme(); load(); window.setInterval(refreshVisible,5000);
})();
