const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
class Node {
 constructor(tag='div'){this.tag=tag;this.children=[];this.dataset={};this.scrollHeight=100;this.scrollTop=100;this.clientHeight=100;this.textContent='';this.style={};this.classList={add(){}};}
 appendChild(n){if(n.tag==='fragment'){for(const c of [...n.children])this.appendChild(c);return n;}n.parent=this;this.children.push(n);return n;}
 append(...nodes){nodes.forEach(n=>this.appendChild(n));}
 remove(){this.parent.children=this.parent.children.filter(x=>x!==this);}
 replaceChildren(...nodes){this.children=[];this.append(...nodes);}
 setAttribute(k,v){this[k]=v;}
}
const listeners=new Map(),timers=new Map();let timer=0;
const document={hidden:false,addEventListener:(k,v)=>listeners.set(k,v),removeEventListener:(k,v)=>{if(listeners.get(k)===v)listeners.delete(k);},createElement:t=>new Node(t),createDocumentFragment:()=>new Node('fragment')};
const context={document,setTimeout:f=>{timers.set(++timer,f);return timer;},clearTimeout:id=>timers.delete(id),console};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);
let connects=0,history=0,resolveHistory;
const component={data:()=>({localLogCache:[],maxLogCacheLength:1000,nextLogId:1,retryAttempts:0}),beforeUnmount(){document.removeEventListener('fullscreenchange',this.handleFullscreenChange);this.eventSource?.close();},methods:{
 connectSSE(){connects++;this.eventSource={close(){},onerror(){throw Error('late error');}};},refreshDisplay(){},
 appendLogContent(e,t){e.textContent=t;},isLogVisible(){return true;},
 buildLogElement(row){const n=new Node('pre');n.dataset.logId=String(row._renderId);this.appendLogContent(n,row.data);return n;},
 fetchLogHistory(){history++;return new Promise(r=>resolveHistory=r);},handleFullscreenChange(){}
}};
context.qqbotConsoleInstall(component);
function instance(){const term=new Node();return Object.assign(component.data(),component.methods,{$el:{querySelector:()=>term},term});}
(async()=>{
 const c=instance(),mount=component.mounted.call(c);component.beforeUnmount.call(c);resolveHistory();await mount;
 assert.equal(connects,0);assert.equal(listeners.size,0);
 const live=instance();for(let i=0;i<10000;i++)live.processNewLogs([{time:i,level:'INFO',data:'line '+i}]);
 assert.equal(timers.size,1);assert.equal(live.qqPending.length,1000);assert.equal(live.term.children.length,0);
 [...timers.values()].forEach(f=>f());timers.clear();
 assert.equal(live.localLogCache.length,1000);assert.equal(live.term.children.length,1000);
 live.processNewLogs(live.localLogCache);live.qqFlush();assert.equal(live.term.children.length,1000);
 document.hidden=true;live.processNewLogs([{time:10001,level:'INFO',data:'hidden'}]);live.qqFlush();
 assert.equal(live.term.children.at(-1).textContent,'line 9999');assert.equal(live.localLogCache.at(-1).data,'hidden');
 document.hidden=false;live.refreshDisplay();assert.equal(live.term.children.at(-1).textContent,'hidden');
 const raw='x'.repeat(50000)+'NEEDLE',element=new Node();live.searchKeyword='NEEDLE';live.appendLogContent(element,raw);
 assert.equal(element.children.length,2);assert.ok(element.children[0].textContent.length<=1002);assert.match(element.children[0].textContent,/NEEDLE/);
 element.children[1].onclick();assert.equal(element.children[0].textContent,raw);element.children[1].onclick();assert.ok(element.children[0].textContent.length<1600);
 component.beforeUnmount.call(live);live.processNewLogs([{time:99999,data:'late'}]);assert.equal(live.qqPending.length,0);
 console.log('console: 10,000 logs bounded, hidden rendering, full-text expand/search, async exit passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
