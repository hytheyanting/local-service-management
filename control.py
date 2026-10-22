#!/usr/bin/python3
"""Manage only registered local services; never remove application data."""
import argparse
import datetime
import fcntl
import json
import os
import pathlib
import re
import shlex
import shutil
import signal
import secrets
import subprocess
import sys
import time
import urllib.request
import urllib.error
import urllib.parse

ROOT = pathlib.Path(__file__).resolve().parent
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
ENV = dict(os.environ, PATH='/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin')


def owns_process(cwd, command, service):
    try:
        args = shlex.split(command)
    except ValueError:
        return False
    tokens = set(args) | {pathlib.Path(arg).name for arg in args}
    return (bool(cwd) and pathlib.Path(cwd).resolve() == pathlib.Path(service['directory']).resolve()
            and bool(service.get('process_tokens'))
            and all(token in tokens for token in service['process_tokens']))


def match_app_pids(rows, executable):
    matches = []
    for row in rows.splitlines():
        fields = row.strip().split(None, 1)
        if len(fields) == 2 and fields[0].isdigit() and fields[1] == executable:
            matches.append(int(fields[0]))
    return matches


def app_pids(service):
    result = subprocess.run(['/bin/ps', '-axo', 'pid=,comm='], capture_output=True, text=True, timeout=5)
    return match_app_pids(result.stdout, service['executable'])


def app_members(service):
    result = subprocess.run(['/bin/ps', '-axo', 'pid=,comm='], capture_output=True, text=True, timeout=5)
    executables = [service['executable']] + service.get('extra_executables', [])
    return sorted({pid for executable in executables for pid in match_app_pids(result.stdout, executable)})


def marker(service):
    return ROOT / 'state' / (service['id'] + '.uninstalled.json')


def validate_compose_uninstall(argv):
    if not argv or pathlib.Path(argv[0]).name != 'docker' or 'compose' not in argv:
        raise ValueError('卸载只允许明确的 Docker Compose 命令')
    if 'down' not in argv or argv[argv.index('down'):] != ['down']:
        raise ValueError('保留数据卸载仅允许 down，不允许卷、镜像或额外实例删除参数')
    if any(arg in ['--volumes', '-v', '--rmi', '--remove-orphans'] for arg in argv):
        raise ValueError('拒绝删除卷、镜像或其他实例')


def http_healthy(service):
    if not service.get('health'):
        return True
    codes = service.get('health_codes', [200])
    try:
        with HTTP.open(service['health'], timeout=2) as response:
            return response.status in codes
    except urllib.error.HTTPError as error:
        return error.code in codes
    except Exception:
        return False


def owns_voice_process(cwd, command, expected):
    try:
        args = shlex.split(command)
    except ValueError:
        return False
    return (bool(cwd) and pathlib.Path(cwd).resolve() == pathlib.Path(expected).resolve()
            and 'bridge:app' in args and any('uvicorn' in arg for arg in args))


def healthy(service):
    if marker(service).exists():
        return False
    kind = service['kind']
    if kind == 'app':
        present = bool(app_members(service))
        return present and http_healthy(service)
    if kind == 'launchd':
        return launch_running(service) and http_healthy(service)
    if kind == 'container':
        try:
            result = subprocess.run(service['inspect'], capture_output=True, text=True, env=ENV, timeout=5)
            return result.returncode == 0 and result.stdout.strip() == 'true'
        except subprocess.TimeoutExpired:
            return False
    if kind in ['process', 'voice']:
        pids = listeners(service['port'])
        return bool(pids) and http_healthy(service) and all(
            owned(service, *process_identity(pid)) for pid in pids)
    return http_healthy(service)


def owned(service, cwd, command):
    return (owns_voice_process(cwd, command, service['directory']) if service['kind'] == 'voice'
            else owns_process(cwd, command, service))


def status(service):
    if marker(service).exists():
        return '已卸载 · 数据保留，可恢复'
    if any(not pathlib.Path(p).exists() for p in service.get('requires_paths', [])):
        return '依赖或程序缺失'
    return '运行中' if healthy(service) else '已停止或健康检查未通过'


def listeners(port):
    result = subprocess.run(['/usr/sbin/lsof', '-nP', '-a', '-iTCP:' + str(port),
                             '-sTCP:LISTEN', '-Fp'], capture_output=True, text=True)
    return sorted({int(line[1:]) for line in result.stdout.splitlines() if line.startswith('p')})


def process_identity(pid):
    cwd = subprocess.run(['/usr/sbin/lsof', '-a', '-p', str(pid), '-d', 'cwd', '-Fn'],
                         capture_output=True, text=True)
    command = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'command='],
                             capture_output=True, text=True)
    paths = [line[1:] for line in cwd.stdout.splitlines() if line.startswith('n')]
    return paths[0] if paths else '', command.stdout.strip()


