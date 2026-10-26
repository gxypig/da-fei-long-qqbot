"""Small, guarded patch for the pinned AstrBot 4.29.1 conversation workspace.

Original dashboard remains AGPL-3.0: https://github.com/AstrBotDevs/AstrBot
Unknown bundles are rejected. Authentication, editing and raw context are retained.
"""
from pathlib import Path
import sysconfig
import hashlib
import uuid

ASSET = Path(sysconfig.get_path('purelib')) / 'astrbot/dashboard/dist/assets/ConversationWorkspacePage-5vJvHc6a.js'
MARKER = '/* QQBOT_LIVE_WORKSPACE_V2 */\n'
EXTRA = '''const qqLive=f([]),qqMode=f(true),qqTime=f(""),qqError=f("");let qqTimer=null,qqBusy=false,qqTicks=0,qqClosed=false,qqCursor=0,qqInstance="",qqDelay=1000;
function qqData(b){qqLive.value=Array.isArray(b.qqbot_live_history)?b.qqbot_live_history:[];qqCursor=0;qqInstance="";qqError.value=b.qqbot_live_error?"本地收发记录保存异常；即时显示仍可用":"";qqTime.value=new Date().toLocaleTimeString()}
async function qqDetail(manual=false){if(!v.value||te.value||R.value)return;const chosen=v.value,id=k(chosen),seq=Q.value,live=qqMode.value;if(!live&&!manual)return;const response=live?await qqHttp.get("/api/plug/astrbot_plugin_proactive_agent/panel_live_messages",{params:{origin:chosen.user_id,after:qqCursor,instance:qqInstance}}):await Z.get(chosen.user_id,chosen.cid);if(qqClosed||seq!==Q.value||!v.value||k(v.value)!==id||live!==qqMode.value)return;if(response.data.status!=="ok")throw new Error(response.data.message||"读取失败");const data=response.data.data||{};if(live){if(!Array.isArray(data.rows))throw new Error("增量格式错误");qqLive.value=(data.reset?data.rows:[...qqLive.value,...data.rows]).slice(-200);qqCursor=data.cursor;qqInstance=data.instance;qqError.value=data.storage?.last_error?"本地保存异常，即时消息仍显示":data.storage?.dropped?"历史落盘缺口 "+data.storage.dropped+" 条（未因恢复而清零）":""}else{const history=typeof data.history==="string"?JSON.parse(data.history):data.history||[];if(!Array.isArray(history))throw new Error("记录格式错误");W.value=history}}
async function qqRefresh(manual=false){if(qqBusy||qqClosed)return;qqBusy=true;try{if(manual||!v.value||++qqTicks%10===0)await H();await qqDetail(manual);qqTime.value=new Date().toLocaleTimeString();qqDelay=1000;if(manual)U("已刷新列表和当前对话 · "+qqTime.value,"success")}catch(e){qqDelay=Math.min(30000,qqDelay*2);qqError.value="刷新失败，"+Math.round(qqDelay/1000)+"秒后重试";if(manual)U(qqError.value,"error")}finally{qqBusy=false}}
function qqPoll(){if(qqClosed)return;qqTimer=window.setTimeout(async()=>{if(!document.hidden&&!X.value&&!oe.value)await qqRefresh();qqPoll()},qqDelay)}
'''
REPLACEMENTS = [
    ('import{U as aa,','import{bH as qqHttp,U as aa,'),
    (',Ia(async()=>{await Promise.all([fa(),H()])})', ';' + EXTRA + 'Ia(async()=>{await Promise.all([fa(),H()]);qqPoll()})'),
    ('Ta(()=>{var t;ne(),', 'Ta(()=>{var t;qqClosed=true;window.clearTimeout(qqTimer);ne(),'),
    ('onClick:H', 'onClick:()=>qqRefresh(true)'),
    ('W.value=q}catch(u)', 'W.value=q;qqData(b)}catch(u)'),
    ('Le.value=window.scrollY,X.value=!1,v.value=t,W.value=[],te.value=!0', 'Le.value=window.scrollY,X.value=!1,v.value=t,W.value=[],qqLive.value=[],qqError.value="",qqMode.value=true,te.value=!0'),
    ('te.value?(r(),c("div",Ws', 'l("div",{style:"padding:12px;display:flex;gap:12px;align-items:center;flex-wrap:wrap",role:"status"},[l("strong",null,"最新在前 · 增量刷新，隐藏暂停"),l("button",{type:"button",onClick:()=>{qqMode.value=!qqMode.value;qqRefresh(true)},style:"border:1px solid #889;padding:6px 10px;border-radius:8px"},qqMode.value?"查看模型上下文":"查看即时收发",1),l("span",null,(qqMode.value&&qqLive.value.length?"即时收发 · 最近200条":"模型上下文（手动刷新）")+" · 更新于 "+qqTime.value,1),l("span",{style:"color:#b45309"},qqError.value,1)]),te.value?(r(),c("div",Ws'),
    ('messages:W.value},null,8,["messages"]', 'messages:[...(qqMode.value&&qqLive.value.length?qqLive.value:W.value)].reverse()},null,8,["messages"]'),
]


def repair(asset=ASSET):
    source = asset.read_text(encoding='utf-8')
    observed=source
    backup = asset.with_suffix('.js.qqbot-original')
    if source.startswith('/* QQBOT_LIVE_WORKSPACE_V1 */'):
        if hashlib.sha256(source.encode()).hexdigest()!='8af1f789227a0203c73af005af2947b062efbeb4a65bb34b77b43148a729d682' or not backup.is_file():
            raise ValueError('旧补丁不匹配，保留文件并检查原始备份')
        source=backup.read_text(encoding='utf-8')
    if source.startswith(MARKER):
        if not all(new in source for _, new in REPLACEMENTS):
            raise ValueError('已装补丁被更改，请核对控制台版本')
        return False
    for old, _ in REPLACEMENTS:
        if source.count(old) != (2 if old == 'onClick:H' else 1):
            raise ValueError('不支持的控制台版本，保留原文件：' + old[:32])
    updated = source
    for old, new in REPLACEMENTS:
        updated = updated.replace(old, new)
    if not backup.exists():
        backup.write_text(source, encoding='utf-8')
    temp = asset.with_name(asset.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        temp.write_text(MARKER + updated, encoding='utf-8')
        if asset.read_text(encoding='utf-8') != observed:
            raise OSError('控制台文件同时被修改')
        temp.replace(asset)
    finally:temp.unlink(missing_ok=True)
    return True
