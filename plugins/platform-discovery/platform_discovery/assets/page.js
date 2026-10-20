"use strict";
(() => {
  const base = "/api/v1/plugins/platform-discovery";
  if(location.protocol==="file:"){
    document.addEventListener("DOMContentLoaded",()=>{document.body.replaceChildren();const box=document.createElement("main");box.style.padding="40px";const title=document.createElement("h1"),note=document.createElement("p"),path=document.createElement("code");title.textContent="请从 MovieClaw 服务器打开平台放映厅";note.textContent="当前打开的是本地源码文件，无法连接服务器或读取账号偏好。请在服务器地址后打开以下入口；你的档案无需重建。";path.textContent=base+"/panel";box.append(title,note,path);document.body.append(box);});return;
  }
  const $ = (id) => document.getElementById(id);
  const preview = document.documentElement.dataset.preview === "true";
  const state = {mode:"personal", scope:"all", kind:"all", platforms:[], region:"US", feeds:new Map(), sections:new Map(), inflight:new Map(), watchers:new Map(), lastVisibleCheck:Date.now(), generation:0};
  const rec = {categories:[],category:"korean",generation:0,sessionId:""};
  let loadObserver, chapterObserver, toastTimer, detailGeneration=0,detailSubscription=null,detailNavigation=null,detailReadController=null,activeNotice=null;
  const compactDate = (value) => {
    if (!value) return "";
    if (/^\d{4}-\d{2}-\d{2}$/.test(String(value))) return String(value);
    const date = new Date(value);
    return Number.isNaN(date.valueOf()) ? String(value).slice(0,40) : new Intl.DateTimeFormat("zh-CN",{timeZone:"Asia/Shanghai",month:"numeric",day:"numeric",hour:"2-digit",minute:"2-digit"}).format(date);
  };
  const posterLoader=globalThis.MovieClawPosters?.create({urlFor:value=>preview?value:"/api/v1/images/proxy?url="+encodeURIComponent(value)});
  image.beginWall=()=>posterLoader?.beginWall();
  image.settled=()=>posterLoader?.settled()||Promise.resolve(false);
  image.canPrepare=()=>posterLoader?.canPrepare()||false;
  image.warmNext=(items,width)=>posterLoader?.warmNext(items,width)||Promise.resolve(false);
  image.cancelNext=()=>posterLoader?.cancelNext();
  image.readyForSwap=(items,width)=>posterLoader?.readyForSwap(items,width)||[];
  image.prepareSwap=(items,width)=>posterLoader?.prepareSwap(items,width)||Promise.resolve([]);
  image.cancelAmbient=()=>posterLoader?.cancelAmbient();
  image.warmWall=(items,width)=>posterLoader?.warmWall(items,width)||Promise.resolve();
  image.preload=value=>posterLoader?.hover(value);
  image.cancelPreload=()=>posterLoader?.cancelHover();
  image.setPreload=value=>posterLoader?.setEnabled(value);
  window.addEventListener("pagehide",()=>posterLoader?.close());
  const personal = globalThis.MovieClawPersonal.create({api,node,button,image,showDetail,compactDate,announce});
  const theater = globalThis.MovieClawTheater.create({node,button,announce});
  function node(tag, text, cls) { const result=document.createElement(tag); if(text!=null) result.textContent=String(text); if(cls) result.className=cls; return result; }
  function safeUrl(value) { try { const url=new URL(value); return url.protocol==="https:" && !url.username && !url.password ? url.href : null; } catch { return null; } }
  function externalLink(text,value,cls) { const url=safeUrl(value); if(!url) return null; const a=node("a",text,cls);a.href=url;a.target="_blank";a.rel="noopener noreferrer";return a; }
  function button(text,cls,action) { const result=node("button",text,cls);result.type="button";if(action) result.addEventListener("click",action);return result; }
  function announce(text) {if(activeNotice)activeNotice.hidden=true;activeNotice=document.querySelector("dialog[open] .dialog-notice")||$("toast");activeNotice.textContent=text;activeNotice.hidden=false;clearTimeout(toastTimer);const notice=activeNotice;toastTimer=setTimeout(()=>{notice.hidden=true;},4000);}

  async function loadProxyStatus() {
    if(preview){$("proxy-mode").textContent="代理设置 · 预览";return;}
    try{const status=await api("/status"),mode=status.proxy?.mode;$("proxy-mode").textContent="代理："+({host:"服务器",direct:"直连",custom:"自定义"}[mode]||"设置");const update=status.automatic_update;if(status.cache?.persistent)$("cache-status").textContent=status.cache.write_errors?"缓存暂不可用":"片单已保存";if(update?.enabled)$("cache-status").textContent+=" · 每 "+update.interval_minutes+" 分钟自动检查"+(update.last_check?" · 最近 "+compactDate(update.last_check):"");}catch{ /* 片单可独立展示，状态读取失败不阻断页面。 */ }
  }
  // 主题偏好按当前屏幕选覆盖值；不读取或猜测登录凭据。
  async function syncTheme() {
    if(preview) return;
    try {
      const response=await fetch("/api/v1/ui/preferences",{credentials:"same-origin",cache:"no-store"});
      if(!response.ok) return;
      const body=await response.json(), prefs=body.data||body;
      const apply=()=>{const theme=(matchMedia("(max-width:767px)").matches?prefs.theme_mobile:prefs.theme_desktop)||prefs.theme;if(["silver","netflix"].includes(theme)) document.documentElement.dataset.theme=theme;};
      apply();matchMedia("(max-width:767px)").addEventListener("change",apply);
    } catch { /* 偏好暂不可读时使用默认银玻璃主题。 */ }
  }
  async function api(path,options={}) {
    if(preview) return globalThis.MovieClawPreview.api(path);
    return globalThis.MovieClawTransport.request(base+path,options);
  }

  function image(value,title,cls,fallbacks=[],options={}) {
    const candidates=[...new Set([value,...(Array.isArray(fallbacks)?fallbacks:[])].map(safeUrl).filter(Boolean))];if(!candidates.length) return null;
    let position=0;
    const img=node("img",null,cls);img.alt=title;img.loading="lazy";img.decoding="async";img.referrerPolicy="no-referrer";
    const setSource=()=>{img.src=preview?candidates[position]:"/api/v1/images/proxy?url="+encodeURIComponent(candidates[position]);};
    img.addEventListener("load",()=>{const placeholder=img.parentElement?.querySelector(".poster-placeholder");if(placeholder)placeholder.hidden=true;});
    if(options.wall&&posterLoader)return posterLoader.attach(img,candidates,options);
    if(cls==="detail-poster"){img.loading="eager";img.fetchPriority="high";}
    img.addEventListener("error",()=>{position++;if(position<candidates.length)setSource();else{const placeholder=img.parentElement?.querySelector(".poster-placeholder");if(placeholder)placeholder.hidden=false;img.remove();}});setSource();return img;
  }
  function placeholder(title) { const wrap=node("div",null,"poster-placeholder");wrap.append(node("span",title,"placeholder-title"),node("span","暂无海报","placeholder-note"));return wrap; }
  function nativeTarget(item) {
    const ref=String(item.detail_ref||"");let match;
    if((match=/^tmdb:(movie|tv):(\d+)$/.exec(ref))) return "/media/"+match[1]+"/"+match[2];
    if((match=/^douban:(\d+)$/.exec(ref))) return "/media/douban/"+match[1];
    // tab=media 避免仅查看公开影视资料时意外发起资源站点搜索。
    return "/search?q="+encodeURIComponent(item.title)+"&tab=media";
  }
  function showDetail(item,platform,feed) {
    if(detailSubscription?.isSubmitting()){announce("正在创建订阅，请等待服务器确认。");return;}
    detailSubscription?.dispose();detailSubscription=null;detailReadController?.abort();detailReadController=new AbortController();$("detail-notice").hidden=true;
    const batch=Array.isArray(feed.items)?feed.items:[],position=batch.findIndex(row=>(row.detail_ref||row.id)===(item.detail_ref||item.id));
    detailNavigation={items:position<0?[item]:batch,index:Math.max(0,position),platform,feed};
    $("detail-header-title").textContent=item.title;$("detail-position").textContent=(detailNavigation.index+1)+" / "+detailNavigation.items.length;
    $("detail-previous").disabled=detailNavigation.index===0;$("detail-next").disabled=detailNavigation.index>=detailNavigation.items.length-1;
    const generation=++detailGeneration,source=feed.source||{}, layout=node("div",null,"detail-layout"),cover=node("div",null,"detail-poster");cover.style.position="relative";
    const art=button("","detail-poster-expand",()=>{const expanded=$("detail-dialog").classList.toggle("poster-expanded");art.setAttribute("aria-expanded",String(expanded));art.setAttribute("aria-label",(expanded?"收起":"放大")+item.title+"的海报");});art.setAttribute("aria-label","放大"+item.title+"的海报");art.setAttribute("aria-expanded","false");
    cover.append(placeholder(item.title));const img=image(item.poster_url,item.title,"detail-poster",item.poster_fallback_urls);if(img)cover.append(img);art.append(cover);
    const main=node("div",null,"detail-main"), block=node("div",null,"detail-title-block");
    block.append(node("p",platform.name+" · "+({tv:"剧集",movie:"电影",variety:"综艺"}[item.media_type]||"影视"),"detail-eyebrow"));
    const heading=node("h2",item.title);heading.id="detail-title";block.append(heading);
    const metadata=[item.year,item.subtitle];if(item.rating!=null && item.rating_source) metadata.push(item.rating_source+" "+Number(item.rating).toFixed(1));
    const overview=node("p",item.overview||"简介正在准备中，可在 MovieClaw 查看完整作品资料。","detail-overview");
    let overviewExpanded=false;const overviewToggle=button("展开完整简介","button ghost detail-overview-toggle",()=>{overviewExpanded=!overviewExpanded;fitOverview();});
    function fitOverview(){const long=overview.textContent.length>220;overviewToggle.hidden=!long;overview.classList.toggle("is-collapsed",long&&!overviewExpanded);overviewToggle.textContent=overviewExpanded?"收起简介":"展开完整简介";overviewToggle.setAttribute("aria-expanded",String(overviewExpanded));}fitOverview();
    block.append(node("p",metadata.filter(Boolean).join(" · "),"detail-meta"));main.append(block,overview,overviewToggle);
    const reasons=item.recommendation_reasons||item.metadata?.recommendation_reasons;
    if(Array.isArray(reasons)&&reasons.length)main.append(node("p","为你推荐："+reasons.filter(reason=>typeof reason==="string").join(" · "),"detail-reasons"));
    const extra=node("section",null,"detail-extra");if(item.detail_ref){extra.append(node("p","正在读取参演人与作品资料…"));extra.setAttribute("aria-live","polite");main.append(extra);}
    const provenance=node("div",null,"detail-source");provenance.append(node("strong","片单来源："+ (source.label||platform.name)));
    if(source.period) provenance.append(node("div","统计周期："+source.period));
    if(source.updated_at) provenance.append(node("div","榜单日期："+compactDate(source.updated_at)));
    if(feed.fetched_at||source.fetched_at) provenance.append(node("div","获取时间："+compactDate(feed.fetched_at||source.fetched_at)));
    provenance.append(node("div",platform.id==="personal"?"按口味测试与历史偏好选片，兼顾年代与题材差异；没有测试时先展示探索片单。":source.kind==="random_library"?"从地区剧集候选中随机抽样，不限制上映年份。":source.kind==="official_selection"?"按平台官方频道推荐顺序展示，没有播放量名次。":source.kind==="provider_popular"?"按 "+state.region+" 地区的平台片库筛选，以 TMDB 热度排序。":"名次来自当前片单，不跨平台比较热度。"));
    if(item.metadata?.rank_scope) provenance.append(node("div","所属榜单："+item.metadata.rank_scope));
    if(item.metadata?.selection_scope) provenance.append(node("div","所属栏目："+item.metadata.selection_scope));
    if(item.metadata?.poster_source) provenance.append(node("div","图片来源："+item.metadata.poster_source+(item.metadata.poster_aspect==="landscape"?" · 官方横幅画面":"")));
    if(feed.status==="stale") provenance.append(node("div","当前展示上次成功获取的缓存，请以来源页面为准。"));
    main.append(provenance);
    if(platform.id==="personal")main.append(personal.detailActions(item));
    const actions=node("div",null,"detail-actions"), native=node("a",item.detail_ref?"查看 MovieClaw 详情":"在 MovieClaw 查找","button");native.href=nativeTarget(item);
    if(!preview&&item.detail_ref){detailSubscription=globalThis.MovieClawSubscriptions.create({item,node,button,announce,onCreated:()=>personal.subscriptionChanged(),onBusy:busy=>{$("detail-close").disabled=busy;$("detail-previous").disabled=busy||detailNavigation.index===0;$("detail-next").disabled=busy||detailNavigation.index>=detailNavigation.items.length-1;}});actions.append(detailSubscription.entry);}
    const nativeStatus=node("p",null,"detail-native-status");nativeStatus.hidden=true;nativeStatus.setAttribute("role","status");
    if(preview) native.addEventListener("click",event=>{event.preventDefault();announce("启用插件后，会跳到 MovieClaw 原生影视资料页。");});
    else if(item.detail_ref){
      let checking=false;
      const label=native.textContent;
      native.addEventListener("click",async event=>{
        event.preventDefault();if(checking)return;
        if(detailSubscription?.isSubmitting()){announce("正在创建订阅，请等待服务器确认。");return;}
        checking=true;native.setAttribute("aria-disabled","true");native.textContent="检查订阅入口…";nativeStatus.hidden=true;
        try{
          const status=await api("/native-title-status/"+encodeURIComponent(item.detail_ref));
          if(generation!==detailGeneration||!$("detail-dialog").open||detailSubscription?.isSubmitting())return;
          if(status.available===true){location.assign(nativeTarget(item));return;}
          nativeStatus.textContent=status.message||"MovieClaw 的影片资料服务暂时不可用，请稍后重试。";nativeStatus.hidden=false;
        }catch(error){if(generation===detailGeneration&&$("detail-dialog").open){nativeStatus.textContent=error.message;nativeStatus.hidden=false;}}
        finally{checking=false;native.removeAttribute("aria-disabled");native.textContent=nativeStatus.hidden?label:"重试 MovieClaw 入口";}
      });
    }
    actions.append(native);
    const itemUrl=safeUrl(item.source_url),hasItemUrl=itemUrl&&itemUrl!==safeUrl(source.url||platform.url);
    const original=externalLink(source.kind==="random_library"?"查看作品资料 ↗":source.kind==="provider_popular"?"查看观看来源 ↗":hasItemUrl?"打开平台条目 ↗":"打开来源榜单 ↗",item.source_url||source.url||platform.url,"button");if(original) actions.append(original);
    main.append(actions,nativeStatus);if(detailSubscription)main.append(detailSubscription.root);main.append(node("p","点击海报可放大查看；订阅在这里选季并确认后创建。","detail-help"));layout.append(art,main);$("detail-content").replaceChildren(layout);$("detail-dialog").classList.remove("poster-expanded");$("detail-dialog").showModal();$("detail-dialog").scrollTop=0;document.body.classList.add("detail-open");if(platform.id==="personal"&&!preview)personal.recordView?.(item);
    if(item.detail_ref&&!preview)api("/public-titles/"+encodeURIComponent(item.detail_ref),{signal:detailReadController.signal}).then(details=>{
      if(generation!==detailGeneration||!$("detail-dialog").open)return;
      if(details.overview){overview.textContent=details.overview;fitOverview();}
      extra.replaceChildren();
      const genres=Array.isArray(details.genres)?details.genres.filter(value=>typeof value==="string"):[];
      const directors=Array.isArray(details.directors)?details.directors.filter(value=>typeof value==="string"):[];
      const facts=[genres.join(" / "),details.runtime_minutes?details.runtime_minutes+" 分钟":null,directors.length?"导演："+directors.join("、"):null].filter(Boolean);
      if(facts.length)extra.append(node("p",facts.join(" · ")));
      const cast=Array.isArray(details.cast)?details.cast.slice(0,12):[];
      if(cast.length){extra.append(node("h3","参演人"));const people=node("div",null,"detail-people");cast.forEach(person=>{const card=node("div",null,"detail-person"),portrait=image(person.profile_url,person.name);if(portrait)card.append(portrait);card.append(node("span",person.name||"未提供姓名"),node("small",person.character||""));people.append(card);});extra.append(people);}
      else extra.append(node("p","来源暂未提供参演人资料。"));
    }).catch(()=>{if(generation===detailGeneration&&$("detail-dialog").open)extra.replaceChildren(node("p","暂时无法读取参演人，可前往 MovieClaw 查看完整资料。"));});
  }
  function moveDetail(direction){
    if(!detailNavigation||detailSubscription?.isSubmitting())return;
    const next=detailNavigation.index+direction;
    if(next>=0&&next<detailNavigation.items.length)showDetail(detailNavigation.items[next],detailNavigation.platform,detailNavigation.feed);
  }
  function posterCard(item,platform,feed) {
    const root=node("article",null,"poster-card"), poster=button("","poster-button",()=>showDetail(item,platform,feed));poster.setAttribute("aria-label","查看 "+item.title+" 详情");poster.append(placeholder(item.title));
    const img=image(item.poster_url,item.title,null,item.poster_fallback_urls);if(img) poster.append(img);
    if(item.rank!=null && feed.source?.kind!=="provider_popular") poster.append(node("span",String(item.rank).padStart(2,"0"),"rank-number"));
    if(item.subtitle && !String(item.subtitle).includes("/")) poster.append(node("span",item.subtitle,"poster-tag"));
    const title=node("h3",item.title);title.title=item.title;
    const meta=node("div",null,"poster-meta");if(item.year) meta.append(node("span",item.year));
    meta.append(node("span",{tv:"剧集",variety:"综艺",movie:"电影"}[item.media_type]||"影视"));
    if(item.rating!=null && item.rating_source) meta.append(node("span",item.rating_source+" "+Number(item.rating).toFixed(1),"poster-rating"));
    root.append(poster,title);
    if(item.metadata?.rank_scope && ["netflix","youku"].includes(platform.id)) {
      const scope=String(item.metadata.rank_scope),english=!/Non-English/i.test(scope);
      const text=platform.id==="netflix"?(english?"英语":"非英语")+(/Films/i.test(scope)?"电影榜":"剧集榜"):({tv:"剧集",variety:"综艺",movie:"电影"}[item.media_type]||"")+" · "+scope;
      root.append(node("span",text,"poster-rank-scope"));
    }
    root.append(meta);return root;
  }
  function featureCard(item,platform,feed) {
    const root=button("","feature-card",()=>showDetail(item,platform,feed));root.setAttribute("aria-label","查看 "+item.title+" 详情");root.append(placeholder(item.title));
    const img=image(item.backdrop_url||item.poster_url,"",null,[item.poster_url,...(item.poster_fallback_urls||[])]);if(img) root.append(img);
    root.firstElementChild.replaceChildren();
    const copy=node("div",null,"feature-copy");copy.append(node("span",feed.source?.kind==="random_library"?"RANDOM PICK · 随心推荐":feed.source?.kind==="official_selection"?"PLATFORM PICKS · 平台推荐":item.rank===1?"IN THE SPOTLIGHT · 榜首":"IN THE SPOTLIGHT · 片单焦点","feature-eyebrow"),node("h3",item.title),node("p",item.overview||item.subtitle||"走进这个平台，发现下一部想看的作品。"),node("span","查看详情  →","feature-more"));root.append(copy);return root;
  }
  function makeChapter(platform,index) {
    const section=node("section",null,"chapter");section.id="platform-"+platform.id;section.dataset.platform=platform.id;section.style.setProperty("--brand",platform.color||"#aab4c8");section.setAttribute("aria-labelledby","heading-"+platform.id);
    const watermark=node("div",platform.wordmark||platform.name,"chapter-watermark");watermark.setAttribute("aria-hidden","true");
    const heading=node("div",null,"chapter-heading"), identity=node("div",null,"chapter-identity"), label=node("div");
    const title=node("h2",platform.wordmark||platform.name,"platform-english");title.id="heading-"+platform.id;
    const subtitle=node("p",null,"subtitle");subtitle.append(node("span",platform.wordmark!==platform.name?platform.name:platform.description||"平台精选"),node("span","读取片单…","source-chip"));
    label.append(title,subtitle);identity.append(node("span",String(index+1).padStart(2,"0"),"chapter-number"),label);
    const actions=node("div",null,"chapter-actions"), source=externalLink(["hbo","disney","mango"].includes(platform.id)?"打开平台 ↗":"来源榜单 ↗",platform.url,"source-link");if(source) actions.append(source);
    const row=node("div",null,"content-row");row.setAttribute("aria-label",platform.name+"片单");
    const move=(direction)=>{row.scrollBy({left:direction*Math.max(200,row.clientWidth*.75),behavior:matchMedia("(prefers-reduced-motion:reduce)").matches?"instant":"smooth"});};
    const previous=button("‹","row-next",()=>move(-1)),next=button("›","row-next",()=>move(1));previous.setAttribute("aria-label","向左浏览 "+platform.name);next.setAttribute("aria-label","向右浏览 "+platform.name);actions.append(previous,next);heading.append(identity,actions);
    for(let i=0;i<6;i++) row.append(node("div",null,"skeleton"));
    const note=node("p",null,"feed-note");section.append(watermark,heading,row,note);
    state.sections.set(platform.id,{section,row,note,subtitle,source,platform});return section;
  }
  function feedMessage(status) {return {needs_config:"等待配置海外片库",unavailable:"片单暂时无法读取",unsupported:"该平台暂无此类榜单"}[status]||"当前片单暂无作品";}
  function renderFeed(platform,feed,append=false) {
    const parts=state.sections.get(platform.id);if(!parts) return;
    if(!append) parts.row.replaceChildren();
    const items=Array.isArray(feed.items)?feed.items:[];
    if(items.length){
      if(!append) parts.row.append(featureCard(items[0],platform,feed));
      items.forEach(item=>parts.row.append(posterCard(item,platform,feed)));
      if(feed.has_more){const more=button("加载更多 →","button load-more",async()=>{more.disabled=true;try{const next=await api("/feeds/"+platform.id+"?kind="+state.kind+"&page="+(Number(feed.page||1)+1));if(state.sections.get(platform.id)!==parts)return;more.remove();renderFeed(platform,next,true);}catch(err){announce(err.message);more.disabled=false;}});parts.row.append(more);}
    }else if(!append){
      const empty=node("div",null,"empty-state"),copy=node("div");copy.append(node("h3",feedMessage(feed.status)),node("p",feed.message||"可以先查看官方页面，稍后再重试。"));empty.append(copy);
      if(feed.status==="needs_config"){const settings=node("a","配置 TMDB","button");settings.href="/settings/plugins";empty.append(settings);}else{empty.append(button("重新读取","button",()=>{state.feeds.delete(platform.id+":"+state.kind);loadFeed(platform,true);}));}
      parts.row.append(empty);empty.style.width="100%";empty.style.flexShrink="0";
    }
    renderFeedNote(parts,feed);
  }
  function renderFeedNote(parts,feed) {
    const source=feed.source||{},items=Array.isArray(feed.items)?feed.items:[];
    const chip=parts.subtitle.querySelector(".source-chip");chip.textContent=source.label||(feed.status==="needs_config"?"待配置":"来源暂不可用");
    if(parts.source&&safeUrl(source.url))parts.source.href=safeUrl(source.url);
    const notes=[];if(source.period) notes.push(source.period);else if(source.updated_at) notes.push("榜单日期 "+compactDate(source.updated_at));if(feed.fetched_at||source.fetched_at) notes.push("获取于 "+compactDate(feed.fetched_at||source.fetched_at));if(feed.status==="stale")notes.push("上次缓存 · 当前来源读取失败");if(feed.message&&items.length)notes.push(feed.message);
    if(source.kind==="provider_popular")notes.push(state.region+" 片库 · TMDB 热度排序");
    if(feed.cache?.refreshing)notes.push("后台更新中，先看已保存片单");
    if(feed.artwork_diagnostics?.message)notes.push(feed.artwork_diagnostics.message);
    parts.note.textContent=notes.join(" · ");parts.note.classList.toggle("stale",feed.status==="stale");
    $("intro-status").textContent=preview?"示例内容 · 可切换平台与分类":"公开榜单与平台片库";
  }
  async function loadFeed(platform,force=false) {
    const kind=state.kind,key=platform.id+":"+kind,generation=state.generation;
    if(state.feeds.has(key)&&!force){const feed=state.feeds.get(key);renderFeed(platform,feed);if(feed.cache?.refreshing)watchFeedRefresh(platform,kind,generation);return;}
    const parts=state.sections.get(platform.id);if(!parts)return;
    if(force){parts.row.replaceChildren();for(let i=0;i<6;i++)parts.row.append(node("div",null,"skeleton"));}
    // A rebuilt chapter must subscribe to an existing request as well; the
    // previous chapter's generation deliberately cannot render its response.
    try{const feed=await readFeed(platform,kind);state.feeds.set(key,feed);if(generation===state.generation){renderFeed(platform,feed);if(feed.cache?.refreshing)watchFeedRefresh(platform,kind,generation);}}
    catch(err){if(generation===state.generation)renderFeed(platform,{status:"unavailable",items:[],message:err.message});}
  }
  async function readFeed(platform,kind) {
    const key=platform.id+":"+kind,promise=state.inflight.get(key)||api("/feeds/"+platform.id+"?kind="+kind+"&page=1");state.inflight.set(key,promise);
    try{return await promise;}finally{if(state.inflight.get(key)===promise)state.inflight.delete(key);}
  }
  function feedVersion(feed) {
    return JSON.stringify([feed?.fetched_at,feed?.source?.fetched_at,feed?.source?.updated_at,feed?.source?.period,feed?.status,!!feed?.cache?.refreshing,feed?.message,feed?.items]);
  }
  function updateFeed(platform,kind,feed) {
    const key=platform.id+":"+kind,previous=state.feeds.get(key),parts=state.sections.get(platform.id);state.feeds.set(key,feed);
    if(!parts||feedVersion(previous)===feedVersion(feed))return;
    // Metadata-only checks leave expanded pages, image elements and focus intact.
    if(previous&&JSON.stringify(previous.items)===JSON.stringify(feed.items)){renderFeedNote(parts,feed);return;}
    const scroll=parts.row.scrollLeft;renderFeed(platform,feed);parts.row.scrollLeft=scroll;
  }
  function stopFeedWatch(watch) {
    clearTimeout(watch.timer);if(state.watchers.get(watch.key)===watch)state.watchers.delete(watch.key);
  }
  function validFeedWatch(watch) {
    return state.watchers.get(watch.key)===watch&&watch.generation===state.generation&&watch.kind===state.kind&&state.sections.has(watch.platform.id);
  }
  function scheduleFeedWatch(watch,delay=watch.delay) {
    if(!validFeedWatch(watch)){stopFeedWatch(watch);return;}
    if(watch.running||watch.timer!=null||document.hidden||state.mode!=="platforms")return;
    watch.timer=setTimeout(async()=>{
      watch.timer=null;if(!validFeedWatch(watch)){stopFeedWatch(watch);return;}
      if(document.hidden||state.mode!=="platforms")return;
      watch.running=true;let again=true;
      try{const feed=await readFeed(watch.platform,watch.kind);if(!validFeedWatch(watch))return;
        // A hidden document retains the result, then renders it on resume.
        if(document.hidden||state.mode!=="platforms")watch.pending=feed;else updateFeed(watch.platform,watch.kind,feed);
        again=!!feed.cache?.refreshing;
      }catch{ /* 保留已有片单，短暂失败后继续检查插件缓存。 */ }
      finally{watch.running=false;if(validFeedWatch(watch)){if(!again&&!watch.pending)stopFeedWatch(watch);else{watch.delay=Math.min(30000,Math.round(watch.delay*1.5));scheduleFeedWatch(watch);}}}
    },delay);
  }
  function watchFeedRefresh(platform,kind,generation) {
    const key=platform.id+":"+kind;let watch=state.watchers.get(key);
    if(watch&&watch.generation!==generation){stopFeedWatch(watch);watch=null;}
    if(!watch){watch={key,platform,kind,generation,delay:1500,timer:null,running:false,pending:null};state.watchers.set(key,watch);}
    scheduleFeedWatch(watch);
  }
  function resumeFeedWatches() {
    state.watchers.forEach(watch=>{
      if(!validFeedWatch(watch)){stopFeedWatch(watch);return;}
      clearTimeout(watch.timer);watch.timer=null;
      if(document.hidden||state.mode!=="platforms")return;
      if(watch.pending){const feed=watch.pending;watch.pending=null;updateFeed(watch.platform,watch.kind,feed);if(!feed.cache?.refreshing){stopFeedWatch(watch);return;}}
      scheduleFeedWatch(watch,0);
    });
  }
  async function checkVisibleFeeds() {
    if(preview||document.hidden||state.mode!=="platforms")return;
    state.lastVisibleCheck=Date.now();const generation=state.generation,kind=state.kind;
    await Promise.all([...state.sections.values()].map(async parts=>{
      const rect=parts.section.getBoundingClientRect(),key=parts.platform.id+":"+kind;
      if(rect.bottom<=0||rect.top>=innerHeight||state.watchers.has(key))return;
      try{const feed=await readFeed(parts.platform,kind);if(generation!==state.generation||kind!==state.kind)return;
        // Cache reads only: never force an upstream refresh from the browser.
        if(document.hidden||state.mode!=="platforms")return;
        updateFeed(parts.platform,kind,feed);if(feed.cache?.refreshing)watchFeedRefresh(parts.platform,kind,generation);
      }catch{ /* 长期开页时网络断开不清空已有片单。 */ }
    }));
  }
  function remember() {try{sessionStorage.setItem("movieclaw.platform-discovery",JSON.stringify({mode:state.mode,scope:state.scope,kind:state.kind,category:rec.category}));}catch{} }
  function browsingSession(){
    if(rec.sessionId)return rec.sessionId;
    try{const saved=sessionStorage.getItem("movieclaw.platform-discovery-session");if(/^[a-f0-9]{32}$/.test(saved||""))return rec.sessionId=saved;}catch{}
    rec.sessionId=Array.from(crypto.getRandomValues(new Uint8Array(16)),byte=>byte.toString(16).padStart(2,"0")).join("");
    try{sessionStorage.setItem("movieclaw.platform-discovery-session",rec.sessionId);}catch{}return rec.sessionId;
  }
  function recommendationNonce(){return Date.now().toString(36)+"_"+Math.random().toString(36).slice(2,10);}
  async function loadRecommendationCategories(){
    if(rec.categories.length)return;
    const result=await api("/recommendation-categories");rec.categories=result.categories||[];
    if(!rec.categories.some(item=>item.id===rec.category))rec.category=rec.categories[0]?.id||"korean";
    $("recommendation-tabs").replaceChildren(...rec.categories.map(category=>{
      const tab=button(category.name,"recommendation-tab",()=>{if(rec.category===category.id)return;rec.category=category.id;remember();loadRecommendations();});
      tab.id="recommendation-tab-"+category.id;tab.dataset.category=category.id;tab.setAttribute("role","tab");tab.setAttribute("aria-controls","recommendation-panel");tab.setAttribute("aria-selected",String(category.id===rec.category));return tab;
    }));
  }
  async function loadRecommendations(){
    const generation=++rec.generation,trigger=$("shuffle-recommendations"),row=$("recommendation-row");trigger.disabled=true;trigger.textContent="准备中…";
    row.replaceChildren();for(let i=0;i<8;i++)row.append(node("div",null,"skeleton"));$("recommendation-note").textContent="";
    try{
      await loadRecommendationCategories();if(generation!==rec.generation)return;
      const category=rec.categories.find(item=>item.id===rec.category);if(!category)throw new Error("暂时没有可用的剧集分类。");
      $("recommendation-panel").style.setProperty("--brand",category.color||"#aab4c8");$("recommendation-watermark").textContent=category.wordmark||category.name;$("recommendation-title").textContent=category.name;
      $("recommendation-subtitle").textContent=category.description||"新剧老剧一起选 · 随机抽样";$("recommendation-panel").setAttribute("aria-labelledby","recommendation-tab-"+category.id);
      $("recommendation-tabs").querySelectorAll("button").forEach(tab=>tab.setAttribute("aria-selected",String(tab.dataset.category===rec.category)));
      const feed=await api("/recommendations/"+category.id+"?nonce="+recommendationNonce()+"&session_id="+browsingSession());if(generation!==rec.generation)return;
      const items=Array.isArray(feed.items)?feed.items:[],source=feed.source||{},platform={...category,url:"https://www.themoviedb.org/"};row.replaceChildren();
      if(items.length){row.append(featureCard(items[0],platform,feed));items.forEach(item=>row.append(posterCard(item,platform,feed)));}
      else{const empty=node("div",null,"empty-state"),copy=node("div");copy.append(node("h3",feedMessage(feed.status)),node("p",feed.message||"这个分类暂时没有可用候选，请稍后再试。"));empty.append(copy);row.append(empty);}
      $("recommendation-note").textContent=[source.label,feed.message,feed.fetched_at?"候选获取于 "+compactDate(feed.fetched_at):null].filter(Boolean).join(" · ");
    }catch(err){if(generation!==rec.generation)return;row.replaceChildren();const empty=node("div",null,"empty-state");empty.append(node("p",err.message));row.append(empty);$("recommendation-note").textContent="可点“换一批”重试。";}
    finally{if(generation===rec.generation){trigger.disabled=false;trigger.textContent="↻ 换一批";}}
  }
  function setMode(mode){
    if(state.mode==="personal"&&mode!=="personal")personal.leave?.();
    if(state.mode==="theater"&&mode!=="theater")theater.leave();
    $("theater-area").hidden=mode!=="theater";
    state.mode=mode;$("platform-area").hidden=mode!=="platforms";$("random-area").hidden=mode!=="random";$("personal-area").hidden=mode!=="personal";document.body.dataset.mode=mode;if(globalThis.MovieClawWorkspace)globalThis.MovieClawWorkspace.setMode(mode);else document.querySelector(".intro").hidden=mode==="personal"||mode==="theater";
    const skip=document.querySelector(".skip-link");skip.href=mode==="theater"?"#theater-wall":mode==="personal"?"#poster-wall":mode==="random"?"#recommendation-row":"#chapters";skip.textContent=mode==="theater"?"跳到录像厅":mode==="personal"?"跳到海报墙":mode==="random"?"跳到随机片单":"跳到平台片单";
    document.querySelectorAll("[data-mode]").forEach(tab=>tab.setAttribute("aria-selected",String(tab.dataset.mode===mode)));remember();
    if(mode==="random")loadRecommendations();
    if(mode==="personal")personal.open();
    if(mode==="theater")theater.open();
    if(mode==="platforms"){state.sections.forEach(parts=>{loadObserver?.observe(parts.section);chapterObserver?.observe(parts.section);});[...state.sections.values()].slice(0,2).forEach(parts=>loadFeed(parts.platform));}
    resumeFeedWatches();if(mode==="platforms"&&Date.now()-state.lastVisibleCheck>=300000)checkVisibleFeeds();
  }
  function renderChapters() {
    state.watchers.forEach(stopFeedWatch);
    state.generation++;loadObserver?.disconnect();chapterObserver?.disconnect();state.sections.clear();
    const visible=state.platforms.filter(platform=>state.scope==="all"||platform.scope===state.scope),fragment=document.createDocumentFragment(),index=document.createDocumentFragment();
    visible.forEach((platform,i)=>{
      fragment.append(makeChapter(platform,i));const tab=button(platform.name,"platform-tab",()=>{$("platform-"+platform.id).scrollIntoView({block:"start",behavior:matchMedia("(prefers-reduced-motion:reduce)").matches?"instant":"smooth"});loadFeed(platform);});tab.dataset.platform=platform.id;tab.style.setProperty("--brand",platform.color);tab.setAttribute("aria-current",String(i===0));index.append(tab);
    });$("chapters").replaceChildren(fragment);$("platform-index").replaceChildren(index);$("page-empty").hidden=visible.length>0;
    document.querySelectorAll("[data-scope]").forEach(button=>button.setAttribute("aria-pressed",String(button.dataset.scope===state.scope)));document.querySelectorAll("[data-kind]").forEach(button=>button.setAttribute("aria-pressed",String(button.dataset.kind===state.kind)));
    loadObserver=new IntersectionObserver(entries=>{if(state.mode!=="platforms")return;entries.filter(entry=>entry.isIntersecting).forEach(entry=>{loadObserver.unobserve(entry.target);const platform=state.platforms.find(item=>item.id===entry.target.dataset.platform);if(platform)loadFeed(platform);});},{rootMargin:"350px 0px"});
    chapterObserver=new IntersectionObserver(entries=>{const active=entries.filter(entry=>entry.isIntersecting).sort((a,b)=>Math.abs(a.boundingClientRect.top-140)-Math.abs(b.boundingClientRect.top-140))[0];if(!active)return;$("platform-index").querySelectorAll("button").forEach(tab=>tab.setAttribute("aria-current",String(tab.dataset.platform===active.target.dataset.platform)));},{rootMargin:"-140px 0px -45% 0px"});
    if(state.mode==="platforms"){state.sections.forEach(parts=>{loadObserver.observe(parts.section);chapterObserver.observe(parts.section);});visible.slice(0,2).forEach(platform=>loadFeed(platform));}
  }
  $("scope-filter").addEventListener("click",event=>{const target=event.target.closest("button[data-scope]");if(!target||state.scope===target.dataset.scope)return;state.scope=target.dataset.scope;remember();renderChapters();});
  $("kind-filter").addEventListener("click",event=>{const target=event.target.closest("button[data-kind]");if(!target||state.kind===target.dataset.kind)return;state.kind=target.dataset.kind;remember();renderChapters();});
  document.querySelectorAll("[data-mode]").forEach(tab=>tab.addEventListener("click",()=>{if(state.mode!==tab.dataset.mode)setMode(tab.dataset.mode);}));
  $("shuffle-recommendations").addEventListener("click",loadRecommendations);
  const moveRecommendation=(direction)=>{const row=$("recommendation-row");row.scrollBy({left:direction*Math.max(200,row.clientWidth*.75),behavior:matchMedia("(prefers-reduced-motion:reduce)").matches?"instant":"smooth"});};
  $("recommendation-previous").addEventListener("click",()=>moveRecommendation(-1));$("recommendation-next").addEventListener("click",()=>moveRecommendation(1));
  $("detail-close").addEventListener("click",()=>{if(!detailSubscription?.isSubmitting())$("detail-dialog").close();});$("detail-dialog").addEventListener("click",event=>{if(event.target===$("detail-dialog")&&!detailSubscription?.isSubmitting())$("detail-dialog").close();});
  $("detail-dialog").addEventListener("cancel",event=>{if(detailSubscription?.isSubmitting()){event.preventDefault();announce("正在创建订阅，请等待服务器确认。");}});
  $("detail-dialog").addEventListener("close",()=>{detailGeneration++;detailReadController?.abort();detailSubscription?.dispose();detailSubscription=null;$("detail-close").disabled=false;document.body.classList.remove("detail-open");});
  $("detail-previous").addEventListener("click",()=>moveDetail(-1));$("detail-next").addEventListener("click",()=>moveDetail(1));
  $("detail-copy").addEventListener("click",async()=>{try{await navigator.clipboard.writeText($("detail-header-title").textContent);announce("片名已复制。");}catch{announce("暂时无法复制，请选择片名后手动复制。");}});
  document.addEventListener("keydown",event=>{if(!$("detail-dialog").open||event.altKey||event.ctrlKey||event.metaKey||event.shiftKey||event.target.closest?.("input,select,textarea,[contenteditable=true],.inline-subscription,.detail-people"))return;if(event.key==="ArrowLeft"||event.key==="ArrowRight"){event.preventDefault();moveDetail(event.key==="ArrowLeft"?-1:1);}});
  document.addEventListener("visibilitychange",()=>{resumeFeedWatches();if(!document.hidden&&Date.now()-state.lastVisibleCheck>=300000)checkVisibleFeeds();});
  setInterval(checkVisibleFeeds,300000);
  $("proxy-test").addEventListener("click",async()=>{
    if(preview){announce("设计预览不发起网络测试。安装后的页面可测试 Netflix 连通性。");return;}
    const trigger=$("proxy-test"),result=$("proxy-result");trigger.disabled=true;trigger.textContent="检测中…";result.textContent="";
    try{const test=await api("/connection-test");result.textContent=test.ok?"可连接":"连接失败";result.className=test.ok?"connection-ok":"connection-error";announce(test.message+(test.latency_ms!=null?" · "+test.latency_ms+" ms":""));}
    catch(err){result.textContent="检测失败";result.className="connection-error";announce(err.message);}
    finally{trigger.disabled=false;trigger.textContent="测试连接";}
  });
  async function start() {
    syncTheme();loadProxyStatus();
    if(preview)$("preview-banner").hidden=false;
    try{const saved=JSON.parse(sessionStorage.getItem("movieclaw.platform-discovery")||"null");if(saved&&["all","domestic","global"].includes(saved.scope))state.scope=saved.scope;if(saved&&["all","tv","variety","movie"].includes(saved.kind))state.kind=saved.kind;if(saved?.category)rec.category=saved.category;}catch{}
    const requestedMode=new URLSearchParams(location.search).get("view"),initialMode=["random","personal","platforms","theater"].includes(requestedMode)?requestedMode:"personal";setMode(initialMode);
    try{
      const catalog=await api("/catalog");state.platforms=catalog.platforms||[];state.region=catalog.region||"US";$("region-label").textContent="海外片库地区 · "+state.region;
      renderChapters();
      const footer=$("attribution");footer.replaceChildren(node("span","平台名称及标识归各自权利人所有。 "));
      const logoLink=externalLink("","https://www.themoviedb.org/","tmdb-credit"),logo=node("img");logo.src="assets/tmdb-logo.svg";logo.alt="TMDB";logo.width=80;logo.height=11;logoLink.append(logo);footer.append(logoLink);
      if(catalog.attribution){const label=node("span",catalog.attribution);footer.append(label);}else{footer.append(node("span","平台片库数据：TMDB / JustWatch。This product uses the TMDB API but is not endorsed or certified by TMDB. "));}
      [externalLink("TMDB","https://www.themoviedb.org/"),externalLink("JustWatch","https://www.justwatch.com/")].filter(Boolean).forEach(link=>{footer.append(node("span"," · "),link);});
    }catch(err){$("page-error").textContent=err.message;$("page-error").hidden=false;$("intro-status").textContent="片单尚未连接";}
  }
  start();
})();
