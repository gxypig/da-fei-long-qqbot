const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
class Element {
  constructor(tag){this.tag=tag;this.children=[];this.handlers={};this._text='';this._value='';this.checked=false;this.disabled=false;this.classList={toggle(){}};}
  set textContent(v){this._text=String(v);this.children=[];}
  get textContent(){return this._text+this.children.map(c=>c.textContent).join('');}
  set innerHTML(v){throw Error('Untrusted data interpreted as HTML');}
  set value(v){this._value=String(v);} get value(){return this._value;}
  get childNodes(){return this.children;}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(){this.children=[];this._text='';}
  addEventListener(n,f){this.handlers[n]=f;}setAttribute(){}
}
const nodes=Object.fromEntries(Object.entries(input.elements).map(([id,tag])=>[id,Object.assign(new Element(tag),{id})]));
const calls=[];let slowResolve;
const bridge={ready:async()=>{},apiGet:async(endpoint,params)=>{
  calls.push({endpoint,params});
  if(endpoint==='panel_settings')return {data:{values:input.values,revision:'v1'}};
  if(endpoint==='emoji_settings')return {data:{available:false,writable:false,message:'not loaded'}};
  if(['panel_groups','panel_friends'].includes(endpoint))return {data:[]};
  if(endpoint==='meme_repository')return {data:{total:1,offset:0,rows:[{phrase:'<script>fake</script>',meaning:'含义',usage:'场景',intent:'自嘲',avoid:'不要冒犯',provenance:'custom',sources:[{title:'unsafe',url:'javascript:alert(1)'},{title:'引用',url:'https://example.invalid/reference'}]}]}};
  if(endpoint==='learning_content'&&params.q==='slow')return new Promise(resolve=>{slowResolve=resolve;});
  if(endpoint==='learning_content')return {data:{jobs:[{id:1,kind:'usage',status:'pending',text:'<img onerror=bad>'}],events:[{created:10,kind:'meme',phrase:'测试词',status:'understood',entry:{meaning:'含义',usage:'语境',intent:'反差',avoid:'严肃时不用'}}],rules:[]}};
  throw Error('Unknown endpoint: '+endpoint);
},apiPost:async(endpoint,body)=>{calls.push({endpoint,body});assert.equal(endpoint,'meme_repository');assert.equal(body.overwrite,false);assert.equal(body.entries[0].phrase,'测试词');return {data:{imported:1,skipped:0}};}};
vm.runInNewContext(input.source,{document:{getElementById:id=>{assert.ok(nodes[id],id);return nodes[id];},createElement:t=>new Element(t),createTextNode:s=>Object.assign(new Element('text'),{textContent:s}),querySelectorAll:()=>[]},window:{AstrBotPluginView:bridge,addEventListener(){},confirm:()=>true},setTimeout,clearTimeout,URL});
const click=id=>nodes[id].handlers.click();
(async()=>{
  await new Promise(r=>setTimeout(r,20));
  nodes['meme-search'].value='中文词';await click('search-memes');await new Promise(r=>setTimeout(r,0));
  const request=calls.find(c=>c.endpoint==='meme_repository');assert.equal(request.params.q,'中文词');assert.equal(request.params.offset,0);
  assert.match(nodes['meme-list'].textContent,/<script>fake<\/script>/);assert.match(nodes['meme-list'].textContent,/自嘲/);
  const links=nodes['meme-list'].children[0].children.filter(c=>c.tag==='a');assert.equal(links.length,1);assert.match(links[0].href,/^https:/);
  await click('refresh-learning-content');assert.match(nodes['learning-content'].textContent,/反差/);assert.match(nodes['learning-content'].textContent,/<img onerror=bad>/);
  const saved=nodes['save-state'].textContent;
  nodes['learning-search'].value='语境';nodes['learning-only-learned'].checked=true;
  nodes['settings'].handlers.input({target:nodes['learning-search']});
  nodes['settings'].handlers.input({target:nodes['learning-only-learned']});
  assert.equal(nodes['save-state'].textContent,saved);
  let prevented=false;await nodes['learning-search'].handlers.keydown({key:'Enter',preventDefault(){prevented=true;}});assert.equal(prevented,true);
  let request2=calls.filter(c=>c.endpoint==='learning_content').at(-1);assert.equal(request2.params.q,'语境');assert.equal(request2.params.learned_only,'1');
  nodes['learning-only-learned'].checked=false;await nodes['learning-only-learned'].handlers.change();
  assert.equal(calls.filter(c=>c.endpoint==='learning_content').at(-1).params.learned_only,'0');
  nodes['learning-search'].value='slow';const oldRequest=click('search-learning');
  nodes['learning-search'].value='fast';await click('search-learning');
  const latest=nodes['learning-content'].textContent;
  slowResolve({data:{jobs:[],events:[],rules:[{rule:'STALE'}]}});await oldRequest;
  assert.equal(nodes['learning-content'].textContent,latest);
  nodes['meme-import'].value=JSON.stringify([{phrase:'测试词',meaning:'含义',usage:'语境'}]);await click('import-memes');assert.match(nodes['meme-import-status'].textContent,/成功导入1条/);
  nodes['meme-import'].value='invalid';await click('import-memes');assert.equal(calls.filter(c=>c.body).length,1);assert.equal(nodes['import-memes'].disabled,false);
})().catch(e=>{console.error(e);process.exitCode=1;});
