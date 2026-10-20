"use strict";
// Library data always uses the browser's current same-origin MovieClaw session.
globalThis.MovieClawTheater={
 create({node,button,announce}){
  const $=id=>document.getElementById(id),base="/api/v1/plugins/platform-discovery",request=globalThis.MovieClawTransport.request.bind(globalThis.MovieClawTransport);
  const defaults={kind:"movie",library:"",collection:"",g:"",c:"",d:"",w:"",res:"",rating_gte:"",sort:"added_at"};
  const state={active:false,account:null,epoch:0,generation:0,detail:0,libraries:[],collections:[],filters:{...defaults},revision:"",busy:false,saving:false,offset:0,more:true,items:[],cards:new Map(),tasks:[],working:0,queued:new Set(),controller:null,prewarm:false,warmGeneration:0,warmTotal:0,warmDone:0};
  let observer,checkTimer;
  const itemPath=item=>"/libraries/"+Number(item.library_id)+"/items/"+Number(item.media_item_id);
  const artPath=item=>"/theater"+itemPath(item);
  const live=(epoch,generation)=>state.active&&state.epoch===epoch&&(generation===undefined||state.generation===generation);
  const accountOptions=options=>({...options,headers:{...(options?.headers||{}),...(state.account?{"X-Platform-Account":state.account}:{})}});
  const api=(path,options={})=>request(base+path,accountOptions(options),60000);
  const native=(path,options={})=>request("/api/v1"+path,options,45000);
  function posterUrl(value,width=400){
   if(typeof value!=="string")return "";
   let url;if(/^https:\/\//.test(value)){try{const parsed=new URL(value);if(parsed.username||parsed.password)return "";}catch{return "";}url="/api/v1/images/proxy?url="+encodeURIComponent(value);}
   else if(/^\/(?:api\/v1\/)?(?:images|libraries)\//.test(value)&&!/[\\\r\n]/.test(value))url=value.startsWith("/api/v1/")?value:"/api/v1"+value;
   else return "";
   return url+(url.includes("?")?"&":"?")+"w="+width;
  }
  function imageUrl(item,key,full=false){return base+artPath(item)+"/images/"+key+(full?"?full=true":"");}
  function makeImage(src,title,priority=false){const image=node("img");image.alt=title;image.decoding="async";image.loading=priority?"eager":"lazy";if(priority)image.fetchPriority="high";image.src=src;return image;}
  function errorText(error){return error?.message||"读取失败，请稍后重试。";}
  async function identity(){const me=await native("/auth/me");return String(me.role)+":"+String(me.username);}
  function resetAccount(account){
   state.epoch++;state.generation++;state.detail++;state.controller?.abort();observer?.disconnect();state.tasks=[];state.queued.clear();state.items=[];state.cards.clear();state.libraries=[];state.collections=[];state.prewarm=false;state.warmGeneration++;state.account=account;
   state.filters={...defaults};state.revision="";$("theater-wall").replaceChildren();$("theater-detail-content").replaceChildren();$("theater-art-content").replaceChildren();
   for(const id of ["theater-detail-dialog","theater-art-dialog"])if($(id).open)$(id).close();renderControls();
  }
  function renderControls(){
   $("theater-kind").value=state.filters.kind;$("theater-sort").value=state.filters.sort;
   const select=$("theater-library");select.replaceChildren();const any=node("option","全部可见媒体库");any.value="";select.append(any);
   for(const row of state.libraries.filter(x=>x.kind===state.filters.kind)){const option=node("option",row.name);option.value=String(row.id);select.append(option);}
   select.value=state.filters.library;if(select.value!==state.filters.library)state.filters.library="";
   const cs=$("theater-collection");cs.replaceChildren();const ca=node("option","全部影片");ca.value="";cs.append(ca);
   const permitted=new Set(state.libraries.filter(x=>x.kind===state.filters.kind&&(!state.filters.library||String(x.id)===state.filters.library)).map(x=>x.id));
   for(const row of state.collections.filter(x=>x.library_id===null||permitted.has(x.library_id))){const o=node("option",row.name);o.value=String(row.id);cs.append(o);}
   if(state.filters.collection&&!state.collections.length){const o=node("option","正在读取已选合集…");o.value=state.filters.collection;cs.append(o);}cs.value=state.filters.collection;
   const parts=[];for(const [key,label]of [["g","题材"],["c","地区"],["d","年代"],["res","画质"],["w","观看状态"],["rating_gte","评分"]])if(state.filters[key])parts.push(label);
   $("theater-filter-open").textContent=parts.length?"筛选 · "+parts.join("、"):"分类与筛选";
  }
  async function open(){
   state.active=true;const oldEpoch=state.epoch;
   try{
    const account=await identity();if(!state.active||oldEpoch!==state.epoch)return;
    const changed=account!==state.account;if(changed)resetAccount(account);
    const epoch=state.epoch;const [prefs,libraries]=await Promise.all([api("/theater/preferences"),native("/libraries?scope=visible")]);if(!live(epoch))return;
    state.libraries=(Array.isArray(libraries)?libraries:[]).filter(x=>["movie","tv"].includes(x.kind)&&x.viewer_access!==false);
    const refresh=changed||state.revision!==prefs.revision||!state.items.length;
    state.filters={...defaults,...prefs.filters};state.revision=prefs.revision;renderControls();
    native("/collections" ).then(rows=>{if(live(epoch)){state.collections=Array.isArray(rows)?rows:[];renderControls();}}).catch(()=>{});
    if(refresh)await load(true);else resumePosition();
    clearInterval(checkTimer);checkTimer=setInterval(()=>{if(state.active&&!document.hidden&&!state.saving)checkAccount();},30000);
   }catch(error){if(state.active){$("theater-status").textContent=errorText(error);$("theater-retry").hidden=false;}}
  }
  async function checkAccount(){if(state.busy||state.saving)return;try{const account=await identity();if(account!==state.account){resetAccount(account);return open();}const epoch=state.epoch,prefs=await api("/theater/preferences");if(!live(epoch))return;if(prefs.revision!==state.revision){state.revision=prefs.revision;state.filters={...defaults,...prefs.filters};renderControls();await load(true);}}catch{/* Existing wall stays visible on temporary network failure. */}}
  function leave(){state.active=false;state.controller?.abort();state.prewarm=false;state.warmGeneration++;observer?.disconnect();state.tasks=[];clearInterval(checkTimer);}
  function query(limit=24,offset=state.offset){const q=new URLSearchParams({limit:String(limit),offset:String(offset),sort:state.filters.sort});for(const key of ["g","c","d","w","res","rating_gte"])if(state.filters[key])q.set(key,state.filters[key]);return q;}
  function wallPath(){return state.filters.library?"/libraries/"+state.filters.library:"/libraries/kinds/"+state.filters.kind;}
  function readItems(limit,offset,options={}){const q=query(limit,offset);if(!state.filters.collection)return native(wallPath()+"/items?"+q,options);q.set("kind",state.filters.kind);if(state.filters.library)q.set("library_id",state.filters.library);return api("/theater/collections/"+state.filters.collection+"/items?"+q,options);}
  async function load(reset=false){
   if(state.busy&&!reset)return;const epoch=state.epoch,generation=reset?++state.generation:state.generation;
   if(reset){state.controller?.abort();observer?.disconnect();state.tasks=[];state.queued.clear();state.prewarm=false;state.warmGeneration++;$("theater-warm").textContent="预缓存前 100 部";state.offset=0;state.more=true;state.items=[];state.cards.clear();$("theater-wall").replaceChildren();}
   state.controller=new AbortController();const signal=state.controller.signal;state.busy=true;$("theater-more").disabled=true;$("theater-status").textContent="正在读取库存…";$("theater-retry").hidden=true;
   try{
    const data=await readItems(24,state.offset,{signal});if(!live(epoch,generation))return;
    const rows=Array.isArray(data)?data:[];state.offset+=rows.length;state.more=rows.length===24;
    const fresh=rows.filter(item=>!state.cards.has(item.media_item_id));state.items.push(...fresh);
    for(const item of fresh){const card=renderCard(item,state.cards.size);state.cards.set(item.media_item_id,{item,card});$("theater-wall").append(card);}
    $("theater-status").textContent=state.items.length?"已显示 "+state.items.length+" 部 · 仅当前账号可见库存 · 海报后台选优缓存":"这个分类下暂无库存影片。可调整筛选后重试。";
    $("theater-more").hidden=!state.more;watchCards(epoch,generation);resumePosition();
   }catch(error){if(error.name!=="AbortError"&&live(epoch,generation)){$("theater-status").textContent=errorText(error);$("theater-retry").hidden=false;}}
   finally{if(live(epoch,generation)){state.busy=false;$("theater-more").disabled=false;}}
  }
  function renderCard(item,index){
   const card=node("article",null,"theater-card");card.dataset.mediaItemId=item.media_item_id;
   const cover=button("","theater-cover",()=>showDetail(item));cover.setAttribute("aria-label","查看 "+item.title);const placeholder=node("span",item.title,"theater-placeholder");cover.append(placeholder);
   const src=posterUrl(item.poster_url);if(src){const img=makeImage(src,item.title,index<8);img.addEventListener("load",()=>placeholder.hidden=true);img.addEventListener("error",()=>{img.hidden=true;placeholder.hidden=false;});cover.append(img);}
   const meta=node("div",null,"theater-card-top");if(Number.isFinite(item.rating)&&item.rating>0){const rating=node("span",Number(item.rating).toFixed(1),"theater-rating");rating.append(node("small"," /10"));rating.title="库存评分（TMDB 或本地资料）";meta.append(rating);}if(item.year)meta.append(node("span",item.year,"theater-year"));
   const status=item.kind==="tv"?({ended:"完结",airing:"在播"}[item.air_status]||"剧集"):item.resolutions?.[0]||"电影";
   meta.append(node("span",status,"theater-badge"));cover.append(meta);
   if(item.missing_count||item.missing_episode_count)cover.append(node("span",item.missing_count?"部分文件失联":"库存缺集","theater-stock"));
   card.append(cover,node("h3",item.title),node("p",[item.year,item.kind==="tv"?(item.episode_count||0)+" 集":item.resolutions?.join(" / ")].filter(Boolean).join(" · "),"theater-card-note"));
   const actions=node("div",null,"theater-card-actions"),art=button("选海报","button ghost",()=>chooseArt(item));actions.append(art);card.append(actions);return card;
  }
  function watchCards(epoch,generation){
   observer?.disconnect();observer=new IntersectionObserver(entries=>{for(const entry of entries)if(entry.isIntersecting){observer.unobserve(entry.target);const row=state.cards.get(Number(entry.target.dataset.mediaItemId));if(row)enqueue(row.item,epoch,generation);}}, {rootMargin:"200px 0px"});
   for(const row of state.cards.values())observer.observe(row.card);
  }
  function enqueue(item,epoch,generation,warm=false){
   const id=epoch+":"+generation+":"+item.media_item_id;if(state.queued.has(id))return false;
   state.queued.add(id);state.tasks.push({item,epoch,generation,warm,warmGeneration:state.warmGeneration});drain();return true;
  }
  function drain(){while(state.working<2&&state.tasks.length){const task=state.tasks.shift();if(!live(task.epoch,task.generation))continue;state.working++;optimize(task).catch(()=>{}).finally(()=>{state.working--;drain();});}}
  async function optimize({item,epoch,generation,warm,warmGeneration}){
   try{
    let art=await api(artPath(item)+"/art");if(!live(epoch,generation))return;
    if(!art.key&&!art.default){art=await api(artPath(item)+"/art",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({operation:"auto"})});if(!live(epoch,generation))return;}
    if(art.key)applyArt(item,art.key,epoch,generation);
   }finally{if(warm&&state.prewarm&&warmGeneration===state.warmGeneration&&live(epoch,generation)){state.warmDone++;warmStatus();}}
  }
  function applyArt(item,key,epoch=state.epoch,generation=state.generation){
   const row=state.cards.get(item.media_item_id);if(!row)return;
   // Keep the old successful poster until the replacement actually decodes.
   const image=makeImage(imageUrl(item,key),item.title,true);image.dataset.posterCache="local";
   image.addEventListener("load",()=>{if(!live(epoch,generation)||!row.card.isConnected)return;const old=row.card.querySelector(".theater-cover img");if(old)old.replaceWith(image);else row.card.querySelector(".theater-cover").append(image);row.card.querySelector(".theater-placeholder").hidden=true;},{once:true});
   image.addEventListener("error",()=>{/* Original source remains on screen. */},{once:true});
  }
  async function saveFilters(filters){
   if(state.saving)return;state.saving=true;const epoch=state.epoch;$("theater-filter-apply").disabled=true;
   try{
    if(await identity()!==state.account){resetAccount(null);throw new Error("登录账号已切换，请重新进入录像厅。");}
    const saved=await api("/theater/preferences",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({revision:state.revision,filters})});if(!live(epoch))return;
    state.filters={...defaults,...saved.filters};state.revision=saved.revision;renderControls();if($("theater-filter-dialog").open)$("theater-filter-dialog").close();state.prewarm=false;state.warmGeneration++;await load(true);
   }catch(error){if(live(epoch)){$("theater-filter-error").textContent=errorText(error);announce(errorText(error));if(error.status===409)await open();}}
   finally{state.saving=false;$("theater-filter-apply").disabled=false;}
  }
  async function filters(){
   const epoch=state.epoch,generation=state.generation;$("theater-filter-content").replaceChildren(node("p","读取当前媒体库可选分类…"));$("theater-filter-error").textContent="";$("theater-filter-dialog").showModal();
   try{
    // Merge facets from visible libraries for the selected kind, never hidden stock.
    const libraries=state.libraries.filter(x=>x.kind===state.filters.kind&&(!state.filters.library||String(x.id)===state.filters.library));
    const maps={genres:new Map(),countries:new Map(),decades:new Map(),resolutions:new Map()};
    for(let start=0;start<libraries.length;start+=3){
     const batch=await Promise.all(libraries.slice(start,start+3).map(row=>native("/libraries/"+row.id+"/facets?tier=all")));
     if(!live(epoch,generation)||!$("theater-filter-dialog").open)return;
     for(const facets of batch)for(const dimension of Object.keys(maps))for(const value of facets[dimension]||[]){const old=maps[dimension].get(String(value.value));maps[dimension].set(String(value.value),{...value,count:(old?.count||0)+value.count});}
    }
    const root=$("theater-filter-content");root.replaceChildren();
    for(const [dimension,key,label]of [["genres","g","题材"],["countries","c","国家／地区"],["decades","d","年代"],["resolutions","res","画质"]]){
     const field=node("fieldset",null,"theater-facet");field.append(node("legend",label+" · 多选"));const list=node("div",null,"theater-facet-options"),chosen=new Set(state.filters[key].split(","));
     for(const row of maps[dimension].values()){const wrap=node("label"),input=node("input");input.type="checkbox";input.name="theater-"+key;input.value=String(row.value);input.checked=chosen.has(input.value);wrap.append(input,node("span",row.label));list.append(wrap);}field.append(list);root.append(field);
    }
    const watch=node("label","观看状态 "),select=node("select");select.id="theater-watch";for(const [value,label]of [["","全部（含已看过）"],["unwatched","未看"],["watching","在看"],["played","已看完"],["favorite","主程序收藏"]]){const option=node("option",label);option.value=value;select.append(option);}select.value=state.filters.w;watch.append(select);
    const rating=node("label","最低评分 "),r=node("select");r.id="theater-min-rating";for(const value of ["","6","7","8","9"]){const option=node("option",value?value+" 分":"不限");option.value=value;r.append(option);}r.value=state.filters.rating_gte;rating.append(r);root.append(watch,rating);
   }catch(error){if(live(epoch))$("theater-filter-error").textContent=errorText(error);}
  }
  function episodeTitle(row){
   if(typeof row?.name!=="string")return "";
   const name=row.name.normalize("NFKC").replace(/\s+/g," ").trim();
   if(!name||/^(?:第\s*\d+\s*[集话話回]|(?:episode|ep)\.?\s*\d+|e\d+|s\d+e\d+)$/i.test(name))return "";
   // Release filenames may be supplied as episode names by local inventory.
   // Require an extension or multiple independent release markers; keep real titles.
   if(/\.(?:mkv|mp4|avi|m4v|ts|m2ts|mov|strm)$/i.test(name)||/^(?:[A-Za-z]:[\\/]|\/(?:media|mnt|volume)\/)/i.test(name))return "";
   const markers=[/\bs\d{1,3}[ ._-]*e\d{1,4}\b/i,/\b(?:720|1080|2160|4320)p\b/i,/\b(?:web[ ._-]?dl|webrip|bluray|bdrip|hdtv|remux)\b/i,/\b(?:x26[45]|h[ ._-]?26[45]|hevc|avc)\b/i,/\b(?:ddp|dts|aac|truehd)(?:\b|\d)/i];
   if(markers.filter(pattern=>pattern.test(name)).length>=2)return "";
   return name;
  }
  function episodeLabel(row,resume){
   const title=episodeTitle(row);
   return (row.episode_number===resume?"继续 · ":"")+"第 "+row.episode_number+" 集"+(title?" · "+title:"")+(row.played?" · 已看完":"");
  }
  function playLink(item,episode=null){let url="/play/"+Number(item.media_item_id);if(episode)url+="/s"+String(episode.season).padStart(2,"0")+"e"+String(episode.episode_number).padStart(2,"0");if(episode?.position_ms>0&&!episode.played)url+="?t="+Math.floor(episode.position_ms/1000);return url;}
  function playback(label,item,episode=null){
   const a=node("a",label,"button primary");a.href=playLink(item,episode);a.target="_top";if(episode){a.className+=" theater-episode";a.title=label;a.setAttribute("aria-label",label);a.replaceChildren(node("span",label,"theater-episode-label"));}a.addEventListener("click",()=>{try{sessionStorage.setItem("movieclaw.player.return-to",globalThis.MovieClawNavigation?.isNative()?"/apps/platform-discovery/theater":base+"/panel?view=theater");sessionStorage.setItem("movieclaw.theater-return",JSON.stringify({account:state.account,scroll:window.scrollY,media_item_id:item.media_item_id,filters:state.filters}));}catch{}});return a;
  }
  function resumePosition(){try{const saved=JSON.parse(sessionStorage.getItem("movieclaw.theater-return")||"null");if(saved?.account===state.account&&JSON.stringify(saved.filters)===JSON.stringify(state.filters)){const row=state.cards.get(saved.media_item_id);if(row){row.card.scrollIntoView({block:"center"});sessionStorage.removeItem("movieclaw.theater-return");}}}catch{}}
  async function showDetail(item){
   const epoch=state.epoch,detail=++state.detail,root=$("theater-detail-content");root.replaceChildren(node("p","读取库存详情…"));$("theater-detail-dialog").showModal();
   try{
    const data=await native(itemPath(item));if(!live(epoch)||detail!==state.detail||!$("theater-detail-dialog").open)return;
    root.replaceChildren();const layout=node("div",null,"theater-detail-layout"),art=node("div"),main=node("div");const cached=await api(artPath(item)+"/art").catch(()=>null);if(!live(epoch)||detail!==state.detail)return;
    const src=cached?.key?imageUrl(item,cached.key,true):posterUrl(data.poster_url,1000);if(src){const image=makeImage(src,item.title,true);image.className="theater-detail-poster";const expand=node("a");expand.href=src;expand.target="_blank";expand.rel="noopener noreferrer";expand.append(image);art.append(expand);}
    main.append(node("h2",data.title),node("p",[data.year,data.kind==="tv"?"剧集":"电影",item.resolutions?.join(" / ")].filter(Boolean).join(" · "),"theater-detail-meta"));
    const meta=data.local_meta||{};main.append(node("p",meta.plot||"本地资料暂无简介。","theater-overview"));if(meta.genres?.length)main.append(node("p",meta.genres.join(" / ")));if(meta.directors?.length)main.append(node("p","导演："+meta.directors.join("、")));if(meta.actors?.length)main.append(node("p","参演："+meta.actors.slice(0,16).map(p=>p.name+(p.role?"（"+p.role+"）":"")).join("、")));
    const actions=node("div",null,"theater-detail-actions"),inPlace=(data.files||[]).some(file=>file.state==="in_place");
    if(data.kind==="movie"){if(inPlace)actions.append(playback("▶ 在 MovieClaw 观看",item));else actions.append(node("p","没有可播放的在位文件，请在主程序检查库存。"));}
    const nativeLink=node("a","主程序库存详情 ↗","button");nativeLink.href="/library/"+item.library_id+"/item/"+item.media_item_id;nativeLink.target="_top";actions.append(nativeLink,button("挑选海报","button",()=>chooseArt(item)));main.append(actions);
    if(data.kind==="tv"){
     const label=node("label","选择季 "),season=node("select");season.id="theater-season";
     const seasons=(data.seasons||item.seasons||[]).map(s=>typeof s==="number"?s:s.season_number).filter(Number.isInteger);
     for(const n of seasons){const option=node("option",n===0?"特别篇":"第 "+n+" 季");option.value=n;season.append(option);}label.append(season);main.append(label);
     const episodes=node("div",null,"theater-episodes");main.append(episodes);
     let seasonGen=0;
     const readSeason=async()=>{const sg=++seasonGen;episodes.replaceChildren(node("p","读取分集…"));try{const result=await native(itemPath(item)+"/episodes?season_number="+season.value);if(!live(epoch)||detail!==state.detail||sg!==seasonGen)return;episodes.replaceChildren();for(const row of result.episodes||[]){if(row.owned)episodes.append(playback(episodeLabel(row,result.resume_episode),item,{...row,season:Number(season.value)}));else episodes.append(node("span","第 "+row.episode_number+" 集 · 缺集","theater-missing"));}}catch(error){if(live(epoch)&&sg===seasonGen)episodes.replaceChildren(node("p",errorText(error)));}};
     season.addEventListener("change",readSeason);if(seasons.length)readSeason();else episodes.append(node("p","暂无可用季集，请在主程序检查库存。"));
    }
    layout.append(art,main);root.append(layout);
   }catch(error){if(live(epoch)&&detail===state.detail)root.replaceChildren(node("p",errorText(error)));}
  }
  async function chooseArt(item){
   const epoch=state.epoch,detail=++state.detail,root=$("theater-art-content");root.replaceChildren(node("p","按作品编号读取 TMDB／Fanart.tv 海报…"));$("theater-art-dialog").showModal();
   try{
    const result=await api(artPath(item)+"/candidates");if(!live(epoch)||detail!==state.detail||!$("theater-art-dialog").open)return;
    root.replaceChildren(node("h3",item.title),node("p",(result.locked?"当前海报已手动锁定。":"自动选优海报。")+" 中文优先，兼顾画幅、清晰度和图片票数；不修改媒体库文件。"),node("p",result.message||""));
    const status=node("p",null,"theater-art-status"),actions=node("div",null,"theater-detail-actions");let busy=false;
    const change=async action=>{if(busy)return;busy=true;root.querySelectorAll("button").forEach(b=>b.disabled=true);try{if(await identity()!==state.account)throw new Error("登录账号已切换，请重新打开。所选海报未提交。");const saved=await api(artPath(item)+"/art",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({...action,revision:result.revision})});if(!live(epoch)||detail!==state.detail)return;if(!saved.key&&action.operation!=="reset")throw new Error(saved.message||"没有可用海报，原海报已保留。");if(saved.key)applyArt(item,saved.key);else{const card=state.cards.get(item.media_item_id)?.card,img=card?.querySelector(".theater-cover img");if(img&&posterUrl(item.poster_url))img.src=posterUrl(item.poster_url);}announce(saved.key?"海报已缓存到服务器，同账号跨设备可用。":"已恢复媒体库原海报。");$("theater-art-dialog").close();}catch(error){if(live(epoch)&&detail===state.detail)status.textContent=errorText(error);}finally{busy=false;root.querySelectorAll("button").forEach(b=>b.disabled=false);}};
    actions.append(button("恢复媒体库原海报","button",()=>change({operation:"reset"})));if(!result.locked)actions.append(button("重新自动选优","button",()=>change({operation:"refresh"})));root.append(actions,status);
    if(result.matches?.length){const matches=node("div",null,"theater-match-options");matches.append(node("p","没有可靠作品编号，请确认匹配；同名作品不会自动套用。"));for(const match of result.matches)matches.append(button(match.title+" · "+(match.year||"年份未知"),"button",()=>change({operation:"match",tmdb_id:match.id})));root.append(matches);}
    const list=node("div",null,"theater-art-grid");for(const c of result.candidates||[]){const card=button("","theater-art-option",()=>change({operation:"choose",candidate_id:c.id}));const src=posterUrl(c.url,300);if(src)card.append(makeImage(src,item.title));card.append(node("span",c.source+" · "+(c.language||"无文字语言")+" · "+c.width+"×"+c.height),node("small","图片票数 "+c.votes+" · 选用后锁定"));list.append(card);}root.append(list);if(!result.candidates?.length)root.append(node("p","暂无可选图片，媒体库原海报继续显示。"));
   }catch(error){if(live(epoch)&&detail===state.detail)root.replaceChildren(node("p",errorText(error)));}
  }
  function warmStatus(){$("theater-warm-status").textContent=state.prewarm?"预缓存 "+state.warmDone+" / "+state.warmTotal:"后台任务已暂停；已缓存海报保留。";if(state.prewarm&&state.warmDone>=state.warmTotal){state.prewarm=false;$("theater-warm").textContent="预缓存前 100 部";$("theater-warm-status").textContent="已处理 "+state.warmDone+" 部；可在缓存状态查看实际保存数量。";}}
  async function prewarm(){
   if(state.prewarm){state.prewarm=false;state.warmGeneration++;state.tasks=state.tasks.filter(t=>!t.warm);$("theater-warm").textContent="预缓存前 100 部";warmStatus();return;}
   state.prewarm=true;const epoch=state.epoch,generation=state.generation,warmGeneration=++state.warmGeneration;state.warmDone=0;state.warmTotal=100;$("theater-warm").textContent="暂停预缓存";warmStatus();
   try{const rows=await readItems(100,0);if(!live(epoch,generation)||!state.prewarm||state.warmGeneration!==warmGeneration)return;state.warmTotal=rows.length;for(const item of rows)if(!enqueue(item,epoch,generation,true)){state.warmDone++;}warmStatus();}catch(error){state.prewarm=false;$("theater-warm").textContent="预缓存前 100 部";$("theater-warm-status").textContent=errorText(error);}
  }
  $("theater-kind").addEventListener("change",()=>saveFilters({...defaults,kind:$("theater-kind").value}));
  $("theater-library").addEventListener("change",()=>saveFilters({...state.filters,library:$("theater-library").value,collection:""}));
  $("theater-collection").addEventListener("change",()=>saveFilters({...state.filters,collection:$("theater-collection").value}));
  $("theater-sort").addEventListener("change",()=>saveFilters({...state.filters,sort:$("theater-sort").value}));
  $("theater-filter-open").addEventListener("click",filters);$("theater-more").addEventListener("click",()=>load());$("theater-retry").addEventListener("click",open);$("theater-warm").addEventListener("click",prewarm);
  $("theater-filter-apply").addEventListener("click",()=>{if(!$("theater-watch"))return;const next={...state.filters};for(const key of ["g","c","d","res"])next[key]=[...document.querySelectorAll('input[name="theater-'+key+'"]:checked')].map(i=>i.value).sort().join(",");next.w=$("theater-watch").value;next.rating_gte=$("theater-min-rating").value;saveFilters(next);});
  $("theater-filter-reset").addEventListener("click",()=>saveFilters({...defaults,kind:state.filters.kind,library:state.filters.library}));
  for(const name of ["filter","detail","art"]){$("theater-"+name+"-close").addEventListener("click",()=>$("theater-"+name+"-dialog").close());}
  $("theater-art-dialog").addEventListener("cancel",event=>{if($("theater-art-content").querySelector("button:disabled"))event.preventDefault();});
  $("theater-cache-open").addEventListener("click",async()=>{try{const result=await api("/theater/cache");announce("服务器海报缓存："+result.images+" 张 · "+(result.bytes/1048576).toFixed(1)+" MB / "+Math.round(result.capacity_bytes/1048576)+" MB · 锁定 "+result.pinned+" 张");}catch(error){announce(errorText(error));}});
  window.addEventListener("focus",()=>{if(state.active)checkAccount();});window.addEventListener("pagehide",leave);
  return {open,leave,playLink,posterUrl,episodeTitle,episodeLabel};
 }
};
