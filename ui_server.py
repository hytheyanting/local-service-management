#!/usr/bin/python3
"""Loopback-only presentation/API server for the registered local services."""
import argparse
import base64
import concurrent.futures
import json
import mimetypes
import pathlib
import re
import secrets
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ui_model import Store, Conflict
from registration import Registrar
from service_editor import ServiceEditor
from trash import TrashManager

ROOT = pathlib.Path(__file__).resolve().parent
UI = ROOT/'ui'
STORE = Store(ROOT/'state')
REGISTRAR = Registrar(ROOT, STORE)
EDITOR = ServiceEditor(ROOT, STORE, REGISTRAR)
TRASH = TrashManager(ROOT, STORE)
TOKEN = secrets.token_urlsafe(32)
JOBS = {}
JOBS_LOCK = threading.Lock()
EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=4)
ACTIONS = {'open','start','stop','restart','status','uninstall-preview','uninstall','restore'}


def services():
    return json.loads((ROOT/'services.json').read_text())


def bootstrap():
    records = services()
    state = STORE.read(records)
    statuses = {}
    try:
        for line in (ROOT/'entities.json').read_text().splitlines():
            row = json.loads(line)
            statuses[row['id']] = row['status']
    except (OSError, ValueError, KeyError):
        pass
    public = []
    for name, service in records.items():
        text = statuses.get(name, '状态更新中')
        trashed = (ROOT/'state'/(name+'.uninstalled.json')).is_file()
        if trashed:text='⚪ 已卸载 · 数据保留，可恢复'
        elif '已卸载' in text:text='状态更新中'
        entry = {k: service.get(k, '') for k in ['id','title','description','url','help','uninstall','directory','kind','open_label','icon']}
        info = state.get('service_info',{}).get(name)
        if service.get('ui_edited'):
            entry.update(display_name=service['title'],display_description=service['description'],display_help=service['help'])
        elif info is not None:
            entry.update(display_description=info['description'],display_help=info['help'])
        entry.update(trashed=trashed,can_open=bool(service.get('url') or service.get('open_command') or service['kind']=='app'),
                     category=STORE.category_for(service, state), status=text.lstrip('🟢⚪ '),
                     running=text.startswith('🟢'), uninstall_enabled=bool(service.get('uninstall_mode')))
        public.append(entry)
    try: age = time.time()-(ROOT/'state/registry-last-success.txt').stat().st_mtime
    except OSError: age = 9999
    return {'csrf': TOKEN, 'preferences': state, 'services': public, 'status_stale': age > 25}


def validate_action(value, records):
    name, action = value.get('service'), value.get('action')
    if not isinstance(name, str) or name not in records or not isinstance(action, str) or action not in ACTIONS:
        raise ValueError('服务或操作不存在。')
    if action in {'uninstall','uninstall-preview','restore'} and not records[name].get('uninstall_mode'):
        raise ValueError('该服务只提供卸载说明。')
    if action == 'uninstall' and value.get('confirm') is not True:
        raise ValueError('请先确认此服务的具体卸载范围。')
    return name, action


def launch_action(value):
    name, action = validate_action(value, services())
    with JOBS_LOCK:
        if any(j['service'] == name and j['state'] == 'running' for j in JOBS.values()):
            raise Conflict('该服务已有操作正在执行。')
        ident = secrets.token_hex(12)
        JOBS[ident] = {'id':ident,'service':name,'action':action,'state':'running','output':'','ok':None}
        for old in list(JOBS)[:-80]:
            if JOBS[old]['state'] != 'running': del JOBS[old]
    def execute():
        argv = ['/usr/bin/python3',str(ROOT/'control.py'),name,action]
        if action == 'uninstall': argv.append('--confirm')
        try:
            result = subprocess.run(argv, cwd=str(ROOT), capture_output=True, text=True, timeout=620)
            output = (result.stdout+'\n'+result.stderr).strip()[-20000:]
            ok = result.returncode == 0
        except Exception as error:
            output, ok = str(error), False
        with JOBS_LOCK:
            JOBS[ident].update(state='complete', output=output, ok=ok)
    EXECUTOR.submit(execute)
    return {'id':ident}


