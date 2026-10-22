"""Explicit, scoped permanent cleanup of already-uninstalled registrations."""
import datetime
import fcntl
import hashlib
import json
import os
import pathlib
import plistlib
import re
import shutil
import subprocess

from control import ENV
from registration import Registrar
from service_editor import atomic_bytes
from ui_model import Conflict


class TrashManager:
    def __init__(self, root, store):
        self.root,self.store=pathlib.Path(root),store

    def docker_read(self, argv, directory):
        result=subprocess.run(argv,cwd=directory,env=ENV,capture_output=True,text=True,timeout=12)
        if result.returncode:raise ValueError('原环境当前不可访问。')
        return result.stdout

    @staticmethod
    def compose_prefix(service):
        argv=service.get('uninstall_command',service.get('start',[]))
        if not argv or pathlib.Path(argv[0]).name!='docker' or 'compose' not in argv:
            raise ValueError('无法读取原 Compose 适配的数据配置。')
        # Trusted registry commands only; never execute a wrapper script.
        index=next((i for i,x in enumerate(argv) if i>argv.index('compose') and x in ['down','up','stop']),len(argv))
        prefix=list(argv[:index])
        context=prefix[prefix.index('--context')+1] if '--context' in prefix else 'default'
        return prefix,context

    def compose_config(self, service):
        prefix,_=self.compose_prefix(service)
        # Config parsing is offline. Do not start a stopped VM just to list data.
        if '--context' in prefix:prefix[prefix.index('--context')+1]='default'
        return json.loads(self.docker_read(prefix+['config','--format','json'],service['directory']))

    @staticmethod
    def app_paths(service, archive=None):
        paths=[]
        app=pathlib.Path(archive or service.get('app',''))
        try:info=plistlib.loads((app/'Contents/Info.plist').read_bytes())
        except (OSError,ValueError,plistlib.InvalidFileException):info={}
        name=pathlib.Path(service.get('app','')).stem
        home=pathlib.Path.home()
        identifier=info.get('CFBundleIdentifier','')
        names={name,info.get('CFBundleName',''),info.get('CFBundleDisplayName','')}
        for label in names:
            if label and '/' not in label and label not in ['.','..']:
                paths.append(home/'Library/Application Support'/label)
                compact=''.join(c for c in label.lower() if c.isalnum() or c in '-_')
                if len(compact)>2:paths.append(home/('.'+compact))
        if identifier and '/' not in identifier and identifier not in ['.','..']:
            paths.extend([home/'Library/Application Support'/identifier,home/'Library/Caches'/identifier,home/'Library/Preferences'/(identifier+'.plist')])
        return sorted({str(p) for p in paths if p.exists() or p.is_symlink()})

    @staticmethod
    def overlaps(a,b):
        a,b=pathlib.Path(a).resolve(),pathlib.Path(b).resolve()
        return a==b or a in b.parents or b in a.parents

    def path_check(self,path,allowed,others):
        p=pathlib.Path(path)
        if not p.is_absolute():return False,'路径不是绝对路径。'
        if any(parent.is_symlink() for parent in [p,*p.parents]):return False,'符号链接路径需人工清理。'
        resolved=p.resolve()
        protected={pathlib.Path(p).resolve() for p in ['/','/Users','/Applications','/System','/Library','/opt','/opt/homebrew','/usr','/private','/tmp','/private/tmp']}
        protected.update([pathlib.Path.home(),pathlib.Path.home()/'Documents',pathlib.Path.home()/'Downloads',pathlib.Path.home()/'Desktop',pathlib.Path.home()/'Library',pathlib.Path.home()/'Library/Application Support',pathlib.Path.home()/'Library/Caches',pathlib.Path.home()/'Library/Preferences',self.root])
        if resolved in protected or not any(pathlib.Path(base).resolve() not in protected and (resolved==pathlib.Path(base).resolve() or pathlib.Path(base).resolve() in resolved.parents) for base in allowed):
            return False,'超出此服务已确认的数据范围，需人工清理。'
        if any(self.overlaps(path,other) for other in others):return False,'其他服务也引用此路径，不能直接删除。'
        if p.is_mount():return False,'此路径是挂载点，需人工清理。'
        if not p.exists():return False,'此路径当前不存在。'
        return True,'已核对路径；仅在勾选后删除。'

    def preview(self,ident):
        records=json.loads((self.root/'services.json').read_text())
        if not isinstance(ident,str) or not re.fullmatch(r'local-[a-z0-9-]+',ident) or ident not in records:raise ValueError('服务不存在。')
        service=records[ident];marker=self.root/'state'/(ident+'.uninstalled.json')
        if not marker.is_file():raise ValueError('只能彻底删除回收站中的已卸载服务。')
        mark=json.loads(marker.read_text());data=[];warnings=[];program=[];source_signatures=[]
        others=[s for key,s in records.items() if key!=ident]
        other_paths=[p for s in others for p in [s.get('directory',''),*s.get('requires_paths',[]),*self.app_paths(s)] if p]
        def add_path(path,label,allowed,extra_reason=''):
            selectable,reason=self.path_check(path,allowed,other_paths)
            if extra_reason:selectable,reason=False,extra_reason
            p=pathlib.Path(path)
            stat=p.lstat() if p.exists() or p.is_symlink() else None
            data.append(dict(id=hashlib.sha256(('path:'+path).encode()).hexdigest()[:24],kind='path',label=label,path=path,
                selectable=selectable,reason=reason,identity=[stat.st_dev,stat.st_ino] if stat else None))
        if mark.get('kind')=='app' and service.get('kind')=='app':
            archive=pathlib.Path(mark.get('archive',''))
            base=self.root/'archive/uninstalled'/ident
            if mark.get('source')!=service.get('app') or not archive.is_absolute() or base.resolve() not in archive.resolve().parents or archive.suffix!='.app' or any(p.is_symlink() for p in [archive,*archive.parents]):
                raise ValueError('归档路径不在此服务的管理目录内，拒绝删除。')
            if archive.exists():program.append(str(archive))
            paths=self.app_paths(service,archive)
            for path in paths:add_path(path,'应用数据 · '+pathlib.Path(path).name,paths,'原位置已有程序，请先核实是否重新安装。' if pathlib.Path(service['app']).exists() else '')
            if not paths:warnings.append('未发现标准位置的应用数据；其他自定义数据位置不会自动删除。')
        elif mark.get('kind')=='compose-instance' and service.get('kind')=='compose':
            try:
                config=self.compose_config(service);prefix,context=self.compose_prefix(service)
                source_signatures.append(Registrar.fingerprint(config))
                docker=[prefix[0],'--context',context]
                project=config.get('name','')
                if not project:raise ValueError('缺少原项目名。')
                try:
                    if self.docker_read(docker+['ps','-aq','--filter','label=com.docker.compose.project='+project],service['directory']).strip():
                        raise Conflict('原项目仍有容器，请先重新卸载，再清理回收站。')
                except Conflict:raise
                except (ValueError,OSError,subprocess.TimeoutExpired):warnings.append('原 Docker 环境未运行；数据卷可展示，但当前不能勾选删除。')
                referenced_volumes=set();unknown_other_contexts=set()
                for other in others:
                    if other.get('kind')!='compose':continue
                    try:
                        _,other_context=self.compose_prefix(other);other_config=self.compose_config(other)
                        for definition in other_config.get('services',{}).values():
                            for mount in definition.get('volumes',[]):
                                if mount.get('type')=='bind':other_paths.append(mount.get('source',''))
                        if other_context==context:
                            referenced_volumes.update(v.get('name','') for v in other_config.get('volumes',{}).values())
                    except (ValueError,OSError,subprocess.TimeoutExpired):
                        unknown_other_contexts.add(context)
                        warnings.append('部分其他服务配置无法读取；归属不明的数据只展示，不允许删除。')
                seen=set()
                for component,definition in config.get('services',{}).items():
                    for mount in definition.get('volumes',[]):
                        key=(mount.get('type'),mount.get('source'))
                        if key in seen:continue
                        seen.add(key)
                        if mount.get('type')=='bind' and mount.get('source'):
                            path=mount['source'];reason='只读配置挂载，默认保留。' if mount.get('read_only') else ''
                            if pathlib.Path(path).resolve()==pathlib.Path(service['directory']).resolve():reason='挂载了整个项目目录，包含源码和配置，需人工清理。'
                            if unknown_other_contexts:reason='其他服务的数据引用尚未核实，需人工清理。'
                            add_path(path,component+' · '+mount.get('target',''),[service['directory']],reason)
                        elif mount.get('type')=='volume':
                            logical=mount.get('source','');definition=config.get('volumes',{}).get(logical,{})
                            name=definition.get('name') or (project+'_'+logical if logical else '')
                            reason='';selectable=False
                            if not name:reason='匿名卷无法确认归属，需人工清理。'
                            elif definition.get('external'):reason='外部数据卷，可能由其他业务共用。'
                            elif name in referenced_volumes:reason='其他服务也引用此数据卷。'
                            elif unknown_other_contexts:reason='其他服务的数据引用尚未核实。'
                            else:
                                try:
                                    info=json.loads(self.docker_read(docker+['volume','inspect',name],service['directory']))[0]
                                    labels=info.get('Labels') or {}
                                    if labels.get('com.docker.compose.project')!=project or labels.get('com.docker.compose.volume')!=logical:reason='卷标签与原项目不匹配。'
                                    elif self.docker_read(docker+['ps','-aq','--filter','volume='+name],service['directory']).strip():reason='仍有容器引用此数据卷。'
                                    else:selectable=True;reason='原项目命名卷，当前无容器引用；仅在勾选后删除。'
                                except (ValueError,OSError,subprocess.TimeoutExpired,IndexError,KeyError):reason='原环境未运行、数据卷不存在或无法核实归属，请启动原环境后重新检测。'
                            data.append(dict(id=hashlib.sha256(('volume:'+context+':'+name).encode()).hexdigest()[:24],kind='volume',label=component+' · '+mount.get('target',''),volume=name,context=context,selectable=selectable,reason=reason))
                warnings.append('项目源码、配置文件、镜像和虚拟机保留；它们可能包含其他依赖。')
            except Conflict:raise
            except (ValueError,OSError,subprocess.TimeoutExpired,KeyError,TypeError):warnings.append('原 Compose 配置暂时无法解析；未识别的数据不会删除。')
        else:raise ValueError('此卸载记录类型尚不支持永久清理。')
        for label in service.get('preserves',[]):
            if not any(label in str(item.get('path','')) or label in str(item.get('volume','')) for item in data):
                warnings.append('原登记补充说明（未识别的具体位置保留）：'+label)
        public=dict(id=ident,title=service['title'],data=data,program=program,warnings=list(dict.fromkeys(warnings)))
        program_identity=[]
        for raw in program:
            stat=pathlib.Path(raw).lstat();program_identity.append([raw,stat.st_dev,stat.st_ino,stat.st_mtime_ns])
        public['version']=Registrar.fingerprint(dict(service=service,marker=mark,plan=public,config=source_signatures,program_identity=program_identity))
        return public

    @staticmethod
    def remove_path(path):
        p=pathlib.Path(path)
        if p.is_dir():
            # Refuse nested mounts before recursive removal. rmtree does not follow symlinks.
            for current,dirs,_ in os.walk(p,followlinks=False):
                if any(pathlib.Path(current,name).is_mount() for name in dirs):raise ValueError('目录内有挂载点，拒绝递归删除。')
            shutil.rmtree(p)
        else:p.unlink()

    def delete(self,ident,version,selected,confirmation):
        if not isinstance(ident,str) or ident not in json.loads((self.root/'services.json').read_text()):raise ValueError('服务不存在。')
        if not isinstance(selected,list) or any(not isinstance(x,str) for x in selected) or len(set(selected))!=len(selected):raise ValueError('删除选择无效。')
        with (self.root/'state'/(ident+'.lock')).open('a') as operation:
            try:fcntl.flock(operation,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise Conflict('服务已有操作正在执行，请稍后重试。')
            with (self.root/'state/registry-write.lock').open('a') as registry_lock:
                fcntl.flock(registry_lock,fcntl.LOCK_EX)
                with self.store.lock.open('a') as preferences_lock:
                    fcntl.flock(preferences_lock,fcntl.LOCK_EX)
                    plan=self.preview(ident)
                    if version!=plan['version']:raise Conflict('回收站内容或部署状态已变化，请重新检测。')
                    if confirmation!=plan['title']:raise ValueError('请输入完整的服务名称以确认不可恢复的删除。')
                    items={item['id']:item for item in plan['data']}
                    if any(key not in items or not items[key]['selectable'] for key in selected):raise ValueError('所选数据不在可删除清单内。')
                    completed=[]
                    try:
                        for key in selected:
                            item=items[key]
                            if item['kind']=='path':self.remove_path(item['path'])
                            else:
                                result=subprocess.run(['/opt/homebrew/bin/docker','--context',item['context'],'volume','rm',item['volume']],env=ENV,capture_output=True,text=True,timeout=60)
                                if result.returncode:raise OSError('volume removal failed')
                            completed.append(item.get('path',item.get('volume')))
                        for path in plan['program']:self.remove_path(path);completed.append(path)
                        for cached in [self.root/'state'/(ident+'.token'),self.root/'state/entities'/(ident+'.json')]:
                            if cached.is_file() or cached.is_symlink():cached.unlink()
                        path=self.root/'services.json';old_registry=path.read_bytes();records=json.loads(old_registry)
                        state=self.store._load(records);old_preferences=self.store.path.read_bytes()
                        records.pop(ident);state.get('assignments',{}).pop(ident,None);state.get('service_info',{}).pop(ident,None);state['revision']+=1
                        try:
                            self.store._write(state)
                            atomic_bytes(path,(json.dumps(records,ensure_ascii=False,indent=2)+'\n').encode())
                            (self.root/'state'/(ident+'.uninstalled.json')).unlink()
                        except Exception:
                            atomic_bytes(path,old_registry);atomic_bytes(self.store.path,old_preferences);raise
                    except Exception as error:
                        # Keep the recycle record on failure. Destructive filesystem work is never described as rolled back.
                        raise ValueError('清理未完成；已删除 '+str(len(completed))+' 项。回收站记录保留，请重新检测后重试。') from error
        return {'id':ident,'deleted':completed,'message':'已永久删除管理记录及所选项目，无法通过管理台恢复。'}
