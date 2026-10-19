"use strict";
// 所有订阅请求由浏览器带当前会话直达宿主；插件进程不代用管理员权限。
globalThis.MovieClawSubscriptions = {
  create({item,node,button,announce,onBusy=()=>{},onCreated=()=>{}}) {
    const root=node("section",null,"inline-subscription");root.hidden=true;root.setAttribute("aria-label","订阅确认");
    const entry=button(item.media_type==="tv"?"订阅剧集":"订阅影片","button primary",()=>{if(state.ready){root.hidden=false;root.scrollIntoView({block:"nearest",behavior:"instant"});}else return open();});
    const state={disposed:false,generation:0,loading:false,busy:false,ready:false,created:false,reference:item.detail_ref,prepared:null,seasons:new Set(),allowed:[],follow:false,smart:null,mode:"rules",principal:null,floors:false};
    const validRef=ref=>/^(tmdb:(movie|tv):[1-9]\d{0,10}|douban:[1-9]\d{0,10})$/.test(ref||"");
    const integer=value=>Number.isSafeInteger(value)&&value>=0;
    const validId=value=>Number.isSafeInteger(value)&&value>0;
    const live=generation=>!state.disposed&&generation===state.generation;
    const person=me=>String(me.role)+":"+String(me.username);
    async function request(path,body){
      return globalThis.MovieClawTransport.request("/api/v1"+path,{method:body===undefined?"GET":"POST",headers:{"Accept":"application/json",...(body===undefined?{}:{"Content-Type":"application/json"})},...(body===undefined?{}:{body:JSON.stringify(body)})},path==="/subscriptions"?60000:30000);
    }

    async function identity(){
      const me=await request("/auth/me");
      if(typeof me.username!=="string"||!me.username)throw new Error("暂时无法确认登录账号，请重新登录。");
      if(me.role!=="admin"&&me.capabilities?.allow_subscribe!==true)throw new Error("当前账号未获准订阅，请联系管理员。");
      state.role=me.role;
      return person(me);
    }
    function notice(message,error=false){const text=node("p",message,error?"subscription-error":"subscription-note");text.setAttribute("role",error?"alert":"status");return text;}
    function header(){const head=node("div",null,"subscription-heading");head.append(node("h3","订阅确认"),button("收起","button ghost",()=>{if(!state.busy){root.hidden=true;entry.focus();}}));return head;}
    function failure(message){
      state.ready=false;root.replaceChildren(header(),notice(message,true),button("重新检查订阅状态","button",()=>open(state.reference)));
    }
    function subscriptionLink(id,label="查看我的订阅"){const link=node("a",label,"button");link.href=validId(id)?"/subscriptions/"+id:"/subscriptions";return link;}
    async function open(reference=state.reference){
      if(state.disposed||state.busy||state.loading||state.created)return;
      if(!validRef(reference))return;
      state.reference=reference;state.loading=true;state.ready=false;const generation=++state.generation;
      root.hidden=false;entry.disabled=true;root.replaceChildren(header(),notice("正在读取季集和订阅状态…"));
      try{
        const principal=await identity();if(!live(generation))return;
        const prepared=await request("/subscriptions/title-preview",{title_ref:reference});if(!live(generation))return;
        state.principal=principal;state.prepared=prepared;
        if(prepared.status==="ambiguous"){
          const choices=node("div",null,"subscription-candidates");
          for(const candidate of Array.isArray(prepared.candidates)?prepared.candidates:[]){if(!/^tmdb:(movie|tv):[1-9]\d{0,10}$/.test(candidate.title_ref||""))continue;choices.append(button([candidate.title,candidate.year].filter(Boolean).join(" · "),"button",()=>open(candidate.title_ref)));}
          if(!choices.children.length)throw new Error("暂时无法确认正确作品，请稍后重试。");
          root.replaceChildren(header(),notice("找到多个同名作品，请先确认你想订阅的版本。"),choices);return;
        }
        if(prepared.status!=="ready"||!prepared.media)throw new Error("MovieClaw 暂未找到可订阅的作品资料。");
        const media=prepared.media,canonical="tmdb:"+media.kind+":"+media.tmdb_id;
        if(!["movie","tv"].includes(media.kind)||!validId(media.tmdb_id)||reference.startsWith("tmdb:")&&canonical!==reference)throw new Error("返回的作品身份不一致，请重新检查。");
        if(validId(prepared.existing_subscription_id)){
          entry.textContent="已在订阅中";
          root.replaceChildren(header(),notice("这部作品已在订阅中，无需重复创建。"),subscriptionLink(prepared.existing_subscription_id));return;
        }
        const [smart,routing]=await Promise.all([request("/subscriptions/smart-profiles/"+media.kind),state.role==="admin"?request("/subscriptions/download-routing-preview?kind="+media.kind+"&tmdb_id="+media.tmdb_id).catch(()=>null):Promise.resolve(null)]);if(!live(generation))return;
        state.routing=routing;
        state.smart=smart?.kind===media.kind&&validId(smart.revision)&&smart.preferences?smart:null;
        state.mode=state.smart?"smart":"rules";
        const seen=new Set();state.allowed=(Array.isArray(prepared.seasons)?prepared.seasons:[]).filter(season=>integer(season.season_number)&&!seen.has(season.season_number)&&seen.add(season.season_number)).sort((a,b)=>a.season_number-b.season_number);
        const suggested=(Array.isArray(prepared.suggested_seasons)?prepared.suggested_seasons:[]).filter(n=>state.allowed.some(s=>s.season_number===n));
        state.seasons=new Set(suggested.length?suggested:state.allowed.filter(s=>s.season_number>0&&s.aired_count>0).map(s=>s.season_number));
        state.follow=media.kind==="tv"&&media.status==="Returning Series";state.floors=false;state.ready=true;render();root.scrollIntoView({block:"nearest",behavior:"instant"});
      }catch(error){if(live(generation))failure(error.message);}
      finally{if(live(generation)){state.loading=false;entry.disabled=false;}}
    }
    function render(){
      const prepared=state.prepared,media=prepared.media,form=node("form",null,"subscription-form"),content=node("div",null,"subscription-options");
      form.setAttribute("aria-label","确认订阅" );
      const summary=node("p",[media.title,media.year,media.kind==="tv"?"剧集":"电影"].filter(Boolean).join(" · "),"subscription-summary");
      const submit=button("确认订阅","button primary");submit.type="submit";
      const selection=node("p",null,"subscription-selection");
      const update=()=>{const selected=[...state.seasons].sort((a,b)=>a-b);selection.textContent=media.kind==="tv"?(selected.length?"已选："+selected.map(n=>n===0?"特别篇":"第 "+n+" 季").join("、"):"尚未选择季")+(state.follow?" · 自动追踪未来新季、新集":" · 仅追踪所选季"):"确认后创建电影订阅。";submit.disabled=media.kind==="tv"&&!selected.length&&!state.follow;};
      if(media.kind==="tv"){
        const seasons=node("fieldset",null,"subscription-seasons");seasons.append(node("legend","选择要订阅的季"));
        const checks=new Map(),quick=node("div",null,"subscription-quick");
        const choose=predicate=>{state.seasons=new Set(state.allowed.filter(predicate).map(s=>s.season_number));checks.forEach((input,n)=>input.checked=state.seasons.has(n));update();};
        quick.append(button("全选正季","button ghost",()=>choose(s=>s.season_number>0)),button("未收录完整的季","button ghost",()=>choose(s=>s.season_number>0&&(s.owned_count||0)<(s.episode_count||s.aired_count||0))),button("清空","button ghost",()=>choose(()=>false)));content.append(quick);
        for(const season of state.allowed){
          const label=node("label",null,"subscription-season"),input=node("input"),copy=node("span"),detail=[];
          input.type="checkbox";input.value=String(season.season_number);input.checked=state.seasons.has(season.season_number);
          checks.set(season.season_number,input);
          copy.append(node("strong",season.season_number===0?"特别篇 / 第 0 季":"第 "+season.season_number+" 季"));
          if(integer(season.episode_count))detail.push("共 "+season.episode_count+" 集");if(integer(season.aired_count))detail.push("已播 "+season.aired_count+" 集");if(season.owned_count>0)detail.push("已入库 "+season.owned_count+" 集");
          if(detail.length)copy.append(node("small",detail.join(" · ")));
          input.addEventListener("change",()=>{if(input.checked)state.seasons.add(season.season_number);else state.seasons.delete(season.season_number);update();});label.append(input,copy);seasons.append(label);
        }
        if(!state.allowed.length)seasons.append(notice("暂未公布季集，可开启自动追踪，或稍后重新检查。"));
        const follow=node("label",null,"subscription-follow"),check=node("input");check.type="checkbox";check.checked=state.follow;
        check.addEventListener("change",()=>{state.follow=check.checked;update();});follow.append(check,node("span","自动追踪未来新季和新集"));content.append(seasons,follow);
        const hint=prepared.douban_episodes;
        if(hint&&integer(hint.season_number)&&validId(hint.douban_count)&&hint.douban_count>hint.tmdb_count){const label=node("label",null,"subscription-follow"),input=node("input");input.type="checkbox";input.addEventListener("change",()=>state.floors=input.checked);label.append(input,node("span","按豆瓣提供的第 "+hint.season_number+" 季 "+hint.douban_count+" 集追踪（TMDB 暂录 "+hint.tmdb_count+" 集）"));content.append(label);}
      }else if(prepared.movie_owned)content.append(notice("媒体库中已有本片；仍可确认订阅，现有库存由 MovieClaw 判断。"));
      const policy=node("div",null,"subscription-policy");
      if(state.smart){
        const label=node("label","订阅策略"),select=node("select");select.setAttribute("aria-label","订阅策略");
        for(const [value,title] of [["smart","沿用已保存的智能设置"],["rules","使用现有默认规则"]]){const option=node("option",title);option.value=value;select.append(option);}select.value=state.mode;
        select.addEventListener("change",()=>state.mode=select.value);label.append(select);policy.append(label);
        const prefs=state.smart.preferences;policy.append(node("small",[(prefs.resolution==="2160p"?"4K":prefs.resolution),String(prefs.source||"").toUpperCase(),Number.isFinite(prefs.wait_seconds)?"最长等待 "+Math.round(prefs.wait_seconds/60)+" 分钟":null].filter(Boolean).join(" · ")));
      }else policy.append(node("p","使用 MovieClaw 现有默认订阅规则。"));
      policy.append(node("small","目标媒体库按 MovieClaw 现有入库路由自动选择。"));content.append(policy,selection);
      if(state.routing){content.append(notice("将存入："+(state.routing.library_name||"默认媒体库")+(state.routing.downloader_name?" · 下载器："+state.routing.downloader_name:"")));if(state.routing.ok===false)content.append(notice("入库配置提示："+(state.routing.warning||"当前入库链路需要检查，可到 MovieClaw 设置中处理。"),true));}
      const actions=node("div",null,"subscription-confirm-actions"),cancel=button("取消","button",()=>{if(!state.busy){root.hidden=true;entry.focus();}});actions.append(cancel,submit);
      const status=notice("");status.hidden=true;
      form.append(content,actions,status);form.addEventListener("submit",event=>{event.preventDefault();return create(submit,cancel,status);});
      root.replaceChildren(header(),summary,form);update();
    }
    async function create(submit,cancel,status){
      if(state.disposed||state.busy||!state.ready||state.created)return;
      const media=state.prepared.media,seasons=[...state.seasons].filter(n=>state.allowed.some(s=>s.season_number===n)).sort((a,b)=>a-b);
      if(media.kind==="tv"&&!seasons.length&&!state.follow)return;
      const generation=state.generation;state.busy=true;entry.disabled=true;root.setAttribute("aria-busy","true");submit.disabled=true;cancel.disabled=true;submit.textContent="正在创建订阅…";status.hidden=true;onBusy(true);
      const controls=[...root.querySelectorAll("input,select,button")];controls.forEach(control=>control.disabled=true);
      try{
        if(await identity()!==state.principal)throw new Error("登录账号已切换，请重新检查订阅状态后确认。");
        if(!live(generation))return;
        const payload={title_ref:"tmdb:"+media.kind+":"+media.tmdb_id,selected_seasons:media.kind==="tv"?seasons:[],follow_future:media.kind==="tv"&&state.follow,selection_mode:state.mode};
        if(item.detail_ref.startsWith("douban:"))payload.source_title_ref=item.detail_ref;
        if(state.mode==="smart")payload.smart_profile_revision=state.smart.revision;
        const hint=state.prepared.douban_episodes;if(state.floors&&hint&&(state.follow||seasons.includes(hint.season_number)))payload.episode_floors={[hint.season_number]:hint.douban_count};
        // 唯一创建点：只有这个表单的明确确认会 POST；不自动重试写请求。
        const result=await request("/subscriptions",payload);if(!live(generation))return;
        const subscription=result.subscription;if(!validId(subscription?.id)||subscription.media?.kind!==media.kind||subscription.media?.tmdb_id!==media.tmdb_id)throw new Error("服务器已响应，但订阅状态尚未确认，请重新检查。");
        state.created=true;state.ready=false;entry.textContent="已订阅";entry.disabled=true;
        root.replaceChildren(notice("订阅成功 · "+media.title),notice(media.kind==="tv"?"追踪范围："+(seasons.map(n=>n===0?"特别篇":"第 "+n+" 季").join("、")||"未来新集")+(state.follow?" · 自动续订开启":""):"电影订阅已创建。"),subscriptionLink(subscription.id));if(result.download_routing?.ok===false)root.append(notice("订阅已创建，但入库链路需要检查："+(result.download_routing.warning||"请检查默认媒体库和下载器设置。"),true));announce("已在当前账号下订阅 "+media.title);Promise.resolve().then(()=>onCreated()).catch(()=>{});
      }catch(error){if(live(generation)){failure(error.message+" 请先重新检查状态，避免重复提交。");}}
      finally{state.busy=false;if(!state.disposed){root.setAttribute("aria-busy","false");if(!state.created)entry.disabled=false;onBusy(false);}}
    }
    if(!validRef(item.detail_ref)){entry.disabled=true;entry.textContent="暂未匹配订阅条目";}
    return {entry,root,isSubmitting:()=>state.busy,dispose(){state.disposed=true;state.generation++;},open};
  }
};
