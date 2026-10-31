const assert = require('node:assert/strict');
const fs = require('fs');
const l = require('./logic.js');
const row = {job_id:'job', job_type:'p115-import:request', status:'running'};
for (const status of ['queued','running','waiting','retry_wait','blocked']) assert(l.canCancel({...row,status}));
for (const status of ['cancelled','cancelling','succeeded','failed']) assert(!l.canCancel({...row,status}));
assert(!l.canCancel({...row,job_type:'library:scan'}));
assert(!l.canCancel(null));
assert(l.canChooseTitle({...row,status:'blocked'}));
assert(l.canChooseTitle({...row,status:'failed'}));
assert(l.canChooseTitle({...row,status:'cancelled'}));
assert(!l.canChooseTitle(row));
assert(!l.canChooseTitle({...row,job_type:'p115-import:transfer',status:'blocked'}));
const titles = [
  {provider:'tmdb',external_id:'123',media_type:'tv',title:'测试剧',release_year:2026},
  {provider:'tmdb',external_id:'456',media_type:'movie',title:'测试电影'},
  {provider:'douban',external_id:'789',media_type:'tv',title:'不能拿豆瓣编号当TMDB'},
  {provider:'tmdb',external_id:'bad',media_type:'tv'},
  {provider:'tmdb',external_id:'0',media_type:'tv'},
  {provider:'tmdb',external_id:'12',media_type:'person'}, null,
];
assert.deepEqual(l.tmdbSearchOptions(titles,'tv').map(r=>r.tmdb_id),[123]);
assert.deepEqual(l.tmdbSearchOptions(titles,'movie').map(r=>r.tmdb_id),[456]);
assert.deepEqual(l.tmdbSearchOptions([], 'tv'),[]);
assert.equal(l.tmdbSearchOptions(titles,'tv')[0].year,2026);
assert(l.needsPoll({live:[{status:'cancelling'}]}));
assert(!l.needsPoll({recent:[{status:'cancelled'}]}));
assert(l.submitState('不是链接').disabled);
assert(!l.submitState('https://115.com/s/test').disabled);
const html = fs.readFileSync(__dirname+'/index.html','utf8');
for (const m of html.matchAll(/<script>([\s\S]*?)<\/script>/g)) new Function(m[1]);
console.log('网页取消/作品搜索正反例、状态轮询与脚本语法全部通过');

assert.equal(l.boardPollDelay({live:[{status:'running'}]},false),4000);
assert.equal(l.boardPollDelay({},false),30000);
assert.equal(l.boardPollDelay({},true),null);
assert.equal(l.boardPollDelay({},false,1),5000);
assert.equal(l.boardPollDelay({},false,8),120000);
const cleanupPreview={status:'preview',approval_token:'fake-preview',expires_at:Date.now()/1000+60,
  files:[{old:{file_id:9},removed_paths:['old.mkv','old.nfo']}]};
assert(l.canConfirmOldCleanup(cleanupPreview));
assert(!l.canConfirmOldCleanup({...cleanupPreview,expires_at:1}));
assert(!l.canConfirmOldCleanup({...cleanupPreview,approval_token:''}));
assert(!l.canConfirmOldCleanup({...cleanupPreview,files:[]}));
assert(!l.canConfirmOldCleanup({...cleanupPreview,files:[{old:{file_id:9},removed_paths:[]}]}));
console.log('v0.44：隐藏页面暂停/错误退避与精确删除确认逻辑通过');
