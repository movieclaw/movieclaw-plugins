"use strict";
// 两种后台任务共用紧凑状态卡；折叠状态仅留在当前账号的页面会话，不改服务器设置。
window.MovieFriendsSyncCard=(()=>{
  let sessionKey;const folds=new Map();
  const el=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;};
  const labels={running:"执行中",done:"已完成",attention:"部分项目需核对",failed:"连接或读取失败",paused:"已暂停",stopped:"本轮已停止",interrupted:"已中断"};
  function create(root,{key,session,title,subtitle,icon,help}){
    if(sessionKey!==session){folds.clear();sessionKey=session;}
    root.classList.add("sync-card");const header=el("div","sync-card-header"),mark=el("span","sync-card-icon"),copy=el("div","sync-card-copy"),heading=el("h3","",title),badge=el("span","auto-sync-badge"),status=el("div","sync-card-status"),details=el("details","sync-card-config"),summary=el("summary"),form=el("form","auto-sync-form");
    mark.append(window.MovieFriendsIcons.create(icon));heading.append(help);copy.append(heading,el("small","field-help",subtitle));header.append(mark,copy,badge);
    summary.append(window.MovieFriendsIcons.create("chevron-right"),el("span","","同步设置"),el("small","sync-config-hint","展开配置"));const footer=el("div","sync-config-footer"),collapse=el("button","button button-outline sync-config-collapse");collapse.type="button";collapse.append(window.MovieFriendsIcons.create("chevron-right"),document.createTextNode("收起同步设置"));collapse.onclick=()=>{details.open=false;summary.focus({preventScroll:true});root.scrollIntoView({block:"start",behavior:"instant"});};footer.append(collapse);details.append(summary,form,footer);details.open=folds.get(key)||false;summary.lastElementChild.textContent=details.open?"收起配置":"展开配置";
    details.addEventListener("toggle",()=>{folds.set(key,details.open);summary.lastElementChild.textContent=details.open?"收起配置":"展开配置";});
    root.replaceChildren(header,status,details);return {badge,status,form};
  }
  function render(card,view,{bridge,button,sourceName,detail,actions,history}){
    const {badge,status}=card,run=view.runs.find(r=>r.status==="running")||view.runs[0],running=run?.status==="running";
    badge.textContent=view.enabled?"已开启":"已关闭";badge.classList.toggle("is-on",view.enabled);status.replaceChildren();
    const line=el("div","sync-state-line");line.append(el("strong","",run?(labels[run.status]||run.status):"尚未运行"));if(run)line.append(el("small","field-help",bridge.dateText(run.created_at)));status.append(line);
    if(run){
      const reading=running&&(!run.total||run.stage==="reading"),progress=el("progress","sync-card-progress");progress.max=Math.max(1,run.total||0);progress.setAttribute("aria-label","同步进度");
      if(!reading)progress.value=run.total?(run.processed||0):run.status==="done"?1:0;
      status.append(progress,el("p","sync-progress-copy",window.MovieFriendsUI.progressText(run,sourceName)));
      if(run.current_media)status.append(el("small","field-help",`正在核验：${run.current_media.title||"影片"}`));
      if(run.error)status.append(el("small","error",run.error));
    }else status.append(el("p","sync-progress-copy muted",view.enabled?"等待下一次检查":"开启后可在这里查看实时进度"));
    const schedule=running?"本轮执行中，结束后安排下一次检查":view.enabled&&view.next_at?`下次检查 ${bridge.dateText(view.next_at)}`:view.last_checked_at?`最近检查 ${bridge.dateText(view.last_checked_at)}`:"";
    if(schedule)status.append(el("small","field-help sync-schedule",schedule));
    // 全局 error 可能来自上一轮；本轮运行时只展示本轮错误，旧结果仍可在历史中核对。
    if(view.error&&!running&&view.error!==run?.error)status.append(el("small","error",view.error));
    const controls=el("div","sync-card-actions");if(run)controls.append(button(detail.label,()=>detail.open(run),"button-quiet"));controls.append(button(history.label,history.open,"button-quiet"),...actions);status.append(controls);
  }
  return {create,render};
})();
