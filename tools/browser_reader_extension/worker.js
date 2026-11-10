'use strict';
importScripts('local-config.js');
let busy=false,timer=null;
const key='reader-job';
function identity(raw){try{const u=new URL(raw);if(u.protocol!=='https:'||u.username||u.password||u.port&&u.port!=='443')return null;const supported=(u.hostname==='tieba.baidu.com'&&/^\/p\/\d+\/?$/.test(u.pathname))||(u.hostname==='www.zhihu.com'&&/^\/question\/\d+(\/answer\/\d+)?\/?$/.test(u.pathname))||(u.hostname==='zhuanlan.zhihu.com'&&/^\/p\/\d+\/?$/.test(u.pathname));return supported?u.origin+u.pathname.replace(/\/$/,''):null;}catch{return null;}}
async function api(path,body){const r=await fetch(READER_CONFIG.endpoint+path,{method:body?'POST':'GET',headers:{Authorization:'Bearer '+READER_CONFIG.token,...(body?{'Content-Type':'application/json'}:{})},...(body?{body:JSON.stringify(body)}:{}),signal:AbortSignal.timeout(12000)});if(!r.ok)throw Error('本机连接返回 '+r.status);return r.json();}
async function status(text){await chrome.storage.local.set({'reader-status':{text,at:Date.now()}});}
async function current(){return (await chrome.storage.session.get(key))[key];}
async function closeOwned(job){if(!job)return;try{const tab=await chrome.tabs.get(job.tab);if(identity(tab.url||tab.pendingUrl)===identity(job.url))await chrome.tabs.remove(job.tab);}catch{}}
async function finish(job,payload){try{await api('/reading/result',{id:job.id,...payload});await status(payload.error?'未取得正文：'+payload.error:'已提交原帖正文');}catch(e){await status(e.message);}finally{await chrome.storage.session.remove(key);await closeOwned(job);}}
async function tick(){if(busy||!READER_CONFIG.token)return;clearTimeout(timer);busy=true;let delay=5000;try{const job=await current();if(job){delay=1000;const state=await api('/reading/task?active='+encodeURIComponent(job.id));if(!state.active){await chrome.storage.session.remove(key);await closeOwned(job);await status('任务已取消，等待新的空闲阅读');return;}if(Date.now()>job.until*1000)await finish(job,{error:'网页未在期限内提供正文'});else try{const response=await chrome.tabs.sendMessage(job.tab,{kind:'read-article',url:job.url});if(response?.html&&identity(response.url)===identity(job.url))await finish(job,response);else if(response?.blocked)await finish(job,{error:response.blocked});}catch{}return;}const work=await api('/reading/task');if(work.id&&identity(work.url)){const tab=await chrome.tabs.create({url:work.url,active:false});await chrome.storage.session.set({[key]:{...work,tab:tab.id}});await status('正在读取原帖');delay=1000;}else await status('已连接，等待后台阅读任务');}catch(e){await status(e.message);}finally{busy=false;timer=setTimeout(tick,delay);}}
chrome.runtime.onInstalled.addListener(()=>{chrome.alarms.create('reader-poll',{periodInMinutes:.5});tick();});
chrome.runtime.onStartup.addListener(()=>tick());
chrome.alarms.onAlarm.addListener(alarm=>{if(alarm.name==='reader-poll')tick();});
chrome.runtime.onMessage.addListener((message,sender,reply)=>{if(message?.kind==='reader-status'){current().then(job=>reply({connected:!!READER_CONFIG.token,busy:!!job}));return true;}});
tick();
