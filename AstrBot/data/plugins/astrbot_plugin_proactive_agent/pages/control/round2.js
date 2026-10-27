/* Layout and read-only projections. Settings still use AstrBot's existing source. */
(() => {
  'use strict';
  const $=id=>document.getElementById(id),el=(tag,text)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;return n;};
  const nav=document.querySelector('nav');
  const labels={groups:'群聊',proactive:'主动消息',character:'龙娘设定',learning:'学习与记忆','ai-profiles':'AI 识别',performance:'运行',health:'健康报告'};
  for(const button of [...nav.children]){if(!labels[button.dataset.tab])button.remove();else button.textContent=labels[button.dataset.tab];}
  for(const id of Object.keys(labels))nav.append(nav.querySelector(`[data-tab="${id}"]`));
  const controls=el('div');controls.className='top-tools';
  const pending=el('button','待回复 0 条');pending.id='pending-shortcut';pending.type='button';
  const compactLabel=el('label','紧凑显示 '),compact=el('input');compact.type='checkbox';compact.checked=true;compactLabel.append(compact);
  compact.onchange=()=>document.body.classList.toggle('compact',compact.checked);document.body.classList.add('compact');
  controls.append(pending,compactLabel);document.querySelector('header').append(controls);
  function field(parent,id,label,type='number',min,max){const l=el('label',label),input=$(id)||el(type==='textarea'?'textarea':'input');input.id=id;
    if(type!=='textarea')input.type=type;if(min!==undefined)input.min=min;if(max!==undefined)input.max=max;
    if(type==='checkbox')l.className='toggle';l.append(input);parent.append(l);return input;}
  function button(parent,id,text,fn){const b=$(id)||el('button',text);b.textContent=text;b.id=id;b.type='button';b.className='secondary';if(fn)b.onclick=fn;parent.append(b);return b;}
  function details(parent,label){const d=el('details');d.append(el('summary',label));parent.append(d);return d;}
  function subviews(root,initial,views){const tabs=el('div');tabs.className='subnav';root.prepend(tabs);
    for(const [id,label,view] of views){button(tabs,'view-'+id,label,()=>showSub(id));view.dataset.subview=id;view.hidden=id!==initial;}
    function showSub(id){for(const [key,,view] of views)view.hidden=key!==id;
      for(const b of tabs.children)b.classList.toggle('active',b.id==='view-'+id);
      document.dispatchEvent(new CustomEvent('qqbot:view',{detail:id}));}
    showSub(initial);return showSub;}
  const speed=el('div');while($('performance').firstChild)speed.append($('performance').firstChild);$('performance').append(speed);
  const queue=$('reply-queue');queue.classList.remove('tab-panel');$('performance').append(queue);
  const queuePager=el('div');queuePager.className='pager';for(const id of ['queue-prev','queue-page','queue-next'])queuePager.append($(id));$('queue-pending').before(queuePager);
  const delivery=el('div');delivery.id='delivery-view';delivery.append(el('h2','投递记录'),el('p','确认成功、明确失败和结果未知分别统计；后台存活不代表消息送达'));
  button(delivery,'delivery-refresh','刷新投递记录',()=>loadDelivery());const deliveryList=el('div');deliveryList.id='delivery-list';delivery.append(deliveryList);$('performance').append(delivery);
  const showRun=subviews($('performance'),'speed',[['speed','速度与审查',speed],['queue','回复队列',queue],['delivery','投递记录',delivery]]);
  pending.onclick=()=>{nav.querySelector('[data-tab="performance"]').click();showRun('queue');};
  const idle=el('div');while($('learning').firstChild)idle.append($('learning').firstChild);$('learning').append(idle);
  const memes=$('memes');memes.classList.remove('tab-panel');$('learning').append(memes);
  const images=el('div');images.id='images-view';images.append(el('h2','图片记忆'),el('p','仅从确认发送的记录恢复识图任务；原文、画面、情绪和用途推测分开显示'));
  button(images,'image-refresh','刷新图片状态',()=>loadImages());button(images,'image-retry','重试失败的识图任务',async()=>{await api('panel_image_memory',{});await loadImages();});
  const imageList=el('div');imageList.id='image-list';images.append(imageList);$('learning').append(images);
  subviews($('learning'),'idle',[['idle','空闲学习',idle],['memes','梗仓库',memes],['images','图片记忆',images]]);
  const learningSummary=$('learning-truth');learningSummary.className='callout';idle.prepend(learningSummary);
  const rawLearning=$('learning-details');if(rawLearning){const d=details(idle,'底层学习诊断');d.append(rawLearning);}
  for(const [id,label] of [['learning-review-report','普通回复复核报告'],['learning-release-report','策略发布验证报告']]){const d=details(idle,label),body=$(id)||el('div');body.id=id;d.append(body);}
  const retention=details(idle,'记录保留设置');field(retention,'learning_record_limit','最多保留过程记录（条）','number',50,50000);field(retention,'learning_record_days','最长保留（天）','number',1,365);
  const learningPager=el('div');learningPager.className='pager';
  for(const [id,label] of [['learning-prev','上一页记录'],['learning-next','下一页记录'],['learning-jobs-prev','上一页任务'],['learning-jobs-next','下一页任务']])button(learningPager,id,label);
  $('learning-content').before(learningPager);
  const proactive=$('proactive'),fixed=$('fixed');fixed.classList.remove('tab-panel');fixed.hidden=false;
  const editor=$('rule-editor');editor.setAttribute('aria-label','编辑主动消息规则');
  const editorHead=el('div');editorHead.className='section-head';editorHead.append(el('h2','编辑主动消息规则'));button(editorHead,'close-rule-editor','保留草稿并关闭',()=>editor.close());editor.append(editorHead);
  const free=el('div');free.id='free-rule-fields';
  editor.append(free);proactive.append(editor);
  for(const child of [...proactive.children])if(child.classList.contains('card-grid'))free.append(child);
  const quiet=$('quiet_hours').closest('article');if(quiet)proactive.append(quiet);
  const channelCard=$('allow_group').closest('article'),overview=el('div');overview.className='card-grid proactive-overview';
  overview.append(channelCard,quiet);proactive.insertBefore(overview,editor);
  field(free,'group_ids','自由群聊范围：留空＝全部对象；多个群号用逗号分隔','textarea');
  field(free,'private_ids','自由私聊范围：留空＝全部好友；多个QQ号用逗号分隔','textarea');
  const advanced=details(free,'高级冷却');field(advanced,'min_action_gap_minutes','自由模式总冷却（分钟）','number',5,10080);
  field(advanced,'group_min_gap_minutes','每个群冷却（分钟）','number',15,10080);field(advanced,'private_min_gap_minutes','每个好友冷却（分钟）','number',15,10080);
  editor.append(free,fixed);proactive.append(editor);
  button(editorHead,'save-rule-editor','保存本区设置',()=>{$('settings').requestSubmit();});
  const saveStatus=$('rule-save-status')||el('p');saveStatus.id='rule-save-status';saveStatus.setAttribute('role','status');editorHead.append(saveStatus);
  document.addEventListener('qqbot:saved',e=>{if(e.detail.section==='proactive'){saveStatus.textContent='已保存，运行摘要正在更新';loadProactive();}});
  const fixedAdvanced=details(fixed,'发送方式与高级冷却');
  field(fixedAdvanced,'fixed_message_required','选中的群和私聊执行明确必发；关闭时为自然闲聊检查机会','checkbox');
  field(fixedAdvanced,'fixed_qzone_required','动态执行明确必发；关闭时允许跳过没有依据的动态','checkbox');
  field(fixedAdvanced,'proactive_unanswered_limit','连续多少次未回应后暂停普通主动消息','number',1,10);
  field(fixedAdvanced,'proactive_min_gap_minutes','普通主动消息的最短间隔（分钟）','number',1,1440);
  const switchCard=el('div');switchCard.className='life-toggle';field(switchCard,'proactive_life_enabled','按连续生活和联系动机判断主动消息','checkbox');channelCard.append(switchCard);
  const actions=el('div');actions.className='toolbar';button(actions,'edit-free','编辑自由规则',()=>openEditor('free'));button(actions,'edit-fixed','编辑定时与动态规则',()=>openEditor('fixed'));
  button(actions,'refresh-proactive','刷新运行摘要',()=>loadProactive());button(actions,'preview-proactive','预览但不发送',()=>preview());
  proactive.append(actions);
  for(const [id,label] of [['rules-enabled','仅看已启用'],['rules-exceptions','仅看异常']]){const l=el('label',label),c=el('input');c.id=id;c.type='checkbox';c.onchange=renderRules;l.prepend(c);actions.append(l);}
  const summary=el('div');summary.id='proactive-summary';summary.className='callout';proactive.append(summary);
  const rules=el('div');rules.id='proactive-rules';proactive.append(rules);
  const previewResult=el('pre');previewResult.id='proactive-preview-result';previewResult.setAttribute('role','status');proactive.append(previewResult);
  const decisions=details(proactive,'查看最近决策：为什么发，为什么跳过');decisions.id='proactive-decisions';
  const lifeDetails=details(proactive,'当前生活与未完成事项');lifeDetails.id='life-details';
  const healthDelivery=el('article');healthDelivery.id='delivery-health';healthDelivery.className='card';$('health').prepend(healthDelivery);
  // Diagnostics are available on demand, not a wall of fields on the first screen.
  for(const id of ['performance-database-pool','performance-delivery-storage','performance-audit-storage','performance-timing-window']){
    const n=$(id);if(n){const card=n.closest('article');if(card){const d=el('details');d.append(el('summary',card.querySelector('h2,h3')?.textContent||'底层诊断'));card.before(d);d.append(card);}}}
  function openEditor(mode){free.hidden=mode!=='free';fixed.hidden=mode==='free';if(!editor.open)editor.showModal();}
  let bridge,state,serial=0,timer,ready=false;
  const visible=id=>!document.hidden&&!$(id).closest('[hidden]');
  async function api(name,body,params){const result=await(body===undefined?bridge.apiGet(name,params):bridge.apiPost(name,body));if(result.status==='error')throw new Error(result.message);return result.data??result;}
  function line(parent,label,value){const p=el('p'),b=el('strong',label+'：');p.append(b,document.createTextNode(String(value??'—')));parent.append(p);}
  function health(d){healthDelivery.replaceChildren(el('h2','最近24小时的真实投递'));
    line(healthDelivery,'确认成功 / 明确失败 / 结果未知',`${d.counts.confirmed} / ${d.counts.failed} / ${d.counts.unknown}`);
    line(healthDelivery,'当前连续失败目标',d.current_faults.length);line(healthDelivery,'最老发送中记录',d.oldest_pending_at?new Date(d.oldest_pending_at*1000).toLocaleString():'无');
    for(const t of d.current_faults)line(healthDelivery,t.origin,`连续失败${t.consecutive_failures}次；恢复检查 ${new Date(t.pause_until*1000).toLocaleString()}`);
    line(healthDelivery,'历史失败记录',d.historical_failures+'（不等于当前故障）');
    if(d.reply_backlog!==undefined)line(healthDelivery,'待回复 / 最老等待',`${d.reply_backlog} 条 / ${Math.round(d.oldest_reply_wait_seconds||0)} 秒`);
    line(healthDelivery,'等待落盘',d.persistence?.pending??0);line(healthDelivery,'最近持久化恢复',d.persistence?.recovered_at?new Date(d.persistence.recovered_at*1000).toLocaleString():'没有历史恢复记录');}
  async function loadProactive(){if(!ready)return;const request=++serial;try{const d=await api('panel_proactive');if(request!==serial)return;state=d;
    summary.textContent=`主动消息${d.enabled?'已开启':'已关闭'} · ${d.quiet_now?'免打扰中':'可检查机会'} · 待回复 ${d.pending_replies} 条；自由群聊：${d.scope.group.label}，自由私聊：${d.scope.private.label}`;
    renderRules();health(d.delivery);for(const child of [...decisions.children].slice(1))child.remove();
    for(const r of d.decisions){const row=el('article');row.className='compact-row';line(row,new Date(r.at*1000).toLocaleString(),`${r.origin||'无对象'} · ${r.decision.decision} · ${r.decision.reason}`);decisions.append(row);}
    for(const child of [...lifeDetails.children].slice(1))child.remove();line(lifeDetails,'当前虚拟活动',d.life.current_activity||'等待下次空闲检查');line(lifeDetails,'上一活动',d.life.previous_activity||'无');
    line(lifeDetails,'近期兴趣',(d.life.interests||[]).join('、')||'暂无');
    for(const [field,label] of [['open_items','未完成事项'],['promises','跟进承诺']])for(const item of d.life[field]||[])line(lifeDetails,label,`${item.summary} · ${item.resolved_at?'已由聊天确认结束':'待跟进'} · 仅在 ${item.visibility} 可见`);
  }catch(e){summary.textContent='读取失败：'+e.message;}}
  function renderRules(){if(!state)return;rules.replaceChildren();const statuses={ready:'可检查',waiting:'等待机会',confirmed:'已确认发送',drop:'已跳过',defer:'已延期',unknown:'投递未知，不重发',failed:'明确失败',unavailable:'对象暂不可用',quiet:'免打扰'};
    for(const r of state.rules){if($('rules-enabled').checked&&!r.enabled)continue;if($('rules-exceptions').checked&&!['failed','unknown','unavailable'].includes(r.status))continue;
      const card=el('article');card.className='rule-row';line(card,({free:'自由模式',opportunity:'定时检查',required:'明确必发'}[r.type]||r.type),`${({group:'群聊',private:'私聊',qzone:'动态'}[r.lane]||r.lane)} · ${r.targets.label} · ${r.enabled?'已启用':'未启用'}`);
      line(card,'间隔',Array.isArray(r.interval)?`${r.interval[0]/60}—${r.interval[1]/60}分钟随机`:`${r.interval/60}分钟`);
      line(card,r.time_label,r.next_at?new Date(r.next_at*1000).toLocaleString():'等待调度初始化');line(card,'状态',statuses[r.status]||r.status);if(r.reason)line(card,'原因',r.reason);
      const d=details(card,'对象范围与详情');if(r.targets.excludes_fixed)line(d,'实际自由范围','排除已由定时规则负责的对象');if(r.targets.actual)for(const t of r.targets.actual)line(d,t.name+' '+t.id,t.availability.available?'可达':t.availability.reason);
      button(card,'edit-'+r.id,'编辑',()=>openEditor(r.type==='free'?'free':'fixed'));button(card,'preview-'+r.id,'预览',()=>preview(r));rules.append(card);
    }
  }
  async function preview(rule){try{const body=rule?.origin?{origin:rule.origin,mode:'fixed'}:{};const d=await api('panel_proactive_preview',body),c=d.candidate;
    previewResult.textContent=`${({send:'现在适合发送',defer:'延期再看',drop:'本次放弃'}[d.decision]||d.decision)}：${d.reason}\n${c?`对象：${c.origin}\n依据：${c.basis.summary}\n来源：${({virtual:'虚拟龙娘日常',tool:'实际操作记录',plan:'尚未完成的计划',chat_confirmed:'本会话确认的事情'}[c.basis.source]||c.basis.source)}\n联系理由：${c.motive}\n有效至：${new Date(c.expires_at*1000).toLocaleString()}\n`:''}仅预览：未改变发送、冷却或学习记录`;
  }catch(e){previewResult.textContent=e.message;}}
  async function loadImages(){try{const d=await api('panel_image_memory');imageList.replaceChildren();line(imageList,'待识图 / 已保存图片',`${d.pending} / ${d.assets}`);
    for(const r of d.failed_tasks||[])line(imageList,'失败，可重试',r.error+' · '+r.id);
    for(const r of d.rows||[]){const a=el('article');a.className='card';line(a,'图片内容',r.semantic?.description||'尚未识别，保持未知');line(a,'原文',JSON.stringify(r.semantic?.ocr||[]));line(a,'情绪',r.semantic?.emotion?.join('、'));line(a,'推测用途',r.semantic?.inferred_usage?.join('、'));imageList.append(a);}
  }catch(e){imageList.textContent=e.message;}}
  async function loadDelivery(){try{const d=await api('panel_delivery');deliveryList.replaceChildren();health(d.health);for(const r of d.rows){const a=el('article');a.className='compact-row';line(a,r.origin,`${r.state} · ${new Date(r.at*1000).toLocaleString()}`);line(a,'实际正文',r.text||'附件');if(r.reason)line(a,'原因',r.reason);deliveryList.append(a);}}catch(e){deliveryList.textContent=e.message;}}
  document.addEventListener('qqbot:view',e=>{if(e.detail==='images')loadImages();if(e.detail==='delivery')loadDelivery();});
  nav.addEventListener('click',e=>{if(['proactive','health'].includes(e.target.dataset.tab))loadProactive();queueMicrotask(()=>{
    if(['learning','performance'].includes(e.target.dataset.tab)){const root=$(e.target.dataset.tab),view=[...root.querySelectorAll('[data-subview]')].find(n=>!n.hidden);if(view)$('save-bar').hidden=!['idle','speed'].includes(view.dataset.subview);}
  });});
  async function poll(){clearTimeout(timer);if(ready&&!document.hidden){try{const d=await api('panel_reply_summary');pending.textContent=`待回复 ${d.pending} 条`;}catch{pending.textContent='队列状态待连接';}}if(!document.hidden)timer=setTimeout(poll,10000);}
  document.addEventListener('visibilitychange',()=>{clearTimeout(timer);if(!document.hidden)poll();});
  bridge=window.AstrBotPluginView||window.AstrBotPluginPage;
  if(bridge)bridge.ready().then(()=>{ready=true;poll();if(visible('proactive'))loadProactive();});
})();
