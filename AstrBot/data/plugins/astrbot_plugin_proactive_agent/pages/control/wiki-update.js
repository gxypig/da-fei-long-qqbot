/* Dictionary updates have their own revision; refreshing never consumes a draft. */
(() => {
  'use strict';const host=document.getElementById('memes');if(!host)return;
  const box=document.createElement('details');box.className='card';box.innerHTML=`<summary>词库更新与回退</summary><p id="wiki-update-summary">展开后读取更新状态</p><p id="wiki-update-status" role="status"></p><div class="wiki-toolbar"><button type="button" id="wiki-update-start">立即更新 / 重试</button><button type="button" id="wiki-update-cancel">取消更新</button><button type="button" id="wiki-update-refresh">刷新状态</button></div><label><input type="checkbox" id="wiki-update-enabled">每日自动更新</label><label>香港时间<input type="time" id="wiki-update-time" value="04:00"></label><button type="button" id="wiki-update-settings">保存更新时间</button><p>繁忙时暂停新抓取，连续空闲5秒后继续。校验完整词库和索引后才切换；取消或失败仍使用原词库</p><label>保留版本<select id="wiki-update-versions"></select></label><button type="button" id="wiki-update-rollback">回退到所选版本</button><details><summary>离线导入</summary><p>支持校验过的 memes.db 或 gzip 压缩包。自定义词条独立保存；文件上传后还会检查数据库结构、完整性和大小</p><input id="wiki-update-file" type="file" accept=".db,.sqlite,.gz"><button type="button" id="wiki-update-import">校验并导入</button></details>`;
  host.querySelector('.section-head').after(box);
  const $=id=>document.getElementById(id);let bridge,state,dirty=false,busy=false,serial=0;
  const visible=()=>box.open&&!document.hidden&&!window.QQBotSubviewActive&&!host.closest('[hidden]');
  const names={queued:'等待开始',running:'抓取中',paused:'等待空闲或限流恢复',validating:'校验并构建索引',completed:'更新完成',cancelled:'已取消',failed:'更新失败，可重试',interrupted:'上次更新中断'};
  const date=t=>t?new Date(t*1000).toLocaleString('zh-CN',{timeZone:'Asia/Hong_Kong'}):'暂无';
  const controls=()=>{for(const id of ['start','cancel','settings','rollback','import'])$('wiki-update-'+id).disabled=busy||!state||(['start','rollback','import'].includes(id)&&state.busy);};
  async function api(body){const r=await(body?bridge.apiPost('meme_wiki_update',body):bridge.apiGet('meme_wiki_update'));if(r.status==='error')throw Error(r.message);return r.data??r;}
  function render(d){state=d;const j=d.job||{};$('wiki-update-summary').textContent=`程序 ${d.program_version} · 词库 ${d.active||'初始离线版本'} · 最近检查 ${date(d.last_check)} · 最近成功 ${date(d.last_success)}`;
    $('wiki-update-status').textContent=`${names[j.state]||'尚未更新'} · 已读 ${j.read||0} 条 / ${j.pages||0} 页 · 新增 ${j.added||0} · 修订 ${j.updated||0}${j.reason?' · '+j.reason:''}`;
    if(!dirty){$('wiki-update-enabled').checked=d.automatic;$('wiki-update-time').value=d.time;}
    const select=$('wiki-update-versions'),selected=select.value;select.replaceChildren();for(const v of d.versions||[]){const o=document.createElement('option');o.value=v.id;o.textContent=`${v.id===d.active?'当前 · ':''}${date(v.at)} · ${v.entries} 条`;select.append(o);}if([...select.options].some(o=>o.value===selected))select.value=selected;controls();}
  async function load(){if(!bridge)return;const n=++serial;try{const d=await api();if(n===serial)render(d);}catch(e){if(n===serial)$('wiki-update-status').textContent=e.message;}}
  async function action(type,extra={}){if(busy||!state)return false;busy=true;controls();const n=++serial;try{const d=await api({action:type,revision:state.revision,...extra});if(n===serial)render(d);if(type==='rollback')window.QQBotWiki?.refresh();return true;}catch(e){$('wiki-update-status').textContent=e.message;return false;}finally{busy=false;controls();}}
  $('wiki-update-start').onclick=()=>action('start');$('wiki-update-cancel').onclick=()=>action('cancel');$('wiki-update-refresh').onclick=load;
  $('wiki-update-rollback').onclick=()=>action('rollback',{version:$('wiki-update-versions').value});
  for(const id of ['enabled','time'])$('wiki-update-'+id).oninput=()=>{dirty=true;};
  $('wiki-update-settings').onclick=async()=>{const before=JSON.stringify([$('wiki-update-enabled').checked,$('wiki-update-time').value]);const saved=await action('settings',{enabled:$('wiki-update-enabled').checked,time:$('wiki-update-time').value,config_revision:state?.config_revision});
    if(saved&&state&&JSON.stringify([state.automatic,state.time])===before&&before===JSON.stringify([$('wiki-update-enabled').checked,$('wiki-update-time').value])){dirty=false;$('wiki-update-status').textContent='更新时间已保存，下次检查采用新设置';}};
  $('wiki-update-import').onclick=async()=>{if(busy)return;const file=$('wiki-update-file').files[0];if(!file)return;if(file.size>25_000_000){$('wiki-update-status').textContent='文件超过25MB，请先压缩为gzip';return;}
    busy=true;controls();try{const bytes=await file.arrayBuffer(),sha=[...new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))].map(x=>x.toString(16).padStart(2,'0')).join('');
      const data=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result).split(',')[1]);reader.onerror=reject;reader.readAsDataURL(file);});busy=false;await action('import',{package:data,sha256:sha});
    }catch(e){$('wiki-update-status').textContent=e.message;}finally{busy=false;controls();}};
  box.ontoggle=()=>{if(visible())load();};document.addEventListener('visibilitychange',()=>{if(visible())load();});
  setInterval(()=>{if(visible()&&!busy)load();},5000);
  window.addEventListener('beforeunload',e=>{if(dirty){e.preventDefault();e.returnValue='';}});
  function boot(){bridge=window.AstrBotPluginPage||window.AstrBotPluginView;if(!bridge){setTimeout(boot,100);return;}if(visible())load();}boot();controls();
})();
