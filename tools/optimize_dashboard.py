"""Guarded console patch; preserves upstream authorization and sandboxing."""
import json
from pathlib import Path
import sysconfig

ROOT=Path(__file__).resolve().parents[1]
ASSET=Path(sysconfig.get_path('purelib'))/'astrbot/dashboard/dist/assets/ConsoleDisplayer-BOqWH71c.js'
MARKER='/* QQBOT_CONSOLE_V1 */\n'
ANCHOR='},ie=Object.assign(ae,'

def patch_source(source):
    if source.startswith(MARKER):return source
    if source.count(ANCHOR)!=1 or 'name:"ConsoleDisplayer"' not in source or 'maxLogCacheLength:1e3' not in source:
        raise ValueError('unknown_dashboard_build')
    # All lifecycle and native auth entry points must exist before extending.
    for fragment in ('async mounted(){await this.fetchLogHistory()', 'beforeUnmount(){document.removeEventListener',
                     'processNewLogs(e){', 'appendLogContent(e,l){', 'new z.EventSourcePolyfill(k.liveUrl()'):
        if source.count(fragment)!=1:raise ValueError('unknown_dashboard_build')
    extension=Path(__file__).with_name('dashboard_console.js').read_text('utf-8')
    return MARKER+source.replace(ANCHOR,'};\n'+extension+'\nqqbotConsoleInstall(ae);\nconst ie=Object.assign(ae,')

def inspect_console(repair=False,asset=ASSET,backup_dir=None):
    result={'name':'控制台日志渲染','ok':False}
    try:
        source=asset.read_text('utf-8');updated=patch_source(source)
        if updated==source:return {**result,'ok':True,'detail':'日志批量渲染与退出清理已启用'}
        if not repair:return {**result,'detail':'可安装日志渲染优化'}
        backup=(backup_dir or ROOT/'backups/dashboard-console')/asset.name
        backup.parent.mkdir(parents=True,exist_ok=True)
        if not backup.exists():backup.write_bytes(asset.read_bytes())
        if asset.read_text('utf-8')!=source:raise OSError('asset_changed')
        tmp=asset.with_suffix('.qqbot.tmp');tmp.write_text(updated,'utf-8');tmp.replace(asset)
        return {**result,'ok':True,'detail':'已备份并优化，刷新页面后生效'}
    except (OSError,ValueError) as exc:return {**result,'detail':str(exc)}

if __name__=='__main__':
    result=inspect_console(True);print(json.dumps(result,ensure_ascii=True));raise SystemExit(not result['ok'])