def wait_health(service, desired, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if healthy(service) == desired:
            return
        time.sleep(1)
    raise RuntimeError('等待服务' + ('启动' if desired else '停止') + '超时；请查看服务日志。')


def run_argv(argv, service, timeout=180):
    subprocess.run(argv, cwd=service['directory'], env=ENV, check=True, timeout=timeout)


def profile_running(service):
    if not service.get('colima_profile'):
        return True
    result = subprocess.run(['/opt/homebrew/bin/colima', 'status', service['colima_profile']],
                             capture_output=True, env=ENV, timeout=10)
    return result.returncode == 0


def run_compose(service, action):
    if not profile_running(service):
        if action == 'stop':
            return
        profile = service['colima_profile']
        if not (pathlib.Path.home() / '.colima' / profile / 'colima.yaml').exists():
            raise RuntimeError('原 Colima profile 不存在；不会自动创建新环境')
        run_argv(['/opt/homebrew/bin/colima', 'start', profile], service, 300)
    run_argv(service[action], service)


def launch_target(service):
    return 'gui/' + str(os.getuid()) + '/' + service['label']


def launch_loaded(service):
    return subprocess.run(['/bin/launchctl', 'print', launch_target(service)],
                          capture_output=True).returncode == 0


def launch_running(service):
    result = subprocess.run(['/bin/launchctl', 'print', launch_target(service)],
                            capture_output=True, text=True, timeout=8)
    return result.returncode == 0 and bool(re.search(r'^\s*(?:state = running|pid = [1-9][0-9]*)\s*$', result.stdout, re.M))


def restore(service):
    path = marker(service)
    if not path.exists():
        return
    record = json.loads(path.read_text())
    if record['kind'] == 'app':
        original, archived = pathlib.Path(record['source']), pathlib.Path(record['archive'])
        if original.exists():
            raise RuntimeError('原程序路径已有文件，拒绝覆盖；请先核对归档记录')
        shutil.move(str(archived), str(original))
    path.unlink()
    print('卸载标记已恢复；原数据未改变。')


def start(service):
    restore(service)
    if healthy(service):
        print('已运行，无需重复启动。')
        return
    if service['kind'] == 'compose':
        run_compose(service, 'start')
    elif service['kind'] == 'launchd':
        if launch_loaded(service):
            run_argv(['/bin/launchctl', 'kickstart', launch_target(service)], service)
        else:
            run_argv(['/bin/launchctl', 'bootstrap', 'gui/' + str(os.getuid()), service['plist']], service)
    elif service['kind'] == 'app':
        run_argv(['/usr/bin/open', '-a', service['app']], service)
    elif service['kind'] == 'container':
        run_argv(service['start'], service)
    elif service['kind'] in ['process', 'voice']:
        if listeners(service['port']):
            raise RuntimeError('端口已被占用但健康检查未通过；没有启动第二个进程。')
        logs = ROOT / 'logs'
        logs.mkdir(exist_ok=True); logs.chmod(0o700)
        env = dict(ENV, **service.get('environment', {}))
        if service.get('token_env'):
            token = secrets.token_hex(32)
            path = ROOT / 'state' / (service['id'] + '.token')
            descriptor = os.open(str(path), os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, 'w') as f:
                f.write(token)
            env[service['token_env']] = token
        log = logs / (service['id'] + '.log')
        descriptor = os.open(str(log), os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        os.chmod(log, 0o600)
        with os.fdopen(descriptor, 'ab') as output:
            subprocess.Popen(service['start'], cwd=service['directory'], env=env,
                             stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                             start_new_session=True, close_fds=True)
    else:
        raise RuntimeError('该类型尚未适配，未执行任何启动命令')
    wait_health(service, True, 90)
    print('启动完成：' + service['title'])


def stop(service):
    if service['kind'] == 'compose':
        run_compose(service, 'stop')
        wait_health(service, False, 45)
        if service.get('stop_profile') and profile_running(service):
            run_argv(['/opt/homebrew/bin/colima', 'stop', service['colima_profile']], service, 120)
    elif service['kind'] == 'launchd':
        if launch_loaded(service):
            run_argv(['/bin/launchctl', 'bootout', launch_target(service)], service)
        wait_health(service, False, 30)
    elif service['kind'] == 'app':
        for pid in app_members(service):
            if pid in app_members(service):
                os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 30
        while app_members(service) and time.monotonic() < deadline:
            time.sleep(0.5)
        if app_members(service):
            raise RuntimeError('应用尚未退出；没有强制关闭或移动程序')
    elif service['kind'] == 'container':
        run_argv(service['stop'], service)
        wait_health(service, False, 30)
    elif service['kind'] in ['process', 'voice']:
        pids = listeners(service['port'])
        if not pids:
            print('已经停止。')
            return
        for pid in pids:
            cwd, command = process_identity(pid)
            if not owned(service, cwd, command):
                raise RuntimeError('端口对应的进程不属于登记项目；拒绝停止。')
        for pid in pids:
            cwd, command = process_identity(pid)
            if not owned(service, cwd, command):
                raise RuntimeError('进程身份已变化；拒绝停止。')
            os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 30
        while listeners(service['port']) and time.monotonic() < deadline:
            time.sleep(0.5)
        if listeners(service['port']):
            raise RuntimeError('服务未在 30 秒内退出；没有强制杀进程。')
    else:
        raise RuntimeError('该类型尚未适配，未执行任何停止命令')
    print('已停止，程序及数据均保留。')


def open_service(service):
    if service['kind'] == 'app':
        restore(service)
        run_argv(['/usr/bin/open', '-a', service['app']], service)
        return
    if service.get('open_command'):
        run_argv(service['open_command'], service)
        return
    if not healthy(service):
        raise RuntimeError('请先启动服务，再打开入口')
    url = service['url']
    if service.get('token_env'):
        token = (ROOT / 'state' / (service['id'] + '.token')).read_text().strip()
        url += ('&' if '?' in url else '?') + urllib.parse.urlencode({'token': token})
    subprocess.run(['/usr/bin/open', url], check=True, timeout=10)
    print('已打开应用。')


def uninstall(service, confirm):
    mode = service.get('uninstall_mode')
    if mode not in ['compose-instance', 'archive-app']:
        raise RuntimeError('该服务暂提供卸载说明，未配置程序删除操作')
    preview = {'service': service['title'], 'mode': mode,
               'effect': service['uninstall'], 'preserves': service.get('preserves', [])}
    if not confirm:
        print(json.dumps(preview, ensure_ascii=False, indent=2))
        return
    if marker(service).exists():
        print('已经卸载；原数据保留。')
        return
    if mode == 'compose-instance':
        validate_compose_uninstall(service['uninstall_command'])
        temporary_profile = not profile_running(service)
        if temporary_profile and not service.get('stop_profile'):
            raise RuntimeError('共享虚拟机未运行，请先启动原虚拟机后再卸载。')
        try:
            # Reuse the original profile and its existence guard, without
            # starting any Compose service or creating another environment.
            run_compose(service, 'uninstall_command')
        finally:
            if temporary_profile and profile_running(service):
                try:
                    run_argv(['/opt/homebrew/bin/colima', 'stop', service['colima_profile']], service, 120)
                except Exception as error:
                    print('注意：恢复专用虚拟机关闭状态失败，请检查 Colima：' + str(error), file=sys.stderr)
        record = {'kind': 'compose-instance'}
    else:
        source = pathlib.Path(service['app'])
        allowed = [pathlib.Path('/Applications'), pathlib.Path.home() / 'Applications']
        if source.parent not in allowed or source.suffix != '.app' or source.is_symlink():
            raise ValueError('只允许归档应用目录中的独立 .app，不接受符号链接或其他路径')
        if not source.exists():
            raise RuntimeError('程序路径不存在，未移动任何文件')
        stop(service)
        archive = ROOT / 'archive' / 'uninstalled' / service['id'] / datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
        archive.mkdir(parents=True, exist_ok=False)
        destination = archive / source.name
        shutil.move(str(source), str(destination))
        record = {'kind': 'app', 'source': str(source), 'archive': str(destination)}
    marker(service).write_text(json.dumps(record, ensure_ascii=False))
    marker(service).chmod(0o600)
    print('已卸载运行实例或归档程序；用户数据保留。可通过“恢复/安装”恢复。')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('service')
    parser.add_argument('action', choices=['start', 'stop', 'restart', 'status', 'open', 'uninstall-preview', 'uninstall', 'restore'])
    parser.add_argument('--confirm', action='store_true')
    args = parser.parse_args()
    services = json.loads((ROOT / 'services.json').read_text())
    if args.service not in services:
        parser.error('服务尚未登记')
    if args.action == 'status':
        service = services[args.service]
        print(json.dumps({'name': service['title'], 'status': status(service),
                          'url': service['url'], 'directory': service['directory']}, ensure_ascii=False))
        return
    locks = ROOT / 'state'
    locks.mkdir(exist_ok=True)
    with (locks / (args.service + '.lock')).open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('该服务已有操作正在执行，请稍后刷新。')
        # The editor uses this lock too. Read the current deployment only
        # after acquiring it, so a completed edit cannot leave a stale target.
        services = json.loads((ROOT / 'services.json').read_text())
        if args.service not in services:
            parser.error('服务尚未登记')
        service = services[args.service]
        if args.action in ['uninstall', 'uninstall-preview']:
            uninstall(service, args.action == 'uninstall' and args.confirm)
            return
        if args.action == 'open':
            open_service(service)
            return
        if args.action == 'restore':
            restore(service)
            start(service)
            return
        if args.action in ['stop', 'restart']:
            stop(service)
        if args.action in ['start', 'restart']:
            start(service)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('操作失败：' + str(error), file=sys.stderr)
        sys.exit(1)
