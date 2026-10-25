import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {mediaTypeLabel} from '../web/core.mjs';

// 直接执行页面中的详情流程，模拟关闭弹窗时仍未返回的库存检查。
class Element {
  children=[]; handlers={}; dataset={}; open=false;
  append(...nodes){this.children.push(...nodes);}
  prepend(...nodes){this.children.unshift(...nodes);}
  replaceChildren(...nodes){this.children=nodes;}
  addEventListener(name,fn){this.handlers[name]=fn;}
  querySelector(selector){return this.children.find(node=>node.className===selector.slice(1));}
  showModal(){this.open=true;}
  close(){this.open=false;}
  after(){}
}
const source=readFileSync(new URL('../web/app.mjs',import.meta.url),'utf8');
async function scenario(cancel){
  const elements=new Map(),$=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
  const row={title:'测试电影',title_ref:'tmdb:movie:12',media_type:'movie'};
  const details={title:{...row,library_status:{file_count:0}}};
  let resolveInventory,writes=0,reads=0;
  const context={document:{getElementById:$,createElement:()=>new Element()},mediaTypeLabel,
    nativePath:()=>'/movies/12',fetch:async(url,options)=>{
      if(options.method==='POST'){writes++;return {ok:true,json:async()=>({data:{subscription:{id:12}}})};}
      reads++;const data=reads===1?details:await new Promise(resolve=>{resolveInventory=()=>resolve(details);});
      return {ok:true,json:async()=>({data})};
    }};
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('const $'),source.indexOf('const query=new URLSearchParams'))+'\nglobalThis.openDetail=showDetail;',context);
  await context.openDetail(row);
  const wide=$('detail-content').children[0].children[2],actions=wide.children[2];
  const pending=actions.children[0].handlers.click();
  assert.equal(reads,2);
  if(cancel==='button')$('close').onclick();
  if(cancel==='escape')$('detail').handlers.cancel();
  resolveInventory();await pending;
  assert.equal(writes,cancel?0:1);
}
await scenario('button');await scenario('escape');await scenario(null);
console.log('PASS: closing details before inventory returns cancels subscription; active confirmation submits once');
