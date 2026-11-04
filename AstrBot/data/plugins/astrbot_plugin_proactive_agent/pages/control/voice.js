/* Native authenticated bridge; API keys are never returned or placed in DOM. */
(() => {
  'use strict';
  const el=(tag,text)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;return n;};
  const root=document.getElementById('character');if(!root)return;
  const card=el('article');card.className='card';card.id='voice-card';card.append(el('h2','语音与试听'));
  const summary=el('p','读取语音设置中'),notice=el('p'),metrics=el('p'),actions=el('div');actions.className='actions';actions.style.cssText='display:flex;flex-wrap:wrap;gap:8px';
  const advanced=el('details');advanced.append(el('summary','模式、用量与高级说明'));
  const mode=el('select');mode.setAttribute('aria-label','试听与切换目标模式');for(const [value,label] of [['free','Fish 免费'],['paid','Fish 付费'],['mimo','原 MiMo']]){const option=el('option',label);option.value=value;mode.append(option);}
  const paid=el('input');paid.type='checkbox';const consent=el('label','明确启用付费：15美元／百万UTF-8字节 ');consent.append(paid);consent.hidden=true;
  const description=el('p','自动语音概率沿用原设置；短回复总期限8秒，明确语音短句15秒，失败回文字。免费失败不会转付费。');
  advanced.append(mode,consent,description,metrics);
  const text=el('textarea');text.rows=2;text.maxLength=200;text.value='本龙在呢，听得到吗';text.setAttribute('aria-label','本地试听台词');
  const expression=el('input');expression.type='checkbox';expression.checked=true;
  const expressionLabel=el('label','自动语气与重音（仅 Fish S2） ');expressionLabel.append(expression);
  const direction=el('input');direction.type='text';direction.maxLength=200;direction.placeholder='可选试听要求：小声说／强调“明天”';direction.setAttribute('aria-label','试听语气要求');direction.style.maxWidth='100%';
  const preview=el('pre');preview.style.cssText='white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px';preview.hidden=true;
  const expressionNote=el('p','按最终台词添加少量分句语气和重音，不额外调用分析模型。文字、记忆保留原文；试听设置不自动保存。支持手动输入 [soft tone]、[emphasis] 等官方标记。');
  const player=el('audio');player.controls=true;player.hidden=true;player.style.width='100%';
  card.append(summary,expressionLabel,expressionNote,actions,text,direction,preview,player,notice,advanced);root.append(card);
  let bridge,revision='',objectURL=null,busy=false,expressionDirty=false,expressionRevision='';
  const buttons=[];
  async function api(name,body){const r=await(body?bridge.apiPost(name,body):bridge.apiGet(name));if(r.status==='error')throw new Error(r.message);return r.data??r;}
  function render(d){revision=d.revision;summary.textContent=`${d.voice||'尚未选择音色'} · ${d.model||'无模型'} · 密钥${d.key_configured?'已配置':'未配置'} · 自动语音 ${Math.round((d.auto_probability||0)*100)}%`;
    if(!expressionDirty){expression.checked=d.expression_enabled!==false;expressionRevision=revision;}
    consent.hidden=mode.value!=='paid';
    const latest=d.recent?.at(-1);metrics.textContent=(latest?`最近合成 ${latest.elapsed_ms||0}毫秒 · ${latest.error||latest.state}；`:'')+`最近记录估算付费 $${Number(d.estimated_paid_usd||0).toFixed(5)}。${d.usage_note||''}`;
  }
  async function load(){try{render(await api('panel_voice'));}catch(e){notice.textContent=e.message;}}
  function button(label,fn){const b=el('button',label);b.type='button';b.className='secondary';buttons.push(b);actions.append(b);b.onclick=async()=>{if(busy)return;busy=true;[...buttons,expression,text,direction,mode,paid].forEach(x=>x.disabled=true);notice.textContent='处理中…';try{await fn();}catch(e){notice.textContent=e.message;}finally{busy=false;[...buttons,expression,text,direction,mode,paid].forEach(x=>x.disabled=false);}};}
  mode.onchange=()=>{consent.hidden=mode.value!=='paid';paid.checked=false;};
  expression.onchange=()=>{expressionDirty=true;notice.textContent='语气设置尚未保存；试听可先验证效果';};
  const showPreview=p=>{preview.hidden=false;preview.textContent=`送入 Fish 的台词（不会把标记写入聊天记忆）：\n${p.text}`;};
  button('保存语气设置',async()=>{const d=await api('panel_voice_action',{action:'expression',enabled:expression.checked,revision:expressionRevision});expressionDirty=false;render(d);notice.textContent='已保存，下次语音生效';});
  button('预览语气（不生成音频）',async()=>{const d=await api('panel_voice_action',{action:'preview',mode:mode.value,text:text.value,expression_enabled:expression.checked,instruction:direction.value});showPreview(d.preview);notice.textContent='仅本地预览，无模型调用';});
  button('刷新',async()=>{await load();expressionRevision=revision;notice.textContent=expressionDirty?'已刷新，保留未保存的语气设置':'已刷新';});
  button('检查音色',async()=>{const d=await api('panel_voice_action',{action:'check',mode:mode.value,confirm_paid:paid.checked});notice.textContent=`${d.title||d.mode||''} · ${d.state||''} · ${d.visibility||''}`;});
  button('试听（不发到QQ）',async()=>{const d=await api('panel_voice_action',{action:'audition',mode:mode.value,confirm_paid:paid.checked,text:text.value,expression_enabled:expression.checked,instruction:direction.value,request_id:crypto.randomUUID()});
    if(d.preview)showPreview(d.preview);
    const bytes=Uint8Array.from(atob(d.audio),c=>c.charCodeAt(0));if(objectURL)URL.revokeObjectURL(objectURL);objectURL=URL.createObjectURL(new Blob([bytes],{type:d.mime}));player.src=objectURL;player.hidden=false;await player.play().catch(()=>{});notice.textContent=`试听已生成，${d.metrics.elapsed_ms}毫秒；仅在此页面播放`;});
  button('切换模式',async()=>{render(await api('panel_voice_action',{action:'switch',mode:mode.value,confirm_paid:paid.checked,revision}));notice.textContent='已保存，下次语音使用新模式';});
  button('回退原语音',async()=>{render(await api('panel_voice_action',{action:'switch',mode:'mimo',revision}));notice.textContent='已回退原语音';});
  document.addEventListener('qqbot:view',e=>{if(e.detail==='character'&&bridge)load();});
  document.querySelector('nav')?.addEventListener('click',e=>{if(e.target.dataset.tab==='character'&&bridge)load();});
  bridge=window.AstrBotPluginView||window.AstrBotPluginPage;if(bridge)bridge.ready().then(load);
  window.addEventListener('pagehide',()=>{if(objectURL)URL.revokeObjectURL(objectURL);});
})();
