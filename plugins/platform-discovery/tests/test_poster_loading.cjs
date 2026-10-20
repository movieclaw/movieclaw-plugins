"use strict";
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const context={URL,setTimeout,clearTimeout};vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(__dirname,'../platform_discovery/assets/posters.js'),'utf8'),context);
const posters=context.MovieClawPosters,base='https://image.tmdb.org/t/p/w500/';
const tick=()=>new Promise(resolve=>setImmediate(resolve));
let started=[];class FakeImage{set src(value){this.value=value;if(value)started.push(this);}get src(){return this.value;}}
const node=()=>({src:'',removed:false,hidden:false,placeholder:{hidden:false},remove(){this.removed=true;},get parentElement(){return {querySelector:()=>this.placeholder};}});
function loader(options={}){return posters.create({urlFor:v=>'/proxy?url='+encodeURIComponent(v),ImageClass:FakeImage,hidden:()=>false,dpr:()=>2,connection:()=>({}),...options});}
(async()=>{
 assert.match(posters.sized(base+'poster.jpg',150,2),/\/w342\//);assert.match(posters.sized(base+'poster.jpg',150,1),/\/w185\//);
 assert.match(posters.sized(base+'poster.jpg',180,4),/\/w500\//);assert.match(posters.sized(base+'poster.jpg',900,2),/\/w780\//);
 assert.equal(posters.sized('javascript:alert(1)',150),null);assert.equal(posters.sized('https://u:p@image.tmdb.org/t/p/w500/a.jpg',150),null);
 let service=loader({timeout:5});const imgs=Array.from({length:36},()=>node());imgs.forEach((img,i)=>service.attach(img,[base+i+'.jpg'],{width:150,priority:i<6?0:1}));
 assert.equal(started.length,0,'displayed elements load directly, no off-DOM transfer gate');assert.ok(imgs.every(i=>i.src&&i.loading==='eager'));assert.ok(imgs.slice(0,6).every(i=>i.fetchPriority==='high'));
 imgs.slice(0,18).forEach(img=>img.onload());await service.settled(10);
 assert.ok(imgs.every(i=>!i.removed),'soft wait cannot delete the slower half');assert.ok(imgs.slice(18).every(i=>i.onerror&&i.onload&&!i.hidden),'slow requests remain live past preload timeout');
 imgs.slice(18).forEach(img=>img.onload());assert.ok(imgs.every(i=>i.placeholder.hidden),'slow half can still finish and display');service.close();
 service=loader();const fallback=node();service.attach(fallback,[base+'broken.jpg','https://cdn.example/fallback.jpg'],{width:150});assert.match(decodeURIComponent(fallback.src),/w342/);fallback.onerror();assert.match(decodeURIComponent(fallback.src),/w500/);fallback.onerror();assert.match(decodeURIComponent(fallback.src),/cdn.example/);fallback.onload();assert.equal(fallback.placeholder.hidden,true);service.close();
 service=loader();const failed=node();service.attach(failed,['https://cdn.example/bad.jpg']);failed.onerror();await new Promise(r=>setTimeout(r,1010));assert.ok(failed.onerror,'transient errors retry once');failed.onerror();assert.equal(failed.hidden,true);assert.equal(failed.removed,false,'permanent failure keeps DOM and title placeholder');assert.equal(failed.onerror,null,'retry is bounded');service.close();
 service=loader();const stale=node();service.attach(stale,[base+'old.jpg']);const oldsrc=stale.src;service.beginWall();assert.equal(stale.onerror,null);assert.equal(stale.src,oldsrc,'cancellation does not blank still displayed old wall');service.close();
 started=[];service=loader({timeout:20});const warming=service.warmNext([{poster_url:base+'good.jpg'},{poster_url:base+'bad.jpg'}],150);assert.equal(started.length,2);started[0].onload();started[1].onerror();await tick();started[2].onerror();assert.equal(await warming,true,'one unavailable poster cannot discard the useful warmed face');service.close();
 started=[];service=loader();const many=Array.from({length:12},(_,i)=>({poster_url:base+'warm'+i+'.jpg'}));const warm=service.warmNext(many,150);assert.equal(started.length,3,'background warming uses only three slots');const duplicate=service.warmWall([many[0]],150);assert.equal(started.length,3,'background in-flight URL deduplicates');
 for(let i=0;i<12;i++){started[i].onload();await tick();}await warm;await duplicate;
 assert.equal(service.readyForSwap([...many,{poster_url:base+'cold.jpg'}],150).length,12,'ready candidates exclude cold posters without starting any download');
 const warmed=node();service.attach(warmed,[many[0].poster_url],{width:150});assert.equal(warmed.src,started[0].src,'foreground uses exact cache URL');service.close();
 started=[];service=loader();const canceled=service.warmNext(many,150);service.cancelNext();assert.equal(await canceled,false);assert.equal(started.length,3,'cancelled warming starts no queued fallback');service.close();
 started=[];service=loader({connection:()=>({saveData:true})});assert.equal(await service.warmNext(many,150),false);await service.warmWall(many,150);assert.equal(started.length,0,'optional loading respects Save-Data');service.close();
 started=[];service=loader();const swapping=service.prepareSwap([{poster_url:base+'swap-broken.jpg',poster_fallback_urls:['https://cdn.example/ambient-ready.jpg']}],150);
 started[0].onerror();await tick();started[1].onerror();await tick();started[2].onload();assert.equal((await swapping).length,1);
 const swapped=node();service.attach(swapped,[base+'swap-broken.jpg','https://cdn.example/ambient-ready.jpg'],{width:150});assert.match(decodeURIComponent(swapped.src),/ambient-ready/,'promote the successfully prepared fallback, never fade into a known broken primary');service.close();
 started=[];service=loader();const aborted=service.prepareSwap(many,150);assert.equal(started.length,3,'ambient jobs are capped at three');service.cancelAmbient();assert.equal((await aborted).length,0);service.close();
 console.log('PASS: native foreground loading, slow half retained and later displayed, bounded fallback/retry, three-slot deduplicated speculative loading, partial failures and cancellation');
})().catch(error=>{console.error(error);process.exitCode=1;});
