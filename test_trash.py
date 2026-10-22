import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch
from trash import TrashManager
from ui_model import Store


class TrashTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=pathlib.Path(self.temp.name).resolve();self.store=Store(self.root/'state')
        self.project=self.root/'project';self.project.mkdir()
        self.photos=self.project/'photos';self.photos.mkdir();(self.photos/'photo.jpg').write_text('keep')
        self.service=dict(id='local-demo',title='Demo',kind='compose',directory=str(self.project),group='Files',
                          uninstall_mode='compose-instance',preserves=['Photos'],uninstall_command=['/opt/homebrew/bin/docker','--context','demo','compose','-p','demo','down'])
        self.records={'local-demo':self.service};self.write()
        self.marker=self.root/'state/local-demo.uninstalled.json';self.marker.write_text('{"kind":"compose-instance"}')
        self.manager=TrashManager(self.root,self.store)
        self.config={'name':'demo','services':{'app':{'volumes':[{'type':'bind','source':str(self.photos),'target':'/photos'}]}},'volumes':{}}
        self.config_patch=patch.object(self.manager,'compose_config',return_value=self.config);self.config_patch.start();self.addCleanup(self.config_patch.stop)
        self.docker_patch=patch.object(self.manager,'docker_read',return_value='');self.docker_patch.start();self.addCleanup(self.docker_patch.stop)

    def write(self):
        (self.root/'services.json').write_text(json.dumps(self.records));self.store.read(self.records)

    def test_default_delete_forgets_record_but_keeps_business_data(self):
        plan=self.manager.preview('local-demo')
        self.manager.delete('local-demo',plan['version'],[], 'Demo')
        self.assertTrue((self.photos/'photo.jpg').exists())
        self.assertNotIn('local-demo',json.loads((self.root/'services.json').read_text()))
        self.assertFalse(self.marker.exists())

    def test_only_explicitly_selected_data_is_deleted(self):
        plan=self.manager.preview('local-demo');photo=next(x for x in plan['data'] if x.get('path')==str(self.photos))
        self.assertTrue(photo['selectable'],photo)
        self.manager.delete('local-demo',plan['version'],[photo['id']],'Demo')
        self.assertFalse(self.photos.exists());self.assertTrue(self.project.exists())

    def test_active_service_and_stale_or_forged_selection_are_rejected(self):
        plan=self.manager.preview('local-demo')
        for version,selection,name in [('wrong',[],'Demo'),(plan['version'],['/Users/example'],'Demo'),(plan['version'],[],'wrong')]:
            with self.assertRaises(ValueError):self.manager.delete('local-demo',version,selection,name)
        self.marker.unlink()
        with self.assertRaises(ValueError):self.manager.preview('local-demo')
        self.assertTrue(self.photos.exists())

    def test_shared_paths_and_symlinks_cannot_be_selected(self):
        self.records['local-other']=dict(self.service,id='local-other',title='Other',directory=str(self.photos));self.write()
        plan=self.manager.preview('local-demo');photo=next(x for x in plan['data'] if x.get('path')==str(self.photos))
        self.assertFalse(photo['selectable'])
        link=self.project/'link';link.symlink_to(self.photos,target_is_directory=True)
        self.config['services']['app']['volumes'][0]['source']=str(link)
        plan=self.manager.preview('local-demo');item=next(x for x in plan['data'] if x.get('path')==str(link))
        self.assertFalse(item['selectable'])

    def test_offline_named_volume_is_listed_but_cannot_be_deleted(self):
        self.config['services']['app']['volumes']=[{'type':'volume','source':'db','target':'/db'}]
        self.config['volumes']={'db':{'name':'demo_db'}}
        with patch.object(self.manager,'docker_read',side_effect=ValueError('offline')):
            plan=self.manager.preview('local-demo')
        volume=next(x for x in plan['data'] if x.get('volume')=='demo_db')
        self.assertFalse(volume['selectable']);self.assertIn('原环境',volume['reason'])

    def test_whole_project_and_external_host_directory_are_not_deletable(self):
        self.config['services']['app']['volumes']=[{'type':'bind','source':str(self.project),'target':'/source'}, {'type':'bind','source':str(pathlib.Path.home()),'target':'/home'}]
        plan=self.manager.preview('local-demo')
        self.assertTrue(all(not item['selectable'] for item in plan['data']))

    def test_archive_outside_managed_directory_is_rejected(self):
        self.records['local-demo']=dict(self.service,kind='app',app='/Applications/Demo.app');self.write()
        self.marker.write_text(json.dumps({'kind':'app','source':'/Applications/Demo.app','archive':str(self.photos)}))
        with self.assertRaises(ValueError):self.manager.preview('local-demo')

    def test_failed_data_cleanup_keeps_recycle_record_for_retry(self):
        plan=self.manager.preview('local-demo');photo=next(x for x in plan['data'] if x.get('path')==str(self.photos))
        with patch.object(self.manager,'remove_path',side_effect=OSError('denied')):
            with self.assertRaises(ValueError):self.manager.delete('local-demo',plan['version'],[photo['id']],'Demo')
        self.assertTrue(self.marker.exists());self.assertIn('local-demo',json.loads((self.root/'services.json').read_text()))

    def test_volume_requires_matching_labels_and_no_container_reference(self):
        self.config['services']['app']['volumes']=[{'type':'volume','source':'db','target':'/db'}]
        self.config['volumes']={'db':{'name':'demo_db'}}
        def read(argv,directory):
            return json.dumps([{'Labels':{'com.docker.compose.project':'demo','com.docker.compose.volume':'db'}}]) if 'inspect' in argv else ''
        with patch.object(self.manager,'docker_read',side_effect=read):
            plan=self.manager.preview('local-demo');volume=next(x for x in plan['data'] if x.get('volume')=='demo_db')
            self.assertTrue(volume['selectable'])
            with patch('trash.subprocess.run') as remove:
                remove.return_value.returncode=0
                self.manager.delete('local-demo',plan['version'],[volume['id']],'Demo')
                self.assertEqual(remove.call_args.args[0],['/opt/homebrew/bin/docker','--context','demo','volume','rm','demo_db'])

    def test_app_archive_is_removed_while_unselected_app_data_stays(self):
        archive=self.root/'archive/uninstalled/local-demo/saved/Demo.app';archive.mkdir(parents=True)
        (archive/'program').write_text('program')
        self.records['local-demo']=dict(self.service,kind='app',app=str(self.root/'Applications/Demo.app'));self.write()
        self.marker.write_text(json.dumps({'kind':'app','source':self.records['local-demo']['app'],'archive':str(archive)}))
        with patch.object(self.manager,'app_paths',return_value=[str(self.photos)]):
            plan=self.manager.preview('local-demo');self.manager.delete('local-demo',plan['version'],[],'Demo')
        self.assertFalse(archive.exists());self.assertTrue(self.photos.exists())

    def test_http_recycle_preview_and_delete_require_same_origin_and_exact_selection(self):
        import threading,urllib.request,urllib.error
        import ui_server
        with patch.object(ui_server,'ROOT',self.root),patch.object(ui_server,'STORE',self.store),patch.object(ui_server,'TRASH',self.manager):
            server=ui_server.ThreadingHTTPServer(('127.0.0.1',0),ui_server.Handler)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            origin='http://127.0.0.1:'+str(server.server_port)
            def post(payload,source=origin):
                request=urllib.request.Request(origin+'/api/trash/delete',data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','Origin':source,'X-Local-CSRF':ui_server.TOKEN})
                try:
                    with urllib.request.urlopen(request) as response:return response.status,json.load(response)
                except urllib.error.HTTPError as error:return error.code,json.load(error)
            try:
                with urllib.request.urlopen(origin+'/api/bootstrap') as response:state=json.load(response)
                self.assertTrue(state['services'][0]['trashed'])
                with urllib.request.urlopen(origin+'/api/trash/local-demo') as response:plan=json.load(response)
                payload=dict(service='local-demo',version=plan['version'],selected=[],confirmation='Demo')
                self.assertEqual(post(payload,'http://external.invalid')[0],403)
                self.assertEqual(post(dict(payload,selected=['forged']))[0],400)
                self.assertTrue(self.photos.exists())
                self.assertEqual(post(payload)[0],200)
                with urllib.request.urlopen(origin+'/api/bootstrap') as response:state=json.load(response)
                self.assertEqual(state['services'],[]);self.assertTrue(self.photos.exists())
            finally:server.shutdown();server.server_close();thread.join()
