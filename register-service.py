#!/usr/bin/python3
"""Register a trusted installation manifest; does not install or start software."""
import argparse
import json
import pathlib

from sync import entity, sync
from registration import write_service

ROOT = pathlib.Path(__file__).resolve().parent
parser = argparse.ArgumentParser(description='将安装完成的服务登记到本机管理台')
parser.add_argument('manifest', type=pathlib.Path)
args = parser.parse_args()
service = json.loads(args.manifest.read_text())
if service.get('kind') not in ['compose', 'voice', 'process', 'app', 'launchd', 'container']:
    parser.error('服务类型尚未适配')
if service['kind'] in ['compose', 'voice', 'process', 'container'] and (not isinstance(service.get('start'), list) or not all(isinstance(x, str) for x in service['start'])):
    parser.error('start 必须是命令参数数组')
if service['kind'] == 'compose' and not isinstance(service.get('stop'), list):
    parser.error('compose 服务必须提供 stop 参数数组')
if service['kind'] == 'process' and not service.get('process_tokens'):
    parser.error('process 服务必须登记进程身份标记')
if service['kind'] == 'app' and not all(service.get(k) for k in ['app', 'executable']):
    parser.error('桌面应用必须登记 app 与 executable')
if service['kind'] == 'launchd' and not all(service.get(k) for k in ['label', 'plist']):
    parser.error('launchd 服务必须登记 label 与 plist')
entity(service)
try:
    write_service(ROOT, service)
except ValueError as error:
    parser.error(str(error))
sync()
print('已登记：' + service['title'] + '；管理台会自动重载后显示，业务服务保持运行。')
