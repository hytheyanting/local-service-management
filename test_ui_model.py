import importlib.util
import pathlib
import tempfile
import unittest


class UiModelTests(unittest.TestCase):
    def setUp(self):
        spec=importlib.util.spec_from_file_location('ui_model',pathlib.Path(__file__).with_name('ui_model.py'))
        self.m=importlib.util.module_from_spec(spec); spec.loader.exec_module(self.m)
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store=self.m.Store(pathlib.Path(self.temp.name))
        self.services={'local-one':{'id':'local-one','title':'One','group':'AI'},'local-two':{'id':'local-two','title':'Two','group':'Dev'}}

    def test_category_rename_and_removal_preserve_service_records(self):
        state=self.store.read(self.services); ai=state['group_map']['AI']
        cats=[dict(c,name='改名') if c['id']==ai else c for c in state['categories']]
        state=self.store.save_preferences(self.services,dict(state,categories=cats))
        self.assertEqual(self.store.category_for(self.services['local-one'],state),ai)
        cats=[c for c in state['categories'] if c['id']!=ai]
        state=self.store.save_preferences(self.services,dict(state,categories=cats))
        self.assertEqual(self.store.category_for(self.services['local-one'],state),'uncategorized')
        again=self.store.read(self.services)
        self.assertNotIn(ai,[c['id'] for c in again['categories']])
        self.assertEqual(self.services['local-one']['group'],'AI')

    def test_concurrent_save_is_rejected(self):
        old=self.store.read(self.services)
        self.store.save_preferences(self.services,old)
        with self.assertRaises(self.m.Conflict):self.store.save_preferences(self.services,old)

    def test_categories_cannot_have_duplicate_names_or_remove_uncategorized(self):
        old=self.store.read(self.services)
        for cats in [[{'id':'cat-a','name':'same'},{'id':'cat-b','name':' SAME '},{'id':'uncategorized','name':'未分类'}],[]]:
            with self.assertRaises(ValueError):self.store.save_preferences(self.services,dict(old,categories=cats))

    def test_new_registration_joins_a_renamed_source_category(self):
        state=self.store.read(self.services); cat=state['group_map']['AI']
        state=self.store.save_preferences(self.services,dict(state,categories=[dict(c,name='AI 工具') if c['id']==cat else c for c in state['categories']]))
        new={'id':'local-new','title':'New','group':'AI'}; self.services[new['id']]=new
        state=self.store.read(self.services)
        self.assertEqual(self.store.category_for(new,state),cat)
        self.assertEqual(len(state['categories']),3)
        self.services['local-new-label']={'id':'local-new-label','title':'New label','group':'AI 工具'}
        state=self.store.read(self.services)
        self.assertEqual(self.store.category_for(self.services['local-new-label'],state),cat)
        self.assertEqual(len(state['categories']),3)

    def test_assignment_and_appearance_persist(self):
        state=self.store.read(self.services); cat=state['group_map']['Dev']
        state=self.store.assign(self.services,state['revision'],'local-one',cat)
        state=self.store.save_preferences(self.services,dict(state,appearance={'transparency':60,'blur':5,'wallpaper':'default'}))
        fresh=self.m.Store(pathlib.Path(self.temp.name)).read(self.services)
        self.assertEqual(self.store.category_for(self.services['local-one'],fresh),cat)
        self.assertEqual(fresh['appearance']['transparency'],60)

    def test_uploaded_image_and_path_validation(self):
        # A real 1x1 PNG, plus invalid file/path cases.
        import base64
        png=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jDz0AAAAASUVORK5CYII=')
        path=self.store.upload(png,'image/png')
        self.assertTrue(path.startswith('/media/'))
        for data,mime in [(b'<svg/>','image/svg+xml'),(b'bad','image/png'),(b'x'*(8*1024*1024+1),'image/jpeg')]:
            with self.assertRaises(ValueError):self.store.upload(data,mime)
        state=self.store.read(self.services)
        with self.assertRaises(ValueError):self.store.save_preferences(self.services,dict(state,appearance={'wallpaper':'http://example.com/a.jpg','transparency':35,'blur':10}))

    def test_custom_logo_persists_and_old_clients_do_not_reset_it(self):
        state=self.store.read(self.services)
        name='a'*64+'.png'
        (self.store.root/'uploads').mkdir()
        (self.store.root/'uploads'/name).write_bytes(b'uploaded image')
        appearance=dict(state['appearance'],logo='/media/'+name)
        state=self.store.save_preferences(self.services,dict(state,appearance=appearance))
        self.assertEqual(self.m.Store(self.store.root).read(self.services)['appearance']['logo'],'/media/'+name)
        old_client=dict(state['appearance']);old_client.pop('logo')
        state=self.store.save_preferences(self.services,dict(state,appearance=old_client))
        self.assertEqual(state['appearance']['logo'],'/media/'+name)
        state=self.store.save_preferences(self.services,dict(state,appearance=dict(state['appearance'],logo='default')))
        self.assertEqual(state['appearance']['logo'],'default')

    def test_logo_rejects_external_missing_and_traversal_paths(self):
        state=self.store.read(self.services)
        for logo in ['https://example.com/icon.png','/media/'+'b'*64+'.png','/media/../../services.json','none',123]:
            with self.assertRaises(ValueError):
                self.store.save_preferences(self.services,dict(state,appearance=dict(state['appearance'],logo=logo)))

    def test_service_info_edit_persists_without_changing_commands_categories_or_appearance(self):
        import copy
        before=copy.deepcopy(self.services)
        state=self.store.read(self.services)
        updated=self.store.edit_service_info(self.services,state['revision'],'local-one',{'description':'新的用途描述','help':'第一步\n第二步'})
        self.assertEqual(self.services,before)
        self.assertEqual(updated['categories'],state['categories'])
        self.assertEqual(updated['appearance'],state['appearance'])
        fresh=self.m.Store(self.store.root).read(self.services)
        self.assertEqual(fresh['service_info']['local-one'],{'description':'新的用途描述','help':'第一步\n第二步'})
        # Saving a stale appearance draft must not overwrite an edit.
        with self.assertRaises(self.m.Conflict):self.store.save_preferences(self.services,state)

    def test_service_info_rejects_missing_service_stale_revision_and_command_fields(self):
        state=self.store.read(self.services)
        for name,values in [('local-missing',{'description':'Info','help':'Use'}),
                            ('local-one',{'description':'Info','help':'Use','start':['sh','bad']}),
                            ('local-one',{'description':'','help':'Use'})]:
            with self.assertRaises(ValueError):self.store.edit_service_info(self.services,state['revision'],name,values)
        self.store.edit_service_info(self.services,state['revision'],'local-one',{'description':'Info','help':'Use'})
        with self.assertRaises(self.m.Conflict):self.store.edit_service_info(self.services,state['revision'],'local-one',{'description':'Old','help':'Old'})
