#!/usr/bin/python3
"""Render an overview and per-service controls from the local registry."""
import html
import json
import pathlib
import shlex
import tempfile
import urllib.parse

ROOT = pathlib.Path(__file__).resolve().parent
OVERVIEW_PATH = '/dashboards/' + urllib.parse.quote('本机服务总览', safe='')


def generate(services):
    actions = [{'id': 'refresh-registry', 'title': '刷新服务登记', 'icon': '&#8635;', 'timeout': 60,
                'shell': shlex.join(['/usr/bin/python3', str(ROOT / 'sync.py'), '--once'])}]
    entities = [{'name': 'overview', 'file': str(ROOT / 'state' / 'overview.json')}]
    dashboards = [{'title': '本机服务总览', 'contents': [
        {'type': 'fieldset', 'entity': 'overview', 'title': '本机部署的服务', 'contents': [
            {'type': 'display', 'title': '{{ overview.html }}'}, {'title': '刷新服务登记'}]}]}]
    labels = [('open', '打开应用', '&#8599;'), ('start', '启动', '&#9654;'),
              ('stop', '停止', '&#9632;'), ('restart', '重启', '&#8635;'),
              ('status', '检查状态', '&#128269;')]
    for index, service in enumerate(services.values()):
        name, title = service['id'], service['title']
        entity_name = 's_' + str(index)
        entities.append({'name': entity_name, 'file': str(ROOT / 'state' / 'entities' / (name + '.json'))})
        buttons = []
        choices = list(labels)
        if service.get('uninstall_mode'):
            choices += [('uninstall-preview', '卸载预览', '&#128214;'),
                        ('uninstall', '卸载（保留数据）', '&#128230;'),
                        ('restore', '恢复/安装', '&#8634;')]
        for action, label, icon in choices:
            if action == 'open':
                label = service.get('open_label', label)
            action_title = label + ' · ' + title
            argv = ['/usr/bin/python3', str(ROOT / 'control.py'), name, action]
            if action == 'uninstall':
                argv.append('--confirm')
            item = {'id': name + '-' + action, 'title': action_title, 'icon': icon,
                    'timeout': 600, 'maxConcurrent': 1, 'shell': shlex.join(argv),
                    'popupOnStart': 'execution-dialog'}
            if action == 'uninstall':
                item['arguments'] = [{'type': 'confirmation',
                                      'title': service['uninstall'] + ' 已了解范围，保留用户数据。'}]
            actions.append(item)
            buttons.append({'title': action_title})
        description = '<strong>{{ ' + entity_name + '.status }}</strong><p>' + html.escape(service['description']) + '</p>'
        if service.get('url'):
            description += '<p>入口：<code>' + html.escape(service['url']) + '</code></p>'
        description += '<p><a href="' + OVERVIEW_PATH + '">返回服务总览</a></p>'
        detail = '<h3>使用方法</h3><p>' + html.escape(service['help']) + '</p><h3>卸载范围</h3><p>' + html.escape(service['uninstall']) + '</p><h3>原项目目录</h3><p>' + html.escape(service['directory']) + '</p>'
        dashboards.append({'title': title, 'category': '服务管理', 'contents': [
            {'type': 'fieldset', 'entity': entity_name, 'title': title, 'contents': [
                {'type': 'display', 'title': description}] + buttons + [
                {'type': 'directory', 'title': '使用说明 · ' + title, 'contents': [
                    {'type': 'display', 'title': detail}]}]}]})
    config = {
        'listenAddressSingleHTTPFrontend': '127.0.0.1:1338',
        'listenAddressWebUI': '127.0.0.1:1344', 'listenAddressRestActions': '127.0.0.1:1342',
        'listenAddressGrpcActions': '127.0.0.1:1343',
        'webUIDir': str(ROOT / 'vendor' / 'OliveTin-darwin-arm64' / 'webui'),
        'logLevel': 'INFO', 'checkForUpdates': False, 'entities': entities,
        'actions': actions, 'dashboards': dashboards}
    # The supervisor stops OliveTin before applying this desired configuration.
    # Updating its live config triggers an upstream concurrent-map panic.
    with tempfile.NamedTemporaryFile(mode='w', dir=ROOT, suffix='.next.tmp', delete=False) as f:
        f.write(json.dumps(config, ensure_ascii=False, indent=2) + '\n')
        temporary = pathlib.Path(f.name)
    temporary.replace(ROOT / 'config.next.json')


if __name__ == '__main__':
    generate(json.loads((ROOT / 'services.json').read_text()))
    print('管理台配置已生成')