class Handler(BaseHTTPRequestHandler):
    server_version = 'LocalServices/1.0'

    def log_message(self, format, *args):
        # Access logs intentionally omit query strings, request bodies and tokens.
        pass

    def trusted_host(self):
        return self.headers.get('Host') in {f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}'}

    def respond(self, code, value, mime='application/json; charset=utf-8'):
        data = json.dumps(value, ensure_ascii=False).encode() if mime.startswith('application/json') else value
        self.send_response(code)
        self.send_header('Content-Type',mime)
        self.send_header('Content-Length',str(len(data)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Security-Policy',"default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'")
        self.end_headers()
        try:self.wfile.write(data)
        except (BrokenPipeError,ConnectionResetError):pass

    def do_GET(self):
        if not self.trusted_host():
            self.respond(403,{'error':'仅允许本机入口访问。'}); return
        path = urllib.parse.urlsplit(self.path).path
        try:
            if path == '/api/bootstrap':
                self.respond(200,bootstrap()); return
            if path.startswith('/api/trash/'):
                self.respond(200,TRASH.preview(path[len('/api/trash/'):]))
                return
            if path.startswith('/api/services/') and path.endswith('/edit'):
                ident=path[len('/api/services/'):-len('/edit')]
                self.respond(200,EDITOR.get(ident));return
            if path.startswith('/api/jobs/'):
                ident = path.rsplit('/',1)[-1]
                with JOBS_LOCK:
                    job = dict(JOBS[ident]) if ident in JOBS else None
                self.respond(200 if job else 404,job or {'error':'操作记录已过期。'}); return
            if path.startswith('/api/'):
                self.respond(404,{'error':'接口不存在。'});return
            if path.startswith('/media/'):
                name = path.rsplit('/',1)[-1]
                if not re.fullmatch(r'[a-f0-9]{64}\.(png|jpg|webp)',name):raise FileNotFoundError()
                target = ROOT/'state/uploads'/name
            else:
                path = urllib.parse.unquote(path)
                target = (UI/path.lstrip('/')).resolve()
                if UI.resolve() not in target.parents and target != UI.resolve():raise FileNotFoundError()
                if path == '/' or path.startswith('/dashboards/'):
                    target = UI/'index.html'
            if not target.is_file():raise FileNotFoundError()
            mime = mimetypes.guess_type(str(target))[0] or 'application/octet-stream'
            if mime in ['text/html','text/css','application/javascript','text/javascript']:mime+='; charset=utf-8'
            self.respond(200,target.read_bytes(),mime)
        except FileNotFoundError:self.respond(404,{'error':'文件不存在。'})
        except Conflict as error:self.respond(409,{'error':str(error)})
        except ValueError as error:self.respond(400,{'error':str(error)})
        except Exception:self.respond(500,{'error':'本机登记暂时无法读取，请稍后刷新。'})

    def do_POST(self):
        expected = f'http://{self.headers.get("Host", "")}'
        if not self.trusted_host() or self.headers.get('Origin') != expected or not secrets.compare_digest(self.headers.get('X-Local-CSRF',''),TOKEN):
            self.respond(403,{'error':'请求来源校验失败，请刷新本机页面。'});return
        try:
            length = int(self.headers.get('Content-Length','0'))
            if not 0 < length <= 12*1024*1024:raise ValueError('请求内容过大或为空。')
            if not self.headers.get('Content-Type','').startswith('application/json'):raise ValueError('请求格式不正确。')
            value = json.loads(self.rfile.read(length))
            if not isinstance(value,dict):raise ValueError('请求格式不正确。')
            path = urllib.parse.urlsplit(self.path).path
            if path == '/api/preferences':
                self.respond(200,{'preferences':STORE.save_preferences(services(),value)})
            elif path == '/api/assign':
                state=STORE.assign(services(),value.get('revision'),value.get('service'),value.get('category'))
                self.respond(200,{'preferences':state})
            elif path == '/api/wallpaper':
                data=base64.b64decode(value.get('data',''),validate=True)
                self.respond(200,{'wallpaper':STORE.upload(data,value.get('mime'))})
            elif path == '/api/services/edit/validate':
                self.respond(200,EDITOR.validate(value.get('service'),value.get('version'),value.get('form')))
            elif path == '/api/services/edit':
                service=EDITOR.save(value.get('service'),value.get('version'),value.get('form'),value.get('validation'))
                self.respond(200,{'service':{'id':service['id'],'title':service['title']}})
            elif path == '/api/trash/delete':
                self.respond(200,TRASH.delete(value.get('service'),value.get('version'),value.get('selected'),value.get('confirmation')))
            elif path == '/api/services/info':
                state = STORE.edit_service_info(services(),value.get('revision'),value.get('service'),value.get('info'))
                self.respond(200,{'preferences':state})
            elif path == '/api/services/validate':
                self.respond(200,REGISTRAR.validate(value.get('form')))
            elif path == '/api/services':
                service = REGISTRAR.add(value.get('form'), value.get('validation'))
                self.respond(201,{'service':{'id':service['id'],'title':service['title']}})
            elif path == '/api/actions':
                self.respond(202,launch_action(value))
            else:self.respond(404,{'error':'接口不存在。'})
        except Conflict as error:self.respond(409,{'error':str(error)})
        except (ValueError,TypeError,AttributeError,KeyError) as error:self.respond(400,{'error':str(error)})
        except Exception:self.respond(500,{'error':'保存失败，原设置已保留。'})


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=1337);args=parser.parse_args()
    server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler)
    server.daemon_threads=True
    print(f'本机服务管理界面 http://127.0.0.1:{args.port}/',flush=True)
    server.serve_forever()


if __name__=='__main__':main()
