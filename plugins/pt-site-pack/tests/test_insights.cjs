const test=require('node:test'),assert=require('node:assert/strict');
const core=require('../pt_pack/assets/insights.js');
const site=(name,n,age)=>({name,key:name,profile:{uploaded_bytes:n,age_days:age},live:{upspeed_bytes:n}});
test('shares exclude unknown values and include true zero without fabricating slices',()=>{
 const rows=[site('A',10),site('B',0),site('C',null),site('D',30),site('E',NaN)];
 const before=rows.slice();const d=core.distribution(rows,'uploaded_bytes');assert.equal(d.sum,40);assert.equal(d.known,3);assert.equal(d.missing,2);assert.deepEqual(d.slices.map(s=>s.fraction),[.75,.25]);assert.deepEqual(rows,before);
 assert.deepEqual(core.distribution([site('zero',0)],'uploaded_bytes').slices,[]);assert.equal(core.total([site('missing',null)],'uploaded_bytes'),null);
});
test('top ranking is bounded; small slices are combined with exact total',()=>{
 const d=core.distribution(Array.from({length:30},(_,i)=>site('S'+i,i+1)),'uploaded_bytes');assert.equal(d.ranking.length,8);assert.equal(d.slices.length,6);assert.equal(d.slices.reduce((n,s)=>n+s.value,0),465);assert.ok(Math.abs(d.slices.reduce((n,s)=>n+s.fraction,0)-1)<1e-12);
});
test('milestones handle unknown, thresholds, progress and highest level',()=>{
 assert.equal(core.milestone([site('unknown',null)]).progress,null);assert.equal(core.milestone([site('zero',0)]).progress,0);
 const m=core.milestone([site('A',2**40)]);assert.equal(m.name,'资源旅人');assert.equal(m.progress,0);
 assert.equal(core.milestone([site('A',100*2**50)]).progress,1);
});
test('manual age validates calendar, range and future; registration age is a lower bound',()=>{
 const now=new Date('2026-10-24T12:00:00Z');assert.equal(core.manualAge('2026-10-24',now),0);assert.equal(core.manualAge('2026-10-23',now),1);
 for(const bad of ['2026-10-25','2019-02-29','1999-12-31','',null])assert.equal(core.manualAge(bad,now),null);
 const rows=[site('A',0,1000),site('B',0,2000),site('C',0,null)];assert.deepEqual(core.age(rows,'',now),{days:2000,source:'已接入站点的最早注册记录',known:2,total:3,manual:false});
 assert.equal(core.age(rows,'2026-10-23',now).days,2000);assert.equal(core.age(rows,'2026-10-23',now).manual,false);
 assert.equal(core.age([site('missing',0,null)],'2026-10-23',now).days,1);assert.equal(core.age([], '2026-10-23',now).manual,true);
});
