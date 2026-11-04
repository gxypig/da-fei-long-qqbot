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
  const player=el('audio');player.controls=true;player.hidden=true;player.style.width='100%';
  card.append(summary,actions,text,player,notice,advanced);root.append(card);
  let bridge,revision='',objectURL=null,busy=false;
  const buttons=[];
  async function api(name,body){const r=await(body?bridge.apiPost(name,body):bridge.apiGet(name));if(r.status==='error')throw new Error(r.message);return r.data??r;}
  function render(d){revision=d.revision;summary.textContent=`${d.voice||'尚未选择音色'} · ${d.model||'无模型'} · 密钥${d.key_configured?'已配置':'未配置'} · 自动语音 ${Math.round((d.auto_probability||0)*100)}%`;
    consent.hidden=mode.value!=='paid';
    const latest=d.recent?.at(-1);metrics.textContent=(latest?`最近合成 ${latest.elapsed_ms||0}毫秒 · ${latest.error||latest.state}；`:'')+`最近记录估算付费 $${Number(d.estimated_paid_usd||0).toFixed(5)}。${d.usage_note||''}`;
  }
  async function load(){try{render(await api('panel_voice'));}catch(e){notice.textContent=e.message;}}
  function button(label,fn){const b=el('button',label);b.type='button';b.className='secondary';buttons.push(b);actions.append(b);b.onclick=async()=>{if(busy)return;busy=true;buttons.forEach(x=>x.disabled=true);notice.textContent='处理中…';try{await fn();}catch(e){notice.textContent=e.message;}finally{busy=false;buttons.forEach(x=>x.disabled=false);}};}
  mode.onchange=()=>{consent.hidden=mode.value!=='paid';paid.checked=false;};
  button('刷新',load);
  button('检查音色',async()=>{const d=await api('panel_voice_action',{action:'check',mode:mode.value,confirm_paid:paid.checked});notice.textContent=`${d.title||d.mode||''} · ${d.state||''} · ${d.visibility||''}`;});
  button('试听（不发到QQ）',async()=>{const d=await api('panel_voice_action',{action:'audition',mode:mode.value,confirm_paid:paid.checked,text:text.value,request_id:crypto.randomUUID()});
    const bytes=Uint8Array.from(atob(d.audio),c=>c.charCodeAt(0));if(objectURL)URL.revokeObjectURL(objectURL);objectURL=URL.createObjectURL(new Blob([bytes],{type:d.mime}));player.src=objectURL;player.hidden=false;await player.play().catch(()=>{});notice.textContent=`试听已生成，${d.metrics.elapsed_ms}毫秒；仅在此页面播放`;});
  button('切换模式',async()=>{render(await api('panel_voice_action',{action:'switch',mode:mode.value,confirm_paid:paid.checked,revision}));notice.textContent='已保存，下次语音使用新模式';});
  button('回退原语音',async()=>{render(await api('panel_voice_action',{action:'switch',mode:'mimo',revision}));notice.textContent='已回退原语音';});
  document.addEventListener('qqbot:view',e=>{if(e.detail==='character'&&bridge)load();});
  document.querySelector('nav')?.addEventListener('click',e=>{if(e.target.dataset.tab==='character'&&bridge)load();});
  bridge=window.AstrBotPluginView||window.AstrBotPluginPage;if(bridge)bridge.ready().then(load);
  window.addEventListener('pagehide',()=>{if(objectURL)URL.revokeObjectURL(objectURL);});
})();
