(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  let bridge, values, revision, groups = [], friends = [], dirty = false, polling = false;
  let statusRequest, learningBusy = false, learningRollbackAvailable = false;
  let emojiRevision, emojiWritable = false, emojiDirty = false, emojiBusy = false;
  const booleans = ['group_flow_enabled','chat_low_latency','context_window_enabled','auto_quote_enabled','group_wake_anywhere','enable','allow_group','allow_private','allow_qzone','discretionary_enabled','fixed_messages_enabled','fixed_qzone_enabled','latency_optimization_enabled','reply_prediction_enabled','idle_learning_enabled','style_learning_enabled'];
  const numbers = ['father_address_probability','meme_separate_probability','group_passive_max_age','group_chat_probability','interval_minutes','fixed_message_interval_minutes','fixed_qzone_interval_minutes','emoji_probability','reply_audit_sample_percent','emoji_late_grace_seconds','reply_latency_target_seconds','model_concurrency','reply_hedge_after_seconds','context_recent_turns','context_recent_tokens'];
  const strings = ['group_chat_ids','group_wake_words','quiet_hours','fixed_group_ids','fixed_private_ids','father_qq','father_nickname','review_policy','prediction_provider_id'];
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
    if(!data?.values || [...booleans,...numbers,...strings].some(k=>!Object.hasOwn(data.values,k))){
      values=undefined;$('save').disabled=true;
      throw new Error('设置字段不完整，请重载插件并刷新页面后再保存');
    }
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
    try{
      fill(await api('panel_settings',{values:next,revision}));
      const available=await refreshPerformance();
      notice('设置已保存，群聊规则立即生效；固定定时约10秒内生效，首次发送等待一个完整间隔。'+(available?'性能与学习配置供后续调度立即读取，已开始的任务继续完成。':'当前运行实例尚未提供性能状态，请确认插件已升级并重载后刷新。'));
    }
    catch(e){notice(e.message,true);}finally{$('save').disabled=!values;}
  }
  function emojiControls() {
    $('emoji-probability-percent').disabled=emojiBusy||!emojiWritable;
    $('save-emoji-probability').disabled=emojiBusy||!emojiWritable||!emojiDirty;
    $('reload-emoji-probability').disabled=emojiBusy;
  }
  function fillEmoji(data) {
    if(typeof data?.available!=='boolean' || typeof data?.writable!=='boolean'
      || (data.available && (!Number.isFinite(data.percent)||data.percent<0||data.percent>100
        || typeof data.revision!=='string'||!data.revision)))throw new Error('图库设置格式不完整，请重新读取');
    emojiWritable=data.available&&data.writable;emojiRevision=data.revision;emojiDirty=false;
    $('emoji-probability-percent').value=data.available?data.percent:'';
    $('emoji-probability-status').textContent=data.message||'已读取图库当前设置';
  }
  async function loadEmojiSettings() {
    if(emojiBusy)return;
    emojiBusy=true;emojiControls();
    try{fillEmoji(await api('emoji_settings'));}
    catch(e){emojiWritable=false;emojiRevision=null;$('emoji-probability-status').textContent='读取失败：'+e.message;}
    finally{emojiBusy=false;emojiControls();}
  }
  async function saveEmojiSettings(event) {
    event.preventDefault();
    if(emojiBusy||!emojiWritable||!emojiRevision||!emojiDirty)return;
    const raw=$('emoji-probability-percent').value.trim(), percent=Number(raw);
    if(!raw||!Number.isFinite(percent)||percent<0||percent>100){
      $('emoji-probability-status').textContent='请填写0至100的百分比';return;
    }
    emojiBusy=true;emojiControls();$('emoji-probability-status').textContent='正在保存表情包概率…';
    try{
      const saved=await api('emoji_settings',{percent,revision:emojiRevision});
      fillEmoji(saved);
      $('emoji-probability-status').textContent='表情包概率已保存；'+(saved.message||'图库开关及其他设置保持原样');
    }catch(e){
      // Retain the draft on conflict/failure; require a fresh version before retry.
      emojiWritable=false;emojiRevision=null;
      $('emoji-probability-status').textContent='保存未完成：'+e.message+'；请重新读取后再保存';
    }finally{emojiBusy=false;emojiControls();}
  }
  const statusLabels = {
    enabled:'已启用', review_policy:'审查策略', active:'进行中', running:'运行中作业数', busy:'忙碌',
    pending:'待办', queued:'排队中', queue:'任务队列', completed:'已完成', failed:'失败',
    version:'版本', current_version:'当前版本', active_version:'生效版本', versions:'版本记录',
    todo:'待办', todos:'待办', status:'状态', count:'次数', total:'总计', hits:'命中', misses:'未命中',
    cancelled:'已取消', skipped:'已跳过', error:'错误', last_error:'最近错误',
    target_seconds:'目标（秒）', concurrency:'并发数', model_concurrency:'模型并发数',
    projected:'已压缩请求', recall_full:'旧事查询恢复完整历史', summary_used:'摘要命中',
    summarized:'已整理摘要', summary_failed:'摘要生成失败', summary_paused:'摘要让出聊天名额',
    foreground:'前台任务', background:'后台任务', learning:'学习', prediction:'预测', scheduler:'模型调度',
    reply:'正文回复', chat:'聊天', tool:'工具任务', tools:'工具任务',
    created_at:'创建时间', updated_at:'更新时间', last_run:'最近运行',
    closed:'已关闭', worker_running:'学习后台存活', idle_learning_enabled:'空闲学习已启用', style_learning_enabled:'风格学习已启用',
    scheduler_available:'调度服务可用', cases:'学习案例', pending_forgets:'待遗忘条目',
    candidate_stage:'候选阶段', last_publish:'最近发布', published:'发布时间', rules:'规则数',
    stats:'统计', last_validation:'最近验证', paused:'已暂停', failures:'失败', dropped:'已丢弃',
    samples:'样本数', p95_ms:'P95耗时（毫秒）', median_ms:'中位耗时（毫秒）', max_ms:'最大耗时（毫秒）', over_target:'超出目标次数',
    reply_text_delivery:'正文全程（收到消息至真实发出）', reply_text_send:'正文发送耗时',
    reply_text_deliveries:'正文发送成功次数', incomplete_deliveries:'未完成发送计时', audit_errors:'统计记录错误',
    trigger_probability:'尝试配图概率（0至1）', triggered:'抽中配图', probability_skipped:'未抽中配图', empty_library:'没有可用图片',
    selected_prediction:'模型语义选中', selected_local_reply:'按正文就地匹配', no_match:'没有合适图片',
    sent_combined:'已与正文一起发送', sent_separate:'已单独发送', image_send_failed:'图片发送失败',
    expired:'补图窗口已过', superseded:'已有更新回复', body_failed:'正文发送失败',
    changed_before_send:'发送前配置或语气改变', preparation_failed:'图片准备失败',
    mean:'平均值', avg:'平均值', last:'最近一次', max:'最大值', min:'最小值',
  };
  const isRecord = value => value!==null && typeof value==='object' && !Array.isArray(value);
  function statusNode(value, depth=0) {
    if(value===null || value===undefined || typeof value!=='object'){
      const text=document.createElement('span');
      text.textContent=value===null||value===undefined?'尚未提供':typeof value==='boolean'?(value?'是':'否'):String(value);
      return text;
    }
    if(depth>=4){const text=document.createElement('pre');text.className='status-detail';text.textContent=JSON.stringify(value,null,2);return text;}
    const list=document.createElement('dl');list.className='status-list';
    const entries=Object.entries(value);
    if(!entries.length){list.textContent='暂无记录';return list;}
    entries.slice(0,50).forEach(([key,item])=>{
      const term=document.createElement('dt'), detail=document.createElement('dd');
      term.textContent=Array.isArray(value)?`第 ${Number(key)+1} 项`:(Object.hasOwn(statusLabels,key)?statusLabels[key]:key);
      detail.append(statusNode(item,depth+1));list.append(term,detail);
    });
    if(entries.length>50){const more=document.createElement('dd');more.textContent=`仅展示前50项，共${entries.length}项`;list.append(more);}
    return list;
  }
  function renderSnapshot(id, data, empty) {
    const container=$(id);container.replaceChildren();
    if(data===undefined || data===null || (typeof data==='object' && !Object.keys(data).length))container.textContent=empty;
    else container.append(statusNode(data));
  }
  const validVersion = version => (typeof version==='string' && version.trim()!=='') || (Number.isInteger(version) && version>=0);
  function renderLearning(data) {
    const learning=isRecord(data)?data:{};
    const current=learning.current_version ?? learning.active_version ?? learning.version;
    const worker=learning.worker_running===true?'学习后台运行中':learning.worker_running===false?'学习后台未运行':'学习状态已刷新';
    $('learning-status').textContent=learning.closed===true?'学习服务已关闭，请重载插件后刷新':Object.keys(learning).length?`${worker}；开关修改需保存后生效`:'学习服务尚未提供状态，请确认插件已升级并重载后刷新';
    $('learning-version').textContent=validVersion(current)?`当前学习版本：${current}`:'尚未提供当前版本，更多信息见学习服务详情';
    renderSnapshot('learning-pending',learning.pending ?? learning.todos ?? learning.todo ?? learning.queue,'尚未提供待办状态，更多信息见学习服务详情');
    renderSnapshot('learning-details',data,'尚未提供学习状态');
    const select=$('learning-rollback-version'), selected=select.value;
    select.replaceChildren();
    const versions=Array.isArray(learning.versions)?[...learning.versions]:isRecord(learning.versions)?Object.keys(learning.versions):[];
    // Numeric policy version 0 is the learning service's built-in baseline.
    if(Number.isInteger(current) && current>=0)versions.push(0);
    const seen=new Set();
    versions.forEach(entry=>{
      const version=isRecord(entry)?(entry.version ?? entry.id):entry;
      if(!validVersion(version))return;
      const key=JSON.stringify(version);if(seen.has(key))return;seen.add(key);
      const option=document.createElement('option');option.value=key;
      option.textContent=`版本 ${version}${version===0?'（内置规则）':''}${String(version)===String(current)?'（当前）':''}`;select.append(option);
    });
    if(!seen.size){const option=document.createElement('option');option.value='';option.textContent='暂无可回退版本';select.append(option);}
    else if(seen.has(selected))select.value=selected;
    learningRollbackAvailable=learning.closed!==true && seen.size>0;
    select.disabled=learningBusy||!learningRollbackAvailable;
    $('learning-rollback').disabled=select.disabled;
  }
  function renderPerformance(data) {
    const performance=isRecord(data?.performance)?data.performance:null;
    const available=performance!==null && Object.keys(performance).length>0;
    const mode=performance?.enabled===true?'延迟优化已开启':performance?.enabled===false?'延迟优化已关闭':'调度开关尚未提供';
    const policy=performance?.review_policy==='all'?'全部审查':performance?.review_policy==='speed_first'?'速度优先':'审查策略尚未提供';
    const target=Number.isFinite(performance?.target_seconds)?` · 正文目标 ${performance.target_seconds} 秒`:'';
    $('performance-status').textContent=available?`${mode} · ${policy}${target} · 运行状态已刷新`:'当前运行实例尚未提供性能状态，请确认插件已升级并重载后刷新';
    renderSnapshot('performance-work',performance?.work,'尚未提供调度状态');
    renderSnapshot('performance-prediction',performance?.prediction,'尚未提供预测统计');
    renderSnapshot('performance-emoji',performance?.emoji,'尚未提供表情统计');
    renderSnapshot('performance-context',performance?.context_window,'尚未提供压缩统计');
    renderSnapshot('performance-timings',performance?.timings,'尚未提供耗时数据');
    renderLearning(performance?.learning);
    return available;
  }
  async function refreshPerformance() {
    if(statusRequest)return statusRequest;
    statusRequest=(async()=>{
      try{return renderPerformance(await api('status'));}
      catch(e){
        renderPerformance(null);
        $('performance-status').textContent='读取性能状态失败：'+e.message;
        $('learning-status').textContent='读取学习状态失败，请刷新重试；当前不可回退';
        return false;
      }
    })();
    try{return await statusRequest;}finally{statusRequest=null;}
  }
  async function rollbackLearning() {
    const select=$('learning-rollback-version');
    if(learningBusy || select.disabled || !select.value)return;
    const version=JSON.parse(select.value);
    learningBusy=true;select.disabled=true;$('learning-rollback').disabled=true;
    $('learning-action-status').textContent='正在回退学习版本…';
    try{
      await api('learning_action',{action:'rollback',version});
      $('learning-action-status').textContent=`已回退到版本 ${version}`;
      // Finish any older status request, then fetch the state after rollback.
      if(statusRequest)await statusRequest;
      await refreshPerformance();
    }catch(e){$('learning-action-status').textContent='回退失败：'+e.message;}
    finally{
      learningBusy=false;select.disabled=!learningRollbackAvailable;
      $('learning-rollback').disabled=select.disabled;
    }
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
  $('refresh-performance').addEventListener('click',refreshPerformance);
  $('refresh-learning').addEventListener('click',refreshPerformance);
  $('learning-rollback').addEventListener('click',rollbackLearning);
  $('emoji-settings-form').addEventListener('submit',saveEmojiSettings);
  $('emoji-probability-percent').addEventListener('input',()=>{
    emojiDirty=true;$('emoji-probability-status').textContent='表情包概率有未保存的修改，请使用本项的保存按钮';emojiControls();
  });
  $('reload-emoji-probability').addEventListener('click',()=>{
    if(!emojiDirty||window.confirm('重新读取会放弃未保存的表情包概率，继续吗？'))return loadEmojiSettings();
  });
  ['fixed_group_ids','fixed_private_ids'].forEach(k=>$(k).addEventListener('change',renderTargets));
  document.querySelectorAll('[data-tab]').forEach(button=>button.addEventListener('click',()=>{
    document.querySelectorAll('[data-tab]').forEach(b=>b.classList.toggle('active',b===button));
    document.querySelectorAll('.tab-panel').forEach(p=>p.hidden=p.id!==button.dataset.tab);
    $('save-bar').hidden=button.dataset.tab==='health';
    if(button.dataset.tab==='fixed')refreshSchedule();
    if(['performance','learning'].includes(button.dataset.tab))refreshPerformance();
    if(button.dataset.tab==='health')refreshHealth().then(r=>{if(r)poll();});
  }));
  $('settings').addEventListener('submit',save);$('settings').addEventListener('input',event=>{if(!['learning-rollback-version','emoji-probability-percent'].includes(event.target.id))changed();});
  $('reload').addEventListener('click',()=>{if(!dirty||window.confirm('重新读取会放弃未保存的修改，继续吗？'))loadSettings();});
  $('load-groups').addEventListener('click',loadGroups);$('group_chat_ids').addEventListener('change',renderGroups);
  $('probability-slider').addEventListener('input',()=>{$('group_chat_probability').value=$('probability-slider').value;});
  $('group_chat_probability').addEventListener('input',()=>{$('probability-slider').value=$('group_chat_probability').value;});
  $('refresh-health').addEventListener('click',refreshHealth);
  window.addEventListener('beforeunload',event=>{if(dirty||emojiDirty){event.preventDefault();event.returnValue='';}});
  (async()=>{try{bridge=window.AstrBotPluginView||window.AstrBotPluginPage;if(!bridge)throw new Error('请从 AstrBot 控制台的插件页面打开');await bridge.ready();await Promise.all([loadSettings(),loadEmojiSettings()]);await loadTargets();}catch(e){notice(e.message,true);}})();
})();
