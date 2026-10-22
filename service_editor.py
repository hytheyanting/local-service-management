"""Round-trip full service forms without running lifecycle commands."""
import copy
import datetime
import fcntl
import hashlib
import json
import os
import pathlib
import re
import secrets
import shlex
import shutil
import subprocess
import tempfile
import time
import threading
import urllib.parse

from control import ENV, listeners, owned, process_identity
from registration import check_unique, HTTP
from ui_model import Conflict

KEEP = '__KEEP_EXISTING__'
RUNTIME_KEYS = {'start','stop','app','executable','label','plist','inspect','process_tokens','port',
    'colima_profile','stop_profile','requires_paths','uninstall_mode','uninstall_command','preserved_mounts','container_aliases'}


def atomic_bytes(path, data):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as f:
            f.write(data);temporary=pathlib.Path(f.name)
        temporary.chmod(0o600);temporary.replace(path)
    finally:
        if temporary and temporary.exists():temporary.unlink()


def command(raw, directory):
    if not isinstance(raw,str) or len(raw)>12000 or any(x in raw for x in ['&&','||',';','|','>','<','`','$(','\x00']):
        raise ValueError('命令需为一个程序及参数，不使用 shell 组合、管道或重定向。')
    argv=shlex.split(raw)
    if not argv or (pathlib.Path(argv[0]).name in ['sh','bash','zsh'] and '-c' in argv):raise ValueError('请填写程序及参数，不使用 shell -c。')
    executable=shutil.which(str(pathlib.Path(argv[0]).expanduser()),path=ENV['PATH'])
    if not executable:raise ValueError('命令的启动程序不存在或不可执行。')
    argv[0]=executable
    for arg in argv[1:]:
        if arg.endswith(('.py','.js','.mjs','.sh','.command')) and not (pathlib.Path(directory)/arg).is_file():
            raise ValueError('命令引用的脚本文件不存在。')
    return argv


