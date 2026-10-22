"""Read-only form validation and atomic registration of installed local services."""
import datetime
import fcntl
import hashlib
import json
import os
import pathlib
import plistlib
import re
import secrets
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from control import ENV, listeners, owned, process_identity, validate_compose_uninstall

HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
DOCKER = '/opt/homebrew/bin/docker'


def deployment_key(s):
    kind = s.get('kind')
    if kind == 'compose':
        argv = s.get('start', [])
        def option(name):
            return argv[argv.index(name)+1] if name in argv and argv.index(name)+1 < len(argv) else ''
        return kind, option('--context') or 'default', option('-p')
    if kind == 'app':return kind, s.get('app')
    if kind == 'launchd':return kind, s.get('label')
    if kind == 'container':
        argv = s.get('inspect', [])
        context = argv[argv.index('--context')+1] if '--context' in argv else 'default'
        return kind, context, argv[-1] if argv else ''
    return 'process', s.get('port')


def check_unique(service, records):
    for existing in records.values():
        if existing['id'] == service['id'] or existing['title'].strip().casefold() == service['title'].casefold():
            raise ValueError('服务名称或编号已存在，请使用原服务的管理入口。')
        same_directory = (existing.get('kind') == service.get('kind') == 'compose' and existing.get('directory') and
                          pathlib.Path(existing['directory']).resolve() == pathlib.Path(service['directory']).resolve())
        old_key, new_key = deployment_key(existing), deployment_key(service)
        same_container = (old_key[0] == new_key[0] == 'container' and old_key[1] == new_key[1] and
                          bool(({old_key[2]} | set(existing.get('container_aliases', []))) &
                               ({new_key[2]} | set(service.get('container_aliases', [])))))
        exact_identity = old_key == new_key and (new_key[0] != 'compose' or bool(new_key[2]))
        if same_directory or same_container or exact_identity:
            raise ValueError('同一个部署实例已登记为“'+existing['title']+'”。')


