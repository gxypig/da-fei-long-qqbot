/* Review controls reuse the existing versioned settings; diagnostics are read-only. */
(() => {
  'use strict';const $=id=>document.getElementById(id),el=(tag,text)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;return n;};
  const card=$('review_policy').closest('article');
  card.append(el('p','已有设置保持原选择；从未保存过时默认关闭。语音、表情包、收件人校验和发送记录继续工作，空闲学习仍由自己的开关控制。'));
  const button=el('button','查看审阅实际工作');button.type='button';const output=el('div');output.id='review-work';output.setAttribute('role','status');card.append(button,output);
  button.onclick=async()=>{const bridge=window.AstrBotPluginPage||window.AstrBotPluginView;if(!bridge)return;button.disabled=true;
    try{const r=await bridge.apiGet('panel_review_metrics');if(r.status==='error')throw Error(r.message);const d=r.data??r;output.replaceChildren();
      for(const text of [`总开关：${d.enabled?'开启':'关闭'} · 策略：${d.policy==='speed_first'?'预测分流':'完整审阅'}`,`保留日志：${d.retained_from||'暂无'} 至 ${d.retained_to||'暂无'}`,`模型审阅 ${d.reviews} 次 · 直接通过 ${d.passed} · 改写 ${d.rewritten} · 未通过 ${d.blocked}`,`模型改写${d.legacy_rewrite_estimate?'（含旧日志估计）':''} ${d.model_rewritten} 次 · 预测分流直发 ${d.prediction_direct??d.local_passes} 次`,`有计时的审阅 ${d.timed} 次 · 中位 ${d.median_ms==null?'—':(d.median_ms/1000).toFixed(2)+'秒'} · P95 ${d.p95_ms==null?'—':(d.p95_ms/1000).toFixed(2)+'秒'}`,d.note])output.append(el('p',text));
    }catch(e){output.textContent=e.message;}finally{button.disabled=false;}};
  $('images-view').querySelector('h2').textContent='表情资料缓存';$('images-view').querySelector('p').textContent='按图片内容编号复用首次识别资料；优先使用图库已有结果，后台预备新图。历史标签不冒充逐字OCR';$('view-images').textContent='表情资料';
  const memoryCard=$('performance-image-memory')?.closest('article');if(memoryCard)memoryCard.querySelector('h2').textContent='表情资料缓存';
})();
