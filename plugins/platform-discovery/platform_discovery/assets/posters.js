"use strict";
// Only public image URLs are cached here. Account preferences and recommendation
// batches never enter this loader, so preloading cannot consume unseen walls.
globalThis.MovieClawPosters={
  sized(value,width,dpr=1){
    try{const url=new URL(value);if(url.protocol!=="https:"||url.username||url.password)return null;
      if(url.hostname==="image.tmdb.org"&&/^\/t\/p\/(?:w\d+|original)\/[A-Za-z0-9._-]+$/.test(url.pathname)){
        const pixels=Math.max(1,Number(width)||170)*Math.max(1,Math.min(2,Number(dpr)||1));
        const size=[185,342,500,780].find(n=>n>=pixels)||780;
        url.pathname=url.pathname.replace(/\/t\/p\/(?:w\d+|original)\//,"/t/p/w"+size+"/");
      }
      return url.href;
    }catch{return null;}
  },
  create({urlFor,ImageClass=globalThis.Image,connection=()=>globalThis.navigator?.connection,hidden=()=>globalThis.document?.hidden,dpr=()=>globalThis.devicePixelRatio||1,timeout=12000}={}){
    const queue=[],jobs=new Map(),ready=new Set(),wallTasks=new Set(),wallCancels=new Set();let active=0,epoch=0,paused=false,enabled=true,nextEpoch=0;
    const constrained=()=>{const c=connection();return c?.saveData||["slow-2g","2g"].includes(c?.effectiveType);};
    function finish(job,ok){
      if(job.done)return;job.done=true;clearTimeout(job.timer);job.image.onload=job.image.onerror=null;
      if(job.started)active--;else{const i=queue.indexOf(job);if(i>=0)queue.splice(i,1);}
      if(!ok)job.image.src="";
      if(jobs.get(job.url)===job)jobs.delete(job.url);
      if(ok){ready.delete(job.url);ready.add(job.url);while(ready.size>384)ready.delete(ready.values().next().value);}
      job.resolve(ok);pump();
    }
    function pump(){
      if(paused)return;
      queue.sort((a,b)=>a.priority-b.priority);
      while(active<(constrained()?1:3)&&queue.length){
        const job=queue.shift();if(job.done)continue;
        if(job.priority===2&&(!enabled||hidden()||constrained())){finish(job,false);continue;}
        job.started=true;active++;job.image.loading="eager";job.image.decoding="async";job.image.fetchPriority=job.priority===0?"high":job.priority===2?"low":"auto";job.image.referrerPolicy="no-referrer";
        job.image.onload=()=>finish(job,true);job.image.onerror=()=>finish(job,false);
        job.timer=setTimeout(()=>finish(job,false),timeout);job.image.src=job.url;
      }
    }
    function acquire(value,priority,group){
      const url=urlFor(value);if(ready.has(url))return Promise.resolve(true);
      let job=jobs.get(url);
      if(job){job.groups.add(group);job.priority=Math.min(job.priority,priority);if(job.started&&priority===0)job.image.fetchPriority="high";pump();return job.promise;}
      job={url,priority,groups:new Set([group]),image:new ImageClass(),started:false,done:false};
      job.promise=new Promise(resolve=>{job.resolve=resolve;});jobs.set(url,job);queue.push(job);pump();return job.promise;
    }
    function discard(group){
      paused=true;
      for(const job of [...jobs.values()]){job.groups.delete(group);if(!job.groups.size)finish(job,false);}
      paused=false;pump();
    }
    function beginWall(){epoch++;for(const cancel of [...wallCancels])cancel();wallTasks.clear();}
    function attach(img,values,{width=170,priority=1}={}){
      const generation=epoch;
      // Load the displayed element itself. A slow image must never hold the
      // entire wall, or be deleted because an unrelated image missed a deadline.
      const candidates=[...new Set(values.flatMap(value=>[globalThis.MovieClawPosters.sized(value,width,dpr()),value]).filter(Boolean))];
      candidates.sort((a,b)=>Number(ready.has(urlFor(b)))-Number(ready.has(urlFor(a))));
      img.loading="eager";img.fetchPriority=priority===0?"high":"auto";
      let index=0,round=0,timer,done=false,resolve;
      const task=new Promise(r=>resolve=r);wallTasks.add(task);
      const finish=()=>{if(done)return;done=true;clearTimeout(timer);img.onload=img.onerror=null;wallCancels.delete(cancel);wallTasks.delete(task);resolve();};
      const cancel=()=>{finish();};wallCancels.add(cancel);
      const next=()=>{
        if(done||generation!==epoch){finish();return;}
        if(index>=candidates.length){
          if(round++===0){index=0;timer=setTimeout(next,1000);return;}
          img.hidden=true;const placeholder=img.parentElement?.querySelector(".poster-placeholder");if(placeholder)placeholder.hidden=false;finish();return;
        }
        img.hidden=false;img.src=urlFor(candidates[index++]);
      };
      img.onload=()=>{const placeholder=img.parentElement?.querySelector(".poster-placeholder");if(placeholder)placeholder.hidden=true;finish();};
      img.onerror=next;next();return img;
    }
    async function warmWall(items,width){
      if(!enabled||hidden()||constrained())return;
      const group="warm-wall";discard(group);
      await Promise.all(items.slice(0,6).map(item=>{const url=globalThis.MovieClawPosters.sized(item.poster_url,width,dpr());return url?acquire(url,0,group):false;}));
    }
    // Only background work uses a soft wait. Expiry neither cancels foreground
    // requests nor removes images; switching walls never waits on this promise.
    async function settled(wait=1500){const generation=epoch;let timer;await Promise.race([Promise.all([...wallTasks]),new Promise(resolve=>{timer=setTimeout(resolve,wait);})]);clearTimeout(timer);return generation===epoch;}
    const canPrepare=()=>enabled&&!hidden()&&!constrained();
    async function warmNext(items,width){
      if(!canPrepare())return false;
      const group="next-wall";cancelNext();const generation=nextEpoch;
      const results=await Promise.all(items.slice(0,288).map(async item=>{
        const candidates=[...new Set([item.poster_url,...(item.poster_fallback_urls||[])].flatMap(value=>[globalThis.MovieClawPosters.sized(value,width,dpr()),value]).filter(Boolean))];
        for(const url of candidates){if(generation!==nextEpoch||!canPrepare())return false;if(await acquire(url,2,group))return generation===nextEpoch;}
        return false;
      }));
      return results.length>0&&results.some(Boolean)&&generation===nextEpoch&&canPrepare();
    }
    let ambientEpoch=0;
    function readyForSwap(items,width){
      if(!canPrepare())return [];
      return items.slice(0,96).filter(item=>[item.poster_url,...(item.poster_fallback_urls||[])].some(value=>[globalThis.MovieClawPosters.sized(value,width,dpr()),value].filter(Boolean).some(url=>ready.has(urlFor(url)))));
    }
    async function prepareSwap(items,width){
      const generation=ambientEpoch;
      const results=await Promise.all(items.slice(0,3).map(async item=>{
        const candidates=[...new Set([item.poster_url,...(item.poster_fallback_urls||[])].flatMap(value=>[globalThis.MovieClawPosters.sized(value,width,dpr()),value]).filter(Boolean))];
        for(const url of candidates){if(generation!==ambientEpoch||!canPrepare())return null;if(await acquire(url,1,"ambient"))return generation===ambientEpoch?item:null;}
        return null;
      }));return results.filter(Boolean);
    }
    function cancelAmbient(){ambientEpoch++;discard("ambient");}
    function cancelNext(){nextEpoch++;discard("next-wall");}
    let hoverTimer=null;
    function hover(value){
      clearTimeout(hoverTimer);
      if(!enabled||hidden()||constrained())return;
      hoverTimer=setTimeout(()=>{discard("detail");if(enabled&&!hidden()&&!constrained()&&value?.startsWith("https://"))acquire(value,2,"detail");},250);
    }
    function cancelHover(){clearTimeout(hoverTimer);discard("detail");}
    function setEnabled(value){enabled=!!value;if(!enabled){cancelAmbient();cancelHover();discard("warm-wall");cancelNext();}}
    function close(){cancelAmbient();beginWall();nextEpoch++;epoch++;clearTimeout(hoverTimer);paused=true;for(const job of [...jobs.values()])finish(job,false);paused=false;}
    return {readyForSwap,prepareSwap,cancelAmbient,attach,beginWall,warmWall,settled,canPrepare,warmNext,cancelNext,hover,cancelHover,setEnabled,close};
  }
};
