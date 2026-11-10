/* The existing settings endpoint remains the sole configuration authority. */
(()=>{'use strict';const host=document.getElementById('ai-profiles');if(!host)return;
const bridge=window.AstrBotPluginView||window.AstrBotPluginPage;
const card=document.createElement('article');card.className='card';
card.innerHTML='<h3>识别运行开关</h3><label><input type="checkbox" id="account-enabled">启用 AI 账号识别</label><label><input type="checkbox" id="account-probe">允许在正常聊天中自然试探</label><p>关闭总开关立即停止观察、分析和相关称呼；保留历史和试探选项</p><button type="button" id="account-save">保存识别开关</button><button type="button" id="account-load">重新读取</button><p id="account-status" role="status"></p>';
host.querySelector('.section-head').after(card);const $=id=>card.querySelector('#'+id);
let snapshot,dirty=false,busy=false,serial=0;
function deps(){$('account-probe').disabled=!$('account-enabled').checked;}
for(const id of ['account-enabled','account-probe'])$(id).onchange=()=>{dirty=true;deps();};
async function request(method,body){const r=await bridge[method]('panel_fullchain_settings',body);if(r?.status==='error')throw Error(r.message);return r.data??r;}
async function load(){if(dirty||busy)return;const token=++serial;try{const next=await request('apiGet');if(token!==serial||dirty)return;snapshot=next;$('account-enabled').checked=!!next.values.ai_profiles_enabled;$('account-probe').checked=!!next.values.ai_probe_enabled;deps();$('account-status').textContent='已读取当前设置';}catch(e){$('account-status').textContent=e.message;}}
$('account-load').onclick=()=>{if(dirty&&!confirm('放弃这两项未保存的修改并重新读取？'))return;dirty=false;load();};
$('account-save').onclick=async()=>{if(!snapshot||busy)return;busy=true;const values={ai_profiles_enabled:$('account-enabled').checked,ai_probe_enabled:$('account-probe').checked};$('account-save').disabled=true;$('account-status').textContent='正在保存';try{snapshot=await request('apiPost',{values,field_revisions:snapshot.field_revisions,revision:snapshot.revision});dirty=Object.entries(values).some(([key,v])=>v!==(key==='ai_profiles_enabled'?$('account-enabled').checked:$('account-probe').checked));$('account-status').textContent=dirty?'本次已保存，新修改仍在草稿中':'已保存并立即生效';document.getElementById('profiles-refresh').click();}catch(e){$('account-status').textContent=e.message+'；草稿已保留';}finally{busy=false;$('account-save').disabled=false;deps();}};
document.querySelector('nav').addEventListener('click',e=>{if(e.target.dataset.tab==='ai-profiles')load();});
bridge.ready().then(load).catch(e=>$('account-status').textContent=e.message);
})();
