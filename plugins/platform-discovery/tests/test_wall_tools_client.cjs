"use strict";
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const nodes=new Map(),writes=[],timers=new Map();let username='alice',epoch=0,waitPreset=null,waitHistory=null,waitUndo=null,initHook=null,images=0,adoptions=0,applied=0,waitApply=null;
function node(tag,text){return {tag,textContent:text||'',value:'',children:[],events:{},attrs:{},dataset:{},open:false,hidden:true,disabled:false,checked:false,style:{},classList:{add(){},remove(){}},append(...rows){this.children.push(...rows);},replaceChildren(...rows){this.children=rows;},setAttribute(k,v){this.attrs[k]=v;},addEventListener(k,fn){this.events[k]=fn;},querySelectorAll(selector){const out=[];for(const row of this.children){if(row.tag===selector)out.push(row);out.push(...(row.querySelectorAll?.(selector)||[]));}return out;},showModal(){this.open=true;},close(){this.open=false;this.events.close?.();}};}
const $=id=>{if(!nodes.has(id))nodes.set(id,node('div'));return nodes.get(id);};
const button=(text,cls,action)=>{const out=node('button',text);if(action)out.events.click=action;return out;};
const genre={include:['18','80','27'].map(value=>Object.assign(node('input'),{value})),exclude:['18','80','27'].map(value=>Object.assign(node('input'),{value}))};
const base={country:['JP','KR'].map(value=>Object.assign(node('input'),{value})),era:['1990s','2000s'].map(value=>Object.assign(node('input'),{value}))};
const slots=[$('wall-undo-bar'),$('detail-undo'),$('shelf-undo')];
const document={getElementById:$,body:{classList:{add(){},remove(){}}},querySelectorAll(selector){if(selector==='.wall-undo-slot')return slots;const match=selector.match(/wall-genre-(include|exclude)/);return match?genre[match[1]]:[];}};
const clone=v=>JSON.parse(JSON.stringify(v));
const records=new Map(),empty=()=>({revision:'',items:[],archived:[]});
let filters={media_type:'all',countries:['JP'],eras:['1990s'],genres:['80'],exclude_genres:['27'],exploration:'discovery'};
const context={document,setTimeout(fn){const id=++epoch;timers.set(id,fn);return id;},clearTimeout(id){timers.delete(id);}};vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname,'../platform_discovery/assets/wall-tools.js'),'utf8'),context);
const tool=context.MovieClawWallTools.create({node,button,image:()=>{images++;return node('img');},getAccount:()=>username,getFilters:()=>filters,filterInputs:kind=>base[kind],openFilters(){},saveFilters(){},applyPreset:async(id,revision)=>{applied++;if(waitApply)await waitApply;const row=records.get(username).items.find(row=>row.id===id);assert.equal(revision,records.get(username).revision);filters=clone(row.filters);return true;},initialize:async()=>{if(initHook)await initHook();},showDetail(){},compactDate:x=>x,announce(){},adoptShelf(){adoptions++;},request:async(url,options={})=>{
 const account=username;
 if(url==='presets'){
  if(options.method!=='POST'){const result=clone(records.get(account)||empty());if(waitPreset)await waitPreset;return result;}
  const value=JSON.parse(options.body).change;writes.push({url,account,value});const current=clone(records.get(account)||empty());
  if(value.revision!==current.revision){const error=Error('conflict');error.status=409;throw error;}
  if(value.operation==='save')current.items.push({id:'new-'+writes.length,name:value.name,filters:value.filters});
  if(value.operation==='remove'){const entry=current.items.find(i=>i.id===value.id);current.items=current.items.filter(i=>i.id!==value.id);current.archived.push(entry);}
  if(value.operation==='restore'){const entry=current.archived.find(i=>i.id===value.id);current.archived=current.archived.filter(i=>i.id!==value.id);current.items.push(entry);}
  current.revision=String(writes.length);records.set(account,current);return clone(current);
 }
 if(url==='history'&&options.method!=='POST'){if(waitHistory)await waitHistory;return {views:Array.from({length:60},(_,i)=>({item:{title:'film'+i,detail_ref:'tmdb:movie:'+(i+1),poster_url:'https://image.tmdb.org/x.jpg'}})),walls:[]};}
 if(url==='actions')return {items:[]};
 if(url==='history'){writes.push({url,account,value:JSON.parse(options.body)});return {saved:true};}
 if(url==='actions/undo'){writes.push({url,account,value:JSON.parse(options.body)});if(waitUndo)await waitUndo;return {saved:[],liked:[],watched:[],dismissed:[]};}
 throw Error(url);
}});
const tick=()=>new Promise(r=>setImmediate(r));
(async()=>{
 assert.equal(writes.length,0);assert.equal(images,0,'new tools do not add cold-open requests or image loading');
 tool.draft();assert.equal(base.country[0].checked,true);assert.equal(genre.include[1].checked,true);assert.equal(genre.exclude[2].checked,true);assert.equal($('wall-exploration').value,'discovery');
 genre.include[2].checked=true;genre.include[2].events.change();assert.equal(genre.exclude[2].checked,false,'conflicting draft checkboxes automatically clear the opposite');
 tool.focusGroup('wall-genres');assert.equal($('filter-genre-section').open,true);assert.equal($('filter-country-section').open,false);
 await tool.readPresets();$('preset-name').value='日韩片单';await $('preset-save').events.click();
 assert.equal(writes.at(-1).value.filters.genres.includes('80'),true);assert.equal(writes.at(-1).value.filters.exploration,'discovery');assert.equal(records.get('alice').items.length,1);assert.equal(filters.countries[0],'JP','collecting does not apply or mutate active filters');
 const saved=records.get('alice').items[0];$('preset-select').value=saved.id;$('preset-select').events.change();assert.equal($('preset-remove').disabled,false);
 await $('preset-remove').events.click();assert.equal(records.get('alice').items.length,0);assert.equal(records.get('alice').archived.length,1);
 // Slow old reads cannot replace a newly saved list.
 let release;waitPreset=new Promise(r=>release=r);const oldRead=tool.readPresets();await tick();$('preset-name').value='新方案';await $('preset-save').events.click();waitPreset=null;release();await oldRead;
 assert.equal($('preset-select').children.some(i=>i.textContent==='新方案'),true);
 // Shortcut labels load after the wall, apply in one request, and never open a dialog.
 tool.shortcutsReady();assert.equal($('wall-preset-shortcuts').hidden,false);await tool.refreshShortcuts(true);
 const shortcut=$('preset-shortcut-list').children.find(row=>row.tag==='button');assert.ok(shortcut);assert.equal(shortcut.textContent,'新方案');
 let applyRelease;waitApply=new Promise(r=>applyRelease=r);const pendingApply=shortcut.events.click();await tick();await shortcut.events.click();assert.equal(applied,1,'duplicate taps cannot enqueue two writes');applyRelease();await pendingApply;waitApply=null;
 assert.equal($('wall-filter-dialog').open,false,'one tap never opens the filter form');assert.equal($('preset-shortcut-list').children[0].attrs['aria-pressed'],'true');assert.equal($('preset-shortcut-list').children[0].disabled,false);
 // UI history uses pages of 24 images, and is only loaded after opening history.
 await $('wall-browse-history').events.click();assert.equal($('browse-history-dialog').open,true);assert.equal(images,24,'history loads the first page only');
 const receipt={id:'a'.repeat(32),label:'已看过',title:'film1'};tool.offerUndo(receipt);assert.equal($('wall-undo-bar').hidden,false);
 let undoRelease;waitUndo=new Promise(r=>undoRelease=r);const undo=$('detail-undo').children[1].events.click();await tick();await $('shelf-undo').children[1].events.click();assert.equal(writes.filter(i=>i.url==='actions/undo').length,1,'all undo surfaces share one in-flight guard');undoRelease();await undo;waitUndo=null;assert.equal(adoptions,1);assert.equal($('wall-undo-bar').hidden,true);
 // No old-account history or receipt can surface after reset.
 let historyRelease;waitHistory=new Promise(r=>historyRelease=r);const late=$('history-refresh').events.click();await tick();username='bob';tool.reset();historyRelease();await late;waitHistory=null;assert.equal($('browse-history-content').children.length,0);assert.equal($('browse-history-dialog').open,false);assert.equal($('wall-undo-bar').hidden,true);assert.equal($('preset-shortcut-list').children.length,0);assert.equal($('wall-preset-shortcuts').hidden,true);
 const count=writes.length;initHook=async()=>{username='carol';tool.reset();};await tool.recordView({detail_ref:'tmdb:movie:1',title:'old'});assert.equal(writes.length,count,'old-account pending view cannot be written into a new account');
 console.log('PASS: account tools stay off cold-open path; draft include/exclude, preset collect/archive, stale reads, lazy history, shared undo guard and account-switch cleanup');
})().catch(e=>{console.error(e);process.exitCode=1;});