class ServiceEditor:
    def __init__(self, root, store, registrar):
        self.root,self.store,self.registrar=pathlib.Path(root),store,registrar
        self.tickets={}
        self.ticket_lock=threading.Lock()

    def marker(self, ident):return self.root/'state'/(ident+'.uninstalled.json')

    def version(self, service, state):
        marker=self.marker(service['id'])
        return self.registrar.fingerprint({'service':service,'category':self.store.category_for(service,state),
            'info':state.get('service_info',{}).get(service['id']),
            'marker':hashlib.sha256(marker.read_bytes()).hexdigest() if marker.exists() else None})

    def snapshot(self, ident):
        records=json.loads((self.root/'services.json').read_text())
        if not isinstance(ident,str) or ident not in records:raise ValueError('服务不存在，请刷新列表。')
        state=self.store.read(records)
        return records,state,records[ident]

    @staticmethod
    def form(service, state, store):
        def arg(name, argv=None, default=''):
            argv=service.get('start',[]) if argv is None else argv
            return argv[argv.index(name)+1] if name in argv and argv.index(name)+1<len(argv) else default
        argv=service.get('start',[]);down=service.get('uninstall_command',[])
        structured=service['kind']=='compose' and bool(argv) and pathlib.Path(argv[0]).name=='docker' and 'compose' in argv
        info=state.get('service_info',{}).get(service['id'],{})
        result={k:str(service.get(k,'') or '') for k in ['title','kind','directory','url','health','app','plist','token_env','open_label']}
        result.update(icon=service.get('icon','default'),description=info.get('description',service.get('description','')),help=info.get('help',service.get('help','')),
            category=store.category_for(service,state),command_mode='structured' if structured else 'commands',
            context=arg('--context',default=arg('--context',down,default='default')),
            project=arg('-p',default=arg('--project-name')),compose_file=arg('-f',default=arg('--file')),
            env_file=arg('--env-file'),start_command=shlex.join(argv),stop_command=shlex.join(service.get('stop',[])),
            uninstall_command=shlex.join(down),uninstall_text=service.get('uninstall',''),
            enable_uninstall=bool(service.get('uninstall_mode')),preserves='\n'.join(service.get('preserves',[])),
            confirm_persistence=False,stop_profile=bool(service.get('stop_profile')),colima_profile=service.get('colima_profile',''),
            port=str(service.get('port','')),process_tokens=shlex.join(service.get('process_tokens',[])),
            container=service.get('inspect',[''])[-1],health_codes=','.join(map(str,service.get('health_codes',[200]))),
            open_command=shlex.join(service.get('open_command',[])),requires_paths='\n'.join(service.get('requires_paths',[])),
            extra_executables='\n'.join(service.get('extra_executables',[])),
            environment_json=json.dumps({k:KEEP for k in service.get('environment',{})},ensure_ascii=False,indent=2))
        return result

    def get(self, ident):
        records,state,service=self.snapshot(ident)
        return {'id':ident,'version':self.version(service,state),'form':self.form(service,state,self.store),
                'uninstalled':self.marker(ident).exists()}

    @staticmethod
    def operations(form):
        common=['kind','directory','url','health','health_codes','open_command','open_label','requires_paths','enable_uninstall']
        kind=form.get('kind')
        if kind in ['process','voice']:common+=['start_command','port','process_tokens','token_env','environment_json']
        elif kind=='compose':
            common+=['command_mode','stop_profile']
            common+=['start_command','stop_command','colima_profile'] if form.get('command_mode')=='commands' else ['context','project','compose_file','env_file']
        elif kind=='app':common+=['app','extra_executables']
        elif kind=='container':common+=['context','container']
        elif kind=='launchd':common+=['plist']
        if form.get('enable_uninstall'):common+=['preserves','uninstall_command'] if form.get('command_mode')=='commands' and kind=='compose' else ['preserves']
        return {k:form.get(k,False if k in ['enable_uninstall','stop_profile'] else '') for k in common}

    @classmethod
    def identity(cls, form):
        values=cls.operations(form)
        for k in ['url','health','health_codes','open_command','open_label','requires_paths','environment_json','token_env','extra_executables','enable_uninstall','preserves','uninstall_command']:
            values.pop(k,None)
        return values

    def source_signature(self, service):
        paths=list(service.get('requires_paths',[]))
        if service['kind']=='app':paths += [str(pathlib.Path(service['app'])/'Contents/Info.plist'),service.get('executable','')]
        if service['kind'] in ['process','voice']:
            paths += [service['start'][0]]
            paths += [str(pathlib.Path(service['directory'])/arg) for arg in service['start'][1:] if arg.endswith(('.py','.js','.sh','.command','.mjs'))]
        sources=[]
        for raw in sorted(set(filter(None,paths))):
            p=pathlib.Path(raw)
            if p.exists():
                st=p.stat();row=[str(p.resolve()),st.st_ino,st.st_size,st.st_mtime_ns]
                if p.is_file() and st.st_size<8*1024*1024:row.append(hashlib.sha256(p.read_bytes()).hexdigest())
                sources.append(row)
            else:sources.append([raw,'missing'])
        return self.registrar.fingerprint({'service':service,'sources':sources})

    def candidate(self, ident, version, form):
        if not isinstance(form,dict):raise ValueError('编辑表单格式不正确。')
        records,state,old=self.snapshot(ident)
        if version!=self.version(old,state):raise Conflict('此服务已被其他页面或程序修改，请重新打开编辑。')
        base=self.form(old,state,self.store);checks=[]
        def note(level,field,message):checks.append({'level':level,'field':field,'message':message})
        def text(key,required=False,limit=4000):
            value=form.get(key,'')
            if not isinstance(value,str) or len(value)>limit or '\x00' in value:raise ValueError('字段无效或过长：'+key)
            value=value.strip()
            if required and not value:raise ValueError('请填写：'+key)
            return value
        changed=self.operations(form)!=self.operations(base)
        deployment_changed=self.identity(form)!=self.identity(base)
        candidate=copy.deepcopy(old)
        try:
            title=text('title',True,80);description=text('description',True,240);help_text=text('help')
            if any(ord(c)<32 for c in title+description):raise ValueError('名称和用途简介不能包含换行或控制字符。')
            category=next((c for c in state['categories'] if c['id']==form.get('category')),None)
            if not category:raise ValueError('分类不存在，请重新选择。')
            for field in ['url','health']:
                value=text(field)
                if value:
                    parsed=urllib.parse.urlsplit(value)
                    if parsed.scheme not in ['http','https'] or not parsed.hostname or parsed.username or parsed.password or any(c.isspace() for c in value):raise ValueError('地址需为完整 HTTP(S) URL，不包含账号密码。')
                    parsed.port
            if self.marker(ident).exists() and (deployment_changed or form.get('enable_uninstall')!=base.get('enable_uninstall')):raise ValueError('此服务已卸载。为保留原恢复记录，请先恢复服务，再修改部署身份。')
            if old['kind']=='compose' and form.get('command_mode')=='commands' and form.get('uninstall_command','')!=base.get('uninstall_command',''):
                raise ValueError('自定义卸载目标不能单独更换；请改用标准 Compose 配置并验证持久化挂载。')
            rebuild=deployment_changed or form.get('enable_uninstall')!=base.get('enable_uninstall') or (form.get('enable_uninstall') and form.get('preserves')!=base.get('preserves'))
            if rebuild:
                kind=text('kind',True)
                if kind in ['compose','voice'] and (kind=='voice' or form.get('command_mode')=='commands'):
                    directory=pathlib.Path(text('directory',True)).expanduser()
                    if not directory.is_absolute() or not directory.is_dir():raise ValueError('项目目录不存在。')
                    start=command(text('start_command',True,12000),directory)
                    fresh=dict(kind=kind,directory=str(directory.resolve()),start=start)
                    if kind=='compose':fresh['stop']=command(text('stop_command',True,12000),directory)
                    else:
                        port=int(text('port',True))
                        if not 1<=port<=65535:raise ValueError('监听端口需为1–65535。')
                        fresh['port']=port
                        for pid in listeners(port):
                            if not owned(fresh,*process_identity(pid)):raise ValueError('监听端口不属于此声音工作台，拒绝接管。')
                    profile=text('colima_profile') if kind=='compose' else ''
                    if profile:
                        if not re.fullmatch(r'[A-Za-z0-9_.-]+',profile) or not (pathlib.Path.home()/'.colima'/profile/'colima.yaml').is_file():raise ValueError('原 Colima 环境不存在。')
                        if form.get('stop_profile') and any(k!=ident and s.get('colima_profile')==profile for k,s in records.items()):raise ValueError('共享环境不能随此服务关闭。')
                        fresh.update(colima_profile=profile,stop_profile=form.get('stop_profile') is True)
                    elif form.get('stop_profile'):raise ValueError('请先填写已有的专用 Colima 环境。')
                    if form.get('enable_uninstall'):raise ValueError('修改自定义启停命令时请先关闭自动卸载；需开启时改用标准 Compose 配置并验证挂载。')
                    note('pass','deployment','自定义命令与原项目路径已检查；未执行命令。')
                else:
                    fresh,generated=self.registrar.inspect(form,exclude_id=ident)
                    checks+=generated
                if old['kind']!=fresh['kind']:candidate={}
                else:
                    for key in RUNTIME_KEYS:candidate.pop(key,None)
                candidate.update(fresh)
            candidate.update(id=ident,title=title,description=description,help=help_text,url=text('url'),health=text('health'),
                             icon=self.store.service_icon(form.get('icon',base['icon'])),ui_edited=True,group=category['name'])
            if text('uninstall_text'):candidate['uninstall']=text('uninstall_text')
            if not form.get('enable_uninstall'):
                for key in ['uninstall_mode','uninstall_command','preserved_mounts']:candidate.pop(key,None)
                candidate['preserves']=[s.strip() for s in text('preserves').splitlines() if s.strip()]
            if changed:
                self.apply_options(candidate,form,old)
                if not any(c['level']=='error' for c in checks):
                    note('pass','configuration','地址和高级配置格式已检查，保存不会自动启停业务。')
                    for field in ['url','health']:
                        if candidate.get(field) and candidate.get(field)!=old.get(field):
                            try:
                                with HTTP.open(candidate[field],timeout=3) as response:accepted=response.status in candidate.get('health_codes',[200])
                                note('pass' if accepted else 'warning',field,'地址可连接。' if accepted else '地址已响应，但状态码不在设定范围内。')
                            except Exception:note('warning',field,'地址暂时不可访问，可能尚未启动或需要登录。')
            check_unique(candidate,{k:s for k,s in records.items() if k!=ident})
            note('pass','basic','名称、分类和说明已检查；服务 ID 与恢复记录保持不变。')
        except (ValueError,KeyError,OSError,TypeError) as error:note('error','configuration',str(error))
        return candidate,checks,changed

    def apply_options(self, service, form, old):
        codes=[int(x) for x in re.split(r'[,\s]+',form.get('health_codes','200').strip()) if x]
        if not codes or any(not 100<=n<=599 for n in codes):raise ValueError('健康检查状态码需为100–599的数字，多个用逗号分隔。')
        service['health_codes']=list(dict.fromkeys(codes))
        service['open_label']=str(form.get('open_label','')).strip()
        if form.get('open_command','').strip():service['open_command']=command(form['open_command'],service['directory'])
        else:service.pop('open_command',None)
        paths=[str(pathlib.Path(s.strip()).expanduser()) for s in form.get('requires_paths','').splitlines() if s.strip()]
        old_paths='\n'.join(old.get('requires_paths',[]))
        check_paths=form.get('requires_paths','')!=old_paths or self.identity(form)!=self.identity(self.form(old,{'categories':[],'assignments':{},'group_map':{}},self.store))
        for value in paths:
            p=pathlib.Path(value)
            if not p.is_absolute() or (check_paths and not p.exists()):raise ValueError('依赖路径不存在，请核对高级设置。')
        service['requires_paths']=list(dict.fromkeys(service.get('requires_paths',[])+paths)) if self.identity(form)!=self.identity(self.form(old,{'categories':[],'assignments':{},'group_map':{}},self.store)) else paths
        if service['kind'] in ['process','voice']:
            environment=json.loads(form.get('environment_json','{}'))
            if not isinstance(environment,dict):raise ValueError('环境变量应为JSON对象。')
            resolved={}
            for key,value in environment.items():
                if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',key) or not isinstance(value,str) or '\x00' in value:raise ValueError('环境变量名称或内容无效。')
                if value==KEEP:
                    if key not in old.get('environment',{}):raise ValueError('保留占位符只能用于已有环境变量。')
                    value=old['environment'][key]
                resolved[key]=value
            service['environment']=resolved
            variable=form.get('token_env','').strip()
            if variable and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',variable):raise ValueError('令牌变量名称无效。')
            if variable:service['token_env']=variable
            else:service.pop('token_env',None)
        if service['kind']=='app':
            extras=[s.strip() for s in form.get('extra_executables','').splitlines() if s.strip()]
            app=pathlib.Path(service['app']).resolve()
            if any(not pathlib.Path(s).is_file() or app not in pathlib.Path(s).resolve().parents for s in extras):raise ValueError('辅助进程路径必须是此 .app 内的实际程序。')
            service['extra_executables']=extras

    def validate(self, ident, version, form):
        service,checks,changed=self.candidate(ident,version,form)
        ok=not any(c['level']=='error' for c in checks);token=None
        if ok:
            token=secrets.token_urlsafe(32)
            ticket={'id':ident,'version':version,'form':self.registrar.fingerprint(form),'source':self.source_signature(service),'expires':time.monotonic()+600}
            with self.ticket_lock:
                self.tickets={k:v for k,v in self.tickets.items() if v['expires']>time.monotonic()}
                if len(self.tickets)>=128:self.tickets.pop(next(iter(self.tickets)))
                self.tickets[token]=ticket
        preview={k:service.get(k) for k in ['id','title','kind','directory','url','help','uninstall','preserves','preserved_mounts']}
        preview.update(start=shlex.join(service.get('start',[])),stop=shlex.join(service.get('stop',[])) or '由原适配器核实身份后停止')
        return {'ok':ok,'checks':checks,'validation':token,'preview':preview,'requires_validation':changed}

    def save(self, ident, version, form, token=None):
        # Same per-service lock used by control.py; saving cannot race a lifecycle action.
        records,_,_=self.snapshot(ident)
        with (self.root/'state'/(ident+'.lock')).open('a') as operation:
            try:fcntl.flock(operation,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise Conflict('此服务已有操作正在执行，请稍后保存。')
            service,checks,changed=self.candidate(ident,version,form)
            errors=[c['message'] for c in checks if c['level']=='error']
            if errors:raise ValueError('；'.join(errors))
            if changed:
                with self.ticket_lock:ticket=self.tickets.get(token) if isinstance(token,str) else None
                if not ticket or ticket['expires']<time.monotonic() or ticket['id']!=ident or ticket['version']!=version or ticket['form']!=self.registrar.fingerprint(form):
                    raise ValueError('运行配置已修改，请先验证配置。')
                if ticket['source']!=self.source_signature(service):raise ValueError('部署文件或挂载在验证后变化，请重新验证。')
            with (self.root/'state/registry-write.lock').open('a') as registry_lock:
                fcntl.flock(registry_lock,fcntl.LOCK_EX)
                with self.store.lock.open('a') as preferences_lock:
                    fcntl.flock(preferences_lock,fcntl.LOCK_EX)
                    path=self.root/'services.json';old_bytes=path.read_bytes();records=json.loads(old_bytes)
                    state=self.store._load(records)
                    if ident not in records or version!=self.version(records[ident],state):raise Conflict('此服务已更新，请重新打开编辑。')
                    category=next((c for c in state['categories'] if c['id']==form.get('category')),None)
                    if not category:raise Conflict('分类已变化，请重新打开编辑。')
                    check_unique(service,{k:s for k,s in records.items() if k!=ident})
                    if changed and ticket['source']!=self.source_signature(service):raise ValueError('部署定义已变化，请重新验证。')
                    backup=self.root/'state'/('before-service-edit-'+datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
                    backup.mkdir(mode=0o700)
                    (backup/'services.json').write_bytes(old_bytes);(backup/'services.json').chmod(0o600)
                    old_preferences=self.store.path.read_bytes();(backup/'ui-preferences.json').write_bytes(old_preferences);(backup/'ui-preferences.json').chmod(0o600)
                    service.pop('_validated_definition',None);records[ident]=service
                    state['assignments'][ident]=category['id'];state.setdefault('service_info',{}).pop(ident,None);state['revision']+=1
                    try:
                        atomic_bytes(path,(json.dumps(records,ensure_ascii=False,indent=2)+'\n').encode())
                        self.store._write(state)
                    except Exception:
                        atomic_bytes(path,old_bytes);atomic_bytes(self.store.path,old_preferences)
                        raise
            if isinstance(token,str):
                with self.ticket_lock:self.tickets.pop(token,None)
        return service
