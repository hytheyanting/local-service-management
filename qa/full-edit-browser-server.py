"""Isolated browser acceptance server. Never launches a business service."""
import json
import pathlib
import sys
import tempfile
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
import ui_server
from registration import Registrar
from service_editor import ServiceEditor
from ui_model import Store
from trash import TrashManager
from http.server import ThreadingHTTPServer

with tempfile.TemporaryDirectory(prefix='service-edit-qa-') as directory:
    root=pathlib.Path(directory).resolve()
    project=root/'process';project.mkdir()
    script=project/'server.py'
    script.write_text("from pathlib import Path\nPath(__file__).with_name('started.flag').touch()\n")
    service=dict(id='local-edit-qa',title='完整编辑验收',description='仅用于表单验收',help='此验收服务不启动。',group='验收分类',
        kind='process',directory=str(project),start=['/usr/bin/python3',str(script)],port=65431,process_tokens=['server.py'],url='',health='',environment={'DEMO_SETTING':'original-value'})
    archived=root/'archive/uninstalled/local-trash-qa/saved/QA.app';archived.mkdir(parents=True)
    (archived/'program').write_text('isolated program fixture')
    photos=root/'trash-data';photos.mkdir();(photos/'photo.txt').write_text('isolated disposable fixture')
    recycled=dict(service,id='local-trash-qa',title='回收站验收',kind='app',directory=str(root/'trash-project'),app=str(root/'Applications/QA.app'),uninstall_mode='archive-app')
    records={service['id']:service,recycled['id']:recycled}
    (root/'services.json').write_text(json.dumps(records,ensure_ascii=False))
    (root/'entities.json').write_text(json.dumps({'id':service['id'],'status':'⚪ 已停止'},ensure_ascii=False))
    ui_server.ROOT=root
    ui_server.STORE=Store(root/'state')
    ui_server.STORE.read(records)
    (root/'state/local-trash-qa.uninstalled.json').write_text(json.dumps(dict(kind='app',source=recycled['app'],archive=str(archived))))
    class FixtureTrash(TrashManager):
        def app_paths(self,service,archive=None):
            return [str(photos)] if service['id']=='local-trash-qa' else []
    ui_server.TRASH=FixtureTrash(root,ui_server.STORE)
    (root/'state/registry-last-success.txt').touch()
    ui_server.REGISTRAR=Registrar(root,ui_server.STORE)
    ui_server.EDITOR=ServiceEditor(root,ui_server.STORE,ui_server.REGISTRAR)
    print('QA_ROOT='+str(root),flush=True)
    ThreadingHTTPServer(('127.0.0.1',1340),ui_server.Handler).serve_forever()
