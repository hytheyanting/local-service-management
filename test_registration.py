import json
import pathlib
import plistlib
import shlex
import tempfile
import unittest
from registration import Registrar
from ui_model import Store


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=pathlib.Path(self.temp.name).resolve()
        (self.root/'services.json').write_text('{}')
        self.store=Store(self.root/'state')
        self.store.read({})
        self.reg=Registrar(self.root,self.store)
        self.script=self.root/'server.py'
        self.script.write_text("from pathlib import Path\nPath('should-not-start').touch()\n")
        self.form={'title':'测试服务','description':'用于验证登记','category':'uncategorized','kind':'process',
                   'directory':str(self.root),'url':'','health':'','help':'打开网页使用',
                   'start_command':shlex.join(['/usr/bin/python3',str(self.script)]),'port':'65431',
                   'process_tokens':'server.py','enable_uninstall':False}

    def test_validation_does_not_start_program_or_write_registry(self):
        result=self.reg.validate(self.form)
        self.assertTrue(result['ok'],result)
        self.assertTrue(result['validation'])
        self.assertFalse((self.root/'should-not-start').exists())
        self.assertEqual(json.loads((self.root/'services.json').read_text()),{})

    def test_registration_saves_uploaded_icon_and_rejects_untrusted_image_urls(self):
        name='d'*64+'.png';uploads=self.root/'state/uploads';uploads.mkdir();(uploads/name).write_bytes(b'uploaded image')
        form=dict(self.form,icon='/media/'+name)
        validated=self.reg.validate(form);self.assertTrue(validated['ok'],validated)
        saved=self.reg.add(form,validated['validation']);self.assertEqual(saved['icon'],form['icon'])
        for icon in ['https://example.com/icon.png','/media/'+'e'*64+'.png','/media/../../services.json','javascript:alert(1)']:
            result=self.reg.validate(dict(self.form,title='Another',port='65432',icon=icon))
            self.assertFalse(result['ok']);self.assertTrue(any(c['field']=='icon' and c['level']=='error' for c in result['checks']))

    def test_changed_form_and_unvalidated_submissions_are_rejected(self):
        valid=self.reg.validate(self.form)
        for form,token in [(self.form,''),(dict(self.form,title='改过名称'),valid['validation'])]:
            with self.assertRaises(ValueError):self.reg.add(form,token)
        self.assertEqual(json.loads((self.root/'services.json').read_text()),{})

    def test_registration_persists_commands_without_starting_and_rejects_duplicate(self):
        valid=self.reg.validate(self.form)
        service=self.reg.add(self.form,valid['validation'])
        saved=json.loads((self.root/'services.json').read_text())[service['id']]
        self.assertEqual(saved['start'],['/usr/bin/python3',str(self.script)])
        self.assertNotIn('uninstall_mode',saved)
        self.assertEqual(self.store.category_for(saved,self.store.read({saved['id']:saved})),'uncategorized')
        self.assertFalse((self.root/'should-not-start').exists())
        again=self.reg.validate(dict(self.form,title='另一个名字'))
        self.assertFalse(again['ok'])
        self.assertTrue(list((self.root/'state').glob('services-before-register-*.json')))

    def test_missing_paths_bad_urls_and_shell_syntax_are_reported(self):
        for changes in [{'directory':'/no/such/service-dir'}, {'url':'javascript:alert(1)'},
                        {'start_command':'/usr/bin/python3 server.py && touch wrong'},
                        {'start_command':'/no/such/program server.py'},{'port':'0'},
                        {'url':'http://user:password@localhost:1234/'}]:
            result=self.reg.validate(dict(self.form,**changes))
            self.assertFalse(result['ok'],changes)
            self.assertTrue(any(c['level']=='error' for c in result['checks']))

    def test_live_registry_change_blocks_a_previously_valid_submission(self):
        valid=self.reg.validate(self.form)
        (self.root/'services.json').write_text(json.dumps({'local-existing':{'id':'local-existing','title':'测试服务','kind':'process','directory':'/other','port':65432}}))
        with self.assertRaises(ValueError):self.reg.add(self.form,valid['validation'])
        self.assertEqual(list(json.loads((self.root/'services.json').read_text())),['local-existing'])

    def test_deleted_category_requires_revalidation(self):
        state=self.store.read({})
        state=self.store.save_preferences({},dict(state,categories=[{'id':'cat-demo','name':'测试分类'},{'id':'uncategorized','name':'未分类'}]))
        form=dict(self.form,category='cat-demo')
        valid=self.reg.validate(form);self.assertTrue(valid['ok'])
        self.store.save_preferences({},dict(state,categories=[{'id':'uncategorized','name':'未分类'}]))
        with self.assertRaises(ValueError):self.reg.add(form,valid['validation'])

    def test_desktop_app_entry_is_read_from_bundle_and_does_not_run(self):
        app=self.root/'Demo.app';(app/'Contents/MacOS').mkdir(parents=True)
        (app/'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleExecutable':'demo'}))
        exe=app/'Contents/MacOS/demo';exe.write_text('#!/bin/sh\ntouch should-not-start\n');exe.chmod(0o755)
        form=dict(self.form,kind='app',app=str(app))
        result=self.reg.validate(form);self.assertTrue(result['ok'],result)
        service=self.reg.add(form,result['validation'])
        self.assertEqual(service['executable'],str(exe))
        self.assertFalse((self.root/'should-not-start').exists())

    def test_launchd_reads_label_and_rejects_malformed_plist(self):
        plist=self.root/'demo.plist';plist.write_bytes(plistlib.dumps({'Label':'local.demo','ProgramArguments':['/usr/bin/true']}))
        form=dict(self.form,kind='launchd',plist=str(plist))
        result=self.reg.validate(form);self.assertTrue(result['ok'],result)
        self.assertEqual(self.reg.add(form,result['validation'])['label'],'local.demo')
        plist.write_text('bad plist')
        self.assertFalse(self.reg.validate(dict(form,title='bad'))['ok'])

    def test_stopped_health_endpoint_is_a_warning_not_an_error(self):
        form=dict(self.form,url='http://127.0.0.1:65431/',health='http://127.0.0.1:65431/')
        result=self.reg.validate(form)
        self.assertTrue(result['ok'],result)
        self.assertTrue(any(c['level']=='warning' and c['field']=='health' for c in result['checks']))

    def test_expired_validation_cannot_register(self):
        valid=self.reg.validate(self.form)
        token=valid['validation']; fingerprint,_,signature=self.reg.tickets[token]
        self.reg.tickets[token]=(fingerprint,0,signature)
        with self.assertRaises(ValueError):self.reg.add(self.form,token)

    def test_two_concurrent_registrations_preserve_both_records(self):
        from concurrent.futures import ThreadPoolExecutor
        first=self.reg.validate(self.form)
        other=dict(self.form,title='Other',port='65432')
        second=self.reg.validate(other)
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs=[pool.submit(self.reg.add,self.form,first['validation']),pool.submit(self.reg.add,other,second['validation'])]
            for job in jobs:job.result()
        self.assertEqual(len(json.loads((self.root/'services.json').read_text())),2)

    def test_compose_validation_reads_config_but_never_runs_up_down_or_removes_volumes(self):
        from unittest.mock import patch
        docker,form=self.docker_fixture([{'ID':'container1','Labels':'com.docker.compose.project=demo'}],
            config={'services':{'web':{'image':'demo','volumes':[{'type':'volume','source':'db','target':'/data'}]}},'volumes':{'db':{'name':'demo_db'}}},
            inspect=[{'Config':{'Labels':{'com.docker.compose.service':'web'}},'Mounts':[{'Type':'volume','Name':'demo_db','Destination':'/data'}]}])
        form.update(enable_uninstall=True,confirm_persistence=True,preserves='demo_db')
        with patch('registration.DOCKER',str(docker)):
            result=self.reg.validate(form);self.assertTrue(result['ok'],result)
            service=self.reg.add(form,result['validation'])
        self.assertEqual(service['uninstall_command'][-1:],['down'])
        self.assertNotIn('-v',service['uninstall_command'])
        self.assertEqual(service['preserved_mounts'],[{'service':'web','type':'命名卷','source':'demo_db','target':'/data'}])
        calls=[json.loads(line) for line in (self.root/'docker-calls.jsonl').read_text().splitlines()]
        self.assertFalse(any(word in a for a in calls for word in ['up','down','start','stop','rm']))

    def test_enabling_uninstall_requires_explicit_preserved_data(self):
        result=self.reg.validate(dict(self.form,enable_uninstall=True))
        self.assertFalse(result['ok'])

    def test_compose_requires_a_real_health_signal_and_launchd_checks_program_path(self):
        # A Compose service without any endpoint used to be reported healthy unconditionally.
        form=dict(self.form,kind='compose',context='default',project='demo',compose_file='compose.yaml')
        self.assertFalse(self.reg.validate(form)['ok'])
        plist=self.root/'missing.plist';plist.write_bytes(plistlib.dumps({'Label':'local.missing','ProgramArguments':['/no/such/executable']}))
        result=self.reg.validate(dict(self.form,kind='launchd',plist=str(plist)))
        self.assertFalse(result['ok'])

    def docker_fixture(self, containers, config=None, inspect=None):
        docker=self.root/'docker'
        config=config or {'services':{'web':{'image':'demo'}}}
        docker.write_text('#!/usr/bin/python3\nimport sys,json\nfrom pathlib import Path\na=sys.argv[1:]\nwith Path('+repr(str(self.root/'docker-calls.jsonl'))+').open("a") as f:f.write(json.dumps(a)+"\\n")\nif "config" in a:print(json.dumps('+repr(config)+'))\nelif "container" in a and "inspect" in a:print(json.dumps('+repr(inspect or [])+'))\nelif "ps" in a:\n for c in '+repr(containers)+':print(json.dumps(c))\nelif "info" in a:print("27")\nelse:print("[]")\n')
        docker.chmod(0o755)
        file=self.root/'compose.yaml';file.write_text('services: {}')
        return docker,dict(self.form,kind='compose',context='default',project='demo',compose_file=str(file),url='http://127.0.0.1:65431/')

    def test_wrong_compose_project_name_is_rejected_against_existing_container_labels(self):
        from unittest.mock import patch
        labels='com.docker.compose.project=actual-project,com.docker.compose.project.config_files='+str(self.root/'compose.yaml')
        docker,form=self.docker_fixture([{'Labels':labels}])
        with patch('registration.DOCKER',str(docker)):
            result=self.reg.validate(form)
        self.assertFalse(result['ok'])
        self.assertTrue(any(c['field']=='project' and c['level']=='error' for c in result['checks']))

    def test_shared_colima_cannot_be_registered_as_exclusive(self):
        from unittest.mock import patch
        profile=self.root/'.colima/shared';profile.mkdir(parents=True);(profile/'colima.yaml').write_text('cpu: 2')
        docker,form=self.docker_fixture([])
        existing={'id':'local-other','title':'Other','kind':'compose','colima_profile':'shared','start':['docker','--context','colima-shared','compose','-p','other','up']}
        (self.root/'services.json').write_text(json.dumps({'local-other':existing}))
        form.update(context='colima-shared',stop_profile=True)
        with patch('registration.DOCKER',str(docker)),patch('registration.pathlib.Path.home',return_value=self.root):
            result=self.reg.validate(form)
        self.assertFalse(result['ok'])

    def test_changed_app_entry_requires_new_validation(self):
        app=self.root/'Change.app';(app/'Contents/MacOS').mkdir(parents=True)
        info=app/'Contents/Info.plist';info.write_bytes(plistlib.dumps({'CFBundleExecutable':'first'}))
        for name in ['first','second']:
            exe=app/'Contents/MacOS'/name;exe.write_text('#!/bin/sh\n');exe.chmod(0o755)
        form=dict(self.form,kind='app',app=str(app));valid=self.reg.validate(form)
        info.write_bytes(plistlib.dumps({'CFBundleExecutable':'second'}))
        with self.assertRaises(ValueError):self.reg.add(form,valid['validation'])

    def test_launchd_label_cannot_take_over_another_loaded_plist(self):
        from unittest.mock import patch
        import subprocess
        plist=self.root/'demo.plist';plist.write_bytes(plistlib.dumps({'Label':'local.demo','ProgramArguments':['/usr/bin/true']}))
        form=dict(self.form,kind='launchd',plist=str(plist))
        with patch('registration.subprocess.run',return_value=subprocess.CompletedProcess([],0,'local.demo = {\n path = /other/deployed.plist\n}\n','')):
            result=self.reg.validate(form)
        self.assertFalse(result['ok'])

    def test_same_compose_directory_with_wrapper_is_a_duplicate(self):
        from registration import check_unique
        existing={'id':'local-old','title':'Old','kind':'compose','directory':str(self.root),'start':['/bin/zsh','start.sh']}
        new={'id':'local-new','title':'New','kind':'compose','directory':str(self.root),'start':['docker','compose','-p','project','up']}
        with self.assertRaises(ValueError):check_unique(new,{'local-old':existing})

    def test_anonymous_volumes_block_automatic_uninstall(self):
        from unittest.mock import patch
        docker,form=self.docker_fixture([{'ID':'container1','Labels':'com.docker.compose.project=demo'}],
            config={'services':{'web':{'volumes':[{'type':'volume','target':'/data'}]}}},
            inspect=[{'Config':{'Labels':{'com.docker.compose.service':'web'}},'Mounts':[{'Type':'volume','Name':'anonymous123','Destination':'/data'}]}])
        form.update(enable_uninstall=True,confirm_persistence=True,preserves='业务数据')
        with patch('registration.DOCKER',str(docker)):
            result=self.reg.validate(form)
        self.assertFalse(result['ok'])
        self.assertTrue(any(c['field']=='preserves' and c['level']=='error' for c in result['checks']))

    def test_interpreter_marker_cannot_take_over_another_script_in_same_directory(self):
        from unittest.mock import patch
        (self.root/'other.py').write_text('')
        form=dict(self.form,process_tokens='python3')
        with patch('registration.listeners',return_value=[123]),patch('registration.process_identity',return_value=(str(self.root),'/usr/bin/python3 other.py')):
            self.assertFalse(self.reg.validate(form)['ok'])

    def test_container_name_alias_matches_existing_long_format_registration(self):
        from registration import check_unique
        old={'id':'local-old','title':'Old','kind':'container','inspect':['docker','--context','default','inspect','--format','{{.State.Running}}','demo-container']}
        new={'id':'local-new','title':'New','kind':'container','inspect':['docker','--context','default','inspect','-f','{{.State.Running}}','a'*64],'container_aliases':['a'*64,'demo-container']}
        with self.assertRaises(ValueError):check_unique(new,{'local-old':old})

    def test_stopped_original_colima_profile_can_validate_without_recreating_context(self):
        from unittest.mock import patch
        profile=self.root/'.colima/demo';profile.mkdir(parents=True);(profile/'colima.yaml').write_text('cpu: 2')
        docker,form=self.docker_fixture([])
        script=docker.read_text().replace('if "config" in a:', 'if "context" in a or "info" in a:sys.exit(1)\nelif "config" in a:')
        docker.write_text(script);form.update(context='colima-demo')
        with patch('registration.DOCKER',str(docker)),patch('registration.pathlib.Path.home',return_value=self.root):
            result=self.reg.validate(form)
        self.assertTrue(result['ok'],result)
        calls=[json.loads(line) for line in (self.root/'docker-calls.jsonl').read_text().splitlines()]
        self.assertFalse(any('start' in call or 'create' in call for call in calls))

    def test_changed_bind_mount_cannot_claim_recoverable_uninstall(self):
        from unittest.mock import patch
        old=self.root/'old-data';old.mkdir();new=self.root/'new-data';new.mkdir()
        docker,form=self.docker_fixture([{'ID':'container1','Labels':'com.docker.compose.project=demo'}],
            config={'services':{'web':{'volumes':[{'type':'bind','source':str(new),'target':'/data'}]}}},
            inspect=[{'Config':{'Labels':{'com.docker.compose.service':'web'}},'Mounts':[{'Type':'bind','Source':str(old),'Destination':'/data'}]}])
        form.update(enable_uninstall=True,confirm_persistence=True,preserves=str(old))
        with patch('registration.DOCKER',str(docker)):
            result=self.reg.validate(form)
        self.assertFalse(result['ok'])
        self.assertTrue(any(c['field']=='preserves' and c['level']=='error' for c in result['checks']))
