// Exercise the actual refresh handler with delayed and failed responses.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const source=fs.readFileSync(path.resolve(__dirname,'../AstrBot/data/plugins/astrbot_plugin_proactive_agent/pages/control/app.js'),'utf8');
const code=source.slice(source.indexOf('  let healthRefreshPromise=null;'),source.indexOf('  async function poll()'));
const elements=new Map();const $=id=>{if(!elements.has(id))elements.set(id,{disabled:false,textContent:'',attributes:{},setAttribute(k,v){this.attributes[k]=v},removeAttribute(k){delete this.attributes[k]}});return elements.get(id)};
let resolve,reject,calls=0;const api=()=>{calls++;return new Promise((a,b)=>{resolve=a;reject=b})};
const refresh=new Function('$','api','renderHealth','notice',code+';return refreshHealth;')($,api,()=>false,()=>{});
(async()=>{
 const first=refresh(),second=refresh();assert.equal(calls,1);assert.equal($('refresh-health').disabled,true);
 assert.match($('refresh-health').textContent,/正在刷新/);assert.match($('job-status').textContent,/已收到/);
 resolve({});await Promise.all([first,second]);assert.equal($('refresh-health').disabled,false);assert.match($('refresh-health').textContent,/已刷新/);
 const failed=refresh();reject(new Error('synthetic network failure'));await failed;
 assert.equal(calls,2);assert.equal($('refresh-health').disabled,false);assert.match($('refresh-health').textContent,/刷新失败/);
 console.log('Health refresh: immediate feedback, coalescing, success, failure and retry availability passed');
})().catch(e=>{console.error(e);process.exitCode=1});
