"use strict";
// 私人档案；凭据不写浏览器存储，主程序标记只使用当前账号会话。
window.MovieFriendsProfile = (() => {
  let bridge, generation = 0, loadedSession = -1, avatarSession = -1, data, connections = [], busy = false, syncJob = null, timer, checkingSeries = false, settingsVersion = 0;
  const name = (key) => ({ movieclaw: "MovieClaw", emby: "Emby", plex: "Plex" })[key] || key;
  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; };
  const help = (label,text) => window.MovieFriendsHelp.create(label,text);
  const active = (v, s) => generation === v && bridge.user() && bridge.sessionVersion() === s;
  function button(text, action, style = "button-outline") {
    const n = el("button", `button ${style}`, text); n.type = "button";
    n.onclick = async () => { const s = bridge.sessionVersion(); n.disabled = true; try { await action(n); } catch (e) { if (!e.staleSession && s === bridge.sessionVersion()) bridge.toast(bridge.safeError(e), true); } finally { n.disabled = false; } }; return n;
  }
  function input(type, placeholder = "") { const n = el("input"); n.type = type; n.placeholder = placeholder; return n; }
  function field(text, n) { const label = el("label", "watch-field", text); label.append(n); return label; }
  function check(text) { const box = input("checkbox"), label = el("label", "watch-check"); label.append(box, document.createTextNode(text)); return { box, label }; }
  function dialog(title, note) {
    const modal = el("dialog", "watch-dialog"), body = el("div", "watch-dialog-body"), head = el("div", "section-heading");
    head.append(el("h2", "", title), button("关闭", () => modal.close(), "button-quiet")); body.append(head); if (note) body.append(el("p", "muted", note)); modal.append(body);
    document.body.append(modal); modal.onclose = () => modal.remove(); window.MovieFriendsUI.open(modal); return { modal, body };
  }
  async function sameAccount(owner) {
    const result = await bridge.api("/me");
    if (!bridge.user() || result.user.id !== owner || bridge.user().id !== owner) { const error = new Error("登录账号已改变，请刷新页面后重新预览。"); error.accountChanged = true; throw error; }
  }
  async function syncAvatar() {
    const s = bridge.sessionVersion(), v = generation; if (avatarSession === s) return;
    const own = await bridge.hostApi("/auth/me"); if (!active(v, s)) return; let snapshot = null;
    if (own?.avatar_url) {
      const url = new URL(own.avatar_url, location.origin);
      if (url.origin !== location.origin || url.pathname !== "/api/v1/auth/avatar") throw new Error("主程序头像地址不兼容，请在账号设置中重新保存头像。");
      snapshot = await new Promise((resolve, reject) => {
        const image = new Image(); const timeout = setTimeout(() => { image.src = ""; reject(new Error("主程序头像读取超时。")); }, 10000);
        image.onload = () => { clearTimeout(timeout); try { const canvas = document.createElement("canvas"); canvas.width = canvas.height = 160; const ctx = canvas.getContext("2d"); ctx.fillStyle = "#16191e"; ctx.fillRect(0, 0, 160, 160); const size = Math.min(image.naturalWidth, image.naturalHeight); ctx.drawImage(image, (image.naturalWidth-size)/2, (image.naturalHeight-size)/2, size, size, 0, 0, 160, 160); resolve(canvas.toDataURL("image/jpeg", .85)); } catch { reject(new Error("无法同步主程序头像。")); } };
        image.onerror = () => { clearTimeout(timeout); reject(new Error("主程序头像暂时无法读取。")); }; image.src = url.href;
      });
    }
    if (!active(v,s)) return; await sameAccount(bridge.user().id);
    const result = await bridge.api("/watch/avatar", { method: "PUT", body: { data: snapshot } });
    if (!active(v,s)) return; avatarSession = s; bridge.onAvatarUpdated(result.avatar_data);
  }
  function hero() {
    const root = document.getElementById("profile-root"); root.replaceChildren();
    const section = el("section", "personal-hero"), identity = el("div", "personal-identity"), copy = el("div");
    identity.append(bridge.avatar({ ...bridge.user(), nickname: data.profile.nickname }));
    copy.append(el("p", "eyebrow", "MY LIFE IN FILMS"), el("h1", "", data.profile.nickname));
    const joined = data.profile.plugin_joined_at ? new Date(data.profile.plugin_joined_at).toLocaleDateString("zh-CN", { year:"numeric", month:"long", day:"numeric" }) + " 进入映迹" : "欢迎进入映迹";
    copy.append(el("p", "muted", joined)); identity.append(copy);
    const metrics = el("div", "personal-stats"); metrics.setAttribute("aria-label", "已观看统计");
    for (const [value, label] of [[data.stats.movies, "电影"], [data.stats.shows, "电视剧"], [data.stats.episodes, "集"]]) {
      const row = el("div", "personal-stat"), exact = value.toLocaleString("zh-CN");
      const short = value >= 1000 ? `${(value / 1000).toFixed(1).replace(/\.0$/, "")}K` : exact;
      const number = el("strong", "", short); number.title = exact; number.setAttribute("aria-label", exact);
      row.append(number, el("span", "", label)); metrics.append(row);
    }
    const scope=data.display_scope||{history_hidden:true,statistics_excluded:true};
    const scopeNote=data.display_restricted ? `${scope.history_hidden?"观看足迹已隐藏所选排除库及来源未确认的内容。":"观看足迹保留所有私人记录。"}${scope.statistics_excluded?"统计不包含这些内容。":"统计仍包含这些记录。"}原始记录保留，可在设置中分别调整。` : "未设置媒体库排除。";
    metrics.append(help("观看统计",`已完成观看的电影与剧集，电影和单集跨平台去重。${scopeNote}`));
    section.append(identity, metrics); root.append(section);
    const heading = el("div", "section-heading personal-history-heading"),title=el("h2","","我的观看足迹"),actions=el("div","watch-row-actions");
    title.append(help("观看足迹",`按观看时间合并排列，缺少日期的记录放在最后。自动读取会复用最近五分钟的成功结果，手动同步会立即重新读取。电视剧按作品合并，未看完显示最高已看季集，展开可查单集；跳集不补算。平台标记表示记录来源，不一定是实际播放平台。${scopeNote}${data.native_sync?.finished_at?`主程序上次同步：${bridge.dateText(data.native_sync.finished_at)}。`:""}`));
    actions.append(button(syncJob?"正在读取…":"同步主程序记录",()=>refresh(),"button-quiet"),button("核对全集完成情况",()=>checkSeries(true),"button-quiet"));heading.append(title,actions);
    if(data.stats.pending)heading.append(el("span","field-help",`${data.stats.pending} 条待匹配`));
    const seriesStatus=el("p","watch-status");seriesStatus.id="series-completion-status";seriesStatus.hidden=true;
    if(syncJob)root.append(el("p","watch-status","正在读取主程序观影记录…"));
    else if(data.native_sync?.error)root.append(el("p","watch-status",data.native_sync.error));
    const history = el("div", "personal-history-grid"), more = el("div", "personal-more"); history.id="personal-history-grid"; more.id="personal-history-more"; root.append(heading, seriesStatus, history, more);
  }
  function renderSources() {
    const root=document.getElementById("watch-source-settings");if(!root)return;
    const sources = el("section", "personal-sources"), head = el("div", "section-heading"); head.append(el("h2", "", "把观影记忆带回来"), el("span", "small-label", "EMBY / PLEX")); head.querySelector("h2").append(help("观影数据导入","连接本人账号，预览确认后导入私人档案，跨平台同一电影和单集去重。地址和令牌加密保存，不展示给影友；局域网或 HTTP 连接需管理员在插件设置中开启。"));head.append(button("任务中心",()=>window.MovieFriendsTasks.open(),"button-outline"));sources.append(head);
    const grid = el("div", "watch-source-grid");
    for (const key of ["emby","plex"]) { const card = el("div", `watch-source watch-source-${key}`), text = el("div"); text.append(el("h3", "", name(key)), el("p", "field-help", key === "emby" ? "读取本人已看电影与剧集" : "读取本人令牌对应的已看状态")); const mark=el("span","watch-platform-mark");mark.append(window.MovieFriendsIcons.create(key==="emby"?"clapperboard":"film"));card.append(mark,text,button("连接账号",()=>connect(key))); grid.append(card); } sources.append(grid);
    for (const source of connections) {
      const row = el("div", "watch-connection"), text = el("div"), actions = el("div", "watch-row-actions");
      text.append(el("strong", "", `${name(source.platform)} · ${source.account_name || "等待验证账号"}`), el("small", "field-help", source.synced_at ? `上次导入 ${bridge.dateText(source.synced_at)}` : "尚未确认导入"));
      if (source.platform === "emby" && source.needs_account) actions.append(button("指定 Emby 账号", () => bindEmbyAccount(source), "button-quiet"));
      if (source.latest_job) actions.append(button("查看任务", () => showJob(source.latest_job), "button-quiet"));
      actions.append(button("重新预览", async () => { const job = await bridge.api(`/watch/connections/${source.id}/preview`, { method:"POST" }); await showJob(job.id); }, "button-quiet"), button("断开连接", () => disconnect(source), "button-quiet")); row.append(text, actions); sources.append(row);
    }
    const alignment=el("div","watch-alignment-entry");const alignmentTitle=el("h3","","三平台已看对齐");alignmentTitle.append(help("已看对齐","先预览，确认后补充本人平台账号缺少的已看标记。沿用媒体库排除范围，不同步播放进度。"));alignment.append(alignmentTitle,button("预览三平台已看对齐",()=>window.MovieFriendsAlignment.open(),"button-outline"));sources.append(alignment);const auto=el("section","watch-auto-sync");const syncCards=el("div","watch-sync-cards");syncCards.append(auto);sources.append(syncCards);root.replaceChildren(sources);window.MovieFriendsAutoSync?.mount(auto);const resume=el("section","watch-resume-sync");syncCards.append(resume);window.MovieFriendsResume?.mount(resume);
  }
  async function loadSettings() {
    const v=generation,s=bridge.sessionVersion(),request=++settingsVersion;
    const result=await bridge.api("/watch/connections");
    if(!active(v,s)||request!==settingsVersion)return;
    connections=result.connections;renderSources();
  }
  function recordCard(record, jobId = null, historyOnly = false) {
    const card = el("article", "personal-film"); card.append(bridge.mediaPoster(record), bridge.mediaTitle(record));
    if (record.series_complete) card.append(el("span", "series-complete-badge", record.completion_basis === "platform" ? "片库内已看完" : record.completion_basis === "airing" ? "目录内已看完" : "全剧已看完"));
    if(record.series_grouped&&!record.series_complete){const progress=el("span","series-progress-badge",`已看到第 ${record.progress_season} 季 · 第 ${record.progress_episode} 集`);progress.title="按已完成观看记录的最高季集号展示；跳集和重看可展开单集记录核对。";card.append(progress);}
    const meta = record.series_complete ? `${record.season_count} 季 · 共 ${record.episode_count} 集` : record.series_grouped ? `累计已看 ${record.episode_count} 集` : bridge.mediaMeta(record); if (meta) card.append(el("small", "muted", meta));
    card.append(el("small", "field-help", record.watched_at ? bridge.dateText(record.watched_at,false) : "来源未提供观看日期"));
    if (record.sources) card.append(el("small", "watch-record-source", record.sources.map(name).join(" · ")));
    if (!record.matched) {
      const pending=el("span","watch-pending","待匹配 · 不计统计");pending.append(help("作品匹配",record.media.tmdb_id?"缺少有效的季集信息，暂无法跨平台去重。":"来源缺少 TMDB 作品编号，暂无法跨平台去重或查询海报，可校正匹配。"));card.append(pending);
      if(!jobId&&!historyOnly)card.append(button("校正匹配",()=>matchRecord(record),"button-quiet"));
    }
    if (record.series_grouped || record.series_complete) card.append(button("查看单集记录", async () => {
      const v=generation,s=bridge.sessionVersion(); const result=await bridge.api(record.matched?`/watch/series/${record.media.tmdb_id}/records`:`/watch/pending-series/${record.id}/records`); if(!active(v,s))return;
      const view=dialog(`${record.media.title} · 单集观看记录`, "合并只影响足迹展示，每一集的原始记录与已看统计仍保留。");
      const grid=el("div","personal-history-grid series-episode-grid"); for(const row of result.records) grid.append(recordCard(row)); view.body.append(grid);
    }, "button-quiet"));
    if (jobId) card.append(button("校正匹配", () => matchRecord(record,jobId), "button-quiet")); return card;
  }
  async function records(offset=0) {
    const v=generation, s=bridge.sessionVersion(), result=await bridge.api(`/watch/footprints?offset=${offset}&limit=18`); if (!active(v,s)) return;
    const grid=document.getElementById("personal-history-grid"), more=document.getElementById("personal-history-more"); if (!grid) return;
    if (!offset) grid.replaceChildren(); for (const record of result.records) grid.append(recordCard(record));
    if (!offset && !result.records.length) { const empty=el("div","watch-empty"); empty.append(el("span","eyebrow","THE NEXT CHAPTER"),el("h3","","你的故事，从下一部开始"),el("p","muted","同步主程序已完成记录，或从 Emby / Plex 导入自己的观影记忆。")); grid.append(empty); }
    more.replaceChildren(); if (result.next_offset !== null) more.append(button("更多观看记录",()=>records(result.next_offset)));
  }
  async function checkSeries(force=false) {
    if(checkingSeries || !bridge.user())return; const v=generation,s=bridge.sessionVersion(); checkingSeries=true;
    const status = text => {if(active(v,s)){const node=document.getElementById("series-completion-status");if(node){node.textContent=text;node.hidden=!text;}}};
    try {
      const pending=await bridge.api(`/watch/series-progress${force?"?force=true":""}`); if(!active(v,s))return;
      let index=0,processed=0;
      async function worker(){while(active(v,s)&&index<pending.series.length){
        const item=pending.series[index++]; let catalog={tmdb_id:item.tmdb_id,status:"unknown",seasons:[]}; let covered=false;
        try{
          const result=await bridge.hostApi("/subscriptions/title-preview",{method:"POST",body:{title_ref:`tmdb:tv:${item.tmdb_id}`}}); if(!active(v,s))return;
          if(result.status==="ready"&&result.media?.kind==="tv"&&Number(result.media.tmdb_id)===item.tmdb_id){
            const ended=["ended","canceled","cancelled"].includes(String(result.media.status||"").toLowerCase());
            const seasons=Array.isArray(result.seasons)?result.seasons.filter(row=>Number.isSafeInteger(row.season_number)&&row.season_number>0):[];
            if(seasons.length<=200&&seasons.length&&seasons.every(row=>row.season_number<=1000&&Number.isSafeInteger(row.episode_count)&&row.episode_count>0&&row.episode_count<=10000)&&seasons.reduce((sum,row)=>sum+row.episode_count,0)<=30000) {
              catalog={tmdb_id:item.tmdb_id,status:ended?"ended":"airing",seasons:seasons.map(row=>({number:row.season_number,episode_count:row.episode_count}))};
              const expected=new Set(seasons.flatMap(row=>Array.from({length:row.episode_count},(_,i)=>`${row.season_number}:${i+1}`))),watched=new Set((item.episodes||[]).map(unit=>unit.join(":")));
              covered=expected.size===watched.size&&[...expected].every(unit=>watched.has(unit));
            }
            else if(!ended)catalog.status="airing";
          }
        }catch(error){if(error.staleSession||!active(v,s))return;}
        if(!covered&&active(v,s)) {
          try { catalog.native_episodes=await nativeSeriesCatalog(item.tmdb_id); } catch(error) { if(error.staleSession||!active(v,s))return; }
        }
        if(!active(v,s))return; await bridge.api("/watch/series-catalog",{method:"PUT",body:catalog});
        if(!covered&&active(v,s))await bridge.api(`/watch/series/${item.tmdb_id}/platform-catalogs${force?"?force=true":""}`,{method:"POST"});
        processed++;status(`正在核对全集完成情况 ${processed}/${pending.series.length}…`);
      }}
      if(pending.series.length){status(`正在核对全集完成情况 0/${pending.series.length}…`);const outcomes=await Promise.allSettled([worker(),worker(),worker()]);if(outcomes.some(row=>row.status==="rejected"))throw new Error("部分目录未保存");if(active(v,s))await records();}
      status("");
    }catch(error){if(active(v,s)&&!error.staleSession)status("部分全集目录暂未核实，可稍后重试。");}
    finally{if(active(v,s))checkingSeries=false;}
  }
  async function nativeSeriesCatalog(number) {
    // 只通过当前账号的正式库存接口读取在位文件的季集号，不保存文件路径或写已看状态。
    const ref=`tmdb:tv:${number}`,found=await bridge.hostApi(`/discover/titles/${encodeURIComponent(ref)}`);
    if(found.title?.title_ref!==ref||found.title?.media_type!=="tv")return [];
    const units=new Map(),links=Array.isArray(found.library_links)?found.library_links:[];
    if(links.length>20)return [];
    for(const link of links) {
      if(!Number.isSafeInteger(link.library_id)||!Number.isSafeInteger(link.media_item_id)||link.library_id<1||link.media_item_id<1)continue;
      const detail=await bridge.hostApi(`/libraries/${link.library_id}/items/${link.media_item_id}`);
      if(detail.kind!=="tv"||detail.tmdb_id!==number||!Array.isArray(detail.files))return [];
      for(const file of detail.files)if(!file.missing&&(!file.state||file.state==="in_place")&&Number.isSafeInteger(file.season_number)&&file.season_number>0&&Number.isSafeInteger(file.episode_number)&&file.episode_number>0)units.set(`${file.season_number}:${file.episode_number}`,{season:file.season_number,episode:file.episode_number});
    }
    return [...units.values()];
  }
  async function load({automatic=true}={}) {
    if (!bridge.user() || busy) return; busy=true; const v=generation,s=bridge.sessionVersion();
    try { if (loadedSession!==s) await syncAvatar().catch(e=>{ if (!e.staleSession && active(v,s)) bridge.toast(bridge.safeError(e),true); });
      const result=await Promise.all([bridge.api("/watch/profile"),bridge.api("/watch/connections")]); if (!active(v,s)) return;
      data=result[0]; connections=result[1].connections; const first=loadedSession!==s; loadedSession=s; hero(); renderSources(); await records(); checkSeries(); if (automatic && first && !syncJob) await refresh({automatic:true});
    } finally { busy=false; }
  }
  async function refresh({automatic=false}={}) {
    if (syncJob) return; const result=await bridge.api(`/watch/refresh${automatic?"?automatic=true":""}`,{method:"POST"}); syncJob=result.id; const v=generation,s=bridge.sessionVersion(); if (data) { hero(); await records(); }
    const poll=async()=>{ if (!active(v,s) || !syncJob) return; try { const job=await bridge.api(`/watch/jobs/${syncJob}`); if (!active(v,s)) return; if (job.status==="reading") { timer=setTimeout(poll,1200); return; } syncJob=null; if (job.status!=="complete") bridge.toast(job.error||"主程序记录读取未完成，请重试。",true); await load({automatic:false}); } catch(e) { syncJob=null; if (active(v,s)&&!e.staleSession) bridge.toast(bridge.safeError(e),true); } }; timer=setTimeout(poll,500);
  }
  function connect(platform) {
    const isEmby = platform === "emby";
    const { modal, body } = dialog(`连接 ${name(platform)}`, isEmby
      ? "填写令牌及本人 Emby 账号。服务器 API 密钥不绑定用户，插件只读取你明确指定账号的已看数据；读取完成后仍需确认导入。"
      : "使用本人的 Plex 登录令牌，读取完成后仍需确认导入。");
    const form = el("form", "watch-form"), url = input("url", "https://media.example.com");
    const token = input("password", isEmby ? "API 密钥或个人访问令牌" : "个人访问令牌");
    url.required = token.required = true; url.maxLength = 500; token.maxLength = 4096;
    url.autocomplete = token.autocomplete = "off";
    form.append(field("服务器地址", url), field(isEmby ? "Emby 令牌／API 密钥" : "个人令牌", token));
    let account;
    if (isEmby) {
      account = input("text", "Emby 中的用户名或用户 ID"); account.required = true; account.maxLength = 80; account.autocomplete = "off";
      form.append(field("Emby 用户名／用户 ID", account));
    }
    form.append(el("p", "field-help", isEmby
      ? "服务器 API 密钥可配合准确用户名；个人令牌若无用户查询权限，请填写用户 ID。绑定后固定该用户 ID，不读取其他用户观影。"
      : "Plex：填写本人 X-Plex-Token 和媒体服务器地址。读取该令牌的已看状态，不读取其他用户历史。"));
    const consent = check("我确认所选账号属于本人，允许插件加密保存连接并读取这个账号的已看状态。");
    consent.box.required = true;
    const submit = el("button", "button button-primary", "连接并生成预览"); submit.type = "submit";
    form.append(consent.label, submit); body.append(form);
    form.onsubmit = async (event) => {
      event.preventDefault(); submit.disabled = true; const session = bridge.sessionVersion();
      try {
        const result = await bridge.api("/watch/connections", { method: "POST", body: {
          platform, url: url.value.trim(), token: token.value.trim(), ...(isEmby ? { emby_user: account.value.trim() } : {}),
        } });
        token.value = "";
        if (!modal.open || session !== bridge.sessionVersion()) return;
        modal.close(); await load({ automatic: false }); await showJob(result.id);
      } catch (error) { if (!error.staleSession && session === bridge.sessionVersion()) bridge.toast(bridge.safeError(error), true); }
      finally { submit.disabled = false; }
    };
  }
  function bindEmbyAccount(source) {
    const { modal, body } = dialog("指定 Emby 账号", "保留已有连接和令牌，补充需要读取的本人账号。API 密钥不会自动代表某个用户，绑定后只查询该账号的已看记录。");
    const form = el("form", "watch-form"), account = input("text", "Emby 中的用户名或用户 ID");
    account.required = true; account.maxLength = 80; account.autocomplete = "off";
    const consent = check("确认这个 Emby 账号属于本人，允许读取并生成私人导入预览。"); consent.box.required = true;
    const submit = el("button", "button button-primary", "绑定并重新预览"); submit.type = "submit";
    form.append(field("Emby 用户名／用户 ID", account), consent.label, submit); body.append(form);
    form.onsubmit = async (event) => {
      event.preventDefault(); submit.disabled = true; const session = bridge.sessionVersion();
      try {
        const result = await bridge.api(`/watch/connections/${source.id}/account`, { method: "PUT", body: { emby_user: account.value.trim(), consent: true } });
        if (!modal.open || session !== bridge.sessionVersion()) return;
        modal.close(); await load({ automatic: false }); await showJob(result.id);
      } catch (error) { if (!error.staleSession && session === bridge.sessionVersion()) bridge.toast(bridge.safeError(error), true); }
      finally { submit.disabled = false; }
    };
  }
  function disconnect(source) { const {modal,body}=dialog("断开数据连接","断开后删除保存的地址和令牌并停止读取。已写入主程序的已看标记保留。"); const erase=check("同时移除此连接导入的私人档案记录（其他来源保留）"); body.append(erase.label,button("确认断开",async()=>{await bridge.api(`/watch/connections/${source.id}?erase=${erase.box.checked}`,{method:"DELETE"}); modal.close(); await load({automatic:false});})); }
  async function showJob(id) {
    const {modal,body}=dialog("导入预览",null),v=generation,s=bridge.sessionVersion();let offset=0,currentJob,poll,searchTimer;
    const status=el("div","watch-preview-stats"),actions=el("div","watch-row-actions"),filters=el("form","task-record-filters watch-row-actions"),query=input("search","搜索影片名称"),match=el("select"),change=el("select"),grid=el("div","personal-history-grid watch-preview-grid"),more=el("div","personal-more");
    query.setAttribute("aria-label","搜索导入记录");match.setAttribute("aria-label","导入匹配状态");change.setAttribute("aria-label","导入变化类型");
    for(const [node,options] of [[match,[["all","全部匹配状态"],["matched","已匹配"],["pending","待匹配"]]],[change,[["all","全部导入记录"],["new","新增原始记录"],["updated","实际修改记录"],["unchanged","内容未变化"]]]])for(const [value,text] of options){const option=el("option","",text);option.value=value;node.append(option);}
    filters.append(query,match,change);body.append(status,actions,filters,grid,more);const alive=()=>modal.open&&active(v,s);modal.addEventListener("close",()=>{clearTimeout(poll);clearTimeout(searchTimer);});let pageVersion=0;
    async function page(append=false){const request=++pageVersion,params=new URLSearchParams({offset,limit:12,match_state:match.value,change:change.value,query:query.value.trim()}),result=await bridge.api(`/watch/jobs/${id}/records?${params}`);if(!alive()||request!==pageVersion)return;if(!append)grid.replaceChildren();for(const row of result.records){const card=recordCard(row,currentJob.can_commit?id:null,true);if(row.change_kind)card.append(el("span","task-change-badge",row.change_kind==="new"?"本次新增":row.change_kind==="unchanged"?"内容未变化":currentJob.result?.unchanged_records!==undefined?"本次修改":"来源已存在"));grid.append(card);}if(!append&&!result.records.length)grid.append(el("p","muted","当前筛选没有记录。"));more.replaceChildren();if(result.next_offset!==null)more.append(button("更多预览",async()=>{offset=result.next_offset;await page(true);}));}
    const filter=()=>{offset=0;page().catch(e=>{if(alive())bridge.toast(bridge.safeError(e),true);});};filters.onsubmit=e=>{e.preventDefault();filter();};match.onchange=change.onchange=filter;query.oninput=()=>{clearTimeout(searchTimer);searchTimer=setTimeout(filter,250);};
    async function update(){try{const job=await bridge.api(`/watch/jobs/${id}`);if(!alive())return;currentJob=job;status.replaceChildren();actions.replaceChildren();filters.hidden=job.status==="reading";change.hidden=!job.result_available;
      if(job.status==="reading"){status.append(el("strong","",`正在读取 ${name(job.platform)}…已处理 ${job.processed} 条`),el("small","field-help","来源尚未返回总数，读取完成后显示完整明细。"));poll=setTimeout(update,1200);return;}
      if(!["preview","complete"].includes(job.status)){status.append(el("p","",job.error||"读取已中断，请重新预览。"));return;}
      status.append(el("strong","",job.status==="complete"?"已导入私人档案":"读取完成，等待确认"));
      if(job.status!=="complete"||job.result_available)status.append(el("p","muted",`共 ${job.total} 条记录 · ${job.new_unique} 个新增已看 · ${job.already_known} 个已存在 · ${job.pending} 条待匹配`));
      else status.append(el("p","muted",`共 ${job.total} 条记录 · ${job.pending} 条待匹配 · 历史任务未记录当时的新增数量`));
      if(job.result)status.append(el("p","task-import-result",job.result.unchanged_records!==undefined?`新增原始记录 ${job.result.new_records} · 更新原始记录 ${job.result.updated_records} · 内容未变化 ${job.result.unchanged_records}`:`新增原始记录 ${job.result.new_records} · 读取已有记录 ${job.result.updated_records}（旧口径）`));
      const meta=el("small","field-help",`${name(job.platform)} · ${bridge.dateText(job.created_at)}${job.finished_at?` · 完成于 ${bridge.dateText(job.finished_at)}`:""}`);meta.append(help("导入数量","原始记录按来源条目计数；新增已看按电影或单集跨平台去重，同一影片可有多个来源条目。仅导入私人档案，不自动公开。历史新增量仅在本版本之后的任务中保存。"));status.append(meta);
      if(job.can_commit)actions.append(button("确认导入私人档案",async()=>{await bridge.api(`/watch/jobs/${id}/commit`,{method:"POST",body:{consent:true}});bridge.toast("已导入私人档案，未发布或写入主程序。");await load({automatic:false});offset=0;await update();},"button-primary"));
      else if(job.status==="preview")status.append(el("small","field-help","这份预览已被更新的任务替代，仅供查看。"));
      else if(job.matched)actions.append(button("预览主程序已看标记",()=>previewWriteback(id)));await page();
    }catch(e){if(alive()&&!e.staleSession)status.replaceChildren(el("p","",bridge.safeError(e)));}}await update();
  }
  async function matchRecord(record,jobId=null) {
    const media=record.media,s=bridge.sessionVersion(),owner=bridge.user().id,{modal,body}=dialog("校正作品匹配",jobId?"填写准确的 TMDB 作品编号；电视剧还需确认季号、集号。读取作品资料核对后再保存。":"来源缺少作品编号。搜索并核对正确的作品后，仅修正本人的私人档案，不修改三平台已看状态，也不发布到广场。");
    const searchForm=el("form","watch-form"),query=input("search","影片名称"),results=el("div","watch-match-results"),search=el("button","button button-outline","搜索作品");query.value=media.title;search.type="submit";searchForm.append(field("搜索正确的作品",query),search,results);body.append(searchForm);
    const form=el("form","watch-form"),id=input("number","TMDB 编号"),season=input("number","季号"),episode=input("number","集号");id.min="1";id.required=true;id.value=media.tmdb_id||"";form.append(field(media.kind==="tv"?"电视剧 TMDB 编号":"电影 TMDB 编号",id));
    if(jobId&&media.kind==="tv"){season.min=episode.min="0";season.required=episode.required=true;season.value=media.season_number??"";episode.value=media.episode_number??"";form.append(field("季号",season),field("集号",episode));}
    const batch=check("同时修正同一来源、同名同年份电视剧的其他待匹配单集（保留原季集号）");if(!jobId&&media.kind==="tv")form.append(batch.label);
    const info=el("div","watch-match-info"),submit=el("button","button button-primary","读取并核对影片");submit.type="submit";form.append(info,submit);body.append(form);let verified=null,poster=null;
    searchForm.onsubmit=async(event)=>{event.preventDefault();search.disabled=true;results.replaceChildren(el("p","muted","正在搜索…"));try{const found=await bridge.hostApi("/search/titles",{method:"POST",body:{query:query.value.trim(),provider:"tmdb",save_history:false}});if(!modal.open||s!==bridge.sessionVersion())return;results.replaceChildren();for(const item of (found.titles||[]).filter(item=>item.media_type===media.kind&&Number.isSafeInteger(Number(item.external_id))&&Number(item.external_id)>0)){results.append(button(`${item.title} · ${item.release_year||"年份未知"} · TMDB ${item.external_id}`,()=>{id.value=item.external_id;verified=null;poster=null;info.replaceChildren();submit.textContent="读取并核对影片";},"button-quiet"));}if(!results.children.length)results.append(el("p","muted","没有找到对应作品，可换名称搜索或填写 TMDB 编号。"));}catch(e){if(modal.open&&s===bridge.sessionVersion())results.textContent=bridge.safeError(e);}finally{search.disabled=false;}};
    for(const entry of [id,season,episode])entry.oninput=()=>{verified=null;poster=null;info.replaceChildren();submit.textContent="读取并核对影片";};
    form.onsubmit=async(event)=>{event.preventDefault();submit.disabled=true;try{const number=Number(id.value),key=`tmdb:${media.kind}:${number}`;
      if(!verified){const result=await bridge.hostApi(`/discover/titles/${encodeURIComponent(key)}`);if(!modal.open||s!==bridge.sessionVersion())return;if(result.title?.title_ref!==key||result.title?.media_type!==media.kind)throw new Error("无法核对作品身份。");verified=key;const match=result.title.poster_url?.match(/^https:\/\/image\.tmdb\.org\/t\/p\/(?:w[0-9]+|original)(\/[A-Za-z0-9_-]{1,200}\.(?:jpg|jpeg|png|webp))$/i);poster=match?match[1]:null;info.replaceChildren(bridge.mediaPoster({media:{...media,tmdb_id:number,poster_path:poster}},false,false),el("p","",`请确认：${result.title.title||media.title} · ${result.title.release_year||"年份未知"} · TMDB ${number}${media.kind==="tv"?` · 第 ${jobId?season.value:media.season_number} 季第 ${jobId?episode.value:media.episode_number} 集`:""}`));submit.textContent="确认匹配这部作品";return;}
      await sameAccount(owner);if(!modal.open||s!==bridge.sessionVersion())return;
      if(jobId){await bridge.api(`/watch/jobs/${jobId}/records/${record.id}/match`,{method:"PUT",body:{tmdb_id:number,kind:media.kind,...(media.kind==="tv"?{season_number:Number(season.value),episode_number:Number(episode.value)}:{})}});modal.close();bridge.toast("匹配已更新，请重新打开导入预览核对后确认导入。");}
      else{const changed=await bridge.api(`/watch/records/${record.id}/match`,{method:"PUT",body:{tmdb_id:number,kind:media.kind,poster_path:poster,all_episodes:batch.box.checked,consent:true}});if(s!==bridge.sessionVersion())return;modal.close();bridge.toast(`已修正 ${changed.updated} 条私人足迹，已更新海报与统计。`);await load({automatic:false});}
    }catch(e){if(!e.staleSession&&s===bridge.sessionVersion())info.textContent=bridge.safeError(e);}finally{submit.disabled=false;}};
  }
  async function previewWriteback(jobId) {
    const owner=bridge.user().id,s=bridge.sessionVersion(),v=generation,{modal,body}=dialog("预览主程序已看标记","只对本服准确匹配且可访问的电影或单集设为已看。保留收藏与现有进度，不生成虚构播放流水，也不修改外部平台。");
    const progress=el("p","watch-status","正在核对本服片源和当前账号已看状态…"),rows=el("div","watch-write-rows");body.append(progress,rows);const alive=()=>modal.open&&active(v,s),targets=[],seen=new Set();let skipped=0,missing=0,failed=0,offset=0;
    try{await sameAccount(owner);do{const result=await bridge.api(`/watch/jobs/${jobId}/records?offset=${offset}&limit=100&matched_only=true`);if(!alive())return;
      for(const record of result.records){if(!alive())return;const media=record.media,key=`tmdb:${media.kind}:${media.tmdb_id}`,unit=`${key}:${media.kind==="tv"?`${media.season_number}:${media.episode_number}`:"movie"}`;if(seen.has(unit))continue;seen.add(unit);
        try{const found=await bridge.hostApi(`/discover/titles/${encodeURIComponent(key)}`);if(!alive())return;const title=found?.title,status=title?.library_status;
          if(title?.title_ref!==key||title?.media_type!==media.kind||(title.provider&&title.provider!=="tmdb")||(title.external_id!=null&&Number(title.external_id)!==media.tmdb_id))throw new Error("作品身份不一致");
          if(!status||!Number.isSafeInteger(status.media_item_id)||status.media_item_id<1||!(status.file_count>0)||!Array.isArray(found.library_links)||!found.library_links.some(x=>x.media_item_id===status.media_item_id&&Number.isSafeInteger(x.library_id)&&x.library_id>0)){missing++;continue;}
          const target={media_item_id:status.media_item_id};if(media.kind==="tv"){if(!Number.isInteger(media.season_number)||!Number.isInteger(media.episode_number)){missing++;continue;}target.season_number=media.season_number;target.episode_number=media.episode_number;}
          const mark=await bridge.hostApi(`/playback/marks?${new URLSearchParams(target)}`);if(!alive())return;if(typeof mark.played!=="boolean")throw new Error("已看状态格式不兼容");if(mark.played){skipped++;continue;}
          const line=el("p","watch-write-row",`${media.title}${media.kind==="tv"?` · S${media.season_number} E${media.episode_number}`:""}`);rows.append(line);targets.push({target,line,media,done:false});
        }catch(e){if(e.staleSession||e.status===401)throw e;failed++;}progress.textContent=`已核对 ${seen.size} 个电影或单集…`;
      }offset=result.next_offset;}while(offset!==null&&alive());if(!alive())return;
      progress.textContent=`可标记 ${targets.length} 项 · 已看跳过 ${skipped} 项 · 无本服片源 ${missing} 项 · 核对失败 ${failed} 项。当前账号：${data.profile.nickname}`;if(!targets.length)return;
      const consent=check("确认将以上电影／单集设为我在 MovieClaw 中的已看内容。");const submit=button("确认标为主程序已看",async(control)=>{consent.box.disabled=true;let written=0,errors=0;
        for(const entry of targets){if(!alive())return;if(entry.done)continue;try{await sameAccount(owner);if(!alive())return;const key=`tmdb:${entry.media.kind}:${entry.media.tmdb_id}`,found=await bridge.hostApi(`/discover/titles/${encodeURIComponent(key)}`);
          if(found?.title?.title_ref!==key||found.title.library_status?.media_item_id!==entry.target.media_item_id||!found.library_links?.some(x=>x.media_item_id===entry.target.media_item_id))throw new Error("本服匹配已变化，请重新预览");
          if(!alive())return;await sameAccount(owner);if(!alive())return;const result=await bridge.hostApi("/playback/marks",{method:"POST",body:{...entry.target,played:true}});if(result?.played!==true)throw new Error("主程序未确认标记");entry.done=true;written++;entry.line.textContent+=" · 已标记";
        }catch(e){if(e.accountChanged||e.staleSession||e.status===401||s!==bridge.sessionVersion())throw e;errors++;entry.line.dataset.failed="true";}}
        if(!alive())return;progress.textContent=`本次已确认 ${written} 项，失败 ${errors} 项。成功项目再次提交会跳过。`;control.textContent=errors?"重试未完成项目":"已全部完成";if(!errors){consent.label.remove();control.remove();}else consent.box.disabled=false;
      },"button-primary");submit.disabled=true;consent.box.onchange=()=>{submit.disabled=!consent.box.checked;};body.append(consent.label,submit);
    }catch(e){if(alive()&&!e.staleSession)progress.textContent=bridge.safeError(e);}
  }
  function reset(){window.MovieFriendsAutoSync?.reset();window.MovieFriendsResume?.reset();generation++;settingsVersion++;loadedSession=avatarSession=-1;busy=false;checkingSeries=false;data=null;syncJob=null;connections=[];clearTimeout(timer);document.getElementById("profile-root")?.replaceChildren();document.getElementById("watch-source-settings")?.replaceChildren();for(const modal of document.querySelectorAll(".watch-dialog"))modal.close();}
  return {configure(value){bridge=value;window.MovieFriendsAlignment?.configure({...value,sameAccount});window.MovieFriendsTasks?.configure({...value,sameAccount});window.MovieFriendsAutoSync?.configure({...value,sameAccount});window.MovieFriendsResume?.configure({...value,sameAccount});},load,loadSettings,reset,showJob};
})();
