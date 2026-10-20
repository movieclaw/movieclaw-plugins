"use strict";
globalThis.MovieClawAmbientSettings={
 create({request,initialize,getAccount,onChange}){
  const $=id=>document.getElementById(id),defaults=globalThis.MovieClawAmbient.defaults;
  const fields={fade_out:'fade-out',fade_in:'fade-in',gap:'gap',overlap:'overlap',variation:'variation'};
  let record=null,owner='',formOwner='',busy=false,readEpoch=0;
  const message=text=>{$('ambient-settings-status').textContent=text;};
  function render(settings){for(const [key,id] of Object.entries(fields)){$('ambient-'+id).value=String(settings[key]);label(key,id);}}
  function label(key,id){const value=Number($('ambient-'+id).value);$('ambient-'+id+'-value').textContent=['overlap','variation'].includes(key)?value+'%':(value/1000).toFixed(1)+' 秒';}
  function lock(value){$('ambient-save').disabled=value;$('ambient-reset').disabled=value;for(const id of Object.values(fields))$('ambient-'+id).disabled=value;}
  async function refresh(){
   const epoch=++readEpoch;await initialize();const account=getAccount();
   // Never continue another account's rhythm while its settings are being read.
   if(owner!==account){record=null;onChange({...defaults});}
   const value=await request('ambient');if(epoch!==readEpoch||account!==getAccount())return null;
   if(!value?.settings||typeof value.revision!=='string')throw new Error('屏保节奏暂时无法读取，请重试。');
   record=value;owner=account;onChange({...value.settings});return value;
  }
  async function open(){
   if(busy)return;formOwner='';const dialog=$('ambient-dialog');dialog.showModal();lock(true);message('正在读取账号设置…');
   try{const value=await refresh();if(!value||!dialog.open)return;formOwner=owner;render(value.settings);lock(false);message('已读取账号设置 · 保存后生效');}
   catch(error){if(dialog.open)message(error.message||'读取失败，请关闭后重试。');}
  }
  for(const [key,id] of Object.entries(fields))$('ambient-'+id).addEventListener('input',()=>{label(key,id);message('尚未保存 · 保存后生效');});
  $('ambient-reset').addEventListener('click',()=>{render(defaults);message('已恢复推荐默认值，点击保存后生效。');});
  $('ambient-close').addEventListener('click',()=>{if(!busy)$('ambient-dialog').close();});
  $('ambient-dialog').addEventListener('cancel',event=>{if(busy)event.preventDefault();});
  $('ambient-form').addEventListener('submit',async event=>{
   event.preventDefault();if(busy||!record||!formOwner)return;busy=true;lock(true);message('正在保存…');
   try{
    await initialize();if(formOwner!==getAccount())throw new Error('登录账号已切换，请关闭后重新打开。');
    const settings=Object.fromEntries(Object.entries(fields).map(([key,id])=>[key,Number($('ambient-'+id).value)]));
    const value=await request('ambient',{method:'POST',body:JSON.stringify({selection:{settings,revision:record.revision}})});
    if(formOwner!==getAccount())throw new Error('登录账号已切换，请关闭后重新打开。');
    record=value;onChange({...value.settings});message('已保存 · 此账号在其他设备打开全屏时会载入');
   }catch(error){message(error.message||'保存失败，请重试。');}
   finally{busy=false;lock(false);}
  });
  for(const id of ['wall-ambient-settings','immersive-ambient-settings'])$(id).addEventListener('click',open);
  return {refresh,open};
 }
};
