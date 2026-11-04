const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const source=fs.readFileSync(process.argv[2],'utf8');
class Element {
  constructor(tag){this.tag=tag;this.children=[];this.style={};this.handlers={};this.text='';}
  set textContent(v){this.text=String(v);this.children=[];} get textContent(){return this.text+this.children.map(c=>c.textContent).join('');}
  set innerHTML(v){throw Error('unsafe HTML');} append(...nodes){this.children.push(...nodes);}
  setAttribute(k,v){this[k]=v;} addEventListener(n,fn){this.handlers[n]=fn;}
}
const root=new Element('section'),nav=new Element('nav'),events={},calls=[];
let revision='v1',enabled=true;
const status=()=>({revision,expression_enabled:enabled,model:'s2.1-pro-free',voice:'fixture',key_configured:true,auto_probability:.1});
const bridge={ready:async()=>{},apiGet:async()=>({data:status()}),apiPost:async(name,body)=>{
  calls.push(body);assert.equal(name,'panel_voice_action');
  if(body.action==='expression'){
    if(body.revision!==revision)return {status:'error',message:'refresh required'};
    enabled=body.enabled;revision='v3';return {data:status()};
  }
  if(body.action==='preview')return {data:{preview:{text:'[soft tone]<script>fixture</script>'}}};
  if(body.action==='audition')return {status:'error',message:'network down'};
  throw Error(body.action);
}};
vm.runInNewContext(source,{document:{getElementById:()=>root,createElement:t=>new Element(t),querySelector:()=>nav,addEventListener:(n,fn)=>events[n]=fn},window:{AstrBotPluginView:bridge,addEventListener(){}},URL,crypto:{randomUUID:()=> 'fixture-uuid'}});
const flatten=n=>[n,...n.children.flatMap(flatten)];
const findButton=text=>flatten(root).find(n=>n.tag==='button'&&n.textContent===text);
const tick=()=>new Promise(r=>setTimeout(r,0));
(async()=>{
  await tick();
  const toggle=flatten(root).find(n=>n.type==='checkbox'&&n.checked===true);
  assert.ok(toggle);toggle.checked=false;toggle.onchange();
  revision='v2';events['qqbot:view']({detail:'character'});await tick();
  assert.equal(toggle.checked,false,'background load must keep unsaved draft');
  await findButton('保存语气设置').onclick();assert.equal(calls.at(-1).revision,'v1');assert.equal(enabled,true);
  await findButton('刷新').onclick();assert.equal(toggle.checked,false);
  await findButton('保存语气设置').onclick();assert.equal(calls.at(-1).revision,'v2');assert.equal(enabled,false);
  await findButton('预览语气（不生成音频）').onclick();assert.equal(calls.at(-1).expression_enabled,false);
  assert.ok(flatten(root).find(n=>n.tag==='pre').textContent.includes('<script>fixture</script>'));
  await findButton('试听（不发到QQ）').onclick();assert.equal(toggle.disabled,false);
  assert.ok(flatten(root).filter(n=>n.tag==='button').every(n=>!n.disabled));
  console.log('voice controls: draft, conflict, preview escaping, failure recovery OK');
})().catch(e=>{console.error(e);process.exitCode=1;});
