"use strict";
// Idle work never owns navigation or account state. The host controller validates
// identity/filter versions before preparing, displaying and confirming a patch.
globalThis.MovieClawAmbient={
 defaults:Object.freeze({fade_out:2000,fade_in:2600,gap:800,variation:25,overlap:60}),
 timing(settings={},random=Math.random){
  const defaults=this.defaults,limits={fade_out:[600,6000],fade_in:[600,6000],gap:[200,5000],variation:[0,45],overlap:[0,80]};
  const config=Object.fromEntries(Object.entries(limits).map(([key,[min,max]])=>[key,Number.isFinite(settings[key])?Math.max(min,Math.min(max,settings[key])):defaults[key]]));
  const vary=value=>Math.round(value*(1+(random()*2-1)*config.variation/100));
  return {out:vary(config.fade_out),incoming:vary(config.fade_in),gap:vary(config.gap),overlap:config.overlap/100};
 },
 // One ordered clock keeps cancellation reliable while individual posters overlap.
 async transition({rows,valid,wait,out,swap,restore,reduced=false,random=Math.random,settings=this.defaults}){
  let start=0,elapsed=0;const events=[],shown=[];
  rows.forEach((row,i)=>{const timing=this.timing(settings,random);if(i)start+=reduced?0:timing.gap;events.push({row,timing,at:start,swap:false},{row,timing,at:start+(reduced?0:Math.round(timing.out*(1-timing.overlap))),swap:true});});
  events.sort((a,b)=>a.at-b.at||Number(a.swap)-Number(b.swap));
  try{
   for(const event of events){if(!await wait(event.at-elapsed,valid))break;elapsed=event.at;if(event.swap){swap(event.row,event.timing);shown.push(event.row);}else out(event.row,event.timing);}
   return shown;
  }finally{rows.forEach(restore);}
 },
 create({active,prepare,apply,confirm,recover=()=>{},status=()=>{},now=()=>Date.now(),setTimer=setTimeout,clearTimer=clearTimeout,idle=5000,interval=1000,random=Math.random}){
  let last=now(),timer=null,epoch=0,work=null,waiter=null,closed=false,committing=false;
  const eligible=()=>!closed&&active();
  function clear(){if(timer!==null)clearTimer(timer);timer=null;}
  function arm(delay=idle){clear();if(eligible())timer=setTimer(()=>{timer=null;run();},Math.max(1,delay));}
  function interrupt(){epoch++;if(waiter){clearTimer(waiter.timer);waiter.resolve(false);waiter=null;}}
  function activity(){last=now();interrupt();status(false);arm();}
  function wait(ms,valid){if(!valid())return Promise.resolve(false);return new Promise(resolve=>{const row={resolve,timer:setTimer(()=>{if(waiter===row)waiter=null;resolve(valid());},ms)};waiter=row;});}
  async function cycle(){
   if(!eligible())return;
   const delay=idle-(now()-last);if(delay>0){arm(delay);return;}
   const version=epoch,valid=()=>version===epoch&&eligible()&&now()-last>=idle;
   let proposal,shown=false;
   try{
    proposal=await prepare(1+Math.floor(random()*3),valid);if(!proposal||!valid())return;
    status(true);shown=await apply(proposal,valid,wait);if(!shown)return;
    // This confirmation follows an actual DOM presentation. It must finish even
    // when the user resumes interacting; further rotations stop immediately.
    committing=true;await confirm(proposal);
   }catch(error){await recover(error,proposal,shown);last=now()+25000;status(false);}
   finally{committing=false;}
  }
  function run(){if(work||!eligible())return;work=cycle().finally(()=>{work=null;if(eligible())arm(Math.max(100,idle-(now()-last),typeof interval==='function'?interval():interval));});return work;}
  function wake(){if(eligible())arm(Math.max(1,idle-(now()-last)));else{clear();interrupt();status(false);}}
  function stop(){clear();interrupt();status(false);}
  function close(){closed=true;stop();}
  return {activity,wake,stop,close,flush:()=>work||Promise.resolve(),get committing(){return committing;}};
 }
};
