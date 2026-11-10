(()=>{'use strict';
const el=(tag,text)=>{const n=document.createElement(tag);n.textContent=text??'';return n;};
const clock=t=>t?new Date(t*1000).toLocaleString():'—';
const names={planning:'选择下一步',waiting:'等待空闲',resting:'休息',finished:'已结束',paused:'已暂停',paused_for_chat:'聊天优先',reading:'读取中',read_ready:'已读，等待理解',read_failed:'来源读取失败',retry:'等待重试',failed:'失败，等待处理',found:'已找到候选',shared:'分享已确认'};
const source=(url,title)=>{try{const u=new URL(url);if(!['https:','http:'].includes(u.protocol))return el('span','无有效来源网址');const a=el('a',title||'查看来源');a.href=u.href;a.target='_blank';a.rel='noopener noreferrer';return a;}catch{return el('span','未提供网址');}};
window.renderExploration=(root,r)=>{root.replaceChildren();if(!r)return;
root.append(el('p',`${names[r.state]||r.state} · ${r.reason||''}`),el('p',`兴趣来源：${r.source?.text||r.goal||'待选择'} · 范围：${r.source_scope||'待确定'}`),
el('p',`继续条件：${r.resume_condition||'出现新线索'} · 最早 ${clock(r.next_at)}`),el('p',`本轮已读 ${(r.cycle_pages||[]).length}/8 页 · 实际运行 ${Math.round(r.active_seconds||0)}/600 秒`));
if(r.questions?.length)root.append(el('p','待查问题：'+r.questions.join('；')));
const materials=el('details');materials.append(el('summary',`已读内容与线索（${r.materials?.length||0}）`));
for(const m of r.materials||[]){const card=el('article');card.append(source(m.url,m.title),el('p',`${m.reader||'读取工具'} · ${m.scope||m.reading_level||'范围未知'} · ${m.body_verified?'正文已取得':'不能当作完整正文'}`),el('p',m.thought?'看法：'+m.thought:'尚无看法'),el('p',m.basis?'依据：'+m.basis:''));
for(const l of m.links||[]){const p=el('p');p.append(source(l.url,l.title),document.createTextNode(' · '+l.paragraph));card.append(p);}
for(const img of m.recognized_images||[])card.append(el('p',`图片 ${img.index+1}：${img.description}`));materials.append(card);}root.append(materials);
const steps=el('details');steps.append(el('summary',`实际行动记录（${r.actions?.length||0}）`));for(const a of r.actions||[]){const p=el('p',`${clock(a.at)} · ${a.action} · ${a.state} · ${a.detail||a.reason||a.error||a.query||''}`);if(a.url)p.append(document.createTextNode(' '),source(a.url));steps.append(p);}root.append(steps);
const shares=el('details');shares.append(el('summary',`分享草稿与结果（${r.shares?.length||0}）`));for(const s of r.shares||[]){const card=el('article');card.append(el('p',`${s.origin} · ${s.status} · ${s.reason||''}`),el('p',s.draft||s.content||''));for(const d of s.drafts||[])card.append(el('p',`${d.at?clock(d.at):''} ${d.issue||'检查通过'}：${d.content}`));if(s.delivery_id)card.append(el('small','投递编号：'+s.delivery_id));shares.append(card);}root.append(shares);
};})();
