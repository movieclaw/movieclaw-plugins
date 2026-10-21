"use strict";
// 所有弹窗共用标题、关闭和滚动结构；不接触业务授权或表单提交逻辑。
window.MovieFriendsUI=(()=>{
  let sequence=0;
  const el=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;};
  function open(dialog,{title="详情"}={}){
    if(!dialog.classList.contains("ui-dialog")){
      let header=dialog.querySelector(":scope>.dialog-heading,:scope>.watch-dialog-head,:scope>.community-dialog-head,:scope>.watch-dialog-body>.section-heading");
      let body=dialog.querySelector(":scope>.watch-dialog-body,:scope>.community-dialog-content");
      if(!header){header=el("div");const heading=dialog.querySelector(":scope>h2")||el("h2","",title);header.append(heading);}
      header.classList.add("ui-dialog-head");dialog.prepend(header);
      const heading=header.querySelector("h2");if(heading){if(!heading.id)heading.id=`yingji-dialog-${++sequence}`;dialog.setAttribute("aria-labelledby",heading.id);}
      let close=header.querySelector("button");
      if(!close){close=el("button");close.type="button";close.onclick=()=>{if(dialog.dispatchEvent(new Event("cancel",{cancelable:true})))dialog.close();};header.append(close);}
      close.setAttribute("aria-label",close.getAttribute("aria-label")||"关闭");close.title=close.getAttribute("aria-label");close.classList.add("button","button-quiet","ui-dialog-close");close.replaceChildren(window.MovieFriendsIcons.create("x"));
      if(!body){body=el("div");for(const child of [...dialog.childNodes])if(child!==header)body.append(child);dialog.append(body);}
      body.classList.add("ui-dialog-body");const footer=body.querySelector(":scope>.ui-dialog-actions");if(footer){footer.classList.add("ui-dialog-footer");dialog.append(footer);}dialog.classList.add("ui-dialog");
    }
    if(!dialog.open)dialog.showModal();
  }
  // 未知总量和没有待写入项不是「0/0 已处理」；三处任务视图使用同一个口径。
  function progressText(run,name=()=>"当前平台"){
    if(run.status==="running"&&["reading","matching"].includes(run.stage))return `${run.stage==="matching"?"正在核对库存":"正在读取"} ${name(run.source_id)} · 已处理 ${run.read_count||0} 条`;
    if(run.total)return `已处理 ${run.processed||0} / ${run.total} · 剩余 ${run.remaining||0}`;
    if(run.status==="done")return "核对完成 · 无需新增写入";
    if(["attention","failed"].includes(run.status))return "检查结束 · 部分来源需核对";
    if(run.status==="running")return "正在整理待同步项目…";
    return "本轮未进入写入阶段";
  }
  const platforms={movieclaw:"MovieClaw",plex:"Plex",emby:"Emby"};
  function report(root,run,connections,kind,{dateText}){
    const names=id=>{const c=connections.find(c=>c.id===id);return c?`${platforms[c.platform]||c.platform} · ${c.account_name||"本人账号"}`:"历史连接";};
    const labels={running:"执行中",done:"已完成",attention:"部分项目需核对",failed:"连接或读取失败",paused:"已暂停",stopped:"本轮已停止",interrupted:"已中断"};
    const panel=el("section","sync-report"),top=el("div","sync-report-heading"),state=el("span",`sync-report-state is-${run.status}`),meta=el("small","field-help");
    state.append(window.MovieFriendsIcons.create(run.status==="done"?"check":run.status==="attention"||run.status==="failed"?"circle-alert":"clock"),document.createTextNode(labels[run.status]||run.status));meta.textContent=`${dateText(run.created_at)}${run.finished_at?` — ${dateText(run.finished_at)}`:""}`;top.append(state,meta);
    const copy=el("p","sync-report-copy",progressText(run,names)),track=el("progress");track.setAttribute("aria-label","本轮同步进度");track.max=Math.max(1,run.total||0);if(run.status!=="running"||!["reading","matching"].includes(run.stage))track.value=run.total?run.processed:run.status==="done"?1:0;
    panel.append(top,copy,track);if(run.current_media)panel.append(el("p","field-help",`正在核验：${run.current_media.title||"影片"}`));if(run.error)panel.append(el("p","error",run.error));
    const cards=el("div","sync-platform-grid");
    for(const [id,summary] of Object.entries(run.summary||{})){
      const connection=connections.find(c=>c.id===id),platform=connection?.platform||(id==="movieclaw"?"movieclaw":null),card=el("article","sync-platform-card"),heading=el("div","sync-platform-heading"),identity=el("div"),metrics=el("dl","sync-platform-metrics"),reads=el("div","sync-platform-read");
      card.dataset.platform=platform||"history";identity.append(el("h3","",platforms[platform]||"历史连接"),el("small","field-help",connection?.account_name||"本人账号"));heading.append(window.MovieFriendsIcons.create(platform==="movieclaw"?"list-video":platform==="emby"?"clapperboard":"film"),identity);reads.append(el("strong","",Number(summary.read||0).toLocaleString("zh-CN")),el("span","","读取条目"));
      const count=state=>(run.counts||[]).filter(r=>r.source_id===id&&r.state===state).reduce((n,r)=>n+r.count,0),values=[["已同步",count("done")],["失败",count("failed")],["待核验",count("uncertain")]];if(kind!=="resume")values.splice(1,0,["已有已看",(summary.already||0)+count("skipped")]);
      for(const [label,value] of values){const cell=el("div");cell.append(el("dt","",label),el("dd","",String(value)));metrics.append(cell);}card.append(heading,reads,metrics);if(summary.error)card.append(el("p","sync-platform-error",summary.error));cards.append(card);
    }
    root.replaceChildren(panel,cards);
  }
  return {open,progressText,report};
})();
