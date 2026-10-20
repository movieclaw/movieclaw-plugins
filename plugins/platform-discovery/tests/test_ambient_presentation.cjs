"use strict";
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const assets=path.join(__dirname,'../platform_discovery/assets');
const controller=fs.readFileSync(path.join(assets,'personal.js'),'utf8');
const applySource=controller.slice(controller.indexOf('    async function applyAmbient('),controller.indexOf('    async function confirmAmbient('));
async function present(cancelAt=Infinity){
 let time=0;const cards=[],before=[1,2,3].map(n=>({detail_ref:'old'+n})),after=[1,2,3].map(n=>({detail_ref:'new'+n}));
 function card(item){const names=new Set();return {dataset:{itemRef:item.detail_ref},classList:{add:n=>names.add(n),remove:n=>names.delete(n),toggle(){},contains:n=>names.has(n)},getBoundingClientRect:()=>({height:300}),replaceWith(next){cards[cards.indexOf(this)]=next;}};}
 cards.push(...before.map(card));const originals=[...cards],wall={dataset:{},querySelectorAll:()=>cards};
 const state={account:'account',generation:1,ambientTiming:{fade_out:1100,fade_in:1500,gap:650,overlap:0,variation:0},feed:{items:before},rendered:{items:before,columns:3}};
 const proposal={account:'account',generation:1,before:state.feed,targets:before,rotation:{current:before.map(v=>v.detail_ref),replace:{old1:'new1',old2:'new2',old3:'new3'},operation:'test'},result:{items:after,rotation:{count:3}}};
 const context={state,$:()=>wall,matchMedia:()=>({matches:false}),randomTake:values=>values,wallCard:card,setTimeout(){}};
 vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(assets,'ambient.js'),'utf8'),context);vm.runInContext(applySource+'globalThis.apply=applyAmbient;',context);
 const shown=await context.apply(proposal,()=>time<cancelAt,async ms=>{time+=ms;return time<cancelAt;});
 return {shown,proposal,state,wall,refs:cards.map(v=>v.dataset.itemRef),originals};
}
(async()=>{
 let result=await present(1200);assert.equal(result.shown,true);assert.deepEqual(result.refs,['new1','old2','old3']);
 assert.deepEqual(JSON.parse(JSON.stringify(result.proposal.rotation.replace)),{old1:'new1'});
 assert.equal(result.proposal.result.rotation.count,1);assert.deepEqual(Array.from(result.state.rendered.items,v=>v.detail_ref),result.refs);
 assert.ok(result.originals.every(tile=>!tile.classList.contains('ambient-out')),'cancelled outgoing posters return to visibility');
 result=await present(300);assert.equal(result.shown,false);assert.deepEqual(result.refs,['old1','old2','old3']);
 result=await present();assert.equal(result.shown,true);assert.equal(result.proposal.result.rotation.count,3);assert.deepEqual(result.refs,['new1','new2','new3']);
 console.log('PASS: interrupted stagger acknowledges only displayed replacements, restores untouched posters and preserves ordered wall state');
})().catch(error=>{console.error(error);process.exitCode=1;});
