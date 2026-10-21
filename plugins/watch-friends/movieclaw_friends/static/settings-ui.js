"use strict";
// 草稿只留在当前标签页内存，按登录会话隔离；授权确认框不命名，绝不恢复。
window.MovieFriendsSettings = (() => {
  let activeSession;
  const drafts=new Map(),bindings=new WeakMap();
  function enter(session){if(session!==activeSession){drafts.clear();activeSession=session;}}
  function clear(key,session){enter(session);drafts.delete(key);}
  function bind(form,{key,session,onRestore=()=>{}}){
    enter(session);bindings.get(form)?.destroy();
    const fields=[...form.querySelectorAll("input[name],select[name],textarea[name]")].filter(n=>n.type!=="password"&&n.type!=="hidden");
    const value=n=>n.type==="checkbox"?n.checked:n.value;
    const baseline=new Map(fields.map(n=>[n.name,value(n)]));
    const write=(n,v)=>{if(n.type==="checkbox")n.checked=Boolean(v);else if(n.tagName!=="SELECT"||[...n.options].some(o=>o.value===v))n.value=v;};
    for(const n of fields){const saved=drafts.get(key);if(saved?.has(n.name))write(n,saved.get(n.name));}
    const bar=document.createElement("div"),status=document.createElement("small"),discard=document.createElement("button");
    bar.className="settings-draft-bar";status.setAttribute("role","status");discard.type="button";discard.className="button button-quiet";discard.textContent="放弃更改";
    bar.append(status,discard);form.append(bar);
    function update(){
      if(session!==activeSession)return;
      const changes=new Map(fields.filter(n=>value(n)!==baseline.get(n.name)).map(n=>[n.name,value(n)]));
      if(changes.size)drafts.set(key,changes);else drafts.delete(key);
      const dirty=changes.size>0;form.dataset.settingsDirty=String(dirty);bar.classList.toggle("is-dirty",dirty);status.textContent=dirty?"有更改待保存":"已保存";status.title=dirty?"草稿仅在当前标签页临时保留，刷新或关闭页面前请保存。":"此模块独立保存；修改不会自动提交。";discard.hidden=!dirty;
    }
    discard.onclick=()=>{for(const n of fields)write(n,baseline.get(n.name));drafts.delete(key);onRestore();update();};
    form.addEventListener("input",update);form.addEventListener("change",update);
    onRestore();update();
    const binding={saved(){for(const n of fields)baseline.set(n.name,value(n));drafts.delete(key);update();},destroy(){form.removeEventListener("input",update);form.removeEventListener("change",update);bar.remove();}};
    bindings.set(form,binding);return binding;
  }
  function requireSaved(key,session,label){enter(session);if(drafts.has(key))throw new Error(`${label}有未保存的更改，请先保存或放弃更改，再预览或开启分享。`);}
  return {bind,clear,requireSaved};
})();
