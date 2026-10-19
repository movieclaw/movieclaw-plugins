"use strict";
// Idle work never owns navigation or account state. The host controller validates
// identity/filter versions before preparing, displaying and confirming a patch.
globalThis.MovieClawAmbient={
 create({active,prepare,apply,confirm,recover=()=>{},status=()=>{},now=()=>Date.now(),setTimer=setTimeout,clearTimer=clearTimeout,idle=5000,interval=7000}){
  let last=now(),timer=null,epoch=0,work=null,waiter=null,sequence=0,closed=false,committing=false;
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
    proposal=await prepare(1+sequence%3,valid);if(!proposal||!valid())return;
    status(true);shown=await apply(proposal,valid,wait);if(!shown)return;
    // This confirmation follows an actual DOM presentation. It must finish even
    // when the user resumes interacting; further rotations stop immediately.
    committing=true;await confirm(proposal);sequence++;
   }catch(error){await recover(error,proposal,shown);last=now()+25000;status(false);}
   finally{committing=false;}
  }
  function run(){if(work||!eligible())return;work=cycle().finally(()=>{work=null;if(eligible())arm(Math.max(1000,idle-(now()-last),interval));});return work;}
  function wake(){if(eligible())arm(Math.max(1,idle-(now()-last)));else{clear();interrupt();status(false);}}
  function stop(){clear();interrupt();status(false);}
  function close(){closed=true;stop();}
  return {activity,wake,stop,close,flush:()=>work||Promise.resolve(),get committing(){return committing;}};
 }
};
