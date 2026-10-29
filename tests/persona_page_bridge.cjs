const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const source=fs.readFileSync(process.argv[2],'utf8');
class Element{
 constructor(){this.children=[];this.textContent='';this.value='';this.disabled=false;this.className='';}
 append(...items){this.children.push(...items);}
 replaceChildren(...items){this.children=items;}
}
const nodes=new Map();const el=id=>{if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);};
const state={revision:2,source_fingerprint:'source',draft:'服务端草稿',active:'version-1',versions:[],sources:[],jobs:[]};
let readFail=false,saveFail=false,timer;const events=[];
const bridge={ready:async()=>{},apiGet:async(name,params)=>{
 if(readFail)throw Error('Network Error');
 return {data:params?state:{people:[{persona_id:'龙娘'}],selected_persona_id:'龙娘'}};
},apiPost:async()=>{if(saveFail)throw Error('来源冲突，请先检查差异');return {data:state};}};
vm.runInNewContext(source,{document:{getElementById:el,createElement:()=>new Element(),hidden:false,addEventListener(){}},
 window:{AstrBotPluginView:bridge,addEventListener(){},dispatchEvent:e=>events.push(e)},
 CustomEvent:class{constructor(name,options){this.type=name;this.detail=options.detail;}},
 crypto:{randomUUID:()=> 'test-request'},setTimeout:f=>{timer=f;return 1;},clearTimeout(){},console});
const settle=()=>new Promise(r=>setImmediate(r));
(async()=>{
 await settle();await settle();assert.equal(el('master').value,'服务端草稿');
 el('master').value='未保存本地修改';el('master').oninput();
 readFail=true;await timer();assert.equal(el('notice').textContent,'Network Error');
 readFail=false;await el('refresh').onclick();
 assert.equal(el('notice').textContent,'已重新连接，草稿保持不变');assert.equal(el('notice').className,'');
 assert.equal(el('master').value,'未保存本地修改');assert.match(el('dirty').textContent,/未保存/);
 saveFail=true;await el('save').onclick();assert.match(el('notice').textContent,/来源冲突/);
 await timer();assert.match(el('notice').textContent,/来源冲突/);assert.equal(el('notice').className,'error');
 assert.equal(el('master').value,'未保存本地修改');assert.equal(events.at(-1).detail,true);
 console.log('Persona reconnect clears only read errors; draft and write conflicts retained');
})().catch(e=>{console.error(e);process.exitCode=1;});
