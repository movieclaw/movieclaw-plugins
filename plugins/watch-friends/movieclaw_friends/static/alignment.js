"use strict";
// 只在已确认的当前窗口逐项提交；任务历史和进度保存在插件内。
window.MovieFriendsAlignment = (() => {
  let bridge;
  const el=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;};
  const platform=value=>value==="emby"?"Emby":value==="plex"?"Plex":"MovieClaw";
  const labels={pending:"待补标",skipped:"已看，跳过",missing:"缺少片源",ambiguous:"匹配不唯一",excluded:"媒体库已排除",unavailable:"暂无法核实",writing:"正在同步",done:"已同步",failed:"失败，可重试"};
  const executionLabels={running:"正在同步",completed:"本轮完成",stopped:"本轮已停止",interrupted:"本轮已中断"};
  function button(text,handler,cls="button-outline"){const n=el("button",`button ${cls}`,text);n.type="button";n.onclick=()=>Promise.resolve(handler()).catch(e=>{if(!e.staleSession)bridge.toast(bridge.safeError(e),true);});return n;}
  async function pool(items,handler,alive){let index=0,failure;await Promise.allSettled(Array.from({length:3},async()=>{while(!failure&&alive()&&index<items.length){try{await handler(items[index++]);}catch(error){failure=error;throw error;}}}));if(failure)throw failure;}
  async function open(options={}) {
    const session=bridge.sessionVersion(),owner=bridge.user().id,modal=el("dialog","watch-dialog alignment-dialog"),body=el("div","watch-dialog-body"),heading=el("div","section-heading");
    const title=el("h2","","三平台已看对齐");title.append(window.MovieFriendsHelp.create("手动已看对齐","仅补充电影或准确单集的已看标记，沿用媒体库排除范围。不会撤销已看、复制播放事件、日期与次数，也不同步评分或播放进度。平台可能记录本次补标时间。关闭执行窗口会停止后续提交，已发出的请求可能完成。预览超过一小时或连接与库范围变化后须重新核对。"));
    const close=button("关闭",()=>modal.close(),"button-quiet");close.autofocus=true;heading.append(title,close);
    const choices=el("div","alignment-choices"),status=el("p","watch-status","正在读取本人连接…"),live=el("div","alignment-live"),content=el("div");live.setAttribute("aria-live","polite");body.append(heading,choices,status,live,content);modal.append(body);document.body.append(modal);window.MovieFriendsUI.open(modal);
    let stopped=false,running=false,jobId=null,rows=[],selected=new Set(),config,report,ownExecution=null,pollTimer,heartbeatTimer,stopRequested=false,drawSummary=()=>{},drawRows=()=>{};
    const alive=()=>!stopped&&modal.open&&session===bridge.sessionVersion()&&bridge.user()?.id===owner;
    const check=()=>{if(!alive()){const e=new Error("当前预览已关闭或登录身份改变。");e.staleSession=true;throw e;}};
    const sourceName=id=>{if(id==="movieclaw")return "MovieClaw";const source=config?.sources.find(s=>s.id===id)||report?.sources?.find(s=>s.id===id);return source?`${platform(source.platform)} · ${source.account_name||"本人账号"}`:"已断开的平台";};
    const activeExecution=()=>report?.execution?.status==="running";
    const action=(value,id=ownExecution)=>bridge.api(`/watch/alignment/${jobId}/execution/${id}`,{method:"POST",body:{action:value}});
    modal.addEventListener("close",()=>{stopped=true;clearTimeout(pollTimer);clearTimeout(heartbeatTimer);if(ownExecution&&session===bridge.sessionVersion()&&bridge.user()?.id===owner)action("stop").catch(()=>{});modal.remove();},{once:true});
    try{
      config=await bridge.api("/watch/alignment/config");check();
      for(const target of [{id:"movieclaw",label:"MovieClaw · 当前账号"},...config.sources.map(s=>({id:s.id,label:`${platform(s.platform)} · ${s.account_name||"本人账号"}`}))]){const label=el("label","watch-check"),input=el("input");input.type="checkbox";input.checked=true;input.value=target.id;label.append(input,el("span","",target.label));choices.append(label);}
      choices.append(button("开始只读预览",preview,"button-primary"));if(config.recent_id)choices.append(button("查看上次对齐预览",()=>resume(config.recent_id)));
      status.textContent="选择平台，生成只读预览后确认同步。";
      if(options.jobId)await resume(options.jobId);
    }catch(e){if(alive())status.textContent=bridge.safeError(e);}
    function lockChoices(value){choices.querySelectorAll("input,button").forEach(n=>{n.disabled=value;});}
    async function readReport(){const fresh=await bridge.api(`/watch/alignment/${jobId}/report`);check();report=fresh;showLive();drawSummary();return fresh;}
    function showLive(){
      live.replaceChildren();const execution=report?.execution;if(!execution)return;
      const panel=el("section","task-progress-panel"),top=el("div","task-card-heading"),copy=el("div");copy.append(el("strong","",executionLabels[execution.status]||execution.status),el("small","field-help",`本轮开始于 ${bridge.dateText(execution.started_at)}`));
      top.append(copy,el("strong","task-progress-numbers",`${execution.processed} / ${execution.total}`));const progress=el("progress");progress.max=Math.max(1,execution.total);progress.value=execution.processed;progress.setAttribute("aria-label","同步处理进度");
      panel.append(top,progress,el("p","muted",`已处理 ${execution.processed} · 剩余 ${execution.remaining} · 总计 ${execution.total}`));
      if(execution.current)panel.append(el("small","field-help",`${sourceName(execution.current.source_id)} · 正在核验 ${execution.current.media.title} ${bridge.mediaMeta(execution.current)}`));
      if(execution.uncertain)panel.append(el("small","field-help",`${execution.uncertain} 项提交结果未确认，请重新核对后再决定是否补标。`));
      if(execution.status==="running")panel.append(button("停止后续同步",async()=>{stopRequested=true;report.execution=await action("stop",execution.id);showLive();if(!running){await loadTargets(false);render();}},"button-quiet"));live.append(panel);
    }
    function pollProgress(){clearTimeout(pollTimer);if(alive()&&activeExecution())pollTimer=setTimeout(async()=>{try{await readReport();if(!running){await loadTargets(false);drawRows();}if(activeExecution())pollProgress();else if(!running)render();}catch(e){if(alive())status.textContent=bridge.safeError(e);}},1000);}
    function heartbeat(){clearTimeout(heartbeatTimer);if(alive()&&ownExecution&&!stopRequested)heartbeatTimer=setTimeout(async()=>{try{await action("heartbeat");heartbeat();}catch(e){stopRequested=true;if(alive())status.textContent=bridge.safeError(e);}},10000);}
    async function nativeRecords() {
      const libraries=await bridge.hostApi("/libraries?scope=visible");check();
      if(!Array.isArray(libraries))throw new Error("无法读取主程序可见媒体库。");
      const result=[];
      for(const library of libraries.filter(l=>["movie","tv"].includes(l.kind)&&!config.excluded_native.includes(String(l.id)))) {
        const seen=new Set();let offset=0;
        while(alive()) {
          const items=await bridge.hostApi(`/libraries/${library.id}/items?w=seen&limit=200&offset=${offset}`);check();
          if(!Array.isArray(items))throw new Error("主程序已看列表格式不兼容。");
          if(items.some(item=>seen.has(item.media_item_id)))throw new Error("主程序分页重复，请稍后重新预览。");
          items.forEach(item=>seen.add(item.media_item_id));
          await pool(items,async item=>{
            if(!Number.isSafeInteger(item.tmdb_id)||item.tmdb_id<1||!Number.isSafeInteger(item.media_item_id))return;
            const media={tmdb_id:item.tmdb_id,kind:item.kind,title:item.title,year:item.year||null};
            if(item.kind==="movie") {
              const mark=await bridge.hostApi(`/playback/marks?media_item_id=${item.media_item_id}`);check();
              if(mark.played===true)result.push({media,played:true,library_ids:[String(library.id)]});
            } else {
              if(!Array.isArray(item.seasons))throw new Error("主程序季集目录缺失，请重试。");
              for(const season of item.seasons) {
                if(!Number.isSafeInteger(season)||season<0)continue;
                const data=await bridge.hostApi(`/playback/items/${item.media_item_id}/episodes?season_number=${season}`);check();
                if(data.season_number!==season||!Array.isArray(data.episodes))throw new Error("主程序单集状态格式不兼容。");
                for(const episode of data.episodes)if(episode.owned===true&&episode.played===true&&Number.isSafeInteger(episode.episode_number)&&episode.episode_number>0)result.push({media:{...media,season_number:season,episode_number:episode.episode_number},played:true,library_ids:[String(library.id)]});
              }
            }
            if(result.length>100000)throw new Error("已看内容超过十万项，请缩小参与范围。");
            status.textContent=`正在核对 MovieClaw 已看状态…已读取 ${result.length} 个电影或单集`;
          },alive);
          if(items.length<200)break;offset+=items.length;
        }
      }
      return result;
    }


    async function prepare(number){
      jobId=number;let state;
      do{state=await readReport();if(state.status==="reading"){const phase=state.phase;status.textContent=phase?.stage==="matching"?`正在匹配 ${phase.processed} / ${phase.total} 项`:`正在只读核对 ${phase?.source_id?sourceName(phase.source_id):"平台"}…已读取 ${state.progress} 条`;await new Promise(resolve=>setTimeout(resolve,1000));}}while(alive()&&state.status==="reading");
      if(state.status==="failed"){await loadTargets();render();return;}
      if(state.status==="native"){
        const union=[];let offset=0;do{const page=await bridge.api(`/watch/alignment/${jobId}/records?offset=${offset}`);check();union.push(...page.records);offset=page.next_offset;}while(offset!==null);
        await nativeTargets(union);check();await bridge.api(`/watch/alignment/${jobId}/ready`,{method:"POST"});
      }
      await readReport();await loadTargets();render();pollProgress();
    }
    async function preview(){
      if(running||activeExecution())return;const ids=[...choices.querySelectorAll("input:checked")].map(input=>input.value),native=ids.includes("movieclaw");if(!ids.length)throw new Error("请至少选择一个参与平台。");
      running=true;lockChoices(true);content.replaceChildren();live.replaceChildren();report=null;
      try{await bridge.sameAccount(owner);check();status.textContent="正在只读核对 MovieClaw 的真实已看状态…";const records=native?await nativeRecords():[];check();const job=await bridge.api("/watch/alignment/preview",{method:"POST",body:{include_native:native,source_ids:ids.filter(id=>id!=="movieclaw"),native_records:records}});check();await prepare(job.id);}
      catch(e){if(alive())status.textContent=bridge.safeError(e);}finally{running=false;if(alive()){lockChoices(activeExecution());if(["ready","failed"].includes(report?.status))render();}}
    }
    async function nativeTargets(union) {
      const groups=new Map();for(const record of union){const key=`${record.media.kind}:${record.media.tmdb_id}`;if(!groups.has(key))groups.set(key,[]);groups.get(key).push(record);}
      let processed=0;
      await pool([...groups.values()],async records=>{
        const first=records[0].media,key=`tmdb:${first.kind}:${first.tmdb_id}`,targets=[];
        try {
          const found=await bridge.hostApi(`/discover/titles/${encodeURIComponent(key)}`);check();
          if(found.title?.title_ref!==key||found.title.media_type!==first.kind)throw new Error("作品身份未核实");
          const links=(found.library_links||[]).filter(l=>Number.isSafeInteger(l.media_item_id)&&l.media_item_id>0&&!config.excluded_native.includes(String(l.library_id))),ids=new Set(links.map(l=>l.media_item_id));
          if(ids.size!==1||!(found.title.library_status?.file_count>0)) {
            records.forEach(record=>targets.push({canonical_key:record.canonical_key,state:ids.size>1?"ambiguous":"missing"}));
          } else {
            const id=[...ids][0],seasons=new Map();
            for(const record of records) {
              let state;
              if(first.kind==="movie") {const mark=await bridge.hostApi(`/playback/marks?media_item_id=${id}`);check();if(typeof mark.played!=="boolean")throw new Error("已看状态未知");state=mark.played?"skipped":"pending";}
              else {
                const season=record.media.season_number;
                if(!seasons.has(season)){const data=await bridge.hostApi(`/playback/items/${id}/episodes?season_number=${season}`);check();if(data.season_number!==season||!Array.isArray(data.episodes))throw new Error("单集状态未知");seasons.set(season,data.episodes);}
                const episodes=seasons.get(season).filter(e=>e.episode_number===record.media.episode_number&&e.owned===true);
                state=episodes.length===1&&typeof episodes[0].played==="boolean"?(episodes[0].played?"skipped":"pending"):episodes.length>1?"ambiguous":"missing";
              }
              targets.push({canonical_key:record.canonical_key,state,media_item_id:id});
            }
          }
        }catch(e){if(e.staleSession||!alive())throw e;targets.length=0;records.forEach(record=>targets.push({canonical_key:record.canonical_key,state:"unavailable"}));}
        for(let offset=0;offset<targets.length;offset+=200){check();await bridge.api(`/watch/alignment/${jobId}/native-targets`,{method:"POST",body:{targets:targets.slice(offset,offset+200)}});}
        processed+=records.length;status.textContent=`正在核对 MovieClaw 缺少的已看标记 ${processed}/${union.length}…`;
      },alive);
    }


    async function loadTargets(reset=true){
      const loaded=[];let offset=0;do{const page=await bridge.api(`/watch/alignment/${jobId}/targets?offset=${offset}`);check();loaded.push(...page.targets);offset=page.next_offset;}while(offset!==null);rows=loaded;
      if(reset)selected=new Set(rows.filter(row=>["pending","failed"].includes(row.state)).map(row=>row.id));
    }
    async function resume(number){if(running)return;running=true;lockChoices(true);content.replaceChildren();try{await prepare(number);}finally{running=false;if(alive()){render();lockChoices(activeExecution());}}}
    async function recheck(){
      if(running||activeExecution()||!selected.size)return;running=true;lockChoices(true);content.querySelectorAll("input,select,button").forEach(n=>{n.disabled=true;});
      try{await bridge.sameAccount(owner);check();const next=await bridge.api(`/watch/alignment/${jobId}/recheck`,{method:"POST",body:{target_ids:[...selected],consent:true}});check();content.replaceChildren();live.replaceChildren();await prepare(next.id);}
      catch(e){if(alive())status.textContent=bridge.safeError(e);}finally{running=false;if(alive()){lockChoices(activeExecution());render();}}
    }
    function render(){
      check();content.replaceChildren();showLive();lockChoices(running||activeExecution());
      if(!report||!["ready","failed"].includes(report.status))return;
      status.textContent=report.status==="failed"?`${report.error||"预览中断"}。已生成的结果仍可查看，可选中条目重新核对。`:!report.writable?`历史结果可查看；${report.write_blocked_reason}。可选择条目重新核对。`:activeExecution()?"本轮正在执行，进度和平台结果会持续更新。":"预览已完成。选择需要补标的条目并确认，或单独重新核对有疑问的影片。";
      const summary=el("div","alignment-summary"),filters=el("div","watch-row-actions"),sourceSelect=el("select"),stateSelect=el("select"),query=el("input"),table=el("div","alignment-rows"),consentLabel=el("label","watch-check"),consent=el("input"),consentText=el("span");
      sourceSelect.setAttribute("aria-label","对齐目标平台");stateSelect.setAttribute("aria-label","对齐状态");query.type="search";query.placeholder="搜索影片名称";query.setAttribute("aria-label","搜索对齐影片");
      for(const [value,text] of [["","全部平台"],...[...new Set(rows.map(r=>r.source_id))].map(id=>[id,sourceName(id)])]){const option=el("option","",text);option.value=value;sourceSelect.append(option);}
      for(const [value,text] of [["actionable","待补标与失败"],["","全部状态"],...Object.entries(labels)]){const option=el("option","",text);option.value=value;stateSelect.append(option);}
      consent.type="checkbox";consentLabel.append(consent,consentText);
      const submit=button("确认同步勾选项",()=>apply(consent),"button-primary"),retry=button("重新核对选中项",recheck),eligible=()=>rows.filter(r=>selected.has(r.id)&&["pending","failed"].includes(r.state));
      const update=()=>{const count=eligible().length;consentText.textContent=`确认将勾选的 ${count} 项补标到所列的本人平台账号；不会撤销已有标记。`;submit.disabled=!report.writable||!consent.checked||!count||running||activeExecution();retry.disabled=!selected.size||running||activeExecution();consent.disabled=running||activeExecution()||!report.writable;};consent.onchange=update;
      drawSummary=()=>{summary.replaceChildren();for(const id of [...new Set([...(report.participants||[]),...rows.map(row=>row.source_id)])]){
        const counts=report.counts.filter(r=>r.source_id===id),count=state=>counts.filter(r=>r.state===state).reduce((n,r)=>n+r.count,0),card=el("section","panel"),batch=report.execution?.counts.filter(r=>r.source_id===id)||[],batchCount=state=>batch.filter(r=>r.execution_state===state).reduce((n,r)=>n+r.count,0);
        card.append(el("strong","",sourceName(id)),el("p","",`待补标 ${count("pending")} · 已看 ${count("skipped")} · 已同步 ${count("done")} · 失败 ${count("failed")}`),el("small","field-help",`缺片源 ${count("missing")} · 匹配不唯一 ${count("ambiguous")} · 排除/未核实 ${count("excluded")+count("unavailable")}`));
        if(batch.length){const total=batch.reduce((n,r)=>n+r.count,0),processed=batchCount("done")+batchCount("skipped")+batchCount("failed");card.append(el("p","task-platform-result",`本轮 ${processed} / ${total} · 剩余 ${total-processed}`),el("small","field-help",`成功 ${batchCount("done")} · 已看跳过 ${batchCount("skipped")} · 失败 ${batchCount("failed")}`));}summary.append(card);
      }};
      let limit=100;const filtered=()=>rows.filter(r=>(!sourceSelect.value||r.source_id===sourceSelect.value)&&(!stateSelect.value||(stateSelect.value==="actionable"?["pending","failed"].includes(r.state):r.state===stateSelect.value))&&r.media.title.toLowerCase().includes(query.value.trim().toLowerCase()));
      drawRows=()=>{table.replaceChildren();const items=filtered();for(const row of items.slice(0,limit)){
        const line=el("div","alignment-row"),box=el("input"),copy=el("div");box.type="checkbox";box.checked=selected.has(row.id);box.disabled=running||activeExecution();box.setAttribute("aria-label",`同步${row.media.title}到${sourceName(row.source_id)}`);box.onchange=()=>{if(box.checked)selected.add(row.id);else selected.delete(row.id);consent.checked=false;update();};
        copy.append(el("strong","",row.media.title),el("small","field-help",`${bridge.mediaMeta(row)} · ${sourceName(row.source_id)}`),el("small","field-help",row.message||labels[row.state]));if(row.attempts)copy.append(el("small","field-help",`已执行 ${row.attempts} 次${row.last_checked_at?` · 最近核验 ${bridge.dateText(row.last_checked_at)}`:""}`));line.dataset.state=row.state;line.append(box,copy);table.append(line);
      }if(items.length>limit)table.append(button(`显示更多（共 ${items.length} 项）`,()=>{limit+=100;drawRows();}));if(!items.length)table.append(el("p","muted","当前筛选没有条目。"));update();};
      sourceSelect.onchange=stateSelect.onchange=query.oninput=()=>{limit=100;drawRows();};
      filters.append(sourceSelect,stateSelect,query,button("全选当前筛选",()=>{selected=new Set(filtered().map(r=>r.id));consent.checked=false;drawRows();},"button-quiet"),button("清空选择",()=>{selected.clear();consent.checked=false;drawRows();},"button-quiet"));
      const actions=el("div","watch-row-actions");actions.append(submit,retry,window.MovieFriendsHelp.create("重新核对","选中已同步、失败或缺片源等任何状态的影片，可单独生成新的只读预览，原任务记录保留。新预览不会写入平台，补标前需要再次确认。成功条目表示平台回读已确认；中断且未确认的条目不能当作成功。"));
      content.append(summary,filters,table,consentLabel,actions);drawSummary();drawRows();
    }
    async function apply(consent){
      if(running||!consent.checked||!report.writable||activeExecution())return;const work=rows.filter(row=>selected.has(row.id)&&["pending","failed"].includes(row.state));if(!work.length)return;
      running=true;stopRequested=false;lockChoices(true);content.querySelectorAll("input,select,button").forEach(n=>{n.disabled=true;});
      try{
        await bridge.sameAccount(owner);check();report.execution=await bridge.api(`/watch/alignment/${jobId}/execution`,{method:"POST",body:{consent:true,target_ids:work.map(r=>r.id)}});check();ownExecution=report.execution.id;showLive();heartbeat();pollProgress();
        for(const row of work){
          check();if(stopRequested||report.execution?.status!=="running")break;await bridge.sameAccount(owner);check();if(stopRequested)break;let result;
          if(row.source_id==="movieclaw") {
            const prepared=await bridge.api(`/watch/alignment/${jobId}/native-prepare/${row.id}`,{method:"POST",body:{consent:true,execution_id:ownExecution,begin_attempt:true}});check();const target=prepared.native_target,media=prepared.media;
            try {
              const key=`tmdb:${media.kind}:${media.tmdb_id}`,found=await bridge.hostApi(`/discover/titles/${encodeURIComponent(key)}`);check();
              const links=(found.library_links||[]).filter(l=>!config.excluded_native.includes(String(l.library_id))),ids=new Set(links.map(l=>l.media_item_id));
              if(found.title?.title_ref!==key||found.title?.media_type!==media.kind||!(found.title.library_status?.file_count>0)||ids.size!==1||!ids.has(target.media_item_id))throw new Error("本服作品匹配已变化，请重新预览。");
              if(media.kind==="tv") {const season=await bridge.hostApi(`/playback/items/${target.media_item_id}/episodes?season_number=${target.season_number}`);check();if(season.season_number!==target.season_number||season.episodes?.filter(e=>e.episode_number===target.episode_number&&e.owned===true).length!==1)throw new Error("这集片源已变化，请重新预览。");}
              const before=await bridge.hostApi(`/playback/marks?${new URLSearchParams(target)}`);check();if(typeof before.played!=="boolean")throw new Error("无法核对当前已看状态。");
              if(!before.played){await bridge.sameAccount(owner);check();await bridge.api(`/watch/alignment/${jobId}/native-prepare/${row.id}`,{method:"POST",body:{consent:true,execution_id:ownExecution,begin_write:true}});check();await bridge.hostApi("/playback/marks",{method:"POST",body:{...target,played:true}});check();const after=await bridge.hostApi(`/playback/marks?${new URLSearchParams(target)}`);check();if(after.played!==true)throw new Error("主程序未确认已看标记。");}
              result=await bridge.api(`/watch/alignment/${jobId}/native-result/${row.id}`,{method:"POST",body:{consent:true,execution_id:ownExecution,success:true,already_played:before.played}});
            }catch(e){if(e.staleSession||e.accountChanged||!alive())throw e;await bridge.api(`/watch/alignment/${jobId}/native-result/${row.id}`,{method:"POST",body:{consent:true,execution_id:ownExecution,success:false}});result={state:"failed",message:bridge.safeError(e)};}
          } else result=await bridge.api(`/watch/alignment/${jobId}/apply/${row.id}`,{method:"POST",body:{consent:true,execution_id:ownExecution}});

          check();Object.assign(row,result);await readReport();drawRows();
        }
        clearTimeout(heartbeatTimer);clearTimeout(pollTimer);
        await readReport();report.execution=await action(stopRequested||report.execution.remaining?"stop":"finish");ownExecution=null;
        await loadTargets();check();running=false;render();status.textContent=stopRequested?"本轮已停止，已完成的结果已保存。":"本次同步已结束。可按平台查看成功、跳过和失败的条目。";
      }catch(e){if(ownExecution){await action("stop").catch(()=>{});ownExecution=null;}if(alive()){try{await readReport();await loadTargets();running=false;render();}catch{}status.textContent=bridge.safeError(e);}}
      finally{clearTimeout(heartbeatTimer);clearTimeout(pollTimer);running=false;if(alive())lockChoices(activeExecution());}
    }
  }
  return {configure(value){bridge=value;},open};
})();
