"use strict";
// All persistent state belongs to the server-verified MovieClaw account.
globalThis.MovieClawWallTools={
  create({node,button,image,request,initialize,getAccount,getFilters,openFilters,saveFilters,applyPreset,filterInputs,loadWall,showDetail,compactDate,announce,adoptShelf}){
    const $=id=>document.getElementById(id);
    const state={epoch:0,presets:null,presetBusy:false,presetRead:0,history:null,actions:[],tab:"views",historyRead:0,historyBusy:false,undoBusy:false,undo:null,undoTimer:null,filtersSaving:false,viewTail:Promise.resolve(),shortcutBusy:false,shortcutAt:0,shortcutRead:null,shortcutsVisible:false,shortcutSignature:""};
    const all=selector=>Array.from(document.querySelectorAll(selector));
    const inputs=kind=>all('input[name="wall-genre-'+kind+'"]');
    const baseDraft=()=>({media_type:$("wall-filter-format").value,countries:filterInputs("country").filter(input=>input.checked).map(input=>input.value),eras:filterInputs("era").filter(input=>input.checked).map(input=>input.value)});
    function selection(){return {genres:inputs("include").filter(input=>input.checked).map(input=>input.value),exclude_genres:inputs("exclude").filter(input=>input.checked).map(input=>input.value),exploration:$("wall-exploration").value||"balanced"};}
    function setDraft(filters){
      $("wall-filter-format").value=filters.media_type;
      for(const [kind,key] of [["country","countries"],["era","eras"]]){const values=filters[key]||[];for(const input of filterInputs(kind))input.checked=values.includes(input.value);$("wall-"+kind+"-any").setAttribute("aria-pressed",String(!values.length));}
      for(const [kind,key] of [["include","genres"],["exclude","exclude_genres"]])for(const input of inputs(kind))input.checked=(filters[key]||[]).includes(input.value);
      $("wall-exploration").value=filters.exploration||"balanced";
    }
    function draft(){setDraft(getFilters());}
    function focusGroup(identity){const chosen={"wall-country":"country","wall-era":"era","wall-genres":"genre"}[identity];for(const kind of ["country","era","genre"])$("filter-"+kind+"-section").open=kind===chosen;}
    function resetDraft(){for(const input of [...inputs("include"),...inputs("exclude")])input.checked=false;$("wall-exploration").value="balanced";}
    function labels(){const filters=getFilters(),count=(filters.genres||[]).length,excluded=(filters.exclude_genres||[]).length;$("wall-genres").textContent=[count?"包含 "+count+" 项":"不限题材",excluded?"排除 "+excluded+" 项":null,{familiar:"贴近口味",balanced:"兼顾惊喜",discovery:"多点意外"}[filters.exploration||"balanced"]].filter(Boolean).join(" · ");renderShortcuts();}
    function valid(account,epoch){return account===getAccount()&&epoch===state.epoch;}
    function presetControls(){
      renderShortcuts();for(const id of ["preset-save","preset-remove","preset-select","preset-name"])$(id).disabled=state.presetBusy||state.filtersSaving||(!state.presets&&id!=="preset-name")||(id==="preset-remove"&&!$("preset-select").value);}
    function renderPresets(){
      const selected=$("preset-select").value,select=$("preset-select"),record=state.presets||{items:[],archived:[]};
      const empty=node("option","选择已收藏方案");empty.value="";select.replaceChildren(empty);
      for(const row of record.items){const option=node("option",row.name);option.value=row.id;select.append(option);}
      select.value=record.items.some(row=>row.id===selected)?selected:"";
      const archive=$("preset-archived");archive.replaceChildren();$("preset-archive").hidden=!(record.archived||[]).length;
      for(const row of record.archived||[]){const line=node("div",null,"tools-line"),restore=button("恢复","button ghost",()=>changePreset({operation:"restore",id:row.id}));restore.disabled=state.presetBusy||state.filtersSaving;line.append(node("span",row.name),restore);archive.append(line);}
      presetControls();
    }
    async function readPresets(){
      const account=getAccount(),epoch=state.epoch,read=++state.presetRead;$("preset-status").textContent="正在读取账号方案…";
      try{const value=await request("presets");if(!valid(account,epoch)||read!==state.presetRead||state.presetBusy)return;state.presets=value;state.shortcutAt=Date.now();renderPresets();$("preset-status").textContent="收藏 "+value.items.length+" / 20 · 载入后点击应用筛选生效。";}
      catch(error){if(valid(account,epoch)&&read===state.presetRead)$("preset-status").textContent=error.message;}
    }
    async function changePreset(change){
      if(state.presetBusy)return;state.presetBusy=true;state.presetRead++;const account=getAccount(),epoch=state.epoch;
      presetControls();
      try{
        await initialize();if(!valid(account,epoch))return;
        if(!state.presets)throw new Error("请先重新打开筛选，读取账号方案。");
        const value=await request("presets",{method:"POST",body:JSON.stringify({change:{...change,revision:state.presets.revision}})});
        if(!valid(account,epoch))return;
        state.presets=value;state.shortcutAt=Date.now();if(change.operation==="save")$("preset-name").value="";
        renderPresets();$("preset-status").textContent=change.operation==="save"?"方案已按账号收藏；点击应用筛选使当前选择生效。":change.operation==="remove"?"方案已移除，可在下方恢复。":"方案已恢复。";
      }catch(error){if(valid(account,epoch)){$("preset-status").textContent=error.message;if(error.status===409){state.presetBusy=false;await readPresets();$("preset-status").textContent=error.message+" 已载入最新列表，请重新选择。";}}}
      finally{if(valid(account,epoch)){state.presetBusy=false;renderPresets();}}
    }
    function normalized(filters){return JSON.stringify([filters.media_type||"all",...["countries","eras","genres","exclude_genres"].map(key=>[...new Set(filters[key]||[])].sort().join(",")),filters.exploration||"balanced"]);}
    function renderShortcuts(){
      if(!state.shortcutsVisible)return;
      const record=state.presets,filters=normalized(getFilters()),busy=state.shortcutBusy||state.filtersSaving;
      const signature=JSON.stringify([record?.items||null,filters,busy]);if(signature===state.shortcutSignature)return;state.shortcutSignature=signature;
      const list=$("preset-shortcut-list"),scroll=list.scrollLeft||0;list.replaceChildren();
      if(!record){list.append(node("span","正在读取账号方案…","preset-shortcut-empty"));return;}
      if(!record.items.length)list.append(node("span","收藏筛选后，可在这里一键切换","preset-shortcut-empty"));
      for(const row of record.items){const control=button(row.name,"button preset-shortcut",()=>switchPreset(row.id));control.dataset.presetId=row.id;control.title=row.name;control.disabled=busy;control.setAttribute("aria-pressed",String(normalized(row.filters)===filters));list.append(control);}
      list.scrollLeft=scroll;list.setAttribute("aria-busy",String(state.shortcutBusy));
    }
    function shortcutsReady(){state.shortcutsVisible=true;$("wall-preset-shortcuts").hidden=false;renderShortcuts();}
    function refreshShortcuts(force=false){
      if(!state.shortcutsVisible||state.presetBusy||state.shortcutBusy)return Promise.resolve();
      if(state.shortcutRead)return state.shortcutRead;
      if(!force&&Date.now()-state.shortcutAt<15000)return Promise.resolve();
      const work=readPresets();state.shortcutRead=work;return work.finally(()=>{if(state.shortcutRead===work)state.shortcutRead=null;});
    }
    async function switchPreset(identity){
      if(state.shortcutBusy||state.filtersSaving)return;
      const account=getAccount(),epoch=state.epoch,row=state.presets?.items.find(entry=>entry.id===identity);if(!row)return;
      const revision=state.presets.revision;state.shortcutBusy=true;renderShortcuts();
      const notice=$("preset-shortcut-status");notice.textContent="正在切换“"+row.name+"”…";notice.hidden=false;announce(notice.textContent);
      try{
        const success=await applyPreset(identity,revision);if(!valid(account,epoch))return;
        if(success){notice.textContent="已切换“"+row.name+"” · 已按账号同步。";announce(notice.textContent);}
        else{await readPresets();if(valid(account,epoch))notice.textContent="未切换；请查看提示，或重新选择最新方案。";}
      }catch(error){if(valid(account,epoch)){notice.textContent=error.message;await readPresets();}}
      finally{if(valid(account,epoch)){state.shortcutBusy=false;renderShortcuts();}}
    }
    function undoSlots(){return all(".wall-undo-slot");}
    function hideUndo(){for(const slot of undoSlots()){slot.hidden=true;slot.replaceChildren();}}
    function offerUndo(row){
      if(!row?.id)return;state.undo={row,account:getAccount(),epoch:state.epoch};clearTimeout(state.undoTimer);
      for(const slot of undoSlots()){
        const action=button("撤销","button",()=>undoAction(row)),text=node("span",row.label+" · "+row.title);slot.replaceChildren(text,action);slot.hidden=false;
      }
      const identity=row.id;state.undoTimer=setTimeout(()=>{if(state.undo?.row.id===identity)hideUndo();},10000);
    }
    async function undoAction(row){
      if(state.undoBusy)return;state.undoBusy=true;const account=getAccount(),epoch=state.epoch;
      for(const slot of undoSlots())for(const control of slot.querySelectorAll("button"))control.disabled=true;
      try{
        await initialize();if(!valid(account,epoch))return;
        const shelf=await request("actions/undo",{method:"POST",body:JSON.stringify({id:row.id})});
        if(!valid(account,epoch))return;
        hideUndo();state.undo=null;state.actions=state.actions.filter(entry=>entry.id!==row.id);adoptShelf(shelf);announce("已撤销“"+row.label+"”，账号偏好已恢复。");
        if($("browse-history-dialog").open)await readHistory();
      }catch(error){if(valid(account,epoch))announce(error.message);}
      finally{if(valid(account,epoch)){state.undoBusy=false;for(const slot of undoSlots())for(const control of slot.querySelectorAll("button"))control.disabled=false;}}
    }
    function recordView(item){
      const account=getAccount(),epoch=state.epoch;
      const run=async()=>{try{if(!valid(account,epoch))return;await initialize();if(!valid(account,epoch))return;await request("history",{method:"POST",body:JSON.stringify({item})});}catch{/* History failure cannot prevent opening a title. */}};
      const work=state.viewTail.then(run,run);state.viewTail=work;return work;
    }
    function showHistoryItem(item,items,label){$("browse-history-dialog").close();showDetail(item,{id:"personal",name:label,url:"https://www.themoviedb.org/"},{items,source:{kind:"personal_library",label}});}
    function historyGrid(rows,label){
      const grid=node("div",null,"shelf-grid history-grid"),items=rows.map(row=>row.item);let shown=0;
      const more=button("显示更多","button ghost");
      const appendPage=()=>{
        for(const row of rows.slice(shown,shown+24)){
          const item=row.item,card=node("article",null,"shelf-card"),poster=button("","wall-poster",()=>showHistoryItem(item,items,label));poster.setAttribute("aria-label","查看 "+item.title+" 详情");
          const img=image(item.poster_url,item.title);if(img)poster.append(img);else poster.append(node("span",item.title,"placeholder-title"));
          card.append(poster,node("h4",item.title),node("p",[item.year,row.viewed_at?compactDate(row.viewed_at):null].filter(Boolean).join(" · ")));grid.append(card);
        }
        shown+=24;more.hidden=shown>=rows.length;
      };
      more.addEventListener("click",appendPage);appendPage();return {grid,more};
    }
    function renderHistory(){
      const content=$("browse-history-content");content.replaceChildren();
      for(const tab of ["views","walls","actions"])$("history-"+tab).setAttribute("aria-pressed",String(state.tab===tab));
      if(state.tab==="views"){
        const rows=state.history?.views||[];if(!rows.length)content.append(node("p","还没有打开过影片详情。点击任一海报，故事就会留在这里。","shelf-note"));
        else{const {grid,more}=historyGrid(rows,"最近打开的影片");content.append(grid,more);}
      }else if(state.tab==="walls"){
        const rows=state.history?.walls||[];if(!rows.length)content.append(node("p","还没有已展示的海报墙记录。后台预载不会出现在这里。","shelf-note"));
        for(const row of rows){
          const section=node("details",null,"history-wall"),summary=node("summary",compactDate(row.displayed_at)+" · "+row.items.length+" 部 · 查看这一面");section.append(summary);let filled=false;
          section.addEventListener("toggle",()=>{if(!section.open||filled)return;filled=true;const {grid,more}=historyGrid(row.items.map(item=>({item})),"当时展示的海报墙");section.append(grid,more);});content.append(section);
        }
      }else{
        if(!state.actions.length)content.append(node("p","暂无可撤销操作。新的喜欢、观看、片单标记会在这里保留 15 分钟。","shelf-note"));
        for(const row of state.actions){const line=node("div",null,"history-action"),control=button("撤销","button",()=>undoAction(row));control.disabled=state.undoBusy;line.append(node("div",row.label+" · "+row.title),node("small",compactDate(row.created_at)),control);content.append(line);}
      }
    }
    async function readHistory(){
      const account=getAccount(),epoch=state.epoch,read=++state.historyRead;state.historyBusy=true;$("browse-history-status").textContent="正在读取账号历史…";
      try{
        await state.viewTail;if(!valid(account,epoch)||read!==state.historyRead)return;
        const [history,actions]=await Promise.all([request("history"),request("actions")]);
        if(!valid(account,epoch)||read!==state.historyRead||!$("browse-history-dialog").open)return;
        state.history=history;state.actions=actions.items;renderHistory();$("browse-history-status").textContent="已按账号同步。";
      }catch(error){if(valid(account,epoch)&&read===state.historyRead)$("browse-history-status").textContent=error.message;}
      finally{if(valid(account,epoch)&&read===state.historyRead)state.historyBusy=false;}
    }
    async function openHistory(){
      try{await initialize();$("browse-history-dialog").showModal();$("browse-history-dialog").scrollTop=0;document.body.classList.add("shelf-open");$("browse-history-content").replaceChildren();await readHistory();}catch(error){announce(error.message);}
    }
    function reset(){
      state.epoch++;state.shortcutBusy=false;state.shortcutAt=0;state.shortcutRead=null;state.shortcutsVisible=false;state.shortcutSignature="";$("wall-preset-shortcuts").hidden=true;$("preset-shortcut-list").replaceChildren();$("preset-shortcut-status").hidden=true;state.viewTail=Promise.resolve();state.presets=null;state.presetBusy=false;state.history=null;state.actions=[];state.undo=null;state.undoBusy=false;state.historyBusy=false;state.presetRead++;state.historyRead++;clearTimeout(state.undoTimer);hideUndo();
      $("preset-name").value="";$("preset-select").replaceChildren();$("preset-archived").replaceChildren();$("browse-history-content").replaceChildren();if($("browse-history-dialog").open)$("browse-history-dialog").close();
    }
    $("preset-shortcut-manage").addEventListener("click",openFilters);
    $("immersive-filters").addEventListener("click",openFilters);$("immersive-history").addEventListener("click",openHistory);
    $("wall-genres").addEventListener("click",openFilters);$("wall-presets").addEventListener("click",openFilters);$("wall-browse-history").addEventListener("click",openHistory);
    $("preset-select").addEventListener("change",()=>{const row=state.presets?.items.find(row=>row.id===$("preset-select").value);if(row){setDraft(row.filters);$("preset-status").textContent="已载入“"+row.name+"”，点击应用筛选生效。";}presetControls();});
    $("preset-save").addEventListener("click",()=>changePreset({operation:"save",name:$("preset-name").value,filters:{...baseDraft(),...selection()}}));
    $("preset-remove").addEventListener("click",()=>{if($("preset-select").value)return changePreset({operation:"remove",id:$("preset-select").value});});
    for(const kind of ["include","exclude"])for(const input of inputs(kind))input.addEventListener("change",()=>{if(input.checked){const opposite=kind==="include"?"exclude":"include";for(const other of inputs(opposite))if(other.value===input.value)other.checked=false;}});
    $("browse-history-close").addEventListener("click",()=>$("browse-history-dialog").close());$("browse-history-dialog").addEventListener("close",()=>document.body.classList.remove("shelf-open"));
    $("history-refresh").addEventListener("click",readHistory);for(const tab of ["views","walls","actions"])$("history-"+tab).addEventListener("click",()=>{state.tab=tab;renderHistory();});
    return {shortcutsReady,refreshShortcuts,selection,draft,labels,focusGroup,resetDraft,readPresets,offerUndo,recordView,reset,saving(value){state.filtersSaving=value;presetControls();}};
  }
};
