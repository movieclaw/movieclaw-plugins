"use strict";
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const v = require('./view.js');
const base = {job_id:'job_1',job_type:'p115-import:request', title:'115 链接入库', status:'cancelled',
  message:'任务已取消', candidates:[{tmdb_id:1,kind:'tv',title:'候选剧',year:2026}]};
const cancelled = v.taskHtml(base);
assert(cancelled.includes('class="task-details" id="detail-job_1" hidden'));
assert(cancelled.includes('aria-expanded="false"'));
assert(!cancelled.includes('data-tmdb='), '已取消历史不再渲染候选按钮');
assert(!cancelled.includes('quick-actions'), '历史行默认没有一排操作');
assert(cancelled.includes('重新继续'), '续跑操作仍在详情中');
assert(cancelled.includes('搜索 TMDB'), '历史仍可手动确认作品');
assert.equal(v.taskTone('cancelled'), '', '取消记录不再当警告高亮');
const blocked = v.taskHtml({...base,status:'blocked'});
assert(blocked.includes('class="quick-actions"'));
assert(blocked.includes('确认作品'));
assert(blocked.includes('data-cancel="job_1"'));
assert(blocked.includes('<details class="candidate-section">'), '候选默认折叠');
assert(!blocked.includes('<details class="candidate-section" open'));
assert(v.taskHtml({...base,status:'running',percent:30},true).includes('aria-valuenow="30"'));
assert(!v.taskHtml({...base,status:'running',percent:'<script>'}).includes('aria-valuenow='));
assert(!v.taskHtml({...base,status:'succeeded',percent:30}).includes('aria-valuenow='));
assert(v.taskHtml({...base,job_type:'p115-import:transfer',status:'running'}).includes('data-cancel'));
assert(!v.taskHtml({...base,job_type:'p115-import:transfer',status:'blocked'}).includes('data-search-job'));
const hostile = v.taskHtml({...base, job_id:'a" onclick="bad',title:'<img onerror=bad>',message:'<script>bad</script>'});
assert(!hostile.includes('<img'));
assert(!hostile.includes('<script>'));
assert(!hostile.includes(' onclick="bad'));
assert(hostile.includes('&lt;img'));
assert(v.taskHtml({...base,status:'succeeded',media_item_id:3,library_id:5}).includes('href="/library/5/item/3" target="_top"'));
assert.equal(v.titleOf(base), '链接入库');
assert.equal(v.taskCaption({...base,inventory_verified:true,library_name:'动漫',status:'succeeded'}), '动漫 · 已核验入库');
assert.deepEqual(v.boardGroups(null), {active:[], attention:[],history:[]});
assert.equal(v.boardGroups({live:[base],inbox:[base]}).active.length,1);
for (const tab of ['active','attention','history']) assert(v.emptyHtml(tab).includes('class="empty"'));

