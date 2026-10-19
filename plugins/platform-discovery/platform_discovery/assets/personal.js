"use strict";
// 服务端从宿主 Principal 隔离账号；浏览器用户名只用于切换检测和旧档案迁移。
globalThis.MovieClawPersonal = {
  preferredColumns({width,gap=13,viewportWidth=width}){
    return viewportWidth<=767?2:Math.max(2,Math.min(24,Math.floor((width+gap)/(220+gap))||1));
  },
  grid({width,height,top,columns,gap=0,mosaic=false,fitted=false}) {
    if(fitted){
      const target=width<=767?width/2:240,desired=Math.min(24,Math.max(2,Math.round(width/target)));let best,score=Infinity;
      for(let cols=1;cols<=24;cols++)for(let rows=Math.ceil(3/cols);rows<=Math.floor(96/cols);rows++){
        const tileWidth=Math.min(width/cols,height/rows/1.5),coverage=tileWidth*tileWidth*1.5*cols*rows/(width*height);
        const cost=1-coverage+.25*Math.abs(Math.log(tileWidth/target))+.03*Math.abs(cols-desired);
        if(cost<score){score=cost;best={columns:cols,rows,count:cols*rows,height,tileWidth};}
      }
      return best;
    }
    columns=Math.max(1,Math.min(24,Math.floor(columns)||1));
    const available=Math.max(mosaic&&top>0&&height<650?Math.min(260,height*.65):width<=767?Math.min(height*.65,width/columns*3):1,height-Math.max(0,top));
    if(mosaic&&width>767){
      const desired=columns,minimum=Math.max(2,Math.min(24,Math.ceil(width/300))),maximum=Math.min(24,Math.max(minimum,Math.floor(width/200)));
      let score=Infinity;
      for(let candidate=minimum;candidate<=maximum;candidate++){
        const ph=width/candidate*1.5,rows=Math.ceil((available+8)/ph);
        if(rows*candidate>96)continue;
        const value=(rows*ph-available-8)/available+Math.abs(candidate-desired)*.025;
        if(value<score){score=value;columns=candidate;}
      }
      // Very large/tall displays may need fewer, larger columns to stay within
      // the server's 96-title batch limit while still covering the whole field.
      if(!Number.isFinite(score))for(let candidate=minimum-1;candidate>=1;candidate--){
        if(candidate*Math.ceil((available+8)/(width/candidate*1.5))<=96){columns=candidate;break;}
      }
    }
    const posterHeight=Math.max(1,(width-gap*(columns-1))/columns*1.5);
    const rows=Math.max(Math.ceil(3/columns),Math.min(Math.floor(96/columns),(mosaic?Math.ceil((available+8)/posterHeight):Math.round(available/posterHeight))||1));
    return {columns,rows,count:columns*rows,height:Math.round(mosaic?Math.min(available,Math.max(1,rows*posterHeight-8)):available)};
  },
  columnize(items,columns){return Array.from({length:columns},(_,col)=>items.filter((_,i)=>i%columns===col));},
  create({api,node,button,image,showDetail,compactDate,announce}) {
    const $=id=>document.getElementById(id);
    const state={token:"",profile:null,schema:null,ready:null,loaded:false,loading:false,pendingReload:false,step:0,answers:{},submitting:false,motion:true,seamless:false,observer:null,generation:0,feed:null,layout:null,resizeTimer:null,immersive:false,nativeFullscreen:false,controlsTimer:null,scrollBeforeFullscreen:0,preload:true};
    let tools=null,ambient=null;state.ambientEnabled=true;state.genres="all";state.excludeGenres="all";state.exploration="balanced";
    state.filtersRevision="";state.filtersReadAt=0;state.filtersPromise=null;state.filtersSaving=false;state.filterEpoch=0;state.filterDraftRevision="";
    state.mediaType="all";state.era="all";state.country="all";state.quickLabels=new Set();state.activeQuick=null;state.feedbackPending=false;state.shelfReadAt=0;state.shelfReadPromise=null;
    state.shelf={saved:[],dismissed:[],watched:[],liked:[]};state.shelfEpoch=0;state.initialized=false;state.account="";state.checking=null;state.feedbackTail=Promise.resolve();state.syncPromise=null;state.syncedAt=0;state.admin=false;state.profileReadAt=0;state.profilePromise=null;state.syncTimer=null;
    state.nextEpoch=0;state.nextWalls=[];state.nextAbort=null;state.refillAfter=0;state.refillTimer=null;
    Object.defineProperty(state,"nextWall",{get:()=>state.nextWalls[0]||null,set:value=>{state.nextWalls=value?[value]:[];}});
    const nonce=()=>globalThis.MovieClawTransport.nonce();
    state.queueId="";
    async function request(path,options={}) {
      const headers={"X-Platform-Account":state.account,...(path.startsWith("recommendations")?{"X-Platform-Queue":state.queueId||(state.queueId=nonce()),"X-Platform-Filter-Revision":state.filtersRevision}:{}),...(options.body?{"Content-Type":"application/json"}:{}),...options.headers};
      return api("/personal/"+path,{...options,headers});
    }
    function initialize(){
      if(state.checking)return state.checking;
      state.checking=initializeCurrent().finally(()=>{state.checking=null;});return state.checking;
    }
    async function initializeCurrent() {
      if(state.ready&&!state.initialized)return state.ready;
      let me;
      if(state.initialized){
        me=await globalThis.MovieClawTransport.request("/api/v1/auth/me");
        if(state.account===String(me.role)+":"+me.username){await refreshProfile();await refreshFilters();tools?.refreshShortcuts();return state.ready;}
        cancelNext();state.ready=null;state.initialized=false;state.token="";state.profile=null;state.feed=null;state.loaded=false;state.shelf={saved:[],dismissed:[],watched:[],liked:[]};state.shelfEpoch++;state.generation++;state.syncedAt=0;state.syncPromise=null;state.profileReadAt=0;state.profilePromise=null;state.filtersRevision="";state.filtersReadAt=0;state.filtersPromise=null;state.filterEpoch++;state.mediaType="all";state.country="all";state.era="all";state.genres="all";state.excludeGenres="all";state.exploration="balanced";tools?.reset();state.shelfReadAt=0;state.shelfReadPromise=null;state.quickLabels.clear();state.activeQuick?.close();
        if(state.loading)state.pendingReload=true;
        image.beginWall?.();state.observer?.disconnect();$("poster-wall").replaceChildren();
        for(const id of ["detail-dialog","shelf-dialog","taste-dialog","wall-filter-dialog"])if($(id).open)$(id).close();
      }
      state.ready=(async()=>{
        me=me||await globalThis.MovieClawTransport.request("/api/v1/auth/me");
        if(typeof me.username!=="string"||!me.username)throw new Error("暂时无法确认当前登录账号，请重新登录后再试。");
        state.account=String(me.role)+":"+me.username;state.admin=me.role==="admin";
        const key="movieclaw.platform-discovery.personal.v1:"+encodeURIComponent(String(me.role)+":"+me.username);
        let legacy=null;
        try{const saved=JSON.parse(localStorage.getItem(key)||"null");if(/^[A-Za-z0-9_-]{43}$/.test(saved?.token||""))legacy=saved.token;}catch{}
        const [opened,schema,settings]=await Promise.all([request("account",{method:"POST",body:JSON.stringify({legacy_token:legacy})}),request("questionnaire"),request("filters")]);
        if(opened.sync?.mode!=="account"||!opened.profile)throw new Error("账号偏好暂时无法同步，请稍后重试。");
        state.profile=opened.profile;state.profileReadAt=Date.now();if(opened.sync.migration_conflict)announce(opened.sync.message);
        // 本地旧凭证保留作失败恢复，服务器只导入一次；新档案不再依赖站点存储。
        state.schema=schema;adoptFilters(settings);
        renderProfile();state.initialized=true;tools?.shortcutsReady();
      })().catch(error=>{state.ready=null;state.initialized=false;throw error;});
      return state.ready;
    }
    function currentFilters(){return {media_type:state.mediaType,countries:state.country==="all"?[]:state.country.split(","),eras:state.era==="all"?[]:state.era.split(","),...(state.genres!=="all"?{genres:state.genres.split(",")}:{}),...(state.excludeGenres!=="all"?{exclude_genres:state.excludeGenres.split(",")}:{}),...(state.exploration!=="balanced"?{exploration:state.exploration}:{})};}
    function filterInputs(kind){return Array.from($("wall-"+kind+"-options").querySelectorAll?.("input")||[]);}
    function filterNames(kind,values){const labels=new Map(filterInputs(kind).map(input=>[input.value,input.parentElement?.querySelector("span")?.textContent||input.value]));return values.map(value=>labels.get(value)||value);}
    function filterLabels(){
      const filters=currentFilters();
      for(const [kind,values] of [["country",filters.countries],["era",filters.eras]]){
        const names=filterNames(kind,values),title=names.join("、"),short=names.slice(0,2).join("、")+(names.length>2?" +"+(names.length-2):"");
        $("wall-"+kind).textContent=names.length?short:(kind==="country"?"全部国家／地区":"全部年代");$("wall-"+kind).title=title||"不限";
      }
      tools?.labels();$("wall-format").value=state.mediaType;$("wall-filter-sync").textContent="筛选已按账号同步 · 不改口味档案";
    }
    function adoptFilters(value){
      const filters=value?.filters;
      if(!filters||!["all","movie","tv"].includes(filters.media_type)||!Array.isArray(filters.countries)||!Array.isArray(filters.eras)||typeof value.revision!=="string")throw new Error("账号筛选暂未返回有效数据，请重试。");
      const country=[...filters.countries].sort().join(",")||"all",era=[...filters.eras].sort().join(",")||"all";
      const genres=[...(filters.genres||[])].sort().join(",")||"all",excluded=[...(filters.exclude_genres||[])].sort().join(",")||"all",exploration=filters.exploration||"balanced";
      const changed=state.country!==country||state.era!==era||state.mediaType!==filters.media_type||state.genres!==genres||state.excludeGenres!==excluded||state.exploration!==exploration;
      state.genres=genres;state.excludeGenres=excluded;state.exploration=exploration;
      state.country=country;state.era=era;state.mediaType=filters.media_type;state.filtersRevision=value.revision;state.filtersReadAt=Date.now();filterLabels();return changed;
    }
    async function refreshFilters(force=false){
      if(!state.initialized||state.filtersSaving)return false;
      if(state.filtersPromise)return state.filtersPromise;
      if(!force&&Date.now()-state.filtersReadAt<10000)return false;
      const account=state.account,epoch=state.filterEpoch;
      const work=(async()=>{
        const value=await request("filters");if(account!==state.account||epoch!==state.filterEpoch)return false;
        const changed=adoptFilters(value);
        if(changed){const loaded=state.loaded;invalidateWall();if(state.loading){state.generation++;state.pendingReload=true;}else if(loaded)loadWall(true);}
        return changed;
      })();state.filtersPromise=work;
      try{return await work;}finally{if(state.filtersPromise===work)state.filtersPromise=null;}
    }
    function filterDraft(){
      const filters=currentFilters();state.filterDraftRevision=state.filtersRevision;$("wall-filter-format").value=filters.media_type;
      for(const [kind,values] of [["country",filters.countries],["era",filters.eras]]){for(const input of filterInputs(kind))input.checked=values.includes(input.value);filterAnyLabel(kind);}
      tools?.draft();$("wall-filter-error").textContent="";$("wall-filter-reload").hidden=true;
    }
    function filterAnyLabel(kind){$("wall-"+kind+"-any").setAttribute("aria-pressed",String(!filterInputs(kind).some(input=>input.checked)));}
    async function openFilters(event){
      const trigger=event?.currentTarget?.id;
      try{await initialize();await refreshFilters(true);filterDraft();$("wall-filter-dialog").showModal();$("wall-filter-dialog").scrollTop=0;document.body.classList.add("filter-open");tools?.focusGroup(trigger);tools?.readPresets();}
      catch(error){announce(error.message);}
    }
    function filterBusy(value){
      for(const control of Array.from($("wall-filter-dialog").querySelectorAll?.("input,select,button")||[]))control.disabled=value;
      $("wall-filter-apply").disabled=value;$("wall-filter-apply").textContent=value?"保存中…":"应用筛选";$("wall-format").disabled=value;tools?.saving(value);
    }
    async function saveFilters(selection,revision=state.filtersRevision,preset=null){
      if(state.filtersSaving)return false;const account=state.account;state.filtersSaving=true;state.filterEpoch++;const epoch=state.filterEpoch;
      filterBusy(true);
      try{
        await initialize();if(account!==state.account)throw new Error("登录账号已切换，请重新选择筛选。");
        $("wall-filter-error").textContent="";
        const value=await request(preset?"presets/apply":"filters",{method:"POST",body:JSON.stringify(preset?{choice:{...preset,filters_revision:revision}}:{selection:{...selection,revision}})});
        if(account!==state.account||epoch!==state.filterEpoch)return false;
        const changed=adoptFilters(value);if($("wall-filter-dialog").open)$("wall-filter-dialog").close();if(changed)changeFilters();return true;
      }catch(error){
        if(account===state.account){$("wall-filter-error").textContent=error.message;$("wall-filter-reload").hidden=error.status!==409;$("wall-format").value=state.mediaType;if(!$("wall-filter-dialog").open)announce(error.message);}
        return false;
      }finally{state.filtersSaving=false;filterBusy(false);}
    }
    async function refreshProfile(force=false){
      if(!state.initialized)return;
      if(state.profilePromise)return state.profilePromise;
      if(!force&&Date.now()-state.profileReadAt<15000)return;
      const account=state.account;
      const work=(async()=>{
        const value=await request("profile");if(account!==state.account)return;
        const changed=value.revision!==state.profile?.revision;
        state.profile=value;state.profileReadAt=Date.now();
        if(changed){renderProfile();state.shelfEpoch++;state.shelfReadAt=0;if($("shelf-dialog").open||$("detail-dialog").open||state.activeQuick){await readShelf(true);if(account===state.account&&$("shelf-dialog").open)renderShelf();}}
        return changed;
      })();
      state.profilePromise=work;
      try{return await work;}finally{if(state.profilePromise===work)state.profilePromise=null;}
    }
    async function syncSubscriptions(force=false){
      if(state.syncPromise)return state.syncPromise;
      if(!force&&Date.now()-state.syncedAt<300000)return;
      const account=state.account,token=state.token;
      const work=(async()=>{
        const rows=await globalThis.MovieClawTransport.request("/api/v1/subscriptions");
        if(!Array.isArray(rows))throw new Error("订阅记录暂未返回有效列表。");
        const refs=[...new Set(rows.slice().sort((a,b)=>String(b.updated_at||b.created_at||"").localeCompare(String(a.updated_at||a.created_at||""))).map(row=>row.media).filter(media=>media&&["movie","tv"].includes(media.kind)&&Number.isSafeInteger(media.tmdb_id)&&media.tmdb_id>0).map(media=>"tmdb:"+media.kind+":"+media.tmdb_id))].slice(0,1000);
        const me=await globalThis.MovieClawTransport.request("/api/v1/auth/me");
        if(account!==String(me.role)+":"+me.username||account!==state.account||token!==state.token)return;
        const profile=await api("/personal/subscriptions",{method:"POST",headers:{"Content-Type":"application/json","X-Platform-Account":account},body:JSON.stringify({refs})});
        if(account!==state.account||token!==state.token)return;
        state.profile=profile;state.syncedAt=Date.now();state.profileReadAt=Date.now();renderProfile();
      })();
      state.syncPromise=work;
      try{await work;}catch{if(token===state.token&&account===state.account)$("wall-learning-status").textContent="订阅同步暂不可用，先使用已有口味记录。";}finally{if(state.syncPromise===work)state.syncPromise=null;}
    }
    function renderLearning(){
      const learning=state.profile?.learning||{enabled:true,saved:0,dismissed:0,subscriptions:0};
      $("wall-learning").setAttribute("aria-checked",String(learning.enabled!==false));
      $("wall-learning").textContent="行为学习："+(learning.enabled===false?"关":"开");
      $("wall-learning-status").textContent=learning.enabled===false?"仅按口味测试推荐；已看过、隐藏及已收藏／订阅作品仍跳过。":"喜欢 "+(learning.liked||0)+" · 已看过 "+(learning.watched||0)+" · 稍后看 "+learning.saved+" · 不感兴趣 "+learning.dismissed+" · 可见订阅 "+learning.subscriptions+(state.admin?"（管理员包含全站）":"")+(Number.isInteger(learning.subscription_features)?" · 已参考 "+learning.subscription_features+" 部资料":"")+" · 保留探索";
    }
    function renderProfile() {
      cancelNext();
      renderLearning();
      const profile=state.profile||{},summary=profile.summary||{};
      $("taste-start").replaceChildren(document.createTextNode(profile.has_test?"再做一次口味测试":"开始口味测试"),node("span","↗"));
      $("wall-title").textContent=profile.has_test?(summary.title||"这一面，越来越像你的片味。"):"你的片味，铺满这一面。";
      $("wall-description").textContent=profile.has_test?(profile.history_count===1?"从这次测试开始，为你挑选新旧好故事。片味有变化，随时再测一次。":summary.description||"结合本次与历次测试，为你挑选跨年代的好故事。随时重测，片味随你一起改变。") : "先随意逛逛，再花一分钟告诉我你的片味。新片、老片，都有惊喜。";
      const tags=Array.isArray(summary.tags)?summary.tags.slice(0,5):[];
      $("wall-profile-tags").replaceChildren(...(profile.has_test?tags:["尚未测试 · 先探索","新旧同选"]).map(tag=>node("span",tag)),node("span","已按 MovieClaw 账号同步","profile-privacy"));
      const history=Array.isArray(profile.history)?profile.history:[];
      $("taste-history-toggle").textContent="片味档案"+(history.length?" · "+history.length:"");
      const list=$("taste-history-list");list.replaceChildren();
      if(!history.length)list.append(node("p","你的第一份片味档案还没开始。完成测试后，就能在这里看见每次变化。","history-empty"));
      history.forEach((entry,index)=>{
        const row=node("article",null,"history-entry"),head=node("div",null,"history-entry-head");
        head.append(node("span",index===0?"最近一次":"往期测试","history-marker"),node("time",compactDate(entry.created_at)));
        const title=entry.summary?.title||"一次新的片味记录",tags=entry.summary?.tags||[];
        row.append(head,node("h4",title),node("p",tags.join(" · ")));list.append(row);
      });
    }
    function motionState() {
      const reduced=matchMedia("(prefers-reduced-motion:reduce)").matches;
      document.body.classList.toggle("wall-still",!state.motion||reduced);
      $("wall-motion").setAttribute("aria-pressed",String(state.motion&&!reduced));
      $("wall-motion").textContent=reduced?"呼吸动效：系统已关闭":"呼吸动效："+(state.motion?"开":"关");
      $("wall-motion").disabled=reduced;
      $("immersive-motion").textContent=reduced?"纵深呼吸：系统已关闭":"纵深呼吸："+(state.motion?"开":"关");$("immersive-motion").setAttribute("aria-pressed",String(state.motion&&!reduced));$("immersive-motion").disabled=reduced;
    }
    function layoutState(){document.body.dataset.wallLayout=state.immersive||state.seamless?"seamless":"cards";$("wall-layout").setAttribute("aria-checked",String(state.seamless));}
    function revealControls(delay=2000){
      if(!state.immersive)return;
      clearTimeout(state.controlsTimer);
      document.body.classList.add("immersive-controls-visible");
      $("immersive-trigger").setAttribute("aria-expanded","true");
      state.controlsTimer=setTimeout(()=>{
        if($("immersive-tools").matches(":hover,:focus-within")||document.activeElement===$("immersive-trigger"))return;
        document.body.classList.remove("immersive-controls-visible");
        $("immersive-trigger").setAttribute("aria-expanded","false");
      },delay);
    }
    function immersiveState(enabled){
      state.immersive=enabled;clearTimeout(state.controlsTimer);
      document.body.classList.toggle("wall-immersive",enabled);
      document.body.classList.remove("immersive-controls-visible");
      $("wall-fullscreen").setAttribute("aria-checked",String(enabled));
      $("wall-fullscreen-launch").setAttribute("aria-pressed",String(enabled));
      $("immersive-trigger").hidden=!enabled;$("immersive-tools").hidden=!enabled;
      $("immersive-trigger").setAttribute("aria-expanded","false");
      layoutState();
      if(enabled){scrollTo(0,0);revealControls(3000);}
      else scrollTo(0,state.scrollBeforeFullscreen);
      resizeWall();ambient?.activity();
    }
    async function enterFullscreen(native=true){
      if(state.immersive)return;
      state.scrollBeforeFullscreen=scrollY;immersiveState(true);
      // Fullscreen the document so top-layer detail/questionnaire dialogs remain
      // usable. Browsers without this API still get an edge-to-edge page view.
      try{
        if(native&&document.documentElement.requestFullscreen){
          await document.documentElement.requestFullscreen();
          if(!state.immersive||document.body.dataset.mode!=="personal"){
            if(document.fullscreenElement===document.documentElement)await document.exitFullscreen();
            return;
          }
          state.nativeFullscreen=!!document.fullscreenElement;
        }
      }catch{announce("已铺满浏览器窗口。当前浏览器未允许系统全屏，可用顶部按钮退出。");}
    }
    function exitFullscreen(restoreFocus=true){
      if(!state.immersive)return;
      immersiveState(false);
      if(document.fullscreenElement===document.documentElement)document.exitFullscreen().catch(()=>{});
      state.nativeFullscreen=false;
      if(restoreFocus&&document.body.dataset.mode==="personal")$("wall-fullscreen").focus({preventScroll:true});
    }
    function measureWall(){
      const wall=$("poster-wall"),previousColumns=wall.style.getPropertyValue("--wall-columns");wall.style.removeProperty("--wall-columns");
      const style=getComputedStyle(wall),rect=wall.getBoundingClientRect();
      const columns=globalThis.MovieClawPersonal.preferredColumns({width:rect.width||innerWidth,gap:parseFloat(style.columnGap)||0,viewportWidth:innerWidth});
      if(previousColumns)wall.style.setProperty("--wall-columns",previousColumns);
      const history=$("taste-history"),historyHeight=history.hidden||history.closest?.("#workspace-settings")?0:history.offsetHeight+24;
      state.layout=globalThis.MovieClawPersonal.grid({width:rect.width||innerWidth,height:innerHeight,top:state.immersive?0:rect.top+scrollY-historyHeight,columns,gap:parseFloat(style.columnGap)||0,mosaic:state.seamless||state.immersive,fitted:state.immersive});
      wall.style.setProperty("--wall-height",state.layout.height+"px");
      if(state.immersive&&state.rendered){const rows=state.rendered.count/state.rendered.columns,fitWidth=Math.min((rect.width||innerWidth)/state.rendered.columns,innerHeight/rows/1.5);wall.style.setProperty("--wall-fit-width",fitWidth+"px");wall.style.setProperty("--wall-fit-height",fitWidth*1.5*rows+"px");}
      if(!wall.querySelector(".wall-poster")){wall.style.setProperty("--wall-columns",state.layout.columns);wall.style.setProperty("--wall-rows",state.layout.rows);}
      return state.layout;
    }
    async function renderWall(feed){
      const generation=state.generation,account=state.account,renderEpoch=state.renderEpoch=(state.renderEpoch||0)+1;
      const wall=$("poster-wall"),geometry=measureWall(),available=(Array.isArray(feed.items)?feed.items:[]).slice(0,geometry.count);
      let columns=geometry.columns;
      if(available.length<columns)columns=available.length;
      else if(available.length<geometry.count){
        for(let candidate=columns;candidate>=Math.max(2,Math.ceil(columns*.7));candidate--){if(available.length%candidate===0){columns=candidate;break;}}
      }
      const rows=Math.floor(available.length/columns),items=available.slice(0,rows*columns);
      image.beginWall?.();
      const labels=state.quickLabels;state.quickLabels=new Set();
      const cards=items.map((item,index)=>wallCard(item,index,feed,columns)),nextLabels=state.quickLabels;state.quickLabels=labels;
      if(generation!==state.generation||account!==state.account||renderEpoch!==state.renderEpoch||document.body.dataset.mode!=="personal")return false;
      wall.style.setProperty("--wall-columns",columns);wall.style.setProperty("--wall-rows",rows);
      state.activeQuick?.close();state.quickLabels=nextLabels;state.observer?.disconnect();
      if(state.seamless||state.immersive){
        const groups=globalThis.MovieClawPersonal.columnize(cards,columns).map((column,index)=>{
          const wrap=node("div",null,"wall-column"),track=node("div",null,"wall-column-track");
          track.style.setProperty("--column-delay",(-index*1.37)+"s");track.style.setProperty("--column-duration",(7+(index%5)*.6)+"s");
          track.append(...column);wrap.append(track);return wrap;
        });wall.replaceChildren(...groups);
      }else wall.replaceChildren(...cards);
      state.observer=new IntersectionObserver(entries=>entries.forEach(entry=>entry.target.classList.toggle("in-view",entry.isIntersecting)),{rootMargin:"40px"});
      wall.querySelectorAll(".wall-tile").forEach(tile=>state.observer.observe(tile));
      $("wall-count").textContent=items.length+" 部 · "+rows+" 行 × "+columns+" 列";
      const known=items.every(item=>typeof item.metadata?.is_new_to_recent==="boolean");
      const fresh=known?items.filter(item=>item.metadata.is_new_to_recent).length:null;
      const windowBatches=Number(feed.freshness?.window_batches)||20;
      const novelty=fresh!==null?"本面 "+fresh+" 部新面孔"+(items.length-fresh?" · "+(items.length-fresh)+" 部循环补充":" · 避开最近 "+windowBatches+" 面"):"";
      $("wall-status").textContent=[novelty,feed.message||(state.profile?.has_test?"根据你的片味与历次测试选片。":"探索片单 · 完成测试后会按你的偏好调整。")].filter(Boolean).join(" · ");
      // Status/count text may wrap on phones. Refit height after final text is
      // laid out, while keeping the chosen complete rows and this same batch.
      const rect=wall.getBoundingClientRect(),history=$("taste-history");
      const finalGeometry=globalThis.MovieClawPersonal.grid({width:rect.width,height:innerHeight,top:state.immersive?0:rect.top+scrollY-(history.hidden||history.closest?.("#workspace-settings")?0:history.offsetHeight+24),columns,mosaic:state.seamless||state.immersive,fitted:state.immersive});
      wall.style.setProperty("--wall-height",finalGeometry.height+"px");
      wall.querySelectorAll(".wall-tile").forEach(tile=>tile.classList.toggle("quick-short",tile.getBoundingClientRect().height<240));
      if(state.immersive){const fitWidth=Math.min(rect.width/columns,innerHeight/rows/1.5);wall.style.setProperty("--wall-fit-width",fitWidth+"px");wall.style.setProperty("--wall-fit-height",fitWidth*1.5*rows+"px");}
      wall.style.setProperty("--tile-width",((rect.width-(state.seamless||state.immersive?0:13)*(columns-1))/columns)+"px");
      state.rendered={columns,count:items.length,width:Math.round(rect.width),mosaic:state.seamless||state.immersive,items};
    }
    async function resizeWall(){
      if(document.body.dataset.mode!=="personal")return;
      if(state.loading)return;
      if(!state.loaded)return;
      const geometry=measureWall();
      if(state.loading||!state.feed||state.feed.items.length<geometry.count){loadWall(true);return;}
      if(state.nextWall?.key!==nextKey())cancelNext();
      const rendered=state.rendered,rect=$("poster-wall").getBoundingClientRect();
      // Mobile address bars resize the viewport repeatedly. Preserve images and
      // their active downloads when only the available height changes.
      if(rendered&&rendered.mosaic===(state.seamless||state.immersive)&&rendered.columns===geometry.columns&&rendered.count===geometry.count&&rendered.width===Math.round(rect.width)){prepareNext();return;}
      await renderWall(state.feed);prepareNext();
    }
    function wallCard(item,index,feed,columns=state.layout?.columns||9) {
      const root=node("article",null,"wall-tile"),poster=button("","wall-poster",()=>showDetail(item,{id:"personal",name:"海报墙",url:"https://www.themoviedb.org/"},feed));
      root.dataset.itemRef=item.detail_ref;
      root.style.setProperty("--float-delay",(-(index%columns)*1.37)+"s");root.style.setProperty("--float-duration",(5+(index%columns%5)*.45)+"s");
      poster.setAttribute("aria-label","查看 "+item.title+" 详情");poster.title=item.title;
      const fallback=node("div",null,"poster-placeholder");fallback.append(node("span",item.title,"placeholder-title"));poster.append(fallback);
      const img=image(item.poster_url,item.title,null,item.poster_fallback_urls,{wall:true,width:($("poster-wall").getBoundingClientRect().width||innerWidth)/columns,priority:index<6?0:1});if(img)poster.append(img);
      const reasons=item.recommendation_reasons||item.metadata?.recommendation_reasons||[];
      const copy=node("div",null,"wall-tile-copy");copy.append(node("h3",item.title),node("p",[item.year,{movie:"电影",tv:"剧集"}[item.media_type],].filter(Boolean).join(" · ")));
      if(reasons.length)copy.append(node("span",reasons[0],"wall-reason"));
      const ratingValue=Number(item.rating);
      if(Number.isFinite(ratingValue)&&ratingValue>0){const badge=node("span",null,"wall-rating-corner theater-card-top"),rating=node("span",ratingValue.toFixed(1),"theater-rating");rating.append(node("small"," /10"));badge.title="TMDB 评分";badge.append(rating);poster.append(badge);}
      if(Number.isFinite(ratingValue)&&ratingValue>0)copy.querySelector?.("p")?.append(node("span"," · TMDB "+ratingValue.toFixed(1),"wall-fullscreen-rating"));
      poster.append(copy);root.append(poster);
      const controls=feedbackActions(item,true),toggle=button("⋯","wall-quick-toggle"),panel=controls.actions;
      panel.id="wall-quick-"+item.detail_ref.replace(/[^a-zA-Z0-9_-]/g,"-");panel.setAttribute("role","group");panel.setAttribute("aria-label",item.title+"的快捷操作");
      toggle.setAttribute("aria-label",item.title+"的快捷操作");toggle.setAttribute("aria-expanded","false");toggle.setAttribute("aria-controls",panel.id);
      const close=()=>{image.cancelPreload?.();root.dataset.quickOpen="false";toggle.setAttribute("aria-expanded","false");toggle.textContent="⋯";if(state.activeQuick?.root===root)state.activeQuick=null;};
      const open=()=>{if(state.activeQuick?.root!==root)state.activeQuick?.close();root.dataset.quickOpen="true";toggle.setAttribute("aria-expanded","true");toggle.textContent="×";state.activeQuick={root,close};controls.refresh();if(state.preload)image.preload?.(item.poster_url);};
      toggle.addEventListener("click",event=>{event.stopPropagation();if(root.dataset.quickOpen==="true")close();else open();});
      root.addEventListener("pointerenter",event=>{if(event.pointerType!=="touch"&&!document.body.classList.contains?.("wall-screen-running")&&!matchMedia("(max-width:767px)").matches&&matchMedia("(hover:hover)").matches)open();});
      root.addEventListener("pointerleave",event=>{if(event?.pointerType!=="touch"&&!matchMedia("(max-width:767px)").matches&&!root.contains(document.activeElement))close();});
      root.addEventListener("focusin",event=>{if(event.target.matches?.(":focus-visible"))open();});
      root.addEventListener("focusout",event=>{if(!root.contains(event.relatedTarget))close();});
      root.addEventListener("keydown",event=>{if(event.key==="Escape"&&root.dataset.quickOpen==="true"){event.preventDefault();event.stopPropagation();close();toggle.focus({preventScroll:true});close();}});
      root.dispose=()=>{state.quickLabels.delete(controls.label);};
      root.append(toggle,panel);return root;
    }
    function invalidateWall(){cancelNext();state.loaded=false;state.feed=null;state.activeQuick?.close();state.quickLabels.clear();state.observer?.disconnect();image.beginWall?.();$("poster-wall").replaceChildren();}
    function changeFilters(){invalidateWall();loadWall(true);}
    function nextStatus(text){const label=$("wall-next-status");if(label)label.textContent=text;}
    function cancelNext(){
      ambient?.activity();image.cancelAmbient?.();state.nextEpoch++;clearTimeout(state.refillTimer);state.refillTimer=null;clearTimeout(state.nextTimer);state.nextTimer=null;state.nextWall=null;state.nextAbort?.abort();state.nextAbort=null;image.cancelNext?.();nextStatus("");
    }
    function nextKey(){const g=measureWall();return [state.account,state.mediaType,state.era,state.country,state.genres,state.excludeGenres,state.exploration,g.count,g.columns,Math.round($("poster-wall").getBoundingClientRect().width||innerWidth)].join(":");}
    function extraQuery(){return "&genres="+encodeURIComponent(state.genres)+"&exclude_genres="+encodeURIComponent(state.excludeGenres)+"&exploration="+encodeURIComponent(state.exploration);}
    function readyStatus(){nextStatus(state.nextWalls.length?"已备选 "+state.nextWalls.length+" 面 · 海报后台预载":"");}
    function pausePreparation(){
      clearTimeout(state.refillTimer);state.refillTimer=null;
      clearTimeout(state.nextTimer);state.nextTimer=null;state.nextAbort?.abort();state.nextAbort=null;image.cancelNext?.();
    }
    async function prepareNext(){
      if(state.loading||!state.loaded||!state.preload||!image.warmNext||!image.canPrepare?.()||document.body.dataset.mode!=="personal")return;
      if(state.nextWall&&Date.now()-state.nextWall.at>=240000)cancelNext();
      if(state.nextWalls.length>=3||state.nextAbort)return;
      const idleWait=state.refillAfter-Date.now();
      if(idleWait>0){clearTimeout(state.refillTimer);state.refillTimer=setTimeout(()=>{state.refillTimer=null;prepareNext();},idleWait);return;}
      const epoch=state.nextEpoch,key=nextKey(),generation=state.generation,abort=new AbortController();state.nextAbort=abort;
      const timer=setTimeout(()=>{if(state.nextAbort===abort)pausePreparation();},90000);state.nextTimer=timer;
      const valid=()=>state.nextAbort===abort&&epoch===state.nextEpoch&&generation===state.generation&&key===nextKey()&&state.preload&&image.canPrepare?.();
      try{
        if(!await image.settled()||!valid())return;
        while(state.nextWalls.length<3&&valid()){
          nextStatus(state.nextWalls.length?"已准备 "+state.nextWalls.length+" 面 · 正在补充…":"正在准备后续海报…");
          const geometry=measureWall();
          const feed=await request("recommendations/prepare?nonce="+nonce()+"&after="+(state.nextWalls[state.nextWalls.length-1]?.feed.prepared_ticket||"")+"&limit="+geometry.count+"&media_type="+state.mediaType+"&era="+state.era+"&country="+encodeURIComponent(state.country)+extraQuery(),{signal:abort.signal});
          if(!valid()||!feed.prepared_ticket||!feed.items?.length||!["ready","stale"].includes(feed.status))return;
          if(state.nextWalls.some(row=>row.feed.prepared_ticket===feed.prepared_ticket))return;
          state.nextWalls.push({feed,key,at:Date.now()});readyStatus();
        }
        // Queue the three account-scoped lists before warming their pictures.
        // One broken poster cannot prevent the following lists being available.
        if(valid())await image.warmNext(state.nextWalls.flatMap(row=>row.feed.items),($("poster-wall").getBoundingClientRect().width||innerWidth)/measureWall().columns);
      }catch{/* Speculative failure never replaces the current wall or preference state. */}
      finally{clearTimeout(timer);if(state.nextTimer===timer)state.nextTimer=null;if(state.nextAbort===abort){state.nextAbort=null;readyStatus();}}
    }
    async function loadWall(force=false) {
      ambient?.activity();image.cancelAmbient?.();await ambient?.flush();
      if(force)cancelNext();
      if(state.loading){if(force){state.generation++;state.pendingReload=true;}return;}
      pausePreparation();state.loading=true;const generation=++state.generation,wall=$("poster-wall");
      $("wall-shuffle").disabled=true;$("wall-shuffle-bottom").disabled=true;$("immersive-shuffle").disabled=true;wall.setAttribute("aria-busy","true");
      $("wall-status").textContent=state.loaded?"正在为你换一面…":"正在从电影与剧集片库中挑选，新片老片一起看…";
      $("wall-status").classList.remove("has-error");
      if(!wall.querySelector(".wall-poster")){const geometry=measureWall();$("wall-count").textContent="";wall.replaceChildren();for(let i=0;i<geometry.count;i++)wall.append(node("div",null,"wall-skeleton"));}
      try{
        await initialize();if(generation!==state.generation)return;
        const geometry=measureWall();
        let feed,prepared=state.nextWall;
        // Only a server-side, account-scoped commit may promote speculative data.
        if(prepared&&prepared.key===nextKey()&&Date.now()-prepared.at<240000){
          try{feed=await request("recommendations/commit",{method:"POST",body:JSON.stringify({ticket:prepared.feed.prepared_ticket})});}catch{/* Re-select against latest exclusions on conflict or failure. */}
        }
        if(!feed)prepared=null;
        if(feed){if(Array.isArray(feed.prepared_queue))adoptPreparedQueue(feed);else state.nextWalls.shift();readyStatus();}else cancelNext();
        if(generation!==state.generation)return;
        if(!feed&&!state.loaded&&!force){
          try{const restored=await request("recommendations/resume?limit="+geometry.count+"&media_type="+state.mediaType+"&era="+state.era+"&country="+encodeURIComponent(state.country)+extraQuery());if(restored.items?.length>=geometry.count)feed=restored;}catch{/* A failed resume cannot prevent a fresh, authenticated selection. */}
        }
        if(!feed)feed=await request("recommendations?nonce="+nonce()+"&limit="+geometry.count+"&media_type="+state.mediaType+"&era="+state.era+"&country="+encodeURIComponent(state.country)+extraQuery());
        if(generation!==state.generation)return;
        const items=Array.isArray(feed.items)?feed.items:[];
        if(!items.length)throw new Error(feed.message||"影片片库暂时没有可用海报，请稍后换一面重试。");
        if(await renderWall(feed)===false||generation!==state.generation)return;
        const hadWall=state.loaded;state.feed=feed;state.loaded=true;if(hadWall)state.refillAfter=Date.now()+1800;
        readShelf().catch(()=>{});
      }catch(error){
        if(generation!==state.generation)return;
        if(error.status===409){try{if(await refreshFilters(true)){state.pendingReload=true;return;}}catch{}}
        if(!state.loaded){const empty=node("div",null,"wall-empty");empty.append(node("h3","暂时无法载入海报墙"),node("p",error.message||"暂时连接不到影片片库，请稍后重试。"),node("p","无需重新做口味测试，你的片味档案会保留。"),button("重新连接","button",()=>loadWall()));wall.replaceChildren(empty);}
        $("wall-status").textContent=error.message;$("wall-status").classList.add("has-error");
      }finally{
        state.loading=false;ambient?.wake();wall.setAttribute("aria-busy","false");$("wall-shuffle").disabled=false;$("wall-shuffle-bottom").disabled=false;$("immersive-shuffle").disabled=false;
        if(state.initialized)tools?.refreshShortcuts();
        if(state.pendingReload){state.pendingReload=false;loadWall();}else if(state.initialized){Promise.resolve(state.syncPromise||syncSubscriptions()).finally(()=>prepareNext());}
      }
    }
    function labelFeedback(){state.feedbackLabel?.();for(const label of state.quickLabels)label();}
    async function readShelf(force=false){
      if(!force&&Date.now()-state.shelfReadAt<15000)return state.shelf;
      if(!force&&state.shelfReadPromise)return state.shelfReadPromise;
      const account=state.account,epoch=state.shelfEpoch;
      const work=(async()=>{const value=await request("shelf");if(account===state.account&&epoch===state.shelfEpoch){state.shelf=value;state.shelfReadAt=Date.now();labelFeedback();}return value;})();
      state.shelfReadPromise=work;
      try{return await work;}finally{if(state.shelfReadPromise===work)state.shelfReadPromise=null;}
    }
    function updateShelf(action,item){
      const account=state.account;
      const run=async()=>{
        await initialize();if(account&&account!==state.account)throw new Error("登录账号已切换，请重新打开作品详情。");
        state.shelfEpoch++;const token=state.token;
        try{const value=await request("feedback",{method:"POST",body:JSON.stringify({action,item})});if(token!==state.token||account!==state.account)throw new Error("登录账号已切换，请重新打开作品详情。");cancelNext();state.shelf=value;state.shelfReadAt=Date.now();labelFeedback();state.profile.learning={...(state.profile.learning||{enabled:true}),saved:value.saved.length,dismissed:value.dismissed.length,watched:(value.watched||[]).length,liked:(value.liked||[]).length,subscriptions:state.profile.learning?.subscriptions||0};renderLearning();tools?.offerUndo(value.undo_action);return value;}finally{state.shelfEpoch++;}
      };
      const result=state.feedbackTail.then(run,run);state.feedbackTail=result.catch(()=>{});return result;
    }
    function feedbackActions(item,compact=false){
      const actions=node("div",null,compact?"wall-quick":"personal-feedback");
      const save=button(compact?"稍后看":"加入稍后看","button"),like=button("喜欢","button ghost"),watch=button("已看过","button ghost"),hide=button(compact?"不感兴趣":"暂不推荐","button ghost");
      actions.append(...(compact?[like,watch,save,hide]:[save,like,watch,hide]));
      let account=state.account,pending=false;
      const contains=name=>(state.shelf[name]||[]).some(row=>(row.item?.detail_ref||row.detail_ref)===item.detail_ref);
      const label=()=>{
        if(account!==state.account)return;
        save.textContent=compact?(contains("saved")?"稍后看 ✓":"稍后看"):(contains("saved")?"已加入稍后看 · 移除":"加入稍后看");
        like.textContent=compact?(contains("liked")?"喜欢 ✓":"喜欢"):(contains("liked")?"已喜欢 · 取消":"喜欢");
        watch.textContent=compact?(contains("watched")?"已看过 ✓":"已看过"):(contains("watched")?"已看过 · 取消":"已看过");
        hide.textContent=contains("dismissed")?"恢复推荐":compact?"不感兴趣":"暂不推荐";
        for(const [control,name,add,remove] of [[save,"saved","稍后看","移除稍后看"],[like,"liked","喜欢","取消喜欢"],[watch,"watched","已看过","取消已看过"],[hide,"dismissed","不感兴趣","恢复推荐"]]){
          control.setAttribute("aria-pressed",String(contains(name)));
          if(compact){control.setAttribute("aria-label",(contains(name)?remove:add)+"："+item.title);control.title=contains(name)?remove:add;control.dataset.symbol={saved:"◷",liked:contains(name)?"♥":"♡",watched:"✓",dismissed:"⊘"}[name];control.dataset.caption=contains(name)?remove:add;}
        }
      };
      const refresh=async()=>{
        try{await initialize();if(!account)account=state.account;if(account!==state.account)return;await readShelf();label();}catch{/* 写入前重新核验，读取失败不会冒充已保存。 */}
      };
      if(compact)state.quickLabels.add(label);else{state.feedbackLabel=label;refresh();}
      for(const [control,name,add,remove,message] of [
        [save,"saved","save","remove","已保存，下一面会参考这类故事。"],
        [like,"liked","like","unlike","已喜欢，会优先探索相近的故事。"],
        [watch,"watched","watch","unwatch","已标记看过，海报墙将跳过这部作品。"],
        [hide,"dismissed","hide","unhide","已隐藏，可在我的片单里恢复。"]]){
        control.addEventListener("click",async event=>{
          event?.stopPropagation();
          if(pending||state.feedbackPending)return;pending=true;state.feedbackPending=true;
          for(const b of [save,like,watch,hide])b.disabled=true;actions.setAttribute("aria-busy","true");
          try{
            await initialize();if(account!==state.account)throw new Error("登录账号已切换，请重新打开作品操作。");
            const latest=await readShelf(true);if(account!==state.account)throw new Error("登录账号已切换，请重新打开作品操作。");state.shelf=latest;
            const removing=contains(name);
            await updateShelf(removing?remove:add,item);label();state.loaded=false;
            announce(removing?"已取消，推荐偏好已更新。":message);
            if(!removing&&(name==="watched"||name==="dismissed")){
              if(state.feed){state.feed={...state.feed,items:state.feed.items.filter(row=>row.detail_ref!==item.detail_ref)};if(state.feed.items.length)await renderWall(state.feed);}
              loadWall(true);
            }
          }catch(error){announce(error.message);}finally{pending=false;state.feedbackPending=false;actions.setAttribute("aria-busy","false");for(const b of [save,like,watch,hide])b.disabled=false;}
        });
      }
      label();return {actions,refresh,label};
    }
    function detailActions(item){return feedbackActions(item).actions;}
    async function openShelf(){
      if(!$("shelf-dialog").open)$("shelf-dialog").showModal();document.body.classList.add("shelf-open");$("shelf-content").replaceChildren(node("p","正在读取你的片单…"));
      try{await initialize();const epoch=state.shelfEpoch,value=await request("shelf");if(epoch===state.shelfEpoch&&$("shelf-dialog").open){state.shelf=value;state.shelfReadAt=Date.now();labelFeedback();renderShelf();}}catch(error){$("shelf-content").replaceChildren(node("p",error.message),button("重试","button",openShelf));}
    }
    function renderShelf(){
      const content=$("shelf-content"),grid=node("div",null,"shelf-grid"),rows=state.shelf.saved;
      const expanded={};for(const name of ["liked","watched","dismissed"]){const old=document.getElementById("shelf-"+name);expanded[name]={open:old?.open===true,shown:Number(old?.getAttribute?.("data-shown"))||24};}
      content.replaceChildren(node("h3","稍后看 · "+rows.length));
      if(!rows.length)grid.append(node("p","在私人海报详情中点击“加入稍后看”，把想看的故事留在这里。","shelf-note"));
      rows.forEach(row=>{const card=node("article",null,"shelf-card"),poster=button("","wall-poster",()=>{$("shelf-dialog").close();showDetail(row.item,{id:"personal",name:"我的稍后看",url:"https://www.themoviedb.org/"},{items:rows.map(entry=>entry.item),source:{kind:"personal_library",label:"本人保存的稍后看"}});});poster.setAttribute("aria-label","查看 "+row.item.title+" 详情");const img=image(row.item.poster_url,row.item.title);if(img)poster.append(img);card.append(poster,node("h4",row.item.title),button("移除","button ghost",async event=>{const control=event.currentTarget;control.disabled=true;try{await updateShelf("remove",{detail_ref:row.item.detail_ref});renderShelf();}catch(error){announce(error.message);control.disabled=false;}}));grid.append(card);});content.append(grid);
      for(const [name,title,action,removeLabel] of [["liked","喜欢","unlike","取消喜欢"],["watched","已看过","unwatch","取消已看过"]]){
        const rows=state.shelf[name]||[],section=node("details",null,"shelf-hidden"),grid=node("div",null,"shelf-grid");
        section.id="shelf-"+name;section.append(node("summary",title+" · "+rows.length+" 部（可取消）"));
        let shown=0;const more=button("显示更多","button ghost");
        const appendPage=()=>{
          const page=rows.slice(shown,shown+24);shown+=page.length;
          page.forEach(row=>{const item=row.item,card=node("article",null,"shelf-card"),poster=button("","wall-poster",()=>{$("shelf-dialog").close();showDetail(item,{id:"personal",name:"我的"+title,url:"https://www.themoviedb.org/"},{items:rows.map(row=>row.item),source:{kind:"personal_library",label:"本人标记的"+title}});});
            poster.setAttribute("aria-label","查看 "+item.title+" 详情");const img=image(item.poster_url,item.title);if(img)poster.append(img);
            card.append(poster,node("h4",item.title+(item.year?" · "+item.year:"")),button(removeLabel,"button ghost",async event=>{const control=event.currentTarget;control.disabled=true;try{await updateShelf(action,{detail_ref:item.detail_ref});renderShelf();state.loaded=false;}catch(error){announce(error.message);control.disabled=false;}}));grid.append(card);});
          more.hidden=shown>=rows.length;section.setAttribute("data-shown",String(shown));
        };
        more.addEventListener("click",appendPage);section.addEventListener("toggle",()=>{if(section.open&&!shown)appendPage();});
        section.append(grid,more);content.append(section);
        if(expanded[name].open){section.open=true;do{appendPage();}while(shown<Math.min(expanded[name].shown,rows.length));}
      }
      const hidden=node("details",null,"shelf-hidden");hidden.id="shelf-dismissed";hidden.open=expanded.dismissed.open;hidden.append(node("summary","不感兴趣 · "+state.shelf.dismissed.length+" 部（可恢复）"));
      state.shelf.dismissed.forEach(row=>{const line=node("div",null,"shelf-hidden-row");line.append(node("span",row.title),button("恢复推荐","button ghost",async event=>{const control=event.currentTarget;control.disabled=true;try{await updateShelf("unhide",{detail_ref:row.detail_ref});renderShelf();}catch(error){announce(error.message);control.disabled=false;}}));hidden.append(line);});content.append(hidden);
    }
    function currentQuestion(){return state.schema.questions[state.step];}
    function renderQuestion() {
      const q=currentQuestion(),saved=state.answers[q.id],selected=new Set(Array.isArray(saved)?saved:saved?[saved]:[]);
      $("taste-step").textContent=String(state.step+1).padStart(2,"0")+" / "+String(state.schema.questions.length).padStart(2,"0");
      $("taste-progress").firstElementChild.style.width=((state.step+1)/state.schema.questions.length*100)+"%";
      $("taste-title").textContent=q.title;$("taste-description").textContent=q.description|| (q.multiple?"最多选 "+q.max_choices+" 项":"选择最贴近你的一项");
      $("taste-error").textContent="";$("taste-next").textContent=state.step===state.schema.questions.length-1?"生成我的海报墙 →":"下一题 →";$("taste-back").disabled=state.step===0;
      $("taste-options").replaceChildren(...q.choices.map(choice=>{
        const label=node("label",null,"taste-option"),input=node("input"),copy=node("span",null,"taste-option-copy");input.type=q.multiple?"checkbox":"radio";input.name=q.id;input.value=choice.id;input.checked=selected.has(choice.id);
        copy.append(node("strong",choice.label));if(choice.description)copy.append(node("span",choice.description));
        label.append(input,copy,node("span",null,"taste-check"));
        input.addEventListener("change",()=>{
          const exclusive={regions:"global",avoid:"none"}[q.id];
          if(q.multiple&&input.checked&&exclusive)$("taste-options").querySelectorAll("input").forEach(option=>{if(option!==input&&(input.value===exclusive||option.value===exclusive))option.checked=false;});
          const values=[...$("taste-options").querySelectorAll("input:checked")].map(option=>option.value);
          if(q.multiple&&values.length>q.max_choices){input.checked=false;$("taste-error").textContent="这一题最多选择 "+q.max_choices+" 项。";return;}
          state.answers[q.id]=q.multiple?values:input.value;$("taste-error").textContent="";
        });return label;
      }));
    }
    async function startTest() {
      try{
        await initialize();await refreshProfile(true);state.testAccount=state.account;state.testRevision=state.profile.test_revision||"";state.step=0;state.answers=structuredClone(state.profile?.answers||{});renderQuestion();
        $("taste-dialog").showModal();document.body.classList.add("taste-open");
      }catch(error){announce(error.message);}
    }
    $("taste-form").addEventListener("submit",async event=>{
      event.preventDefault();if(state.submitting)return;
      const q=currentQuestion(),answer=state.answers[q.id];
      if(!answer||(Array.isArray(answer)&&!answer.length)){$("taste-error").textContent="选一个最贴近你的答案，再继续。";return;}
      if(q.id==="avoid"&&answer.includes("animation")&&state.answers.themes?.includes("animation")){$("taste-error").textContent="你在第一题选了动画世界，请取消跳过动画，或返回修改题材偏好。";return;}
      if(state.step<state.schema.questions.length-1){state.step++;renderQuestion();return;}
      state.submitting=true;$("taste-next").disabled=true;$("taste-back").disabled=true;$("taste-close").disabled=true;$("taste-next").textContent="保存片味中…";
      try{
        await initialize();if(state.testAccount!==state.account)throw new Error("登录账号已切换，请关闭并重新开始口味测试。");
        state.profile=await request("answers",{method:"POST",headers:{"X-Platform-Test-Revision":state.testRevision},body:JSON.stringify({answers:state.answers})});renderProfile();
        $("taste-dialog").close();announce("这次片味已记录，正在为你换一面。");
        await loadWall(true);
      }catch(error){$("taste-error").textContent=error.message;}
      finally{state.submitting=false;$("taste-next").disabled=false;$("taste-back").disabled=false;$("taste-close").disabled=false;$("taste-next").textContent="生成我的海报墙 →";}
    });
    $("taste-start").addEventListener("click",startTest);
    $("taste-close").addEventListener("click",()=>$("taste-dialog").close());
    $("taste-dialog").addEventListener("cancel",event=>{if(state.submitting)event.preventDefault();});
    $("taste-dialog").addEventListener("close",()=>document.body.classList.remove("taste-open"));
    $("taste-back").addEventListener("click",()=>{if(state.step>0){state.step--;renderQuestion();}});
    $("taste-history-toggle").addEventListener("click",()=>{const hidden=!$("taste-history").hidden;$("taste-history").hidden=hidden;$("taste-history-toggle").setAttribute("aria-expanded",String(!hidden));});
    $("wall-shuffle").addEventListener("click",()=>loadWall());$("wall-shuffle-bottom").addEventListener("click",()=>{loadWall();$("poster-wall").scrollIntoView({block:"start",behavior:"instant"});});
    $("wall-format").addEventListener("change",()=>saveFilters({genres:[],exclude_genres:[],exploration:"balanced",...currentFilters(),media_type:$("wall-format").value}));
    $("wall-country").addEventListener("click",openFilters);$("wall-era").addEventListener("click",openFilters);
    for(const kind of ["country","era"]){
      $("wall-"+kind+"-options").addEventListener("change",()=>filterAnyLabel(kind));
      $("wall-"+kind+"-any").addEventListener("click",()=>{for(const input of filterInputs(kind))input.checked=false;filterAnyLabel(kind);});
    }
    for(const id of ["wall-filter-cancel","wall-filter-cancel-bottom"])$(id).addEventListener("click",()=>{if(!state.filtersSaving)$("wall-filter-dialog").close();});
    $("wall-filter-dialog").addEventListener("cancel",event=>{if(state.filtersSaving)event.preventDefault();});
    $("wall-filter-dialog").addEventListener("close",()=>document.body.classList.remove("filter-open"));
    $("wall-filter-reset").addEventListener("click",()=>{tools?.resetDraft();$("wall-filter-format").value="all";for(const kind of ["country","era"]){for(const input of filterInputs(kind))input.checked=false;filterAnyLabel(kind);}});
    $("wall-filter-reload").addEventListener("click",async()=>{try{await refreshFilters(true);filterDraft();}catch(error){$("wall-filter-error").textContent=error.message;}});
    $("wall-filter-apply").addEventListener("click",()=>saveFilters({media_type:$("wall-filter-format").value,countries:filterInputs("country").filter(input=>input.checked).map(input=>input.value),eras:filterInputs("era").filter(input=>input.checked).map(input=>input.value),...tools?.selection()},state.filterDraftRevision));
    try{state.preload=localStorage.getItem("movieclaw.platform-discovery.poster-preload")!=="off";}catch{}
    const preloadLabel=()=>{$("wall-preload").setAttribute("aria-checked",String(state.preload));$("wall-preload").textContent="提前加载："+(state.preload?"开":"关");image.setPreload?.(state.preload);};
    preloadLabel();$("wall-preload").addEventListener("click",()=>{state.preload=!state.preload;try{localStorage.setItem("movieclaw.platform-discovery.poster-preload",state.preload?"on":"off");}catch{}preloadLabel();if(state.preload)prepareNext();else cancelNext();});
    $("wall-learning").addEventListener("click",async()=>{
      const control=$("wall-learning");control.disabled=true;
      try{await initialize();state.profile=await request("learning",{method:"POST",body:JSON.stringify({enabled:state.profile?.learning?.enabled===false})});renderLearning();await loadWall(true);}catch(error){announce(error.message);}finally{control.disabled=false;}
    });
    $("wall-sync-subscriptions").addEventListener("click",async()=>{const control=$("wall-sync-subscriptions");control.disabled=true;try{await initialize();await syncSubscriptions(true);await loadWall(true);}catch(error){announce(error.message);}finally{control.disabled=false;}});
    $("wall-shelf").addEventListener("click",openShelf);$("shelf-close").addEventListener("click",()=>$("shelf-dialog").close());$("shelf-dialog").addEventListener("close",()=>document.body.classList.remove("shelf-open"));
    $("wall-fullscreen").addEventListener("click",()=>state.immersive?exitFullscreen():enterFullscreen());
    $("immersive-exit").addEventListener("click",()=>exitFullscreen());
    $("immersive-shuffle").addEventListener("click",()=>loadWall());
    $("immersive-taste").addEventListener("click",startTest);
    $("immersive-shelf").addEventListener("click",openShelf);
    $("immersive-trigger").addEventListener("click",()=>{revealControls();$("immersive-shuffle").focus();});
    $("immersive-trigger").addEventListener("pointerenter",()=>revealControls());
    $("immersive-trigger").addEventListener("focus",()=>revealControls());
    $("immersive-trigger").addEventListener("blur",()=>revealControls(900));
    $("immersive-tools").addEventListener("pointerenter",()=>revealControls());
    $("immersive-tools").addEventListener("pointerleave",()=>revealControls(900));
    $("immersive-tools").addEventListener("focusin",()=>revealControls());
    $("immersive-tools").addEventListener("focusout",()=>revealControls(900));
    document.addEventListener("pointerdown",event=>{if(state.activeQuick&&!state.activeQuick.root.contains(event.target))state.activeQuick.close();});
    document.addEventListener("fullscreenchange",()=>{
      if(document.fullscreenElement){state.nativeFullscreen=state.immersive;resizeWall();}
      else if(state.nativeFullscreen){state.nativeFullscreen=false;exitFullscreen();}
    });
    document.addEventListener("keydown",event=>{
      if(event.key==="Escape"&&state.immersive&&!document.querySelector("dialog[open]")){event.preventDefault();exitFullscreen();}
    });
    try{state.motion=localStorage.getItem("movieclaw.platform-discovery.motion")!=="off";state.seamless=localStorage.getItem("movieclaw.platform-discovery.wall-layout.v3")==="seamless";}catch{}
    $("wall-fullscreen-launch").addEventListener("click",()=>enterFullscreen(false));
    $("immersive-motion").addEventListener("click",()=>$("wall-motion").click());
    $("wall-layout").addEventListener("click",()=>{state.seamless=!state.seamless;try{localStorage.setItem("movieclaw.platform-discovery.wall-layout.v3",state.seamless?"seamless":"cards");}catch{}layoutState();resizeWall();});layoutState();
    $("wall-motion").addEventListener("click",()=>{state.motion=!state.motion;try{localStorage.setItem("movieclaw.platform-discovery.motion",state.motion?"on":"off");}catch{}motionState();});
    document.addEventListener("visibilitychange",()=>document.body.classList.toggle("page-hidden",document.hidden));
    window.addEventListener("resize",()=>{clearTimeout(state.resizeTimer);state.resizeTimer=setTimeout(resizeWall,250);});
    window.addEventListener("focus",()=>{if(document.body.dataset.mode==="personal"&&!state.loading)initialize().then(async()=>{await refreshFilters(true);if(!state.loaded)loadWall();}).catch(error=>announce(error.message));});
    state.syncTimer=setInterval(()=>{if(state.initialized&&!document.hidden&&document.body.dataset.mode==="personal"&&!state.submitting)initialize().catch(()=>{});},30000);
    window.addEventListener("pagehide",()=>clearInterval(state.syncTimer));
    matchMedia("(prefers-reduced-motion:reduce)").addEventListener("change",motionState);motionState();
    document.addEventListener("visibilitychange",()=>{if(document.hidden)cancelNext();else initialize().then(()=>refreshFilters(true)).then(()=>prepareNext()).catch(()=>{});});
    window.addEventListener("pagehide",cancelNext);
    tools=globalThis.MovieClawWallTools?.create({node,button,image,request,initialize,getAccount:()=>state.account,getFilters:currentFilters,openFilters,
      draftBase:filterDraft,saveFilters,filterInputs,loadWall,showDetail,compactDate,announce,
      async applyPreset(id,presets_revision){await initialize();await refreshFilters(true);return saveFilters(null,state.filtersRevision,{id,presets_revision});},
      adoptShelf(value){cancelNext();state.shelf=value;state.shelfReadAt=Date.now();state.shelfEpoch++;labelFeedback();state.loaded=false;refreshProfile(true).catch(()=>{});if($("shelf-dialog").open)renderShelf();loadWall(true);}})||null;
    function adoptPreparedQueue(result){
      const key=nextKey(),at=Date.now();
      state.nextWalls=(result.prepared_queue||[]).slice(0,3).map(feed=>({feed,key,at}));readyStatus();
    }
    function ambientActive(){return state.immersive&&state.ambientEnabled&&state.preload&&state.loaded&&!state.loading&&!state.feedbackPending&&!document.hidden&&document.body.dataset.mode==="personal"&&!document.querySelector("dialog[open]");}
    function randomTake(values,count){const rows=[...values];for(let i=rows.length-1;i>0;i--){const j=Math.floor(Math.random()*(i+1));[rows[i],rows[j]]=[rows[j],rows[i]];}return rows.slice(0,count);}
    async function prepareAmbient(count,valid){
      if(!valid())return null;
      prepareNext();
      const head=state.nextWall,items=state.rendered?.items;
      if(!head||!items?.length||head.key!==nextKey()||Date.now()-head.at>=240000)return null;
      const account=state.account,generation=state.generation,refs=new Set(items.map(item=>item.detail_ref));
      const candidates=head.feed.items.filter(item=>!refs.has(item.detail_ref));
      const slots=items.filter(item=>{const tile=[...$("poster-wall").querySelectorAll(".wall-tile")].find(n=>n.dataset.itemRef===item.detail_ref),r=tile?.getBoundingClientRect();return r&&r.top<innerHeight-40&&r.bottom>40;});
      const chosen=randomTake(candidates,Math.min(count,slots.length));if(!chosen.length)return null;
      const ready=await image.prepareSwap(chosen,($("poster-wall").getBoundingClientRect().width||innerWidth)/state.rendered.columns);
      if(!valid()||account!==state.account||generation!==state.generation||!ready.length)return null;
      const targets=randomTake(slots,ready.length),rotation={ticket:head.feed.prepared_ticket,operation:nonce(),current:items.map(item=>item.detail_ref),replace:Object.fromEntries(targets.map((item,i)=>[item.detail_ref,ready[i].detail_ref]))};
      const result=await request("recommendations/rotate-preview",{method:"POST",body:JSON.stringify({rotation})});
      if(!valid()||account!==state.account||generation!==state.generation)return null;
      return {rotation,result,account,generation,before:state.feed,targets};
    }
    async function applyAmbient(proposal,valid,wait){
      const wall=$("poster-wall"),tiles=proposal.targets.map(item=>[...wall.querySelectorAll(".wall-tile")].find(n=>n.dataset.itemRef===item.detail_ref));
      if(!valid()||tiles.some(tile=>!tile))return false;
      state.activeQuick?.close();tiles.forEach(tile=>tile.classList.add("ambient-out"));
      try{
        if(!await wait(matchMedia("(prefers-reduced-motion:reduce)").matches?0:650,valid)||proposal.account!==state.account||proposal.generation!==state.generation)return false;
        proposal.targets.forEach((item,i)=>{
          const index=proposal.rotation.current.indexOf(item.detail_ref),next=proposal.result.items[index],tile=wallCard(next,index,proposal.result,state.rendered.columns);
          tile.classList.add("ambient-in");state.observer?.unobserve?.(tiles[i]);tiles[i].dispose?.();tiles[i].replaceWith(tile);state.observer?.observe(tile);
          tile.classList.toggle("quick-short",tile.getBoundingClientRect().height<240);setTimeout(()=>tile.classList.remove("ambient-in"),700);
        });
        state.feed=proposal.result;state.rendered.items=proposal.result.items;wall.dataset.rotationCount=String(proposal.result.rotation.count);wall.dataset.rotationOperation=proposal.rotation.operation;return true;
      }finally{tiles.forEach(tile=>tile.classList.remove("ambient-out"));}
    }
    async function confirmAmbient(proposal){
      const result=await request("recommendations/rotate",{method:"POST",body:JSON.stringify({rotation:proposal.rotation}),keepalive:true});
      if(proposal.account!==state.account||proposal.generation!==state.generation)return;
      const {prepared_queue,...feed}=result;state.feed=feed;adoptPreparedQueue(result);if(document.body.classList.contains("wall-screen-running"))$("immersive-ambient-status").textContent="屏保轮替中 · 本次换入 "+result.rotation.count+" 张";prepareNext();
    }
    ambient=globalThis.MovieClawAmbient?.create({active:ambientActive,prepare:prepareAmbient,apply:applyAmbient,confirm:confirmAmbient,
      status(running){document.body.classList.toggle("wall-screen-running",running);$("immersive-ambient-status").textContent=running?"屏保轮替中 · 每次 1–3 张":state.ambientEnabled?"静置 5 秒自动轮替":"屏保轮替已暂停";},
      async recover(error,proposal,shown){cancelNext();if(shown&&proposal?.account===state.account&&proposal.generation===state.generation){if(error.status>=400&&error.status<500)await renderWall(proposal.before);setTimeout(()=>loadWall(true),0);}$("immersive-ambient-status").textContent="保留当前海报 · 稍后重试";}
    });
    try{state.ambientEnabled=localStorage.getItem("movieclaw.platform-discovery.ambient.v1")!=="off";}catch{}
    const ambientLabel=()=>{$("immersive-ambient").setAttribute("aria-checked",String(state.ambientEnabled));$("immersive-ambient").textContent="屏保轮替："+(state.ambientEnabled?"开":"关");};ambientLabel();
    $("immersive-ambient").addEventListener("click",()=>{state.ambientEnabled=!state.ambientEnabled;try{localStorage.setItem("movieclaw.platform-discovery.ambient.v1",state.ambientEnabled?"on":"off");}catch{}ambientLabel();ambient?.activity();});
    const ambientActivity=()=>{image.cancelAmbient?.();ambient?.activity();};
    for(const event of ["pointermove","pointerdown","wheel","touchstart","keydown","scroll"])document.addEventListener(event,ambientActivity,{capture:true,passive:true});
    document.addEventListener("close",ambientActivity,true);document.addEventListener("visibilitychange",ambientActivity);window.addEventListener("blur",()=>ambient?.stop());window.addEventListener("focus",ambientActivity);window.addEventListener("resize",ambientActivity);window.addEventListener("pagehide",()=>ambient?.close());
    return {open(){if(!state.loaded)loadWall();else resizeWall();},recordView(item){return tools?.recordView(item);},leave(){ambient?.stop();cancelNext();state.activeQuick?.close();exitFullscreen(false);},detailActions,async subscriptionChanged(){state.syncedAt=0;await initialize();await syncSubscriptions(true);await loadWall(true);}};
  }
};
