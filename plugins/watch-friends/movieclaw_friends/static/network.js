"use strict";
// 联网名片拥有独立授权；界面不解析连接码，也不显示网络地址。
window.MovieFriendsNetwork = (() => {
  let bridge, generation=0, directoryVersion=0, timer, leaseTimer, scope="local", expires=0, view;
  const byId=id=>document.getElementById(id);
  const el=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;};
  const check=(text,value=false)=>{const label=el("label","watch-check"),input=el("input");input.type="checkbox";input.checked=value;label.append(input,el("span","",text));return {label,input};};
  const alive=(session,version)=>bridge.user()&&session===bridge.sessionVersion()&&version===generation;
  async function api(path,options) {
    if(options?.method&&options.method!=="GET") {
      const owner=bridge.user()?.id,session=bridge.sessionVersion();
      const current=await bridge.api("/me");
      if(!owner||current.user?.id!==owner||session!==bridge.sessionVersion()) {
        const error=new Error("登录账号已改变，请刷新页面后再操作。");error.accountChanged=true;throw error;
      }
    }
    return bridge.api(path,options);
  }
  function button(text,fn,cls="button-outline") {const b=window.MovieFriendsIcons.label(el("button",`button ${cls}`),text);b.type="button";b.onclick=async()=>{b.disabled=true;try{await fn();}catch(e){if(!e.staleSession)bridge.toast(bridge.safeError(e),true);}finally{b.disabled=false;}};return b;}
  function modal(title) {const old=byId("network-dialog");if(old)old.remove();const d=el("dialog","community-dialog network-dialog");d.id="network-dialog";const head=el("div","community-dialog-head"),body=el("div","community-dialog-content");head.append(el("h2","",title),button("关闭 ✕",()=>d.close(),"button-quiet"));d.append(head,body);d.onclose=()=>d.remove();document.body.append(d);window.MovieFriendsUI.open(d);return {d,body};}
  function info(title,text){return window.MovieFriendsHelp.create(title,text);}
  function cardContent(card,root,interactive=false) {
    const head=el("div","network-person"),avatar=el("div","avatar");
    if(typeof card.avatar_data==="string"&&card.avatar_data.startsWith("data:image/jpeg;base64,")){const image=el("img");image.src=card.avatar_data;image.alt=`${card.nickname}的头像`;avatar.append(image);}else avatar.textContent=(card.nickname||"影").slice(0,1);
    const name=el("div","network-person-name");name.append(el("h3","",card.nickname));if(card.is_self)name.append(el("span","network-self-label","我的联网名片 · 已发布"));head.append(avatar,name);root.append(head);if(card.bio)root.append(el("p","muted",card.bio));
    for(const [field,title] of [["favorites","喜欢的电影"],["recommendations","公开推荐"],["history","近期观看"]]){
      if(!card[field]?.length)continue;
      const section=el("section",`network-film-section network-film-section--${field}`),heading=el("div","network-film-heading");
      heading.append(el("h4","",title),el("span","network-film-count",`${card[field].length} ${field==="recommendations"?"条":"部"}`));
      const shelf=el("div","network-movies");section.append(heading,shelf);
      for(const [index,item] of card[field].entries()){
        const media=item.media||item,row=el("article","network-movie"),poster=bridge.mediaPoster(media,false,false);
        const open=button("",()=>bridge.showMedia(media),"network-poster");
        open.setAttribute("aria-label",`${media.title||"未命名影片"}，查看影片详情`);open.append(poster);
        const copy=el("div","network-movie-copy"),title=bridge.mediaTitle(media);title.title=media.title||"未命名影片";copy.append(title);
        if(item.rating){
          const rating=el("div","network-movie-rating"),stars=el("span","profile-recommendation-rating");
          rating.setAttribute("aria-label",`${item.rating} / 5 星`);stars.setAttribute("aria-hidden","true");
          for(let i=1;i<=5;i++)stars.append(window.MovieFriendsIcons.create("star",i<=item.rating?"is-filled":""));
          rating.append(stars,el("small","",`${item.rating} / 5`));copy.append(rating);
        }
        if(item.body)copy.append(el("p","network-movie-review",item.body));
        if(item.ended_at){const date=el("time","network-movie-date",`观看于 ${bridge.dateText(item.ended_at)}`);date.dateTime=item.ended_at;copy.append(date);}
        if(interactive&&field==="recommendations"&&!card.is_self&&!card.discovered)copy.append(button("点赞与评论",()=>interact(card,card.recommendation_keys[index]),"button-quiet"));
        row.append(open,copy);shelf.append(row);
      }
      root.append(section);
    }
  }
  async function friend(card){
    const {d,body}=modal("申请跨服好友");body.append(el("p","",`向 ${card.nickname} 申请好友。确认后双方服务器会交换网络地址，对方可能记录地址及获准共享的数据；页面隐藏地址不等于匿名。双方同意前不能读取私人观影记录。各项共享仍按设置独立控制。`));const agree=check("我已了解地址与共享提示，同意申请");body.append(agree.label,button("确认申请",async()=>{if(!agree.input.checked)throw new Error("请先确认共享提示");await api(`/network/users/${encodeURIComponent(card.id)}/interact`,{method:"POST",body:{action:"invite",consent:true}});d.close();bridge.toast("好友申请已送达，等待对方确认");},"button-primary"));
  }
  async function interact(card,key){
    const {body}=modal("公开推荐互动"),summary=el("div"),comments=el("div"),form=el("form"),input=el("textarea"),agree=check("同意向对方服务器和推荐查看者公开昵称、头像及评论"),more=el("div");let current,number=null;
    input.maxLength=500;input.placeholder="聊聊你的看法";input.setAttribute("aria-label","联网推荐评论");form.append(input,agree.label);body.append(summary,comments,more,form);
    const call=data=>api(`/network/users/${encodeURIComponent(card.id)}/interact`,{method:"POST",body:{post_key:key,...data}});
    async function load(before=null){const result=await call({action:"view",before});current=result;summary.replaceChildren(el("p","muted",`点赞 ${result.like_count}`),button(result.liked?"取消点赞":"点赞",async()=>{await call({action:"like",liked:!current.liked,consent:true});await load();}));if(!before)comments.replaceChildren();for(const row of result.comments){const line=el("article","network-comment");line.append(el("strong","",row.author.nickname),el("p","",row.body),el("small","muted",bridge.dateText(row.created_at)));if(row.is_self)line.append(button("删除评论",async()=>{await call({action:"delete_comment",comment_id:row.id,consent:true});await load();},"button-quiet"));comments.append(line);}more.replaceChildren();if(result.next_before!==null)more.append(button("更多评论",()=>load(result.next_before)));}
    form.append(button("发送评论",async()=>{if(!agree.input.checked||!input.value.trim())throw new Error("请填写评论并确认公开范围");number=number||crypto.randomUUID();await call({action:"comment",body:input.value.trim(),request_id:number,consent:true});number=null;input.value="";await load();},"button-primary"));form.onsubmit=e=>e.preventDefault();input.oninput=()=>{number=null;};
    body.append(button("举报推荐",async()=>{const reason=el("textarea"),consent=check("同意将举报理由与昵称提交给来源服务器管理员");reason.maxLength=500;reason.setAttribute("aria-label","举报理由");body.append(reason,consent.label,button("提交举报",async()=>{if(!consent.input.checked||!reason.value.trim())throw new Error("请填写理由并确认提交");await call({action:"report",body:reason.value.trim(),consent:true});bridge.toast("举报已提交来源服务器");}));},"button-quiet"));await load();
  }
  async function reports(){const {body}=modal("互联举报审核");let before=null;const list=el("div"),more=el("div");body.append(list,more);async function load(){const result=await api(`/network/admin/reports${before?`?before=${before}`:""}`);for(const row of result.reports){const line=el("article","network-report panel");line.append(el("h3","",row.snapshot.media.title),el("p","",row.reason),el("small","muted",row.status==="pending"?"待审核":"已处理"));for(const [action,label] of [["hide","隐藏推荐"],["unhide","恢复推荐"],["dismiss","忽略举报"]])line.append(button(label,async()=>{await api(`/network/admin/reports/${row.id}/resolve`,{method:"POST",body:{action}});bridge.toast("审核已记录");},"button-quiet"));list.append(line);}more.replaceChildren();if(result.next_before!==null){before=result.next_before;more.append(button("更多举报",load));}if(!list.children.length)list.append(el("p","muted","暂无互联举报。"));}await load();}
  async function settings() {
    const session=bridge.sessionVersion(),version=++generation,root=byId("network-settings");
    // 先完整读取再一次性替换，后台刷新不能把用户刚展开的设置重新收起。
    const isAdmin=bridge.user().role==="admin";
    const [result,adminState,discoveryState]=await Promise.all([api("/network/settings"),isAdmin?api("/network/admin"):null,isAdmin?api("/discovery/admin"):null]);
    if(!alive(session,version))return;view=result;
    const opened=[...root.querySelectorAll("details[id][open]")].map(n=>n.id);root.replaceChildren();
    const section=el("section","panel network-settings"),heading=el("div","section-heading"),title=el("h3","","联网广场");title.append(info("联网广场","与管理员批准的服务器互联。发布、转发、浏览独立设置，默认关闭。联网公开包含本服公开，好友权限单独管理。名片只保留本次预览确认的影片，后续新增内容需重新预览。"));heading.append(title,el("span","small-label",result.server_enabled?`互联已启用 · ${result.peer_count||0} 个直连 · ${result.discovery?.seed_count||0} 个公共入口`:"服务器互联未开启"));section.append(heading);
    const browse=check("显示外服影友",result.browse);browse.label.append(el("small","settings-auto-label","自动保存"));browse.input.id="network-browse";browse.input.onchange=async()=>{browse.input.disabled=true;try{await api("/network/browse",{method:"PUT",body:{enabled:browse.input.checked}});if(alive(session,version)){view.browse=browse.input.checked;bridge.toast("浏览偏好已保存");}}catch(e){if(alive(session,version)){browse.input.checked=!browse.input.checked;bridge.toast(bridge.safeError(e),true);}}finally{browse.input.disabled=false;}};section.append(browse.label);
    const form=el("form","network-publication"),publish=check("将我的公开名片发布到联网广场",result.options.publish),forward=check("允许公开名片继续转发（最多两次传递）",result.options.forward),favorites=check("喜欢的电影（最多 12 部）",result.options.favorites),recommendations=check("公开推荐（最多 6 条）",result.options.recommendations),recent=check("近期观看（最多 5 条，包含已确认导入的记录）",result.options.recent),discovery=check("允许通过公共节点被其他服务器的用户发现",result.options.discovery);
    discovery.label.append(info("公共发现","需管理员先开启公共发现。此选项另行授权名片在公共目录传播，不受可信服务器两次传递限制；私人记录不会随之开放。没有公共入口时不会自动发现互联网用户。"));discovery.input.id="network-discovery";
    for(const [key,control] of Object.entries({publish,forward,favorites,recommendations,recent,discovery}))control.input.name=key;
    publish.input.id="network-publish";forward.input.id="network-forward";favorites.input.id="network-favorites";recommendations.input.id="network-recommendations";recent.input.id="network-recent";
    form.append(publish.label,forward.label,discovery.label,el("h4","","选择名片内容"),favorites.label,recommendations.label,recent.label);
    const actions=el("div","watch-row-actions"),preview=button("预览联网名片",async()=>{
      const options={publish:publish.input.checked,forward:forward.input.checked,favorites:favorites.input.checked,recommendations:recommendations.input.checked,recent:recent.input.checked,discovery:discovery.input.checked};if(!options.publish){if(!result.published)return;await api("/network/publication",{method:"PUT",body:{publish:false}});if(!alive(session,version))return;window.MovieFriendsSettings.clear("network-publication",session);await settings();bridge.toast("联网名片已撤下，已发送撤回通知");return;}
      window.MovieFriendsSettings.requireSaved("display-policy",session,"媒体库展示范围");
      const projected=await api("/network/preview",{method:"POST",body:options});if(!alive(session,version))return;
      const {d,body}=modal("确认联网名片");cardContent(projected.card,body);body.append(el("p","network-consent",projected.notice));const consent=check("我已了解网络地址及转发范围，同意发布以上具体内容");consent.input.id="network-public-consent";const confirm=button("确认发布",async()=>{if(!consent.input.checked)return;await api("/network/publication",{method:"PUT",body:{...options,consent:true,preview_token:projected.preview_token}});if(!alive(session,version))return;d.close();window.MovieFriendsSettings.clear("network-publication",session);await settings();bridge.toast("联网名片已发布");},"button-primary");confirm.disabled=true;consent.input.onchange=()=>{confirm.disabled=!consent.input.checked;};body.append(consent.label,confirm);
    },"button-primary");preview.disabled=!result.server_enabled;
    actions.append(preview);if(result.published)actions.append(button("撤下联网名片",async()=>{await api("/network/publication",{method:"PUT",body:{publish:false}});if(alive(session,version)){window.MovieFriendsSettings.clear("network-publication",session);await settings();bridge.toast("联网名片已撤下，已发送撤回通知");}}));form.append(actions);form.onsubmit=e=>e.preventDefault();
    const updatePublication=()=>{preview.disabled=publish.input.checked?!result.server_enabled:!result.published;window.MovieFriendsIcons.label(preview,!publish.input.checked&&result.published?"保存并撤下名片":"预览联网名片");for(const control of [forward,favorites,recommendations,recent])control.input.disabled=!publish.input.checked;discovery.input.disabled=!publish.input.checked||(!result.discovery?.active&&!discovery.input.checked);};
    window.MovieFriendsSettings.bind(form,{key:"network-publication",session,onRestore:updatePublication});form.addEventListener("change",updatePublication);section.append(form);
    const blocks=el("details","community-blocks");blocks.append(el("summary","",`已屏蔽外服影友 ${result.blocks.length}`));for(const row of result.blocks){const line=el("div","community-row");line.append(el("span","",row.nickname),button("解除屏蔽",async()=>{await api(`/network/blocks/${encodeURIComponent(row.actor)}`,{method:"DELETE"});if(alive(session,version))await settings();},"button-quiet"));blocks.append(line);}section.append(blocks);root.append(section);
    if(isAdmin){await admin(section,session,version,adminState);await discoveryAdmin(section,session,version,discoveryState);}if(alive(session,version))for(const id of opened){const panel=byId(id);if(panel)panel.open=true;}
  }
  async function admin(section,session,version,state){
    const result=state;if(!alive(session,version))return;
    const panel=el("details","network-admin"),summary=el("summary","","互联服务器管理"),body=el("div");panel.id="network-admin";panel.append(summary,body);section.append(panel);
    const status=el("span","network-saved-state",result.enabled?"已保存 · 互联已启用":"已保存 · 互联未启用");
    const enabled=check("允许本服务器加入影友互联网络",result.enabled),consent=check("确认仅连接可信服务器，并知悉双方管理员可能记录网络地址"),save=button("保存服务器互联设置",async()=>{
      await api("/network/admin",{method:"PUT",body:{enabled:enabled.input.checked,consent:consent.input.checked}});
      if(alive(session,version)){window.MovieFriendsSettings.clear("network-admin",session);await settings();byId("network-admin").open=true;bridge.toast("服务器互联设置已保存");}
    });
    enabled.input.id="network-server-enabled";consent.input.id="network-admin-consent";
    consent.label.hidden=result.enabled||!enabled.input.checked;
    consent.label.append(info("启用确认","此确认仅在首次启用或停用后重新启用时填写，不作为常驻开关。服务器是否参与互联以上方已保存状态为准；添加新服务器仍需单独确认。"));
    enabled.input.onchange=()=>{consent.input.checked=false;consent.label.hidden=result.enabled||!enabled.input.checked;status.textContent=enabled.input.checked===result.enabled?(result.enabled?"已保存 · 互联已启用":"已保存 · 互联未启用"):"有更改待保存";};
    const serverForm=el("form","network-server-form");enabled.input.name="enabled";serverForm.onsubmit=e=>e.preventDefault();serverForm.append(enabled.label,consent.label,save);window.MovieFriendsSettings.bind(serverForm,{key:"network-admin",session,onRestore:()=>enabled.input.onchange()});body.append(status,el("small","muted",`本服签名身份 ${result.node.slice(0,16)}`),serverForm);
    const controls=el("div","network-admin-actions"),invite=button("生成一次性连接码",async()=>{const data=await api("/network/admin/invite",{method:"POST"});if(!alive(session,version))return;const {body}=modal("服务器连接码");body.append(el("p","muted","有效期 30 分钟，仅交给可信服务器管理员。对方申请后还需你批准；重新生成会使旧码失效。"));const code=el("textarea");code.readOnly=true;code.value=data.code;code.setAttribute("aria-label","一次性服务器连接码");body.append(code);});invite.disabled=!result.enabled;controls.append(invite);body.append(controls,button("互联举报审核",reports,"button-quiet"));
    const form=el("form","network-connect"),name=el("input"),code=el("textarea"),agreement=check("确认信任对方管理员，并允许双方交换网络地址");name.placeholder="给对方服务器起个名称";name.required=true;name.maxLength=60;name.setAttribute("aria-label","互联服务器名称");code.placeholder="粘贴对方管理员提供的一次性连接码";code.required=true;code.maxLength=4096;code.setAttribute("aria-label","对方服务器连接码");const connect=el("button","button button-outline","申请连接");connect.type="submit";connect.disabled=!result.enabled;form.append(name,code,agreement.label,connect);form.onsubmit=async e=>{e.preventDefault();connect.disabled=true;try{await api("/network/admin/connect",{method:"POST",body:{name:name.value.trim(),code:code.value.trim(),consent:agreement.input.checked}});if(alive(session,version)){await settings();byId("network-admin").open=true;bridge.toast("已申请连接，等待对方管理员批准");}}catch(error){if(alive(session,version))bridge.toast(bridge.safeError(error),true);}finally{connect.disabled=false;}};body.append(form);
    const peers=el("div","network-peer-list"),states={pending:"等待本服批准",accepted:"本服已批准",paused:"已停用",blocked:"已屏蔽"};
    for(const peer of result.peers){const row=el("article","network-peer panel"),head=el("div","section-heading");head.append(el("h4","",peer.name),el("small","muted",states[peer.state]||peer.state));row.append(head,el("small","muted",`签名身份 ${peer.node.slice(0,16)}`));if(peer.remote_state)row.append(el("p","muted",`对方：${states[peer.remote_state]||"等待确认"}`));if(peer.last_at)row.append(el("small","muted",`上次连接 ${bridge.dateText(new Date(peer.last_at*1000).toISOString())}`));if(peer.error)row.append(el("p","error",peer.error));const actions=el("div","watch-row-actions");for(const [state,label] of [["accepted",peer.state==="pending"?"批准连接":"启用连接"],["paused","停用连接"],["blocked","屏蔽服务器"]])if(state!==peer.state)actions.append(button(label,async()=>{await api(`/network/admin/peers/${encodeURIComponent(peer.id)}`,{method:"PATCH",body:{state}});if(alive(session,version)){await settings();byId("network-admin").open=true;}}));actions.append(button("重试连接",async()=>{await api(`/network/admin/peers/${encodeURIComponent(peer.id)}/retry`,{method:"POST"});await settings();byId("network-admin").open=true;}));
      if(peer.state==="accepted")actions.append(button("轮换连接凭据",async()=>{await api(`/network/admin/peers/${encodeURIComponent(peer.id)}/rotate`,{method:"POST"});await settings();byId("network-admin").open=true;bridge.toast("轮换已处理；若仍显示待确认，请重试连接");}));
      actions.append(button("移除连接",async()=>{const {d,body}=modal("移除互联服务器");body.append(el("p","muted","移除后停止交换名片，已缓存名片最迟五分钟过期。好友关系另行管理；再次连接需要新的连接码和管理员批准。"),button("确认移除",async()=>{const result=await api(`/network/admin/peers/${encodeURIComponent(peer.id)}`,{method:"DELETE"});d.close();await settings();byId("network-admin").open=true;bridge.toast(result.remote_notified?"双方连接已移除":"本服连接已移除；对方离线，缓存将到期隐藏");}));},"button-quiet"));row.append(actions);peers.append(row);}body.append(peers,button("刷新连接状态",async()=>{await settings();byId("network-admin").open=true;},"button-quiet"));
  }
  async function discoveryAdmin(section,session,version,state){
    if(!alive(session,version))return;
    const panel=el("details","network-admin discovery-admin"),summary=el("summary","","公共发现（可选）"),body=el("div");panel.id="discovery-admin";panel.append(summary,body);section.append(panel);
    summary.append(info("公共发现入口","安装插件不会自动公开账号。管理员接入可选公共节点后，用户还需单独预览并确认发布。未配置入口时无法凭空发现互联网用户；可以继续使用可信服务器连接和邀请码。"));
    const status=el("div","discovery-status"),copy=el("div");copy.append(el("strong","",state.active?(state.seed_count?`已连接 ${state.seed_count} 个公共入口`:"已开启 · 尚未接入公共入口"):"公共发现未开启"),el("small","muted",`${state.available} 张有效外服名片 · 名片最迟五分钟到期`));status.append(window.MovieFriendsIcons.create("compass"),copy);body.append(status);
    const form=el("form","network-server-form"),enabled=check("开启公共发现",state.enabled),serve=check("将本服提供为公共发现节点",state.serve),consent=check("了解公共节点与通信参与者可能记录服务器地址；仅主动授权的公开名片会被分发");
    enabled.input.id="discovery-enabled";serve.input.id="discovery-serve";consent.input.id="discovery-admin-consent";enabled.input.name="enabled";serve.input.name="serve";
    const dirty=()=>{consent.input.checked=false;serve.input.disabled=!enabled.input.checked;if(!enabled.input.checked)serve.input.checked=false;};enabled.input.onchange=dirty;serve.input.onchange=()=>{consent.input.checked=false;};serve.input.disabled=!enabled.input.checked;
    form.onsubmit=e=>e.preventDefault();form.append(enabled.label,serve.label,consent.label,button("保存公共发现设置",async()=>{await api("/discovery/admin",{method:"PUT",body:{enabled:enabled.input.checked,serve:serve.input.checked,consent:consent.input.checked}});if(!alive(session,version))return;window.MovieFriendsSettings.clear("discovery-admin",session);await settings();byId("discovery-admin").open=true;bridge.toast("公共发现设置已保存；用户发布授权独立设置");}));window.MovieFriendsSettings.bind(form,{key:"discovery-admin",session,onRestore:dirty});body.append(form);
    const tools=el("div","watch-row-actions"),refresh=button("刷新公共目录",async()=>{await api("/discovery/refresh",{method:"POST"});if(!alive(session,version))return;await settings();byId("discovery-admin").open=true;});refresh.disabled=!state.active||!state.seed_count;
    const exportCode=button("查看本服公共节点接入码",async()=>{const result=await api("/discovery/admin/code",{method:"POST"});if(!alive(session,version))return;const {body}=modal("公共发现节点接入码"),code=el("textarea");code.readOnly=true;code.value=result.code;code.setAttribute("aria-label","公共节点接入码");body.append(el("p","muted","此码包含服务器地址和公开签名信息，不含好友访问凭据。仅在你主动提供公共节点服务时，交给其他服务器管理员使用。"),code);});exportCode.disabled=!state.active||!state.serve;tools.append(refresh,exportCode);body.append(tools);
    const connect=el("form","network-connect"),name=el("input"),code=el("textarea"),agree=check("确认接入此公共节点，并向其发送用户已另行授权的名片");name.placeholder="公共节点名称";name.setAttribute("aria-label","公共节点名称");name.maxLength=60;name.required=true;code.placeholder="粘贴公共节点管理员提供的 mcd1 接入码";code.setAttribute("aria-label","公共节点接入码");code.maxLength=4096;code.required=true;const add=el("button","button button-outline","添加公共入口");add.type="submit";add.disabled=!state.active;connect.append(name,code,agree.label,add);connect.onsubmit=async e=>{e.preventDefault();add.disabled=true;try{await api("/discovery/admin/seeds",{method:"POST",body:{name:name.value.trim(),code:code.value.trim(),consent:agree.input.checked}});if(alive(session,version)){await settings();byId("discovery-admin").open=true;bridge.toast("公共入口已添加，目录将每分钟刷新");}}catch(error){bridge.toast(bridge.safeError(error),true);}finally{add.disabled=!state.active;}};body.append(connect);
    const list=el("div","network-peer-list");for(const seed of state.seeds){const row=el("article","network-peer panel"),head=el("div","section-heading");head.append(el("h4","",seed.name),button("移除入口",async()=>{await api(`/discovery/admin/seeds/${encodeURIComponent(seed.id)}`,{method:"DELETE"});if(alive(session,version)){await settings();byId("discovery-admin").open=true;}} ,"button-quiet"));row.append(head,el("small","muted",seed.last_at?`上次连接 ${bridge.dateText(new Date(seed.last_at*1000).toISOString())}`:"等待首次读取"));if(seed.error)row.append(el("p","error",seed.error));list.append(row);}body.append(list);
  }
  async function loadDirectory(){
    if(scope!=="network"||!bridge.user())return;const version=++directoryVersion,session=bridge.sessionVersion(),root=byId("network-directory");root.replaceChildren(el("p","busy-text","正在读取联网名片…"));expires=0;
    try{const result=await api("/network/settings");if(version!==directoryVersion||session!==bridge.sessionVersion()||scope!=="network")return;
      if(!result.server_enabled||!result.browse){root.replaceChildren(el("p","muted",!result.server_enabled?"管理员尚未开启服务器互联。":"开启显示外服影友后，可以浏览联网名片，无需公开自己。"),button("前往互联设置",()=>bridge.settings()));return;}
      const data=await api("/network/directory");if(version!==directoryVersion||session!==bridge.sessionVersion()||scope!=="network")return;root.replaceChildren();let firstExpiry=Infinity;
      for(const card of data.users||[]){if(card.expires_at*1000<=Date.now())continue;firstExpiry=Math.min(firstExpiry,card.expires_at*1000);const article=el("article","panel network-card");cardContent(card,article,true);const foot=el("div","watch-row-actions");
        if(card.is_self){article.classList.add("network-card--self");foot.append(button("编辑联网名片",()=>bridge.settings(),"button-quiet"));}
        else foot.append(button("申请好友",()=>friend(card)),el("small","muted",card.discovered?"公共发现 · 私人访问需双方同意":card.hops===2?"经可信服务器转发":"来自直连服务器"),button("屏蔽影友",async()=>{await api("/network/blocks",{method:"POST",body:{actor:card.id}});await loadDirectory();},"button-quiet"));article.append(foot);root.append(article);}
      expires=firstExpiry;clearTimeout(leaseTimer);if(Number.isFinite(expires))leaseTimer=setTimeout(()=>{if(version===directoryVersion){root.replaceChildren(el("p","muted","名片授权已到期，正在重新确认…"));loadDirectory();}},Math.max(0,expires-Date.now()));if(!root.children.length)root.append(el("p","muted",result.peer_count||result.discovery?.seed_count?"暂时没有外服影友发布名片，目录每分钟更新。":"尚未连接其他服务器或公共发现节点。请在设置中添加入口，或使用好友邀请码。"));
    }catch(error){if(version===directoryVersion&&session===bridge.sessionVersion())root.replaceChildren(el("p","error",bridge.safeError(error)));}
  }
  function setScope(value){scope=value;byId("network-directory-refresh").hidden=value!=="network";directoryVersion++;byId("local-directory").hidden=value!=="local";byId("network-directory").hidden=value!=="network";for(const b of byId("directory-scope").querySelectorAll("button"))b.setAttribute("aria-pressed",String(b.dataset.scope===value));if(value==="network")loadDirectory();}
  function start(){clearInterval(timer);timer=setInterval(()=>{if(!bridge.user()||scope!=="network")return;if(Date.now()>=expires)byId("network-directory").replaceChildren();if(!document.hidden&&!byId("panel-directory").hidden)loadDirectory();},60000);}
  document.addEventListener("visibilitychange",()=>{if(!document.hidden&&scope==="network"){if(Date.now()>=expires)byId("network-directory").replaceChildren();loadDirectory();}});
  return {configure(value){bridge=value;for(const b of byId("directory-scope").querySelectorAll("button"))b.onclick=()=>setScope(b.dataset.scope);byId("network-directory-refresh").onclick=()=>loadDirectory();start();},settings,loadDirectory,renderCard:cardContent,reset(){generation++;directoryVersion++;clearTimeout(leaseTimer);scope="local";byId("network-directory-refresh").hidden=true;byId("local-directory").hidden=false;byId("network-directory").hidden=true;for(const b of byId("directory-scope").querySelectorAll("button"))b.setAttribute("aria-pressed",String(b.dataset.scope==="local"));byId("network-settings").replaceChildren();byId("network-directory").replaceChildren();byId("network-dialog")?.close();view=null;expires=0;}};
})();
