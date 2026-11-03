"use strict";
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor() { this.value=''; this.checked=false; this.hidden=false; this.disabled=false; this.textContent=''; this.className=''; this.open=false; this.events={}; this.children=[]; }
  addEventListener(k,fn){this.events[k]=fn;}
  appendChild(child){this.children.push(child);}
  removeAttribute(key){delete this[key];}
}
const elements = new Map();
const $ = id => { if(!elements.has(id)) elements.set(id,new Element()); return elements.get(id); };
const doc = {documentElement:{lang:'zh-Hans'},getElementById:$,createElement:()=>new Element()};
const initial={app_id:'123',login_app:'鸿蒙端',offline_enabled:true,offline_retry:false,wecom_entry:true,dry_run:false};
const schema={properties:{login_app:{enum:['鸿蒙端','Apple TV 端']}}};
const calls=[], timers=[], events=[], confirms=[];
let allowed=true, state='pending', values={...initial};
const context=vm.createContext({document:doc,console,Event:class {constructor(type){this.type=type;}},
  window:{dispatchEvent(e){events.push(e.type);}}, confirm(text){confirms.push(text);return allowed;},
  setTimeout(fn){timers.push(fn);},
  fetch:async(url,opt={})=>{
    const body=opt.body?JSON.parse(opt.body):null;
    calls.push({url,method:opt.method||'GET',body});
    let data={};
    if(url.includes('/settings')) {
      if(opt.method==='PUT') Object.assign(values,body.values);
      data={editable:true,schema,values:{...values},secrets_set:[]};
    } else if(url==='/api/v1/plugins/p115-import/status') {
      data={web_logged_in:true,account:'测试账号',open_api_authorized:true,open_api_app_id:values.app_id};
    } else if(url.endsWith('/auth/web/start') || url.endsWith('/auth/open/start')) {
      data={id:'abc',qrcode_image:'data:image/png;base64,YWJj',scan_url:'https://115.com/qr'};
    } else if(url.includes('/auth/') && url.includes('/status?')) data={status:state,message:state==='done'?'成功':undefined};
    return {ok:true,json:async()=>({success:true,data})};
  }
});
async function flush(){for(let i=0;i<5;i++)await new Promise(r=>setImmediate(r));}
async function main(){
 vm.runInContext(fs.readFileSync(__dirname+'/logic.js','utf8'),context);
 vm.runInContext(fs.readFileSync(__dirname+'/settings.js','utf8'),context);
 $('settings-panel').open=true; $('settings-panel').events.toggle(); await flush();
 assert.equal($('login-web-badge').textContent,'已连接');
 assert.equal($('login-open-badge').textContent,'已授权');
 assert.equal($('login-web-start').textContent,'重新扫码');
 assert.equal($('settings-save').disabled,true);
 assert.equal($('settings-reset').hidden,true);
 assert.equal($('setting-app_id').value,'123');
 await $('settings-form').onsubmit({preventDefault(){}});
 assert(!calls.some(c=>c.method==='PUT'),'没有修改不保存');
 $('setting-offline_retry').checked=true; $('settings-form').events.change();
 assert.equal($('settings-save').disabled,false);
 assert.equal($('settings-reset').hidden,false);
 $('settings-reset').onclick();
 assert.equal($('setting-offline_retry').checked,false,'放弃修改恢复原配置');
 assert.equal($('settings-save').disabled,true);
 $('setting-offline_retry').checked=true; $('settings-form').events.change(); allowed=false;
 await $('settings-form').onsubmit({preventDefault(){}});
 assert(!calls.some(c=>c.method==='PUT'),'取消确认不得保存');
 allowed=true;await $('settings-form').onsubmit({preventDefault(){}});
 const put=calls.find(c=>c.method==='PUT');
 assert.deepEqual(put.body,{values:{offline_retry:true}},'只更新修改字段，不需要旧的企微地址和接收人');
 assert(confirms[0].includes('立即生效'));
 assert.equal($('settings-save').disabled,true);
 await $('login-web-start').onclick();
 assert.equal($('login-web-qr').hidden,false);
 assert.equal(calls.find(c=>c.url.endsWith('/auth/web/start')).body.app,'鸿蒙端');
 state='scanned';await timers.shift()();assert($('login-web-status').textContent.includes('手机'));
 state='done';await timers.shift()();assert.equal($('login-web-qr').hidden,true);assert(events.includes('p115-auth-changed'));
 $('setting-app_id').value='456'; $('settings-form').events.input();
 assert.equal($('login-open-badge').textContent,'待保存');
 await $('login-open-start').onclick();
 assert($('login-open-status').textContent.includes('保存 AppID'));
 assert(!calls.some(c=>c.url.endsWith('/auth/open/start')),'未保存的 AppID 不触发授权');
 $('setting-app_id').value='123'; $('settings-form').events.input();
 assert.equal($('login-open-badge').textContent,'已授权');
 await $('login-open-start').onclick();
 assert(calls.some(c=>c.url.endsWith('/auth/open/start')));assert.equal($('login-open-qr').hidden,false);
 state='expired';await timers.shift()();assert.equal($('login-open-start').disabled,false);
 state='pending';await $('login-open-start').onclick();
 const beforeClose=calls.length; $('login-open-close').onclick();
 assert.equal($('login-open-result').hidden,true);
 assert.equal($('login-open-start').disabled,false);
 await timers.shift()();assert.equal(calls.length,beforeClose,'关闭二维码后不再查询旧扫码会话');
 const html=fs.readFileSync(__dirname+'/index.html','utf8');
 assert(!html.includes('setting-wecom_user') && !html.includes('setting-wecom_push_url'));
 assert(html.includes('已绑定的系统通知渠道'));
 console.log('设置、扫码与统一通知入口测试通过');
}
main().catch(err=>{console.error(err);process.exitCode=1;});
