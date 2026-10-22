import unittest
from ui_server import validate_action


class ActionBoundaryTests(unittest.TestCase):
    def test_only_registered_actions_and_ids_are_accepted(self):
        records={'local-one':{'uninstall_mode':'archive-app'}}
        self.assertEqual(validate_action({'service':'local-one','action':'start'},records),('local-one','start'))
        for value in [{'service':'local-one','action':'sh'}, {'service':'../other','action':'start'}, {'service':'local-one','action':'uninstall'}]:
            with self.assertRaises(ValueError):validate_action(value,records)
        self.assertEqual(validate_action({'service':'local-one','action':'uninstall','confirm':True},records)[1],'uninstall')

    def test_uninstall_requires_an_adapter(self):
        for action in ['uninstall','uninstall-preview','restore']:
            with self.assertRaises(ValueError):validate_action({'service':'local-one','action':action,'confirm':True},{'local-one':{}})


class RegistrationHttpTests(unittest.TestCase):
    def test_form_validation_and_save_require_matching_validation_and_same_origin(self):
        import json,pathlib,tempfile,threading,urllib.request,urllib.error
        from unittest.mock import patch
        import ui_server
        from registration import Registrar
        from service_editor import ServiceEditor
        from ui_model import Store
        with tempfile.TemporaryDirectory() as tmp:
            root=pathlib.Path(tmp).resolve();(root/'services.json').write_text('{}')
            store=Store(root/'state');store.read({})
            reg=Registrar(root,store)
            form={'title':'HTTP test','description':'Test','category':'uncategorized','kind':'process',
                  'directory':str(root),'url':'','health':'','help':'Use it','start_command':'/usr/bin/python3 -m http.server 65431',
                  'port':'65431','process_tokens':'http.server'}
            with patch.object(ui_server,'ROOT',root),patch.object(ui_server,'STORE',store),patch.object(ui_server,'REGISTRAR',reg,create=True),patch.object(ui_server,'EDITOR',ServiceEditor(root,store,reg)):
                server=ui_server.ThreadingHTTPServer(('127.0.0.1',0),ui_server.Handler)
                thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                origin='http://127.0.0.1:'+str(server.server_port)
                def post(path,value,source=origin):
                    request=urllib.request.Request(origin+path,data=json.dumps(value).encode(),headers={'Content-Type':'application/json','Origin':source,'X-Local-CSRF':ui_server.TOKEN})
                    try:
                        with urllib.request.urlopen(request) as response:return response.status,json.load(response)
                    except urllib.error.HTTPError as error:return error.code,json.load(error)
                try:
                    code,result=post('/api/services/validate',{'form':form});self.assertEqual(code,200);self.assertTrue(result['ok'])
                    self.assertEqual(post('/api/services',{'form':form})[0],400)
                    self.assertEqual(post('/api/services',{'form':form,'validation':result['validation']},'http://external.invalid')[0],403)
                    code,saved=post('/api/services',{'form':form,'validation':result['validation']});self.assertEqual(code,201)
                    self.assertIn(saved['service']['id'],json.loads((root/'services.json').read_text()))
                    before=(root/'services.json').read_text()
                    revision=store.read(json.loads(before))['revision']
                    code,updated=post('/api/services/info',{'service':saved['service']['id'],'revision':revision,'info':{'description':'Edited from browser','help':'New instructions'}})
                    self.assertEqual(code,200)
                    with urllib.request.urlopen(origin+'/api/bootstrap') as response:public=json.load(response)
                    self.assertEqual(public['services'][0]['display_description'],'Edited from browser')
                    self.assertEqual(public['services'][0]['display_help'],'New instructions')
                    self.assertEqual((root/'services.json').read_text(),before)
                    self.assertEqual(post('/api/services/info',{'service':saved['service']['id'],'revision':revision,'info':{'description':'Stale','help':''}})[0],409)
                    ident=saved['service']['id']
                    with urllib.request.urlopen(origin+'/api/services/'+ident+'/edit') as response:editing=json.load(response)
                    edited=dict(editing['form'],title='Renamed service',description='Full editor description',port='65432')
                    payload={'service':ident,'version':editing['version'],'form':edited}
                    self.assertEqual(post('/api/services/edit',payload)[0],400)
                    code,validated=post('/api/services/edit/validate',payload);self.assertEqual(code,200);self.assertTrue(validated['ok'],validated)
                    code,done=post('/api/services/edit',dict(payload,validation=validated['validation']));self.assertEqual(code,200);self.assertEqual(done['service']['id'],ident)
                    with urllib.request.urlopen(origin+'/api/bootstrap') as response:after=json.load(response)
                    self.assertEqual(after['services'][0]['display_name'],'Renamed service')
                    self.assertEqual(after['services'][0]['display_description'],'Full editor description')
                    self.assertEqual(json.loads((root/'services.json').read_text())[ident]['port'],65432)
                    self.assertEqual(post('/api/services/edit',dict(payload,validation=validated['validation']))[0],409)
                finally:server.shutdown();server.server_close();thread.join()
