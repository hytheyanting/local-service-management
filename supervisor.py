#!/usr/bin/python3
"""Apply config changes while OliveTin is stopped, avoiding its reload map race."""
import json
import pathlib
import signal
import subprocess
import time

ROOT = pathlib.Path(__file__).resolve().parent
STOPPING = False


def spawn():
    return subprocess.Popen([str(ROOT/'vendor/OliveTin-darwin-arm64/OliveTin'),
                             '-configdir', str(ROOT)], cwd=str(ROOT))


def reconcile(process, desired, active):
    text = desired.read_text()
    json.loads(text)  # Reject partial or corrupt updates before touching the child.
    changed = not active.exists() or active.read_text() != text
    if changed:
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=30)
        temporary = active.with_suffix('.apply.tmp')
        temporary.write_text(text)
        temporary.replace(active)
        print('服务登记配置已更新；仅重新加载管理网页。', flush=True)
        return spawn()
    if process is None or process.poll() is not None:
        return spawn()
    return process


def request_stop(signum, frame):
    global STOPPING
    STOPPING = True


def main():
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    desired = ROOT/'config.next.json'
    active = ROOT/'config.yaml'
    process = None
    try:
        while not STOPPING:
            try:
                process = reconcile(process, desired, active)
            except Exception as error:
                print('管理网页重载失败：' + str(error), flush=True)
            time.sleep(1)
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=30)


if __name__ == '__main__':
    main()
