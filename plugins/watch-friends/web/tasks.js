"use strict";
// 查看历史不会触发执行；只有本人明确点击任务控制才暂停、停止或重新核验。
window.MovieFriendsTasks = (() => {
  let bridge;
  const el=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;};
  const names={movieclaw:"MovieClaw",emby:"Emby",plex:"Plex"};
  const states={reading:"读取中",preview:"待确认导入",complete:"已导入",ready:"预览完成",native:"待核对本服",failed:"任务未完成",running:"执行中",completed:"已完成",stopped:"已停止",interrupted:"执行中断",done:"已完成",attention:"需核对",paused:"已暂停"};
  function button(text,action){const n=el("button","button button-outline",text);n.type="button";n.onclick=async()=>{n.disabled=true;try{await action();}catch(e){bridge.toast(bridge.safeError(e),true);}finally{n.disabled=false;}};return n;}
  async function open(){
    const session=bridge.sessionVersion(),owner=bridge.user().id,modal=el("dialog","watch-dialog task-center-dialog"),body=el("div","watch-dialog-body"),head=el("div","section-heading"),filters=el("div","watch-row-actions"),select=el("select"),expand=el("input"),expandLabel=el("label","task-duplicate-toggle"),list=el("div","task-list"),more=el("div","personal-more"),status=el("p","watch-status");
    select.setAttribute("aria-label","任务类型");for(const [value,text] of [["all","全部任务"],["import","观影记录导入"],["alignment","三平台已看对齐"],["auto_sync","后台自动同步"],["resume","续播同步"]]){const option=el("option","",text);option.value=value;select.append(option);}
    expand.type="checkbox";expandLabel.append(expand,document.createTextNode("展开重复核对"));
    head.append(el("h2","","观影数据任务中心"),button("关闭",()=>modal.close()));filters.append(select,expandLabel,button("刷新任务",()=>load()));body.append(head,filters,status,list,more);modal.append(body);document.body.append(modal);window.MovieFriendsUI.open(modal);let timer,loadVersion=0,connections=[];
    const alive=()=>modal.open&&session===bridge.sessionVersion()&&owner===bridge.user()?.id;
    modal.onclose=()=>{clearTimeout(timer);modal.remove();};select.onchange=expand.onchange=()=>load().catch(e=>{status.textContent=bridge.safeError(e);});
    const control=async(task,action)=>{await bridge.sameAccount(owner);if(!alive())return;const kind=task.kind==="resume"?"resume-sync":"auto-sync";await bridge.api(`/watch/${kind}/control`,{method:"POST",body:{action,run_id:task.id}});if(alive()){bridge.toast(action==="pause"?"自动同步已暂停；恢复后重新核验，已完成结果保留。":action==="stop"?"本轮已停止，自动检查计划保留。":"已按原授权安排重新检查。");await load();}};
    const sourceName=id=>{const source=connections.find(c=>c.id===id);return source?`${names[source.platform]||source.platform} · ${source.account_name||"本人账号"}`:"当前平台";};
    async function load(offset=0){
      clearTimeout(timer);const request=++loadVersion,result=await bridge.api(`/watch/tasks?kind=${select.value}&offset=${offset}&collapse=${!expand.checked}`);if(!alive()||request!==loadVersion)return;if(!offset)list.replaceChildren();status.textContent=result.tasks.length?`共 ${result.total} 项${result.hidden_count?` · 已合并 ${result.hidden_count} 条重复核对`:""}`:"还没有任务，连接账号或生成对齐预览后会显示在这里。";
      for(const task of result.tasks){const row=el("article","task-card"),heading=el("div","task-card-heading"),copy=el("div"),execution=task.execution,noChange=task.same_as_previous||(task.status==="complete"&&task.result?.unchanged_records===task.total&&!task.result?.new_records&&!task.result?.updated_records);copy.append(el("strong","",task.kind==="import"?`${names[task.platform]} · ${task.account_name||"本人记录"}`:task.kind==="auto_sync"?"后台自动同步":task.kind==="resume"?"续播同步":"三平台已看对齐"),el("small","field-help",bridge.dateText(task.created_at)));
        row.dataset.kind=task.kind;heading.append(copy,el("span","task-state",noChange?"内容无变化":states[execution?.status||task.status]||task.status));row.append(heading);
        if(task.kind==="import"){
          const text=task.status==="reading"?`已读取 ${task.processed} 条`:`共 ${task.total} 条 · ${task.pending} 条待匹配`;
          row.append(el("p","muted",text));
          if(task.same_as_previous)row.append(el("small","field-help",task.repeat_count>1?`合并 ${task.repeat_count} 次相同内容的核对 · 最早 ${bridge.dateText(task.first_repeated_at)}`:"与上次读取内容相同"));
          else if(task.result)row.append(el("small","field-help",task.result.unchanged_records!==undefined?`新增原始记录 ${task.result.new_records} · 更新原始记录 ${task.result.updated_records} · 内容未变化 ${task.result.unchanged_records} · 新增去重已看 ${task.result.new_unique}`:`新增原始记录 ${task.result.new_records} · 读取已有记录 ${task.result.updated_records} · 新增去重已看 ${task.result.new_unique}`));
          else if(task.status==="complete")row.append(el("small","field-help","历史任务未记录当时的新增数量"));
          row.append(button("查看导入明细",()=>window.MovieFriendsProfile.showJob(task.id)));
        }else if(task.kind==="auto_sync"||task.kind==="resume"){
          const reading=task.status==="running"&&["reading","matching"].includes(task.stage),progress=el("progress");progress.max=Math.max(1,task.total||0);progress.setAttribute("aria-label","任务进度");if(!reading)progress.value=task.total?(task.processed||0):task.status==="done"?1:0;
          row.append(progress,el("p","muted",window.MovieFriendsUI.progressText(task,sourceName)));
          if(task.current_media)row.append(el("p","field-help",`正在核验：${task.current_media.title||"影片"}`));
          const actions=el("div","task-control-actions");actions.append(button("查看后台同步详情",()=>window.MovieFriendsAutoSync.openRun(task.id,task.kind)));
          if(task.status==="running")actions.append(button("暂停同步",()=>control(task,"pause")),button("停止本轮",()=>control(task,"stop")));
          else if(task.status==="paused")actions.append(button("恢复同步",()=>control(task,"resume")));
          else if(["failed","attention","interrupted","stopped"].includes(task.status))actions.append(button("重试检查",()=>control(task,"retry")));
          const info=window.MovieFriendsHelp.create("任务控制","暂停会结束本轮并暂停自动检查，恢复后按原授权重新读取。停止本轮保留后续计划。重试先核验最新状态，不盲目重复已完成或未确认的写入；已完成结果都会保留。");actions.append(info);row.append(actions);
          for(const [id,summary] of Object.entries(task.summary||{}))row.append(el("small","field-help",`${sourceName(id)} · 已读取 ${summary.read||0}${summary.error?` · ${summary.error}`:""}`));
        }else{
          const total=task.counts.reduce((n,r)=>n+r.count,0),successful=task.counts.filter(r=>r.state==="done").reduce((n,r)=>n+r.count,0);row.append(el("p","muted",`对齐条目 ${total} · 已同步 ${successful}`));
          if(execution)row.append(el("small","field-help",`最近一轮：已处理 ${execution.processed} / ${execution.total} · 剩余 ${execution.remaining}`));
          row.append(button("查看对齐详情",()=>window.MovieFriendsAlignment.open({jobId:task.id})));
        }list.append(row);
      }
      more.replaceChildren();if(result.next_offset!==null)more.append(button("更多历史任务",()=>load(result.next_offset)));
      if(!offset&&result.tasks.some(t=>t.status==="reading"||t.status==="running"||t.execution?.status==="running"))timer=setTimeout(()=>load().catch(()=>{}),2000);
    }
    try{const view=await bridge.api("/watch/auto-sync");if(!alive())return;connections=view.connections;await load();}catch(e){if(alive())status.textContent=bridge.safeError(e);}
  }
  return {configure(value){bridge=value;},open};
})();
