"""Configure an explicitly authorized local reading extension; never print its token."""
import argparse,json,re,secrets
from pathlib import Path

def configure(data_root,extension_id,extension_root):
    if not re.fullmatch('[a-p]{32}',extension_id):raise ValueError('需要浏览器扩展管理页中的完整扩展 ID')
    data=Path(data_root).resolve();extension=Path(extension_root).resolve()
    if not (data/'cmd_config.json').is_file():raise ValueError('请选择已有的 AstrBot data 目录')
    if not (extension/'manifest.json').is_file():raise ValueError('未找到阅读扩展源码')
    path=data/'plugin_data/astrbot_plugin_proactive_agent/web-reader/bridge.json'
    old=json.loads(path.read_text('utf-8')) if path.exists() else {}
    token=old.get('token') if len(str(old.get('token','')))>=40 else secrets.token_urlsafe(48)
    cfg={'enabled':True,'port':6197,'extension_origin':'chrome-extension://'+extension_id,'token':token}
    path.parent.mkdir(parents=True,exist_ok=True)
    local=extension/'local-config.js'
    local.write_bytes(('const READER_CONFIG='+json.dumps({'endpoint':'http://127.0.0.1:6197','token':token})+';\n').encode())
    path.write_bytes(json.dumps(cfg,indent=2).encode())
    return path,local

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--data-root',required=True);parser.add_argument('--extension-id',required=True)
    parser.add_argument('--extension-root',default=str(Path(__file__).resolve().parent/'browser_reader_extension'));args=parser.parse_args()
    outputs=configure(args.data_root,args.extension_id,args.extension_root)
    print('已保存本机配置；请重新加载扩展并正常重启 AstrBot。令牌未输出。')
    for path in outputs:print(path)