/* VM模拟DOM与网络：验证脚本真正执行及交互请求，不依赖线上账号，不触碰真实任务。 */
class Element {
  constructor(id, attrs={}) {
    this.id=id; this.attributes={...attrs}; this.dataset={}; this.events={}; this.style={};
    this.hidden=false; this.disabled=false; this.value=''; this.textContent=''; this.innerHTML='';
    this.options=[]; this.open=false; this.checked=true; this.classList={add(){}, remove(){}};
    for (const [key,value] of Object.entries(attrs)) if (key.startsWith('data-')) this.dataset[key.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]=value;
  }
  setAttribute(k,v){this.attributes[k]=String(v);}
  getAttribute(k){return this.attributes[k];}
  addEventListener(k,fn){this.events[k]=fn;}
  contains(e){return e===this;}
  querySelectorAll(){return [];}
  showModal(){this.open=true;}
  close(){this.open=false;if(this.events.close)this.events.close();}
  focus(){document.activeElement=this;}
  closest(selector){return selector === '.chip' ? this : null;}
}
const html=fs.readFileSync(__dirname+'/index.html','utf8');
const elements=new Map();
for (const m of html.matchAll(/<[^>]+\bid="([^"]+)"[^>]*>/g)) {
  const attrs={};for (const a of m[0].matchAll(/([\w-]+)="([^"]*)"/g)) attrs[a[1]]=a[2];
  elements.set(m[1],new Element(m[1],attrs));
}
const document={
  activeElement:null, events:{},
  getElementById(id){if(!elements.has(id))elements.set(id,new Element(id));return elements.get(id);},
  querySelectorAll(selector){
    if(selector==='[data-tab]')return ['active','attention','history'].map(k=>elements.get('tab-'+k));
    return [];
  },
  addEventListener(k,fn){this.events[k]=fn;}
};
const $=id=>document.getElementById(id);
$('title-kind').value='tv';
const mocks={
  feed:{inbox:[],live:[],attention:[],recent:Array.from({length:12},(_,i)=>({...base,job_id:'job_'+i}))},
  titles:[{provider:'tmdb',external_id:'123',media_type:'tv',title:'测试剧',release_year:2026,overview:'简介'}],
  calls:[],timers:[],confirms:[],confirmResult:true,
  cleanupPreview:{status:'preview',title:'测试剧',library_name:'测试库',approval_token:'fake-preview-only',expires_at:Date.now()/1000+60,
    files:[{old:{file_id:9,name:'old.mkv',size:80},new:[{name:'new.mkv',size:100}],removed_paths:['old.mkv','old.nfo']}]},
};
const context=vm.createContext({document,window:{addEventListener(){}},console,
  setTimeout(fn){mocks.timers.push(fn);return mocks.timers.length;},clearTimeout(){},setInterval(){},
  confirm(text){mocks.confirms.push(text);return mocks.confirmResult;},
  fetch:async(url,options={})=>{
    const body=options.body ? JSON.parse(options.body):null;mocks.calls.push({url,body});
    let data={};
    if(url.endsWith('/status'))data={web_logged_in:true,file_ops:'openapi',account:'测试账号',wecom_entry:true};
    if(url.includes('/board?'))data=mocks.feed;
    if(url==='/api/v1/search/titles')data={titles:mocks.titles,providers:[{provider:'tmdb',success:true}]};
    if(url.endsWith('/apply'))data={message:'已接收'};
    if(url.endsWith('/resume'))data={message:'已排队'};
    if(url.endsWith('/cancel'))data={message:'已请求取消'};
    if(url.endsWith('/cleanup-old'))data=body.yes ? {status:'started',job_id:'cleanup',message:'已提交'} : mocks.cleanupPreview;
    return {ok:true,json:async()=>({success:true,data})};
  }
});
async function flush(){for(let i=0;i<8;i++)await new Promise(r=>setImmediate(r));}
async function main(){
  for(const path of ['logic.js','view.js'])vm.runInContext(fs.readFileSync(__dirname+'/'+path,'utf8'),context);
  const script=[...html.matchAll(/<script>([\s\S]*?)<\/script>/g)][0][1];
  vm.runInContext(script,context);
  await flush();
  assert($('authcard').innerHTML.includes('已连接'));
  assert($('task-list').innerHTML.includes('暂无进行中的任务'));
  assert(!$('task-list').innerHTML.includes('候选剧'));
  assert(!mocks.calls.some(c=>c.url.includes('libraries')), '维护工具按需加载');
  $('tab-history').onclick();
  assert.equal(($('task-list').innerHTML.match(/<article/g)||[]).length,8);
  assert.equal($('btn-more').hidden,false);
  $('btn-more').onclick();
  assert.equal(($('task-list').innerHTML.match(/<article/g)||[]).length,12);
  assert.equal($('btn-more').hidden,true);
  const toggle=new Element('x',{'data-expand':'job_1','aria-controls':'detail-job_1'});
  toggle.closest=()=>toggle;
  $('task-list').events.click({target:toggle});
  assert.equal($('detail-job_1').hidden,false);
  $('task-list').events.click({target:toggle});
  assert.equal($('detail-job_1').hidden,true);
  $('btn-title-search').onclick();
  assert($('title-dialog').open);
  $('title-query').value='测试剧';
  await $('title-search-form').onsubmit({preventDefault(){}});
  assert($('title-results').innerHTML.includes('测试剧'));
  const search=mocks.calls.find(c=>c.url==='/api/v1/search/titles');
  assert.deepEqual(search.body,{query:'测试剧',provider:'tmdb',save_history:false});
  const pick=new Element('pick',{'data-search-pick':'0'});pick.closest=()=>pick;
  await $('title-results').onclick({target:pick});
  assert.equal($('chosen-title').hidden,false);
  assert($('chosen-title').textContent.includes('测试剧'));
  assert(!$('title-dialog').open);
  assert(!mocks.calls.some(c=>c.url.endsWith('/apply')), '选作品不自动提交');
  $('linkbox').value='https://115.com/s/test';
  $('linkbox').events.input();
  assert.equal($('btn-submit').disabled,false);
  await $('btn-submit').onclick();
  const apply=mocks.calls.find(c=>c.url.endsWith('/apply'));
  assert.equal(apply.body.tmdb_id,123);assert.equal(apply.body.kind,'tv');
  assert.equal($('chosen-title').hidden,true);
  assert.equal($('linkbox').value,'');
  await flush();
  const cancel=new Element('cancel',{'data-cancel':'job_test'});
  await document.events.click({target:cancel});
  assert(mocks.confirms.some(s=>s.includes('已转存文件和检查点会保留')));
  assert.equal(mocks.calls.find(c=>c.url.endsWith('/cancel')).body.job_id,'job_test');
  const before=mocks.calls.filter(c=>c.url.endsWith('/cancel')).length;
  mocks.confirmResult=false;
  await document.events.click({target:cancel});
  assert.equal(mocks.calls.filter(c=>c.url.endsWith('/cancel')).length,before,'拒绝确认不取消');
  mocks.confirmResult=true;
  const searchJob=new Element('search',{'data-search-job':'job_1'});
  await document.events.click({target:searchJob});
  $('title-query').value='测试剧';
  await $('title-search-form').onsubmit({preventDefault(){}});
  await $('title-results').onclick({target:pick});
  assert.equal(mocks.calls.find(c=>c.url.endsWith('/resume')).body.job_id,'job_1');
  await flush();
  mocks.feed={inbox:[],live:[],attention:[{...base,status:'blocked'}],recent:[]};
  await $('btn-refresh').onclick();await flush();
  $('tab-attention').onclick();
  assert($('task-list').innerHTML.includes('确认作品'));
  assert($('task-list').innerHTML.includes('<details class="candidate-section">'));
  assert.equal($('count-attention').textContent,'1');
  $('tab-attention').onkeydown({key:'ArrowRight',preventDefault(){}});
  assert.equal($('tab-history').getAttribute('aria-selected'),'true');
  const cleanup=new Element('cleanup',{'data-cleanup-old':'original'});
  await document.events.click({target:cleanup});
  assert($('cleanup-old-dialog').open);
  assert($('cleanup-old-files').innerHTML.includes('old.nfo'),'删除附属文件必须逐项可见');
  assert.equal($('cleanup-old-confirm').disabled,false);
  assert(!mocks.calls.some(c=>c.url.endsWith('/cleanup-old') && c.body.yes),'打开清单不删除');
  mocks.confirmResult=false;
  await $('cleanup-old-confirm').onclick();
  assert(!mocks.calls.some(c=>c.url.endsWith('/cleanup-old') && c.body.yes),'拒绝确认不提交');
  mocks.confirmResult=true;
  await $('cleanup-old-confirm').onclick();
  const deleteCall=mocks.calls.find(c=>c.url.endsWith('/cleanup-old') && c.body.yes);
  assert.deepEqual(deleteCall.body.file_ids,[9]);
  assert.equal(deleteCall.body.approval_token,'fake-preview-only');
  assert.equal(deleteCall.body.job_id,'original');
  assert(!$('cleanup-old-dialog').open);
  const count=mocks.calls.length;
  document.hidden=true;
  await document.events.visibilitychange();await $('btn-refresh').onclick();await flush();
  assert.equal(mocks.calls.length,count,'隐藏页面不轮询');
  document.hidden=false;
  await document.events.visibilitychange();await flush();
  assert(mocks.calls.length>count,'重新可见刷新进度');
  console.log('工作台：分类/折叠/搜索/取消/恢复/精确清理预览与拒绝确认/确认提交/隐藏暂停与可见恢复模拟交互全部通过');
}
main().catch(err=>{console.error(err);process.exitCode=1;});
