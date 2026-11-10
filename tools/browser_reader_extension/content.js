'use strict';
// No read on the user's ordinary browsing tabs: only an explicit job for an
// extension-owned tab can invoke this handler. No credentials or script state.
chrome.runtime.onMessage.addListener((message,sender,reply)=>{
  if(sender.id!==chrome.runtime.id||message?.kind!=='read-article')return;
  let wanted;try{wanted=new URL(message.url);}catch{return;}
  if(wanted.origin!==location.origin||wanted.pathname.replace(/\/$/,'')!==location.pathname.replace(/\/$/,''))return;
  const tieba=location.hostname==='tieba.baidu.com';
  const selector=tieba?'.pc-pb-box .image-text > .pb-content-wrap, .d_post_content':'.AnswerItem .RichContent-inner, .Post-RichText, .RichContent-inner';
  if(!document.querySelector(selector)){
    const visible=(document.body?.innerText||'').slice(0,1000);
    const gated=/安全验证|访问异常/.test(document.title)||/请求存在异常/.test(visible);
    reply(gated?{blocked:'站点要求验证或限制访问'}:{waiting:true});return;
  }
  const clone=document.documentElement.cloneNode(true);
  clone.querySelectorAll('script,style,noscript,form,input,textarea,iframe,header,nav,aside,svg,meta,link:not([rel="canonical"]),.AppHeader,.Topstory-container').forEach(n=>n.remove());
  clone.querySelectorAll('*').forEach(n=>{for(const a of [...n.attributes])if(/^on/i.test(a.name)||/token|cookie|password|authorization/i.test(a.name))n.removeAttribute(a.name);});
  const html='<!doctype html>'+clone.outerHTML;
  if(new TextEncoder().encode(html).length>2*1024*1024){reply({blocked:'正文页面超过2MB限制'});return;}
  reply({url:location.href,html});
});
