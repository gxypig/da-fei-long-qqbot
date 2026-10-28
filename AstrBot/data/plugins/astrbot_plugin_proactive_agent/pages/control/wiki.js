/* Adapted UI from in1t1a1/astrbot_plugin_memewiki views/slang (MIT).
 * Uses the existing local store and authenticated AstrBot page bridge. */
(() => {
  'use strict';
  const $=id=>document.getElementById(id),el=(tag,text)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;return n;};
  const host=$('memes');host.classList.add('wiki-page');host.querySelector('h2').textContent='梗百科';
  host.querySelector('.section-head p').textContent='公共梗词库与本地自定义用法 · 理解语境后再接梗';
  $('view-memes').textContent='梗百科';$('meme-search').closest('article').hidden=true;
  const root=el('div');root.innerHTML=`<div class="wiki-summary" id="wiki-summary">正在读取词库…</div>
    <div class="wiki-toolbar"><label>搜索词条、含义、用法<input id="wiki-query" type="search" maxlength="100" placeholder="例如：绷不住、下班"></label><label>词条来源<select id="wiki-source"><option value="all">全部词条</option><option value="wiki">公共百科</option><option value="custom">我的自定义</option><option value="learned">本地已学习</option></select></label><button type="button" id="wiki-search">搜索</button><button type="button" id="wiki-new">添加黑话</button></div>
    <p id="wiki-status" role="status"></p><div id="wiki-list" class="wiki-list"></div><div class="wiki-pager"><button type="button" id="wiki-prev">上一页</button><span id="wiki-page"></span><button type="button" id="wiki-next">下一页</button></div>
    <details id="wiki-editor"><summary>自定义黑话编辑</summary><p>保存在原有梗库中；同名自定义优先使用，删除后恢复公共词条。不会把自定义说法当作已核实出处</p><label>名称<input id="wiki-name" maxlength="24"></label><label>含义（最多300字）<textarea id="wiki-meaning" maxlength="300" rows="3"></textarea></label><label>适合使用的语境（最多240字）<textarea id="wiki-usage" maxlength="240" rows="2"></textarea></label><label>不适合的语境（选填，最多100字）<input id="wiki-avoid" maxlength="100"></label><button type="button" id="wiki-save">保存词条</button><button type="button" id="wiki-clear">清空草稿</button><p id="wiki-edit-status" role="status"></p></details>
    <dialog id="wiki-detail"><div class="wiki-detail-head"><h3 id="wiki-detail-title"></h3><button type="button" id="wiki-detail-close">关闭</button></div><div id="wiki-detail-body"></div></dialog>
    <p class="wiki-credit">词库：cnmeme.wiki · 页面样式参考 memewiki / MIT · 批量JSON导入仍在下方</p>`;
  host.querySelector('.section-head').after(root);
  let bridge,serial=0,offset=0,total=0,loading=false,query='',source='all',dirty=false,editing=null,emptyRevision;
  const visible=()=>!document.hidden&&!host.closest('[hidden]');
  const status=(text,error=false)=>{$('wiki-status').textContent=text;$('wiki-status').classList.toggle('error',error);};
  const sourceName=r=>r.provenance==='custom'?'自定义':r.provenance==='memewiki'?'公共百科':'已学习';
  async function api(body,params){const r=await(body===undefined?bridge.apiGet('meme_wiki',params):bridge.apiPost('meme_wiki',body));if(r.status==='error')throw Error(r.message);return r.data??r;}
  function paragraph(parent,label,value){if(!value)return;const p=el('p'),b=el('strong',label+'：');p.append(b,document.createTextNode(String(value)));parent.append(p);}
  function action(parent,text,fn){const b=el('button',text);b.type='button';b.onclick=fn;parent.append(b);return b;}
  async function showDetail(phrase){const d=$('wiki-detail');$('wiki-detail-title').textContent=phrase;$('wiki-detail-body').textContent='读取中…';if(!d.open)d.showModal();
    const request=phrase;try{const r=await api(undefined,{phrase});if($('wiki-detail-title').textContent!==request)return;const content=$('wiki-detail-body');content.replaceChildren();
      paragraph(content,'来源',sourceName(r.entry));for(const [key,label] of [['meaning','含义'],['usage','使用场景'],['intent','表达意图'],['avoid','避用场景']])paragraph(content,label,r.entry[key]);
      if(r.detail){const section=el('details');section.open=true;section.append(el('summary','公共百科完整资料'));for(const [key,label] of [['summary','简介'],['origin','来源与发展'],['usage','用法'],['other','补充']])paragraph(section,label,r.detail[key]);content.append(section);}
      for(const s of r.entry.sources||[]){try{const url=new URL(s.url);if(url.protocol!=='https:')continue;const a=el('a',s.title||'查看来源');a.href=url.href;a.target='_blank';a.rel='noopener noreferrer';content.append(a);}catch{}}
    }catch(e){$('wiki-detail-body').textContent=e.message;}}
  function edit(row){if(dirty){$('wiki-edit-status').textContent='请先保存或清空现有草稿，再编辑另一条';$('wiki-editor').open=true;return;}
    editing=row||null;$('wiki-name').value=row?.phrase||'';$('wiki-name').readOnly=!!row;$('wiki-meaning').value=row?.meaning||'';$('wiki-usage').value=row?.usage||'';$('wiki-avoid').value=row?.avoid||'';
    $('wiki-editor').open=true;$('wiki-edit-status').textContent=row?.provenance==='custom'?'正在编辑已有自定义':row?'保存为本地自定义用法':'填写新的自定义黑话';$('wiki-name').focus();}
  async function remove(row,button){if(button.dataset.confirm!=='yes'){button.dataset.confirm='yes';button.textContent='确认删除';return;}
    button.disabled=true;try{await api({action:'delete',phrase:row.phrase,revision:row.revision});await refresh();}catch(e){status(e.message,true);button.disabled=false;}}
  async function refresh(wanted=offset){if(!bridge)return;const id=++serial,requested=wanted,q=query,s=source;loading=true;status('正在读取…');
    try{const d=await api(undefined,{q,source:s,offset:requested});if(id!==serial)return;offset=d.offset;total=d.total;emptyRevision=d.empty_revision;
      $('wiki-summary').textContent=`公共百科 ${d.dictionary?.entries||0} 条 · 自定义 ${d.custom_count} 条 · 本地学习 ${d.learned_count} 条${d.dictionary?.snapshot_ready?'':' · 公共词典未安装，仍可管理本地词条'}`;
      const list=$('wiki-list');list.replaceChildren();for(const r of d.rows){const card=el('article');card.className='wiki-entry';const title=el('div');title.className='wiki-title';title.append(el('h3',r.phrase),el('span',sourceName(r)));card.append(title);
        paragraph(card,'含义',r.meaning);paragraph(card,'语境',r.usage);const buttons=el('div');buttons.className='wiki-actions';action(buttons,'查看详情',()=>showDetail(r.phrase));action(buttons,r.provenance==='custom'?'编辑':'自定义这个词',()=>edit(r));if(r.provenance==='custom'){const b=action(buttons,'删除',()=>remove(r,b));b.className='danger';}card.append(buttons);list.append(card);}
      if(!d.rows.length)list.textContent='没有匹配词条，可以调整关键词或添加自定义';$('wiki-page').textContent=`第 ${Math.floor(offset/30)+1} 页 · 共 ${total} 条`;status(`已显示 ${d.rows.length} 条`);
    }catch(e){if(id===serial)status(e.message,true);}finally{if(id===serial){loading=false;$('wiki-prev').disabled=offset===0;$('wiki-next').disabled=offset+30>=total;}}}
  window.QQBotWiki={refresh};
  $('wiki-search').onclick=()=>{query=$('wiki-query').value.trim();source=$('wiki-source').value;offset=0;refresh();};
  $('wiki-query').onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();$('wiki-search').click();}};$('wiki-source').onchange=()=>$('wiki-search').click();
  $('wiki-prev').onclick=()=>{if(!loading)refresh(Math.max(0,offset-30));};$('wiki-next').onclick=()=>{if(!loading&&offset+30<total)refresh(offset+30);};
  $('wiki-new').onclick=()=>edit(null);$('wiki-clear').onclick=()=>{dirty=false;edit(null);};
  for(const id of ['wiki-name','wiki-meaning','wiki-usage','wiki-avoid'])$(id).oninput=()=>{dirty=true;};
  window.addEventListener('beforeunload',e=>{if(dirty){e.preventDefault();e.returnValue='';}});
  $('wiki-save').onclick=async()=>{const b=$('wiki-save');b.disabled=true;const draft=JSON.stringify(['wiki-name','wiki-meaning','wiki-usage','wiki-avoid'].map(id=>$(id).value));
    const entry={...(editing||{}),phrase:$('wiki-name').value.trim(),meaning:$('wiki-meaning').value.trim(),usage:$('wiki-usage').value.trim(),avoid:$('wiki-avoid').value.trim()};
    try{const d=await api({action:'save',entry,revision:editing?.revision||emptyRevision});editing={...entry,provenance:'custom',revision:d.revision};$('wiki-name').readOnly=true;dirty=draft!==JSON.stringify(['wiki-name','wiki-meaning','wiki-usage','wiki-avoid'].map(id=>$(id).value));$('wiki-edit-status').textContent=dirty?'已保存提交的版本，新修改的草稿仍保留':'保存成功，聊天可立即使用';await refresh();}catch(e){$('wiki-edit-status').textContent=e.message;}finally{b.disabled=false;}};
  $('wiki-detail-close').onclick=()=>$('wiki-detail').close();
  async function boot(){bridge=window.AstrBotPluginPage||window.AstrBotPluginView;if(!bridge){setTimeout(boot,100);return;}if(visible())await refresh();}boot();
})();
