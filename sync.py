#!/usr/bin/python3
"""Refresh OliveTin entities from the local service registry and health checks."""
import argparse
import concurrent.futures
import datetime
import html
import hashlib
import json
import pathlib
import re
import time
import urllib.parse

from control import status
from build_config import generate

ROOT = pathlib.Path(__file__).resolve().parent


def entity(service):
    if not re.fullmatch(r'local-[a-z0-9-]+', service['id']):
        raise ValueError('非法服务 id')
    for key in ['title', 'description', 'url', 'health', 'directory', 'help', 'uninstall']:
        if not isinstance(service[key], str):
            raise ValueError('服务登记缺少文本字段 ' + key)
    state = status(service)
    return dict(service,
                status=('🟢 ' if state == '运行中' else '⚪ ') + state,
                title_html=html.escape(service['title']),
                description_html=html.escape(service['description']),
                url_html=html.escape(service['url'], quote=True),
                help_html=html.escape(service['help']),
                uninstall_html=html.escape(service['uninstall']),
                directory_html=html.escape(service['directory']))


def sync():
    services = json.loads((ROOT / 'services.json').read_text())
    for name, service in services.items():
        if name != service['id']:
            raise ValueError('服务键与 id 不一致')
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        result = list(pool.map(entity, services.values()))
    target = ROOT / 'entities.json'
    text = ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in result)
    if not target.exists() or target.read_text() != text:
        # OliveTin watches the file inode; replacing it detaches its watcher.
        target.write_text(text)
    state_dir = ROOT / 'state'
    state_dir.mkdir(exist_ok=True); state_dir.chmod(0o700)
    individual = state_dir / 'entities'
    individual.mkdir(exist_ok=True)
    for row in result:
        p = individual / (row['id'] + '.json')
        value = json.dumps(row, ensure_ascii=False) + '\n'
        if not p.exists() or p.read_text() != value:
            p.write_text(value)
    running = sum(row['status'] == '🟢 运行中' for row in result)
    overview = '<p><strong>共 ' + str(len(result)) + ' 项 · ' + str(running) + ' 项运行中</strong></p><p>点击服务进入管理。状态每 5 秒检查；打开、启停、说明和卸载范围集中在详情页。</p>'
    groups = list(dict.fromkeys(s.get('group', '其他') for s in result))
    for group in groups:
        overview += '<h3>' + html.escape(group) + '</h3>'
        for row in result:
            if row.get('group', '其他') != group:
                continue
            url = '/dashboards/' + urllib.parse.quote(row['title'], safe='')
            overview += '<p><a href="' + url + '"><strong>' + row['title_html'] + '</strong></a><br><small>' + html.escape(row['status']) + '</small></p>'
    overview += '<p>安装流程提交登记后会自动出现。未登记程序不会被自动卸载。</p>'
    p = state_dir / 'overview.json'
    value = json.dumps({'title': '本机服务总览', 'html': overview}, ensure_ascii=False) + '\n'
    if not p.exists() or p.read_text() != value:
        p.write_text(value)
    signature = hashlib.sha256(json.dumps(services, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    fingerprint = state_dir / 'registry-schema.sha256'
    if not fingerprint.exists() or fingerprint.read_text() != signature:
        generate(services)
        fingerprint.write_text(signature)
    (ROOT / 'state' / 'registry-last-success.txt').write_text(datetime.datetime.now().isoformat() + '\n')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    while True:
        try:
            sync()
        except Exception as error:
            print('登记刷新失败：' + str(error), flush=True)
            if args.once:
                raise
        if args.once:
            return
        time.sleep(5)


if __name__ == '__main__':
    main()
