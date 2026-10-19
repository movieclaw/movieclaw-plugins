'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const nodes=new Map(),events={},requests=[];let user='alice',delayed=null;
function node(tag,text,cls){return {tag,textContent:text||'',className:cls||'',children:[],events:{},dataset:{},style:{},value:'',disabled:false,hidden:false,open:false,isConnected:true,attrs:{},append(...rows){this.children.push(...rows);},replaceChildren(...rows){this.children=rows;},setAttribute(k,v){this.attrs[k]=v;},addEventListener(k,fn){this.events[k]=fn;},querySelector(){return null;},querySelectorAll(){return [];},showModal(){this.open=true;},close(){this.open=false;},scrollIntoView(){}};}
const $=id=>{if(!nodes.has(id))nodes.set(id,node('div'));return nodes.get(id);};
const button=(text,cls,fn)=>{const out=node('button',text,cls);out.events.click=fn;return out;};
const item={media_item_id:42,library_id:1,kind:'movie',tmdb_id:550,title:'Real inventory title',year:1999,rating:8.1,poster_url:'/images/assets/42/poster.jpg',resolutions:['1080p']};
const context={document:{getElementById:$,querySelectorAll(){return[];},hidden:false},window:{addEventListener(k,f){events[k]=f;},scrollY:0},URL,URLSearchParams,AbortController,setInterval:()=>1,clearInterval(){},sessionStorage:{setItem(){},getItem(){return null;}},IntersectionObserver:class{observe(){}disconnect(){}unobserve(){}},MovieClawTransport:{request:async(url,options={})=>{
 requests.push(url);
 if(url.endsWith('/auth/me'))return {role:'member',username:user};
 if(url.endsWith('/libraries?scope=visible'))return [{id:1,name:'Movies',kind:'movie',viewer_access:true},{id:2,name:'TV',kind:'tv',viewer_access:true}];
 if(url.endsWith('/theater/preferences'))return {revision:'',filters:{kind:'movie'}};
 if(url.endsWith('/collections'))return user==='alice'?[{id:5,name:'Movie collection',library_id:1},{id:6,name:'Private TV collection',library_id:2}]:[];
 if(url.includes('/items?')){if(delayed)return delayed;return [item];}
 if(url.endsWith('/art'))return {key:null};
 if(url.endsWith('/items/42'))return {...item,files:[{state:'in_place'}],local_meta:{plot:'Plot'}};
 throw Error(url);
}}};
vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(__dirname,'../platform_discovery/assets/theater.js'),'utf8'),context);
const tool=context.MovieClawTheater.create({node,button,announce(){}});
(async()=>{
 for(const name of ['第 7 集','第07集','Episode 7','EP. 07','S01E07','爱情故事.Love.Story.2026.S01E07.1080p.WEB-DL.H264.DDP 5.1.CHDWEB','movie.mkv'])assert.equal(tool.episodeTitle({name,episode_number:7}),'');
 assert.equal(tool.episodeTitle({name:'告别的那一天',episode_number:7}),'告别的那一天');
 assert.equal(tool.episodeTitle({name:'Episode 7: A New Beginning',episode_number:7}),'Episode 7: A New Beginning');
 assert.equal(tool.episodeTitle({name:'1080p',episode_number:7}),'1080p','one technical-looking word alone is not enough to discard a title');
 assert.equal(tool.episodeLabel({name:'第 1 集',episode_number:1},null),'第 1 集');
 assert.equal(tool.episodeLabel({name:'Love.Story.S01E07.1080p.WEB-DL.H264',episode_number:7},null),'第 7 集');
 assert.equal(tool.episodeLabel({name:'重逢',episode_number:7,played:true},7),'继续 · 第 7 集 · 重逢 · 已看完');
 assert.equal(tool.playLink(item),'/play/42');
 assert.equal(tool.playLink(item,{season:2,episode_number:3,position_ms:95000}),'/play/42/s02e03?t=95');
 assert.equal(tool.playLink(item,{season:0,episode_number:4,position_ms:95000,played:true}),'/play/42/s00e04');
 assert.equal(tool.posterUrl('/images/assets/42/poster.jpg'),'/api/v1/images/assets/42/poster.jpg?w=400');
 for(const bad of ['//external/secret','javascript:alert(1)','/etc/passwd','https://user:password@image.tmdb.org/a'])assert.equal(tool.posterUrl(bad),'');
 await tool.open();assert.equal($('theater-wall').children.length,1,'inventory card appears before any image load or artwork selection');
 assert.ok($('theater-collection').children.some(x=>x.textContent==='Movie collection'));assert.ok(!$('theater-collection').children.some(x=>x.textContent==='Private TV collection'));
 const card=$('theater-wall').children[0];assert.equal(card.children[0].children[1].loading,'eager');
 await card.children[0].events.click();const root=$('theater-detail-content').children[0],main=root.children[1];const actions=main.children.find(n=>n.className==='theater-detail-actions');
 assert.equal(actions.children[0].href,'/play/42');assert.equal(actions.children[1].href,'/library/1/item/42');
 // A new account removes the previously rendered private inventory before its read finishes.
 tool.leave();user='bob';let release;delayed=new Promise(r=>release=r);const opening=tool.open();await new Promise(r=>setImmediate(r));
 assert.equal($('theater-wall').children.length,0,'old-account cards removed immediately');assert.ok(!$('theater-collection').children.some(x=>x.textContent==='Movie collection'),'old-account collection labels removed');assert.equal($('theater-detail-dialog').open,false);
 tool.leave();release([item]);await opening;assert.equal($('theater-wall').children.length,0,'late read cannot render after leaving');
 console.log('PASS: true media id and season/resume play links, safe native poster URLs, immediate inventory render, account cleanup and stale read rejection');
})().catch(e=>{console.error(e);process.exitCode=1;});