def write_service(root, service):
    """Shared by the browser and CLI; concurrent registrations never overwrite."""
    root = pathlib.Path(root)
    state = root/'state';state.mkdir(exist_ok=True)
    with (state/'registry-write.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = root/'services.json'
        records = json.loads(path.read_text())
        check_unique(service, records)
        backup = state/('services-before-register-'+datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')+'.json')
        shutil.copy2(path, backup);backup.chmod(0o600)
        records[service['id']] = service
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', dir=root, prefix='.services-', delete=False) as f:
                json.dump(records, f, ensure_ascii=False, indent=2);f.write('\n')
                temporary = pathlib.Path(f.name)
            temporary.chmod(0o600);temporary.replace(path)
        finally:
            if temporary and temporary.exists():temporary.unlink()


class Registrar:
    def __init__(self, root, store):
        self.root, self.store = pathlib.Path(root), store
        self.tickets = {}
        self.lock = threading.Lock()

    @staticmethod
    def fingerprint(form):
        return hashlib.sha256(json.dumps(form, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def definition_signature(self, service):
        paths = list(service.get('requires_paths', []))
        if service['kind'] == 'app':paths += [str(pathlib.Path(service['app'])/'Contents/Info.plist'),service['executable']]
        if service['kind'] == 'process':
            paths += [service['start'][0]]
            paths += [str(pathlib.Path(service['directory'])/a) for a in service['start'][1:] if a.endswith(('.py','.js','.mjs','.sh'))]
        identities = []
        for name in sorted(set(paths)):
            p = pathlib.Path(name); stat = p.stat()
            item = [str(p.resolve()),stat.st_dev,stat.st_ino,stat.st_mtime_ns,stat.st_size]
            if p.is_file() and stat.st_size < 8*1024*1024:item.append(hashlib.sha256(p.read_bytes()).hexdigest())
            identities.append(item)
        return self.fingerprint({'service':service,'sources':identities})

    def validate(self, form):
        service, checks = self.inspect(form)
        ok = not any(c['level'] == 'error' for c in checks)
        token = None
        if ok:
            token = secrets.token_urlsafe(32)
            with self.lock:
                self.tickets = {k:v for k,v in self.tickets.items() if v[1] > time.monotonic()}
                if len(self.tickets) >= 128:self.tickets.pop(next(iter(self.tickets)))
                self.tickets[token] = (self.fingerprint(form), time.monotonic()+600, self.definition_signature(service))
        preview = None
        if service:
            preview = {k:service.get(k) for k in ['id','title','kind','directory','url','help','uninstall','preserves','preserved_mounts']}
            preview['start'] = shlex.join(service.get('start', []))
            preview['stop'] = (shlex.join(service.get('stop', [])) if 'stop' in service else
                               '关闭此应用' if service['kind']=='app' else
                               '停止已核实归属的端口进程' if service['kind']=='process' else '停用此用户 LaunchAgent')
        return {'ok':ok, 'checks':checks, 'validation':token, 'preview':preview}

    def add(self, form, token):
        with self.lock:
            ticket = self.tickets.get(token) if isinstance(token, str) else None
        if not ticket or ticket[1] < time.monotonic() or ticket[0] != self.fingerprint(form):
            raise ValueError('配置尚未验证、已修改或验证已过期，请重新点击“验证配置”。')
        service, checks = self.inspect(form)
        errors = [c['message'] for c in checks if c['level']=='error']
        if errors:raise ValueError('配置已变化，请重新验证：'+'；'.join(errors))
        if ticket[2] != self.definition_signature(service):
            raise ValueError('部署配置、入口文件或数据挂载在验证后发生变化，请重新验证。')
        service.pop('_validated_definition',None)
        write_service(self.root, service)
        with self.lock:self.tickets.pop(token, None)
        return service

    def inspect(self, form, exclude_id=None):
        if not isinstance(form, dict):raise ValueError('服务表单格式不正确。')
        records = json.loads((self.root/'services.json').read_text())
        state = self.store.read(records)
        if exclude_id is not None:
            records = {k:v for k,v in records.items() if k!=exclude_id}
        checks = []
        def note(level, field, message):checks.append({'level':level,'field':field,'message':message})
        def text(key, required=False, limit=4000):
            value = form.get(key, '')
            if not isinstance(value, str) or len(value)>limit or '\x00' in value:
                note('error',key,'字段内容无效或过长。');return ''
            value = value.strip()
            if required and not value:note('error',key,'请填写此项。')
            return value
        def path_field(key, directory=False, relative=None):
            raw = text(key, True)
            if not raw:return None
            p = pathlib.Path(raw).expanduser()
            if not p.is_absolute() and relative:p=relative/p
            if not p.is_absolute() or not (p.is_dir() if directory else p.is_file()):
                note('error',key,'路径不存在或类型不正确，请填写本机实际路径。');return None
            return p.resolve()
        def read_command(argv, cwd=None, timeout=15):
            result = subprocess.run(argv, cwd=cwd, env=ENV, capture_output=True, text=True, timeout=timeout)
            if result.returncode:raise ValueError('只读检查未通过，请核对配置和所选环境。')
            return result.stdout
        title, kind = text('title', True, 80), text('kind', True, 30)
        description = text('description', True, 240)
        help_text = text('help', True)
        category = next((c for c in state['categories'] if c['id']==form.get('category')), None)
        if not category:note('error','category','分类不存在，请重新选择。')
        directory = path_field('directory', True)
        urls = {}
        for key in ['url','health']:
            value = text(key)
            if value:
                try:
                    parsed = urllib.parse.urlsplit(value)
                    if parsed.scheme not in ['http','https'] or not parsed.hostname or parsed.username or parsed.password or any(c.isspace() for c in value):raise ValueError()
                    parsed.port
                except ValueError:note('error',key,'请输入完整的 HTTP(S) 地址，不包含账号密码。')
            urls[key] = value
        ident = exclude_id or 'local-'+(re.sub(r'[^a-z0-9]+','-',title.lower()).strip('-')[:50] or hashlib.sha256(title.encode()).hexdigest()[:12])
        group = next((g for g,c in state['group_map'].items() if category and c==category['id']), category['name'] if category else '未分类')
        service = dict(id=ident,title=title,kind=kind,description=description,help=help_text,group=group,
                       directory=str(directory) if directory else '',**urls,
                       uninstall='仅提供启停管理；尚未启用程序卸载。请按原部署方式卸载，并保留业务数据。')
        try:service['icon']=self.store.service_icon(form.get('icon','default'))
        except ValueError as error:note('error','icon',str(error))
        enable_uninstall = form.get('enable_uninstall') is True
        preserves = [x.strip() for x in text('preserves').splitlines() if x.strip()]
        if enable_uninstall and not preserves:note('error','preserves','启用卸载前，请填写需要保留的数据和配置位置。')
        if enable_uninstall and kind not in ['compose','app']:note('error','enable_uninstall','该类型暂不提供自动卸载。')
        if kind == 'compose':
            if not urls['url'] and not urls['health']:note('error','url','Compose 服务请填写访问地址或健康检查地址，以便判断运行状态。')
            service['health'] = urls['health'] or urls['url']
        try:
            if kind in ['compose','container']:
                if not pathlib.Path(DOCKER).is_file():raise ValueError('未找到 Docker 命令，请先完成安装。')
                context = text('context', True, 100)
                if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*',context):raise ValueError('Docker 环境名称无效。')
                profile = context[len('colima-'):] if context.startswith('colima-') else 'default' if context=='colima' else None
                original_profile = profile and (pathlib.Path.home()/'.colima'/profile/'colima.yaml').is_file()
                try:
                    read_command([DOCKER,'context','inspect',context])
                    note('pass','context','Docker 环境存在：'+context+'；不会切换全局环境。')
                except ValueError:
                    if kind!='compose' or not original_profile:raise
                    note('warning','context','原 Colima 环境已关闭；启动原环境时将恢复其 Docker context，不会创建新虚拟机。')
                base = [DOCKER,'--context',context]
                if kind == 'compose':
                    project = text('project', False, 100)
                    if project and not re.fullmatch(r'[a-z0-9][a-z0-9_-]*',project):raise ValueError('Compose 项目名只能使用小写字母、数字、连字符和下划线。')
                    config = path_field('compose_file', relative=directory)
                    parsed = {}
                    if config and directory:
                        env_file = path_field('env_file',relative=directory) if form.get('env_file') else None
                        compose = base+['compose']+(['--env-file',str(env_file)] if env_file else [])+(['-p',project] if project else [])+['-f',str(config)]
                        parsed = json.loads(read_command(compose+['config','--format','json'],str(directory),25))
                        if not parsed.get('services'):raise ValueError('Compose 配置没有任何服务。')
                        project = project or parsed.get('name','')
                        service['_validated_definition'] = self.fingerprint(parsed)
                        service.update(start=compose+['up','-d','--pull','never'],stop=compose+['stop'],requires_paths=[str(config)]+([str(env_file)] if env_file else []))
                        note('pass','compose_file','Compose 配置可解析，包含 '+str(len(parsed['services']))+' 项服务。')
                        if profile:
                            if not (pathlib.Path.home()/'.colima'/profile/'colima.yaml').is_file():raise ValueError('所选 Colima 原环境不存在，不会自动创建虚拟机。')
                            if form.get('stop_profile') and any(v.get('colima_profile')==profile for v in records.values()):
                                raise ValueError('此 Colima 环境已有其他已登记服务，不能标记为独占并随服务关闭。')
                            service.update(colima_profile=profile,stop_profile=form.get('stop_profile') is True)
                        elif form.get('stop_profile'):raise ValueError('仅已有 Colima 专用环境支持随服务关闭虚拟机。')
                        if enable_uninstall:
                            down=compose+['down'];validate_compose_uninstall(down)
                            service.update(uninstall_mode='compose-instance',uninstall_command=down,preserves=preserves,
                                           uninstall='只移除此 Compose 项目的容器和项目网络；保留卷、镜像、源码、配置及虚拟机。保留数据：'+'；'.join(preserves))
                    try:
                        read_command(base+['info','--format','{{.ServerVersion}}'],timeout=5)
                    except (ValueError,subprocess.TimeoutExpired):
                        note('warning','context','Docker 引擎当前未运行或无法连接；配置可登记，启用前需启动原环境。')
                        if enable_uninstall:note('error','enable_uninstall','无法核实数据挂载，请先关闭自动卸载选项；启停功能仍可接入。')
                    else:
                        note('pass','context','Docker 引擎可连接。')
                        rows = [json.loads(line) for line in read_command(base+['ps','-a','--filter','label=com.docker.compose.project','--format','{{json .}}']).splitlines()]
                        target_ids = []
                        for row in rows:
                            labels = dict(x.split('=',1) for x in row.get('Labels','').split(',') if '=' in x)
                            actual = labels.get('com.docker.compose.project')
                            files = labels.get('com.docker.compose.project.config_files','').split(',')
                            matches = config and any(pathlib.Path(f).resolve()==config for f in files if f)
                            if matches and actual != project:
                                note('error','project','此配置实际部署的项目名是“'+str(actual)+'”，请核对后填写。')
                            if actual == project:
                                if config and files[0] and not matches:note('error','project','此项目名已由其他配置文件使用，不能接管。')
                                if row.get('ID'):target_ids.append(row['ID'])
                        if enable_uninstall and parsed:
                            if form.get('confirm_persistence') is not True:
                                note('error','confirm_persistence','请确认业务数据全部在持久化挂载中，容器内部没有需要保留的数据。')
                            mounted = []
                            expected = {}
                            for name, item in parsed['services'].items():
                                expected[name] = {}
                                for volume in item.get('volumes',[]):
                                    if volume.get('type') == 'volume':
                                        source = volume.get('source')
                                        if not source:note('error','preserves','配置包含匿名卷，卸载后无法自动恢复挂载；请关闭自动卸载。')
                                        else:expected[name][volume['target']] = ('volume',parsed.get('volumes',{}).get(source,{}).get('name'))
                                    elif volume.get('type') == 'bind':
                                        expected[name][volume['target']] = ('bind',str(pathlib.Path(volume['source']).resolve()))
                            if not target_ids:
                                note('error','preserves','未找到可核实数据挂载的原容器，请先关闭自动卸载。')
                            else:
                                containers = json.loads(read_command(base+['container','inspect']+target_ids))
                                seen = set()
                                for container in containers:
                                    name = container.get('Config',{}).get('Labels',{}).get('com.docker.compose.service')
                                    if name not in expected:continue
                                    seen.add(name)
                                    destinations = set()
                                    for mount in container.get('Mounts',[]):
                                        if mount['Type']=='volume':
                                            destinations.add(mount['Destination'])
                                            if expected[name].get(mount['Destination']) != ('volume',mount.get('Name')):
                                                note('error','preserves','存在匿名卷或未对应配置的卷，不能保证恢复原挂载；请关闭自动卸载。')
                                            mounted.append({'service':name,'type':'命名卷','source':mount.get('Name'),'target':mount['Destination']})
                                        elif mount['Type']=='bind':
                                            destinations.add(mount['Destination'])
                                            if expected[name].get(mount['Destination']) != ('bind',str(pathlib.Path(mount['Source']).resolve())):
                                                note('error','preserves','目录挂载与当前 Compose 配置不同，不能保证恢复原数据位置；请关闭自动卸载。')
                                            mounted.append({'service':name,'type':'目录','source':mount['Source'],'target':mount['Destination']})
                                    if set(expected[name]) != destinations:note('error','preserves','持久化挂载目标与当前配置不一致，请先关闭自动卸载。')
                                if seen != set(expected):note('error','preserves','部分服务尚无原容器，无法完整核实数据挂载；请关闭自动卸载。')
                                service['preserved_mounts'] = sorted(mounted,key=lambda m:(m['service'],m['target']))
                                if not mounted:note('error','preserves','未找到持久化目录或命名卷，请先关闭自动卸载。')
                                else:note('pass','preserves','已核实 '+str(len(mounted))+' 处持久化挂载，详情见管理操作预览。')
                else:
                    container = text('container', True, 150)
                    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*',container):raise ValueError('容器名称无效。')
                    try:
                        result=json.loads(read_command(base+['container','inspect',container]))[0]
                    except (ValueError,IndexError):raise ValueError('无法核实此容器；请确认原 Docker 环境运行且容器存在。')
                    container=result['Id']
                    service.update(container_aliases=[container,result['Name'].lstrip('/')],start=base+['start',container],stop=base+['stop',container],inspect=base+['inspect','-f','{{.State.Running}}',container])
                    note('pass','container','已核实容器身份：'+result['Name'].lstrip('/'))
            elif kind == 'app':
                app = path_field('app', True)
                if app:
                    if app.suffix != '.app':raise ValueError('请选择 .app 应用程序目录。')
                    info=plistlib.loads((app/'Contents/Info.plist').read_bytes())
                    executable=(app/'Contents/MacOS'/info['CFBundleExecutable']).resolve()
                    if not executable.is_file() or not os.access(executable,os.X_OK) or app not in executable.parents:raise ValueError('应用程序入口不存在或不可执行。')
                    service.update(app=str(app),executable=str(executable),requires_paths=[str(app)])
                    note('pass','app','已读取应用程序入口：'+executable.name)
                    if enable_uninstall:
                        if app.parent not in [pathlib.Path('/Applications'),pathlib.Path.home()/'Applications'] or pathlib.Path(text('app')).is_symlink():raise ValueError('归档卸载仅支持 Applications 中的独立应用。')
                        service.update(uninstall_mode='archive-app',preserves=preserves,uninstall='关闭并归档同一个 .app 程序；保留用户数据：'+'；'.join(preserves)+'。可从管理台恢复。')
            elif kind == 'launchd':
                plist = path_field('plist')
                if plist:
                    info = plistlib.loads(plist.read_bytes()); label=info.get('Label')
                    if not isinstance(label,str) or not re.fullmatch(r'[A-Za-z0-9_.-]+',label) or not (info.get('Program') or info.get('ProgramArguments')):raise ValueError('此文件不是有效的用户 LaunchAgent 配置。')
                    if label.startswith('com.apple.'):raise ValueError('不能接管 macOS 系统服务。')
                    arguments = info.get('ProgramArguments', [])
                    if not isinstance(arguments,list) or any(not isinstance(a,str) for a in arguments):raise ValueError('LaunchAgent 的程序参数格式不正确。')
                    program = info.get('Program') or (arguments[0] if arguments else '')
                    if not isinstance(program,str) or not shutil.which(program,path=ENV['PATH']):raise ValueError('LaunchAgent 的启动程序不存在或不可执行。')
                    loaded = subprocess.run(['/bin/launchctl','print','gui/'+str(os.getuid())+'/'+label],capture_output=True,text=True,env=ENV,timeout=8)
                    if loaded.returncode == 0:
                        match = re.search(r'^\s*path = (.+)$',loaded.stdout,re.M)
                        if not match or pathlib.Path(match.group(1).strip().strip(chr(34))).resolve() != plist:
                            raise ValueError('该服务标识已由另一份已加载的 LaunchAgent 配置使用，不能接管。')
                    service.update(label=label,plist=str(plist),requires_paths=[str(plist)])
                    note('pass','plist','已读取 LaunchAgent：'+label)
            elif kind == 'process':
                raw = text('start_command',True)
                argv=shlex.split(raw)
                if not argv or any(x in raw for x in ['&&','||',';','|','>','<','`','$(']) or (pathlib.Path(argv[0]).name in ['sh','bash','zsh'] and '-c' in argv):raise ValueError('启动命令请填写一个程序及其参数，不使用管道、重定向或 shell 组合语法。')
                binary=shutil.which(argv[0],path=ENV['PATH'])
                if not binary:raise ValueError('启动程序不存在，请填写实际可执行文件路径。')
                argv[0]=binary
                port=int(text('port',True,6))
                if not 1<=port<=65535:raise ValueError('端口必须在 1–65535 之间。')
                tokens=shlex.split(text('process_tokens'))
                known=set(argv)|{pathlib.Path(a).name for a in argv}
                if any(t not in known for t in tokens):raise ValueError('进程识别标记需填写启动命令中实际出现的脚本、模块或参数。')
                if directory:
                    for a in argv[1:]:
                        if a.endswith(('.py','.js','.mjs','.sh')) and not (directory/a).is_file():raise ValueError('启动命令中的入口文件不存在：'+a)
                required = []
                scripts = [a for a in argv[1:] if a.endswith(('.py','.js','.mjs','.sh'))]
                if scripts:required.append(pathlib.Path(scripts[0]).name)
                elif '-m' in argv and argv.index('-m')+1 < len(argv):required.append(argv[argv.index('-m')+1])
                required += [a for a in argv[1:] if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*',a)]
                generic = {pathlib.Path(argv[0]).name,argv[0],'python','python3','node','npm','npx','uvicorn','gunicorn','sh','bash','zsh','-m','-u'}
                if not required and not any(t not in generic and not t.startswith('-') for t in tokens):
                    raise ValueError('无法识别程序入口，请填写实际脚本/模块或独有的进程标记，不能仅使用解释器名称。')
                tokens = list(dict.fromkeys(tokens+required))
                service.update(start=argv,port=port,process_tokens=tokens)
                if directory:
                    pids=listeners(port)
                    if pids and not all(owned(service,*process_identity(p)) for p in pids):raise ValueError('端口已被其他项目占用，不能将其接管为此服务。')
                    note('pass','start_command','启动程序和进程标记已检查；未执行启动命令。')
            else:note('error','kind','请选择支持的部署类型。')
        except (ValueError,OSError,KeyError,TypeError,subprocess.TimeoutExpired,plistlib.InvalidFileException) as error:
            message = str(error) if isinstance(error,ValueError) and str(error) else '无法读取部署配置，请核对路径、文件内容和环境。'
            note('error','deployment',message)
        try:check_unique(service,records)
        except ValueError as error:note('error','title',str(error))
        if not any(c['level']=='error' for c in checks):
            note('pass','basic','名称、分类、项目路径和地址格式已检查。')
            for key in ['url','health']:
                if not urls[key] or (key=='health' and urls[key]==urls['url']):continue
                try:
                    with HTTP.open(urls[key],timeout=3) as response:
                        if response.status>=400:raise ValueError()
                    note('pass',key,('访问地址' if key=='url' else '健康检查地址')+'可连接。')
                except Exception:note('warning',key,'地址暂时不可访问，可能尚未启动或需要登录；添加不会自动启动服务。')
            if urls['health'] and urls['health']==urls['url']:
                source=next((c for c in reversed(checks) if c['field']=='url'),None)
                if source:note(source['level'],'health','健康检查与访问地址相同：'+source['message'])
        return service,checks
