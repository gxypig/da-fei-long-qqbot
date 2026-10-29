(()=>{'use strict';
 const bridge=window.AstrBotPluginView||window.AstrBotPluginPage,allow={persona:['persona_state','persona_action'],sessions:['mute_state','mute_action']};
 let active=null,bodyOverflow='';
 const dialog=document.createElement('dialog');dialog.style.cssText='width:calc(100% - 16px);max-width:1600px;height:calc(100% - 16px);max-height:none;padding:0;border:0;border-radius:12px;overflow:hidden';
 const bar=document.createElement('div');bar.style.cssText='display:flex;align-items:center;gap:16px;padding:8px 16px;background:#eef2fa';
 const back=document.createElement('button');back.textContent='返回控制面板';back.type='button';const title=document.createElement('strong');bar.append(back,title);
 const frame=document.createElement('iframe');frame.setAttribute('sandbox','allow-scripts allow-forms allow-downloads');frame.style.cssText='width:100%;height:calc(100% - 55px);border:0';dialog.append(bar,frame);document.body.append(dialog);
 function close(){if(!active)return;if(active.dirty&&back.dataset.discard!=='yes'){back.dataset.discard='yes';back.textContent='有未保存草稿，再点一次放弃并返回';return;}active=null;back.dataset.discard='';back.textContent='返回控制面板';window.QQBotSubviewActive=false;document.body.style.overflow=bodyOverflow;dialog.close();frame.srcdoc='';document.dispatchEvent(new Event('visibilitychange'));}
 back.onclick=close;dialog.addEventListener('cancel',e=>{e.preventDefault();close();});
 window.addEventListener('message',async e=>{const m=e.data;if(e.source!==frame.contentWindow||!active||m?.channel!==active.channel)return;
   if(m.kind==='dirty'){active.dirty=!!m.value;back.dataset.discard='';back.textContent='返回控制面板';return;}
   if(m.kind!=='request'||!allow[active.view].includes(m.endpoint)||!['get','post'].includes(m.method))return;
   const target=e.source,channel=active.channel;try{const data=await(m.method==='get'?bridge.apiGet(m.endpoint,m.payload):bridge.apiPost(m.endpoint,m.payload));target.postMessage({channel,id:m.id,data},'*');}
   catch(err){target.postMessage({channel,id:m.id,error:err.message},'*');}
 });
 async function open(view){if(!allow[view]||active)return;const result=await bridge.apiGet('panel_subview',{view});if(result.status==='error')throw new Error(result.message);const data=result.data??result,channel=crypto.randomUUID();
   active={view,channel,dirty:false};bodyOverflow=document.body.style.overflow;document.body.style.overflow='hidden';window.QQBotSubviewActive=true;title.textContent=view==='persona'?'人格总设定':'会话管理';
   const bootstrap=`<script>(()=>{const channel=${JSON.stringify(channel)},pending=new Map();let id=0;function req(method,endpoint,payload){return new Promise((resolve,reject)=>{const n=++id;pending.set(n,{resolve,reject});parent.postMessage({channel,kind:'request',id:n,method,endpoint,payload},'*');});}window.addEventListener('message',e=>{if(e.source!==parent||e.data?.channel!==channel)return;const p=pending.get(e.data.id);if(!p)return;pending.delete(e.data.id);e.data.error?p.reject(new Error(e.data.error)):p.resolve(e.data.data);});window.AstrBotPluginView=window.AstrBotPluginPage={ready:async()=>({}),apiGet:(name,params)=>req('get',name,params),apiPost:(name,body)=>req('post',name,body)};window.addEventListener('qqbot-draft-dirty',e=>parent.postMessage({channel,kind:'dirty',value:e.detail},'*'));})();<\/script>`;
   frame.srcdoc=data.html.replace('<head>','<head>'+bootstrap);dialog.showModal();
 }
 document.querySelectorAll('[data-subview]').forEach(el=>el.onclick=()=>open(el.dataset.subview).catch(e=>{const n=document.getElementById('notice');n.textContent=e.message;n.className='error';}));
})();
