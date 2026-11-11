const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(process.argv[2],'utf8'),timers=new Map();let id=0;
const ctx={window:{setTimeout:f=>{timers.set(++id,f);return id;},clearTimeout:i=>timers.delete(i)},TRANSPARENT_IMAGE_SRC:'blank',Promise};
vm.createContext(ctx);vm.runInContext(source.slice(source.indexOf('function setThumbnailSource('),source.indexOf('function enqueueThumbnailLoad(')),ctx);
function image(){const classes=new Set(['is-loading']);return {src:'',isConnected:true,classList:{remove:k=>classes.delete(k),toggle:(k,v)=>v?classes.add(k):classes.delete(k)},classes};}
(async()=>{
 let calls=[],fallback=0;
 Object.assign(ctx,{URLSearchParams,ensureBridgeReady:async()=>({apiGet:async(path,params)=>{calls.push({path,params});if(path.includes('?'))throw Error('invalid bridge endpoint');return {data:{ok:true}};}}),unwrapApiResponse:r=>r.data,withTimeout:p=>p,directPluginApi:async()=>{fallback++;return {};}});
 vm.runInContext(source.slice(source.indexOf('async function pluginApiGet('),source.indexOf('async function pluginApiPost(')),ctx);
 assert.equal((await ctx.pluginApiGet('caption_library?compact=1&since=a%20b')).ok,true);
 assert.equal(calls[0].path,'caption_library');assert.equal(calls[0].params.since,'a b');assert.equal(calls[0].params.compact,'1');assert.equal(fallback,0);
 ctx.ensureBridgeReady=async()=>({apiGet:async()=>{throw Error('authentication failed');}});
 await assert.rejects(ctx.pluginApiGet('caption_library'),/authentication failed/);assert.equal(fallback,0);
 const img=image(),pending=ctx.setThumbnailSource(img,'hung',()=>new Promise(()=>{}));
 img.onerror();for(const fn of [...timers.values()])fn();assert.equal(await pending,false);
 assert.equal(timers.size,0);assert.equal(img.classes.has('is-loading'),false);assert.equal(img.src,'blank');
 const good=image(),done=ctx.setThumbnailSource(good,'works');good.onload();assert.equal(await done,true);assert.equal(timers.size,0);
 const removed=image(),cancelled=ctx.setThumbnailSource(removed,'late');removed.__smartThumbnailCancel();assert.equal(await cancelled,false);
 console.log('gallery: hung image/fallback releases, successful loads preserved, removed image cancelled');
})().catch(e=>{console.error(e);process.exitCode=1;});
