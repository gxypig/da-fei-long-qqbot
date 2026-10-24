"""Small, guarded patch for the pinned AstrBot 4.29.1 conversation workspace.

Original dashboard remains AGPL-3.0: https://github.com/AstrBotDevs/AstrBot
Unknown bundles are rejected. Authentication, editing and raw context are retained.
"""
from pathlib import Path
import sysconfig

ASSET = Path(sysconfig.get_path('purelib')) / 'astrbot/dashboard/dist/assets/ConversationWorkspacePage-5vJvHc6a.js'
MARKER = '/* QQBOT_LIVE_WORKSPACE_V1 */\n'
EXTRA = '''const qqLive=f([]),qqMode=f(true),qqTime=f(""),qqError=f("");let qqTimer=null,qqBusy=false,qqTicks=0,qqClosed=false;
function qqData(b){qqLive.value=Array.isArray(b.qqbot_live_history)?b.qqbot_live_history:[];qqError.value=b.qqbot_live_error?"本地收发记录保存异常；即时显示仍可用":"";qqTime.value=new Date().toLocaleTimeString()}
async function qqDetail(){if(!v.value||te.value||R.value)return;const chosen=v.value,id=k(chosen),seq=Q.value;const response=await Z.get(chosen.user_id,chosen.cid);if(seq!==Q.value||!v.value||k(v.value)!==id)return;if(response.data.status!=="ok")throw new Error(response.data.message||"读取失败");const data=response.data.data||{},history=typeof data.history==="string"?JSON.parse(data.history):data.history||[];if(!Array.isArray(history))throw new Error("记录格式错误");W.value=history;qqData(data)}
async function qqRefresh(manual=false){if(qqBusy)return;qqBusy=true;try{await Promise.all([manual||!v.value||++qqTicks%5===0?H():Promise.resolve(),qqDetail()]);qqTime.value=new Date().toLocaleTimeString();if(manual)U("已刷新列表和当前对话 · "+qqTime.value,"success")}catch(e){qqError.value="刷新失败，下次自动重试";if(manual)U(qqError.value,"error")}finally{qqBusy=false}}
function qqPoll(){if(qqClosed)return;qqTimer=window.setTimeout(async()=>{if(!document.hidden&&!X.value&&!oe.value)await qqRefresh();qqPoll()},1000)}
'''
REPLACEMENTS = [
    (',Ia(async()=>{await Promise.all([fa(),H()])})', ';' + EXTRA + 'Ia(async()=>{await Promise.all([fa(),H()]);qqPoll()})'),
    ('Ta(()=>{var t;ne(),', 'Ta(()=>{var t;qqClosed=true;window.clearTimeout(qqTimer);ne(),'),
    ('onClick:H', 'onClick:()=>qqRefresh(true)'),
    ('W.value=q}catch(u)', 'W.value=q;qqData(b)}catch(u)'),
    ('Le.value=window.scrollY,X.value=!1,v.value=t,W.value=[],te.value=!0', 'Le.value=window.scrollY,X.value=!1,v.value=t,W.value=[],qqLive.value=[],qqError.value="",qqMode.value=true,te.value=!0'),
    ('te.value?(r(),c("div",Ws', 'l("div",{style:"padding:12px;display:flex;gap:12px;align-items:center;flex-wrap:wrap",role:"status"},[l("strong",null,"最新在前 · 每秒自动刷新"),l("button",{type:"button",onClick:()=>qqMode.value=!qqMode.value,style:"border:1px solid #889;padding:6px 10px;border-radius:8px"},qqMode.value?"查看模型上下文":"查看即时收发",1),l("span",null,(qqMode.value&&qqLive.value.length?"即时收发 · 最近200条":"模型上下文")+" · 更新于 "+qqTime.value,1),l("span",{style:"color:#b45309"},qqError.value,1)]),te.value?(r(),c("div",Ws'),
    ('messages:W.value},null,8,["messages"]', 'messages:[...(qqMode.value&&qqLive.value.length?qqLive.value:W.value)].reverse()},null,8,["messages"]'),
]


def repair(asset=ASSET):
    source = asset.read_text(encoding='utf-8')
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
    backup = asset.with_suffix('.js.qqbot-original')
    if not backup.exists():
        backup.write_text(source, encoding='utf-8')
    temp = asset.with_suffix('.qqbot.tmp')
    temp.write_text(MARKER + updated, encoding='utf-8')
    if asset.read_text(encoding='utf-8') != source:
        raise OSError('控制台文件同时被修改')
    temp.replace(asset)
    return True
