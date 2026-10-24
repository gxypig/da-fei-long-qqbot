(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  let bridge, values, revision, groups = [], friends = [], dirty = false, polling = false;
  const booleans = ['group_flow_enabled','chat_low_latency','auto_quote_enabled','group_wake_anywhere','enable','allow_group','allow_private','allow_qzone','discretionary_enabled','fixed_messages_enabled','fixed_qzone_enabled'];
  const numbers = ['father_address_probability','meme_separate_probability','group_passive_max_age','group_chat_probability','interval_minutes','fixed_message_interval_minutes','fixed_qzone_interval_minutes','emoji_probability'];
  const strings = ['group_chat_ids','group_wake_words','quiet_hours','fixed_group_ids','fixed_private_ids','father_qq','father_nickname'];
  const notice = (text, error=false) => { $('notice').textContent=text; $('notice').className=error?'error':''; };
  async function api(endpoint, body) {
    const result = await (body===undefined ? bridge.apiGet(endpoint) : bridge.apiPost(endpoint,body));
    if(result?.status==='error') throw new Error(result.message||'请求失败');
    return result?.data ?? result;
  }
  function changed() {dirty=true; $('save-state').textContent='有未保存的设置';}
  function selected() {return new Set($('group_chat_ids').value.split(/[,，\s]+/).filter(Boolean));}
  function renderGroups() {
    const checked=selected(), rows=[...groups];
    checked.forEach(id=>{if(!rows.some(g=>g.id===id))rows.push({id,name:'已配置的群（暂未获取名称）'});});
    $('group-list').replaceChildren();
    if(!rows.length){$('group-list').textContent='暂无可选群，可展开下方手动填写群号';return;}
    rows.forEach(g=>{
      const displayName=String(g.name).replace(/<\$[^>]*>/g,'').replace(/[\x00-\x1f]/g,'').trim()||'未命名群';
      const label=document.createElement('label');label.className='group';
      const box=document.createElement('input');box.type='checkbox';box.checked=checked.has(g.id);box.setAttribute('aria-label',`将 ${displayName} 设为特殊接话群`);
      const info=document.createElement('div'),name=document.createElement('strong'),id=document.createElement('small');
      name.textContent=displayName;id.textContent=g.id;info.append(name,id);label.append(box,info);
      box.addEventListener('change',()=>{const set=selected();box.checked?set.add(g.id):set.delete(g.id);$('group_chat_ids').value=[...set].join(',');changed();});
      $('group-list').append(label);
    });
  }
  function fill(data) {
    values=data.values;revision=data.revision;
    booleans.forEach(k=>$(k).checked=values[k]);[...numbers,...strings].forEach(k=>$(k).value=values[k]);
    $('probability-slider').value=values.group_chat_probability;
    dirty=false;$('save-state').textContent='已与当前配置同步';$('save').disabled=false;renderGroups();renderTargets();
  }
  async function loadGroups() {try{groups=await api('panel_groups');renderGroups();}catch(e){notice(e.message,true);renderGroups();}}
  async function loadSettings() {try{fill(await api('panel_settings'));$('connection').textContent='面板已连接';}catch(e){notice(e.message,true);$('connection').textContent='连接失败';}}
  async function save(event) {
    event.preventDefault();if(!values)return;
    const next={...values};booleans.forEach(k=>next[k]=$(k).checked);numbers.forEach(k=>next[k]=Number($(k).value));strings.forEach(k=>next[k]=$(k).value);
    $('save').disabled=true;
    try{fill(await api('panel_settings',{values:next,revision}));notice('设置已保存，群聊规则立即生效；固定定时约10秒内生效，首次发送等待一个完整间隔');}
    catch(e){notice(e.message,true);}finally{$('save').disabled=false;}
  }
  function renderHealth(data) {
    const report=data.report, job=data.job;
    const running=job?.status==='running';
    $('job-status').textContent=running?'正在检查，修复时可能短暂断线，请稍候…':job?.status==='failed'?'上次检查未完成，请查看本机日志':job?.status==='completed'?`上次任务已完成${job.exit_code===0?'':'，仍有项目需要处理'}`:'后台守护持续检查中';
    document.querySelectorAll('[data-mode]').forEach(b=>b.disabled=running);
    $('health-summary').textContent=report?`${report.ok?'本轮检查正常':'本轮有项目需要处理'} · ${new Date(report.time).toLocaleString('zh-CN')} · ${report.deep?'含真实模型实测':'连接与组件检查'}`:'还没有检查报告，请在项目目录运行「一键检查修复.bat」';
    $('health-results').replaceChildren();
    (report?.checks||[]).forEach(r=>{
      const row=document.createElement('div');row.className='health-row';
      const icon=document.createElement('span');icon.textContent=r.ok?'✓':'!';icon.className=r.ok?'good':'bad';
      const name=document.createElement('strong');name.textContent=r.name;
      const detail=document.createElement('div');detail.className='detail';detail.textContent=r.detail+(r.next_step?' · '+r.next_step:'');
      row.append(icon,name,detail);$('health-results').append(row);
    });
    return running;
  }
  async function refreshHealth(){try{return renderHealth(await api('panel_health'));}catch(e){notice('暂时无法读取状态，服务可能正在重连',true);return true;}}
  async function poll() {
    if(polling)return;polling=true;
    const deadline=Date.now()+900000;
    while(Date.now()<deadline){await new Promise(r=>setTimeout(r,3000));if(!await refreshHealth())break;}
    polling=false;
  }
  function renderTargets() {
    for (const [list,field,items] of [['fixed-group-list','fixed_group_ids',groups],['fixed-friend-list','fixed_private_ids',friends]]) {
      const selected=new Set($(field).value.split(/[,，\s]+/).filter(Boolean)), rows=[...items];
      selected.forEach(id=>{if(!rows.some(r=>r.id===id))rows.push({id,name:'已选择的目标'});});
      $(list).replaceChildren();
      if(!rows.length)$(list).textContent='暂无可选对象，可手动填写';
      rows.forEach(r=>{const label=document.createElement('label');label.className='group';
        const box=document.createElement('input');box.type='checkbox';box.checked=selected.has(r.id);
        const text=document.createElement('span');text.textContent=String(r.name).replace(/<\$[^>]*>/g,'')+' · '+r.id;
        label.append(box,text);$(list).append(label);
        box.addEventListener('change',()=>{const set=new Set($(field).value.split(/[,，\s]+/).filter(Boolean));box.checked?set.add(r.id):set.delete(r.id);$(field).value=[...set].join(',');changed();});
      });
    }
  }
  async function loadTargets(){await loadGroups();try{friends=await api('panel_friends');}catch(e){notice(e.message,true);}renderTargets();}
  async function refreshSchedule(){try{const d=await api('panel_schedule');$('schedule-status').replaceChildren();
    const head=document.createElement('p');head.textContent=d.quiet_now?'现在处于免打扰时段':'每个目标独立执行';$('schedule-status').append(head);
    (d.rows||[]).forEach(r=>{const row=document.createElement('p');row.textContent=r.target+' · '+({waiting:'等待首次发送',sending:'生成/发送中',sent:'上次发送成功',failed:'上次失败，独立重试'}[r.status]||r.status)+' · 下次 '+new Date(Math.max(r.due,r.retry_at||0)*1000).toLocaleString('zh-CN');$('schedule-status').append(row);});
    if(!d.rows?.length)head.textContent='还没有启用的定时目标，保存选择后约10秒开始计时';
  }catch(e){notice(e.message,true);}}
  $('load-targets').addEventListener('click',loadTargets);
  $('refresh-schedule').addEventListener('click',refreshSchedule);
  ['fixed_group_ids','fixed_private_ids'].forEach(k=>$(k).addEventListener('change',renderTargets));
  document.querySelectorAll('[data-tab]').forEach(button=>button.addEventListener('click',()=>{
    document.querySelectorAll('[data-tab]').forEach(b=>b.classList.toggle('active',b===button));
    document.querySelectorAll('.tab-panel').forEach(p=>p.hidden=p.id!==button.dataset.tab);
    $('save-bar').hidden=button.dataset.tab==='health';
    if(button.dataset.tab==='fixed')refreshSchedule();
    if(button.dataset.tab==='health')refreshHealth().then(r=>{if(r)poll();});
  }));
  $('settings').addEventListener('submit',save);$('settings').addEventListener('input',changed);
  $('reload').addEventListener('click',()=>{if(!dirty||window.confirm('重新读取会放弃未保存的修改，继续吗？'))loadSettings();});
  $('load-groups').addEventListener('click',loadGroups);$('group_chat_ids').addEventListener('change',renderGroups);
  $('probability-slider').addEventListener('input',()=>{$('group_chat_probability').value=$('probability-slider').value;});
  $('group_chat_probability').addEventListener('input',()=>{$('probability-slider').value=$('group_chat_probability').value;});
  $('refresh-health').addEventListener('click',refreshHealth);
  window.addEventListener('beforeunload',event=>{if(dirty){event.preventDefault();event.returnValue='';}});
  (async()=>{try{bridge=window.AstrBotPluginView||window.AstrBotPluginPage;if(!bridge)throw new Error('请从 AstrBot 控制台的插件页面打开');await bridge.ready();await loadSettings();await loadTargets();}catch(e){notice(e.message,true);}})();
})();
