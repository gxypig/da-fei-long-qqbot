/* AstrBot 4.29.1 console lifecycle/rendering extension. Auth and filters stay native. */
function qqbotConsoleInstall(component) {
  const methods = component.methods, previousData = component.data;
  const connect = methods.connectSSE;
  const append = methods.appendLogContent, unmount = component.beforeUnmount;
  component.data = function () {
    return {...previousData.call(this), qqDisposed:false, qqPending:[], qqTimer:null,
      qqHiddenDirty:false, qqVisibility:null};
  };
  component.mounted = async function () {
    this.qqVisibility = () => {
      if (!document.hidden && !this.qqDisposed) {
        this.qqFlush();
        if (this.qqHiddenDirty) { this.qqHiddenDirty=false; this.refreshDisplay(); }
      }
    };
    document.addEventListener('visibilitychange', this.qqVisibility);
    document.addEventListener('fullscreenchange', this.handleFullscreenChange);
    await this.fetchLogHistory();
    if (this.qqDisposed) return;
    if (this.workspaceMode) await this.fetchPluginDisplayNames();
    if (!this.qqDisposed) this.connectSSE();
  };
  component.beforeUnmount = function () {
    this.qqDisposed=true;
    clearTimeout(this.qqTimer); this.qqTimer=null; this.qqPending=[];
    document.removeEventListener('visibilitychange', this.qqVisibility);
    unmount.call(this);
  };
  methods.connectSSE = function () {
    if (this.qqDisposed) return;
    connect.call(this);
    const onerror=this.eventSource?.onerror;
    if (this.eventSource && onerror) this.eventSource.onerror=(e)=>{if(!this.qqDisposed)onerror(e);};
    // The history request above already filled the cache. Fetch again only on
    // reconnect, because native reconnect does not pass Last-Event-ID itself.
    if (this.eventSource) this.eventSource.onopen=()=>{
      if (this.qqDisposed) return;
      const reconnect=this.retryAttempts>0; this.retryAttempts=0;
      if (reconnect) this.fetchLogHistory();
    };
  };
  methods.processNewLogs = function (logs) {
    if (this.qqDisposed || !logs?.length) return;
    this.qqPending.push(...logs.slice(-this.maxLogCacheLength));
    if (this.qqPending.length>this.maxLogCacheLength)
      this.qqPending.splice(0,this.qqPending.length-this.maxLogCacheLength);
    if (this.qqTimer===null) this.qqTimer=setTimeout(()=>{
      this.qqTimer=null; this.qqFlush();
    },50);
  };
  methods.qqFlush = function () {
    if (this.qqDisposed || !this.qqPending.length) return;
    const incoming=this.qqPending.splice(0), key=x=>JSON.stringify([x.time,x.level,x.data]);
    const known=new Set(this.localLogCache.map(key)), added=[];
    for (const row of incoming) {
      const identity=key(row);
      if (!known.has(identity)) {
        known.add(identity); row._renderId=this.nextLogId++; added.push(row);
      }
    }
    if (!added.length) return;
    const last=this.localLogCache.at(-1)?.time ?? -Infinity;
    const merged=[...this.localLogCache,...added].sort((a,b)=>a.time-b.time)
      .slice(-this.maxLogCacheLength);
    this.localLogCache=merged;
    if (document.hidden) { this.qqHiddenDirty=true; return; }
    const term=this.$el?.querySelector('#term');
    if (!term) return;
    if (this.qqHiddenDirty || added.some(x=>x.time<last)) {
      this.qqHiddenDirty=false; this.refreshDisplay(); return;
    }
    const bottom=term.scrollHeight-term.scrollTop-term.clientHeight<=24;
    const retained=new Set(merged.map(x=>String(x._renderId)));
    for (const child of [...term.children]) if (!retained.has(child.dataset.logId)) child.remove();
    const fragment=document.createDocumentFragment();
    for (const row of added.sort((a,b)=>a.time-b.time))
      if (retained.has(String(row._renderId)) && this.isLogVisible(row))
        fragment.appendChild(this.buildLogElement(row));
    term.appendChild(fragment);
    if (bottom) term.scrollTop=term.scrollHeight;
  };
  methods.refreshDisplay = function () {
    if (this.qqDisposed) return;
    if (document.hidden) { this.qqHiddenDirty=true; return; }
    // Native implementation uses a global id; scope the term to this instance.
    const term=this.$el?.querySelector('#term');
    if (!term) return;
    const bottom=term.scrollHeight-term.scrollTop-term.clientHeight<=24, top=term.scrollTop;
    const fragment=document.createDocumentFragment();
    for (const row of this.localLogCache)
      if (this.isLogVisible(row)) fragment.appendChild(this.buildLogElement(row));
    term.replaceChildren(fragment); term.scrollTop=bottom?term.scrollHeight:top;
  };
  methods.appendLogContent = function (element, text) {
    if (text.length<=1600) { append.call(this,element,text); return; }
    const body=document.createElement('span'), button=document.createElement('button');
    button.type='button'; button.style.cssText='display:block;color:inherit;text-decoration:underline;cursor:pointer';
    let expanded=false;
    const render=()=>{
      body.replaceChildren();
      const match=this.searchKeyword?text.toLowerCase().indexOf(this.searchKeyword.toLowerCase()):-1;
      const start=Math.max(0,match-160);
      // Full raw text remains cached and searchable. Rendering the expanded
      // content as text avoids a pathological number of highlight spans.
      if (expanded) body.textContent=text;
      else append.call(this,body,(start?'…':'')+text.slice(start,start+1000)+'…');
      button.textContent=expanded?'收起长日志':`展开完整日志（${text.length} 字符）`;
      button.setAttribute('aria-expanded',String(expanded));
    };
    button.onclick=()=>{expanded=!expanded;render();};render();element.append(body,button);
  };
}
