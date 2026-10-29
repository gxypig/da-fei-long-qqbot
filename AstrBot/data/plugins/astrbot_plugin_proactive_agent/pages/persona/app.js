(()=>{'use strict';
const $=id=>document.getElementById(id),drafts=new Map();let bridge,pid='',data=null,serial=0,busy=false,timer,readFailure=false;
const actions={merge:'整理来源',publish:'保存并同步',rollback:'回退'},states={queued:'等待整理',running:'正在整理',prepared:'已校验',native_updated:'已保存原生人格',completed:'完成',failed:'未完成',conflict:'来源冲突',compensating:'正在恢复',recovery_required:'等待恢复'};
const names={chat:'普通聊天',proactive:'自然主动',schedule:'定时发送',qzone:'动态',bili:'B站',gallery:'图库',image:'生图',review:'发送前审阅'};
function tell(text,error=false,read=false){$('notice').textContent=text;$('notice').className=error?'error':'';readFailure=error&&read;}
function dirty(){window.dispatchEvent(new CustomEvent('qqbot-draft-dirty',{detail:[...drafts.values()].some(d=>d.dirty)}));}
function local(){return drafts.get(pid);}
function keep(){if(!data)return;const d=local();drafts.set(pid,{text:$('master').value,revision:d?.revision??data.revision,source:d?.source??data.source_fingerprint,dirty:$('master').value!==(d?.base??data.draft),base:d?.base??data.draft});$('dirty').textContent=local().dirty?'本地有未保存的编辑':'草稿已保存';dirty();}
async function api(name,body,params){const r=await(body===undefined?bridge.apiGet(name,params):bridge.apiPost(name,body));if(r?.status==='error')throw new Error(r.message||'操作失败');return r?.data??r;}
function item(label,text){const div=document.createElement('div');div.className='record';const b=document.createElement('strong'),p=document.createElement('pre');b.textContent=label;p.textContent=text;div.append(b,p);return div;}
function render(next){data=next;let d=local();if(!d||!d.dirty){d={text:next.draft||'',base:next.draft||'',revision:next.revision,source:next.source_fingerprint,dirty:false};drafts.set(pid,d);$('master').value=d.text;}
 $('version').textContent=`草稿 v${next.revision} · 生效 ${next.active?.slice(0,8)||'原生人格'}`;$('dirty').textContent=d.dirty?'本地有未保存的编辑（刷新不会覆盖）':'草稿已保存';
 $('sources').replaceChildren(...next.sources.map(s=>item(s.label,`${s.path||'AstrBot 原生人格'}\n${s.text}`)));
 $('conflicts').textContent=JSON.stringify({取舍:next.merge_report||null,外部原生编辑:next.native_changed,预设对话:next.begin_dialogs,冲突草稿:next.conflicts},null,2);
 const active=next.versions.find(v=>v.id===next.active);$('variants').replaceChildren(...Object.entries(active?.variants||{}).map(([k,v])=>item(names[k]||k,v)));
 $('jobs').replaceChildren(...next.jobs.slice().reverse().slice(0,6).map(j=>item(`${actions[j.action]||j.action} · ${states[j.status]||j.status}`,j.reason||new Date(j.at*1000).toLocaleString())));
 $('status').textContent=next.native_changed?'原生人格被外部修改：请整理差异并同步，当前用途仍保留上一生效版本':next.jobs.some(j=>['queued','running','prepared','native_updated'].includes(j.status))?'后台同步中，旧版本继续生效':'当前无同步任务';
 dirty();$('versions').replaceChildren(...next.versions.slice().reverse().map(v=>{const el=item(`${v.id.slice(0,8)} · ${new Date(v.at*1000).toLocaleString()}`,v.master);const b=document.createElement('button');b.textContent='回退到此版';b.disabled=v.id===next.active;b.onclick=()=>perform(()=>submit('rollback',{version:v.id}));el.append(b);return el;}));}
async function refresh(){if(!pid)return;const who=pid,seq=++serial;let next;try{next=await api('persona_state',undefined,{persona_id:who});}catch(e){if(who!==pid||seq!==serial)return;throw e;}if(who!==pid||seq!==serial)return;render(next);if(readFailure)tell('已重新连接，草稿保持不变');}
async function save(){keep();const who=pid,d={...local()},next=await api('persona_action',{action:'draft',persona_id:who,text:d.text,revision:d.revision,source_fingerprint:d.source});
 const current=drafts.get(who);drafts.set(who,{text:current?.text??d.text,base:d.text,dirty:(current?.text??d.text)!==d.text,revision:next.revision,source:next.source_fingerprint});if(who===pid)render(next);return next;}
async function submit(action,extra={}){keep();if(local()?.dirty&&action!=='publish')throw new Error('请先保存当前草稿，再整理来源或回退');const next=action==='publish'?await save():data;
 const j=await api('persona_action',{persona_id:pid,action,revision:next.revision,source_fingerprint:next.source_fingerprint,request_id:crypto.randomUUID(),...extra});tell(`已提交 ${actions[j.action]||j.action}，可继续编辑；状态会自动更新`);await refresh();}
async function perform(fn,read=false){if(busy)return;busy=true;['merge','save','publish','people'].forEach(k=>$(k).disabled=true);try{await fn();}catch(e){tell(e.message,true,read);}finally{busy=false;['merge','save','publish','people'].forEach(k=>$(k).disabled=false);}}
async function poll(){clearTimeout(timer);if(!document.hidden&&!busy&&pid)try{await refresh();}catch(e){tell(e.message,true,true);}timer=setTimeout(poll,2500);}
 $('master').oninput=keep;$('people').onchange=()=>{keep();pid=$('people').value;data=null;const d=local();$('master').value=d?.text||'';perform(refresh,true);};
 $('refresh').onclick=()=>perform(refresh,true);$('save').onclick=()=>perform(async()=>{await save();tell('草稿已保存，尚未发布');});$('merge').onclick=()=>perform(()=>submit('merge'));$('publish').onclick=()=>perform(()=>submit('publish'));
 window.addEventListener('beforeunload',e=>{if([...drafts.values()].some(d=>d.dirty)){e.preventDefault();e.returnValue='';}});document.addEventListener('visibilitychange',()=>{if(!document.hidden)poll();});
 (async()=>{try{bridge=window.AstrBotPluginView||window.AstrBotPluginPage;if(!bridge)throw new Error('请从 AstrBot 人格总设定入口打开');await bridge.ready();const list=await api('persona_state');for(const p of list.people){const o=document.createElement('option');o.value=p.persona_id;o.textContent=p.persona_id;$('people').append(o);}if(list.people.some(p=>p.persona_id===list.selected_persona_id))$('people').value=list.selected_persona_id;pid=$('people').value;if(!pid)throw new Error('没有可编辑的原生人格');await refresh();tell('已连接，草稿与生效版本分开保存');poll();}catch(e){tell(e.message,true);}})();
})();
