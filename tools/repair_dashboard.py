"""Guarded AstrBot 4.29.1 plugin-view compatibility patch, retaining auth/sandbox.

Fetch the scoped view as authenticated text and render it in the same opaque
sandbox with a scoped asset base URL. This avoids blank navigation in the local
Edge console. Unknown dashboard builds are reported, never blindly rewritten.
"""
import json
from pathlib import Path
import sysconfig

ROOT = Path(__file__).resolve().parents[1]
ASSET = Path(sysconfig.get_path("purelib")) / "astrbot/dashboard/dist/assets/PluginViewPage-DVqXRTsa.js"
MARKER = "/* QQBOT_VIEW_EMBED_V1 */\n"
REPLACEMENTS = [
    ('u.searchParams.set("theme",Y.value),T.value=u.pathname+u.search+u.hash',
     'u.searchParams.set("theme",Y.value),T.value=u.pathname+u.search+u.hash;'
     'const localView=await L.get(T.value,{responseType:"text"});'
     'T.value=String(localView.data).replace(/<head\\b[^>]*>/i,tag=>tag+\'<base href="\'+u.href.replace(/"/g,"&quot;")+\'">\')'),
    ('xe=["src"]', 'xe=["srcdoc"]'),
    ('src:T.value', 'srcdoc:T.value'),
]


def inspect_view(repair=False, asset=ASSET, backup_dir=None):
    result = {"name": "控制台插件页面", "ok": False, "repair": None}
    try:
        source = asset.read_text(encoding="utf-8")
        if source.startswith(MARKER) and all(new in source for _, new in REPLACEMENTS):
            return {**result, "ok": True, "detail": "面板加载兼容正常，原有鉴权和沙箱保留"}
        if not all(source.count(old) == 1 for old, _ in REPLACEMENTS):
            return {**result, "detail": "控制台版本或文件已变化，需核对兼容补丁；未改写未知文件"}
        if not repair:
            return {**result, "repair": "Dashboard", "detail": "需要恢复已验证的插件页面加载兼容"}
        backup = (backup_dir or ROOT / "backups/dashboard-compat") / asset.name
        backup.parent.mkdir(parents=True, exist_ok=True)
        if not backup.exists():
            backup.write_bytes(asset.read_bytes())
        updated = source
        for old, new in REPLACEMENTS:
            updated = updated.replace(old, new)
        if asset.read_text(encoding="utf-8") != source:
            raise OSError("file changed during repair")
        temp = asset.with_suffix(".qqbot.tmp")
        temp.write_text(MARKER + updated, encoding="utf-8")
        temp.replace(asset)
        return {**result, "ok": True, "detail": "已备份并恢复面板加载，刷新控制台后生效"}
    except OSError as exc:
        return {**result, "detail": "控制台兼容检查失败：" + type(exc).__name__}


if __name__ == "__main__":
    result = inspect_view(repair=True)
    print(json.dumps(result, ensure_ascii=True))
    raise SystemExit(0 if result["ok"] else 1)
