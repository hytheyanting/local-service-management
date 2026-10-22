import copy
import fcntl
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch
from registration import Registrar
from service_editor import ServiceEditor
from ui_model import Store, Conflict


class FullEditTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=pathlib.Path(self.tmp.name).resolve();self.store=Store(self.root/'state')
        self.script=self.root/'server.py';self.script.write_text("from pathlib import Path\nPath('started.flag').touch()\n")
        self.service={'id':'local-existing','title':'Existing','description':'Old','help':'Old help','group':'Dev',
            'kind':'process','directory':str(self.root),'start':['/usr/bin/python3',str(self.script)],
            'port':65431,'process_tokens':['server.py'],'url':'','health':'',
            'token_env':'ACCESS_TOKEN','environment':{'PRIVATE_SETTING':'keep-me'},'open_label':'Open'}
        self.records={'local-existing':self.service}
        self.write_records();self.store.read(self.records)
        self.editor=ServiceEditor(self.root,self.store,Registrar(self.root,self.store))

    def write_records(self):
        (self.root/'services.json').write_text(json.dumps(self.records))

    def test_basic_edit_keeps_id_commands_private_environment_and_category_assignment(self):
        state=self.store.read(self.records)
        state=self.store.save_preferences(self.records,dict(state,categories=state['categories']+[{'id':'cat-target','name':'Target'}]))
        loaded=self.editor.get('local-existing');form=dict(loaded['form'],title='Renamed',description='New description',help='New help',category='cat-target')
        self.editor.save('local-existing',loaded['version'],form)
        saved=json.loads((self.root/'services.json').read_text())['local-existing']
        self.assertEqual(saved['id'],'local-existing');self.assertEqual(saved['title'],'Renamed')
        for field in ['start','port','token_env','environment']:self.assertEqual(saved[field],self.service[field])
        prefs=self.store.read({'local-existing':saved});self.assertEqual(self.store.category_for(saved,prefs),'cat-target')
        self.assertEqual(prefs['appearance'],state['appearance'])
        self.assertFalse((self.root/'started.flag').exists())

    def test_runtime_change_needs_matching_validation_then_saves_without_starting(self):
        loaded=self.editor.get('local-existing');form=dict(loaded['form'],port='65432')
        with self.assertRaises(ValueError):self.editor.save('local-existing',loaded['version'],form)
        result=self.editor.validate('local-existing',loaded['version'],form);self.assertTrue(result['ok'],result)
        with self.assertRaises(ValueError):self.editor.save('local-existing',loaded['version'],dict(form,port='65433'),result['validation'])
        self.editor.save('local-existing',loaded['version'],form,result['validation'])
        saved=json.loads((self.root/'services.json').read_text())['local-existing'];self.assertEqual(saved['port'],65432)
        self.assertEqual(saved['environment'],self.service['environment']);self.assertFalse((self.root/'started.flag').exists())

    def test_stale_edit_does_not_overwrite_other_change(self):
        first=self.editor.get('local-existing');second=self.editor.get('local-existing')
        self.editor.save('local-existing',first['version'],dict(first['form'],title='First'))
        with self.assertRaises(Conflict):self.editor.save('local-existing',second['version'],dict(second['form'],title='Second'))
        self.assertEqual(json.loads((self.root/'services.json').read_text())['local-existing']['title'],'First')

    def test_service_icon_can_be_saved_and_reset_without_runtime_validation(self):
        name='c'*64+'.png';uploads=self.root/'state/uploads';uploads.mkdir();(uploads/name).write_bytes(b'uploaded image')
        loaded=self.editor.get('local-existing');icon='/media/'+name
        self.editor.save('local-existing',loaded['version'],dict(loaded['form'],icon=icon))
        loaded=self.editor.get('local-existing');self.assertEqual(loaded['form']['icon'],icon)
        self.editor.save('local-existing',loaded['version'],dict(loaded['form'],icon='default'))
        saved=json.loads((self.root/'services.json').read_text())['local-existing']
        self.assertEqual(saved['icon'],'default');self.assertEqual(saved['start'],self.service['start'])
        self.assertFalse((self.root/'started.flag').exists())

    def test_lifecycle_lock_blocks_saving(self):
        loaded=self.editor.get('local-existing')
        with (self.root/'state/local-existing.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaises(Conflict):self.editor.save('local-existing',loaded['version'],dict(loaded['form'],description='Busy'))

    def test_failed_preference_write_rolls_back_service_file(self):
        loaded=self.editor.get('local-existing');before=(self.root/'services.json').read_bytes()
        with patch.object(self.store,'_write',side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):self.editor.save('local-existing',loaded['version'],dict(loaded['form'],title='Fail'))
        self.assertEqual((self.root/'services.json').read_bytes(),before)

    def test_archived_service_can_edit_text_but_not_rebind_deployment(self):
        self.records['local-existing']=dict(self.service,kind='app',app='/Applications/Missing.app',executable='/Applications/Missing.app/Contents/MacOS/Missing',uninstall_mode='archive-app')
        self.write_records();marker=self.root/'state/local-existing.uninstalled.json';marker.write_text('{"kind":"app","source":"/Applications/Missing.app","archive":"/archive/Missing.app"}')
        old_marker=marker.read_bytes();loaded=self.editor.get('local-existing')
        self.editor.save('local-existing',loaded['version'],dict(loaded['form'],description='Archived notes'))
        loaded=self.editor.get('local-existing');result=self.editor.validate('local-existing',loaded['version'],dict(loaded['form'],app='/Applications/Other.app'))
        self.assertFalse(result['ok']);self.assertEqual(marker.read_bytes(),old_marker)

    def test_old_wrapper_voice_and_app_extras_survive_basic_roundtrip(self):
        for kind,extra in [('compose',{'start':['/bin/zsh','./wrapper.sh','up'],'stop':['/bin/zsh','./wrapper.sh','stop']}),
                           ('voice',{'start':['/bin/zsh','./voice.command']}),
                           ('app',{'app':'/Applications/Demo.app','executable':'/Applications/Demo.app/Contents/MacOS/Demo','extra_executables':['/Applications/Demo.app/helper']})]:
            self.records['local-existing']=dict(self.service,kind=kind,**extra);self.write_records()
            loaded=self.editor.get('local-existing');self.assertNotIn('keep-me',json.dumps(loaded))
            self.editor.save('local-existing',loaded['version'],dict(loaded['form'],description='Notes '+kind))
            saved=json.loads((self.root/'services.json').read_text())['local-existing']
            for field,value in self.records['local-existing'].items():
                if field not in ['description','title','help','group']:self.assertEqual(saved[field],value,field)

    def test_private_values_are_masked_and_keep_placeholder_does_not_overwrite_them(self):
        loaded=self.editor.get('local-existing');self.assertEqual(json.loads(loaded['form']['environment_json']),{'PRIVATE_SETTING':'__KEEP_EXISTING__'})
        form=dict(loaded['form'],health_codes='200,401',open_command='/usr/bin/true')
        result=self.editor.validate('local-existing',loaded['version'],form);self.assertTrue(result['ok'],result)
        self.editor.save('local-existing',loaded['version'],form,result['validation'])
        saved=json.loads((self.root/'services.json').read_text())['local-existing']
        self.assertEqual(saved['environment'],{'PRIVATE_SETTING':'keep-me'});self.assertEqual(saved['health_codes'],[200,401]);self.assertEqual(saved['open_command'],['/usr/bin/true'])

    def test_edit_excludes_self_but_rejects_other_existing_deployment(self):
        self.records['local-other']=dict(self.service,id='local-other',title='Other',port=65432);self.write_records()
        loaded=self.editor.get('local-existing');result=self.editor.validate('local-existing',loaded['version'],dict(loaded['form'],port='65432'))
        self.assertFalse(result['ok'])

    def test_deployment_file_change_after_validation_rejects_save(self):
        loaded=self.editor.get('local-existing');form=dict(loaded['form'],port='65432')
        result=self.editor.validate('local-existing',loaded['version'],form);self.assertTrue(result['ok'])
        self.script.write_text('print("changed")')
        with self.assertRaises(ValueError):self.editor.save('local-existing',loaded['version'],form,result['validation'])
