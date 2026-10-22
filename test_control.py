import importlib.util
import json
import pathlib
import unittest
import tempfile
from unittest import mock


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        path = pathlib.Path(__file__).with_name('control.py')
        self.assertTrue(path.exists(), 'Service control adapter has not been implemented')
        spec = importlib.util.spec_from_file_location('control', path)
        self.control = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.control)

    def test_lifecycle_reloads_service_after_acquiring_its_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            registry = root / 'services.json'
            registry.write_text(json.dumps({'local-demo': {'directory': 'old'}}))
            def acquire(*args):
                registry.write_text(json.dumps({'local-demo': {'directory': 'new'}}))
            with mock.patch.object(self.control, 'ROOT', root), \
                    mock.patch('sys.argv', ['control.py', 'local-demo', 'start']), \
                    mock.patch.object(self.control.fcntl, 'flock', side_effect=acquire), \
                    mock.patch.object(self.control, 'start') as start:
                self.control.main()
            start.assert_called_once_with({'directory': 'new'})

    def test_stop_accepts_only_the_expected_voice_server(self):
        expected = '/projects/voice-studio'
        self.assertTrue(self.control.owns_voice_process(
            expected, 'python -m uvicorn bridge:app --host 127.0.0.1 --port 8766', expected))
        self.assertFalse(self.control.owns_voice_process(
            expected, 'python another_server.py --port 8766', expected))
        self.assertFalse(self.control.owns_voice_process(
            '/tmp/another-project', 'uvicorn bridge:app --port 8766', expected))
        self.assertFalse(self.control.owns_voice_process('', '', expected))

    def test_generic_process_cannot_match_a_different_project_or_partial_token(self):
        service = {'directory': '/projects/codeflow', 'process_tokens': ['codeflow', 'serve']}
        self.assertTrue(self.control.owns_process(
            '/projects/codeflow', '/projects/codeflow/.venv/bin/python /projects/codeflow/.venv/bin/codeflow serve --port 8765', service))
        self.assertFalse(self.control.owns_process(
            '/projects/other', 'codeflow serve --port 8765', service))
        self.assertFalse(self.control.owns_process(
            '/projects/codeflow', 'fake-codeflow serve --port 8765', service))

    def test_uninstall_compose_never_accepts_data_or_image_removal_flags(self):
        prefix = ['docker', '--context', 'colima-photoprism', 'compose', '-f', '/apps/compose.yaml']
        self.control.validate_compose_uninstall(prefix + ['down'])
        for suffix in [['down', '-v'], ['down', '--volumes'], ['down', '--rmi=all'],
                       ['down', '--remove-orphans'], ['rm', '-f']]:
            with self.assertRaises(ValueError):
                self.control.validate_compose_uninstall(prefix + suffix)

    def test_app_process_match_excludes_helpers_and_other_applications(self):
        executable = '/Applications/LM Studio.app/Contents/MacOS/LM Studio'
        rows = '861 ' + executable + '\n862 /Applications/LM Studio.app/Contents/Frameworks/LM Studio Helper.app/Contents/MacOS/LM Studio Helper\n863 /Applications/Other.app/Contents/MacOS/Other\n'
        self.assertEqual(self.control.match_app_pids(rows, executable), [861])

    def test_app_archive_and_restore_preserve_program_and_user_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            applications = root / 'Applications'
            source = applications / 'Example.app'
            source.mkdir(parents=True)
            (source / 'program').write_text('original program')
            data = root / 'models'
            data.mkdir()
            (data / 'model.bin').write_text('keep this data')
            manager = root / 'manager'
            (manager / 'state').mkdir(parents=True)
            service = {'id': 'local-test', 'kind': 'app', 'app': str(source),
                       'uninstall_mode': 'archive-app', 'title': 'Example',
                       'uninstall': 'archive program only', 'preserves': [str(data)]}
            with mock.patch.object(self.control, 'ROOT', manager), \
                    mock.patch.object(pathlib.Path, 'home', return_value=root), \
                    mock.patch.object(self.control, 'stop') as stop:
                self.control.uninstall(service, False)
                self.assertTrue(source.exists())
                stop.assert_not_called()
                self.control.uninstall(service, True)
                self.assertFalse(source.exists())
                record = json.loads(self.control.marker(service).read_text())
                self.assertEqual((pathlib.Path(record['archive']) / 'program').read_text(), 'original program')
                self.assertEqual((data / 'model.bin').read_text(), 'keep this data')
                self.control.restore(service)
                self.assertEqual((source / 'program').read_text(), 'original program')
                self.assertFalse(self.control.marker(service).exists())

    def test_restore_refuses_to_overwrite_an_existing_application(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / 'state').mkdir()
            source = root / 'Existing.app'
            source.mkdir()
            service = {'id': 'local-test'}
            with mock.patch.object(self.control, 'ROOT', root):
                self.control.marker(service).write_text(json.dumps({
                    'kind': 'app', 'source': str(source), 'archive': str(root / 'archive.app')}))
                with self.assertRaises(RuntimeError):
                    self.control.restore(service)
                self.assertTrue(self.control.marker(service).exists())

    def photoprism_fixture(self, root):
        (root / 'state').mkdir()
        profile = root / '.colima' / 'photoprism'
        profile.mkdir(parents=True)
        (profile / 'colima.yaml').write_text('arch: x86_64\n')
        return {'id': 'local-photoprism', 'title': 'PhotoPrism', 'kind': 'compose',
                'directory': str(root), 'colima_profile': 'photoprism', 'stop_profile': True,
                'uninstall_mode': 'compose-instance', 'uninstall': 'keep photos and volumes',
                'uninstall_command': ['docker', '--context', 'colima-photoprism', 'compose',
                                      '-p', 'photoprism-local', 'down']}

    def test_uninstall_offline_profile_temporarily_starts_and_stops_original_vm(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            service = self.photoprism_fixture(root)
            with mock.patch.object(self.control, 'ROOT', root), \
                    mock.patch.object(pathlib.Path, 'home', return_value=root), \
                    mock.patch.object(self.control, 'profile_running', side_effect=[False, False, True]), \
                    mock.patch.object(self.control, 'run_argv') as run:
                self.control.uninstall(service, True)
                commands = [c.args[0] for c in run.call_args_list]
                self.assertEqual(commands, [['/opt/homebrew/bin/colima', 'start', 'photoprism'],
                                            service['uninstall_command'],
                                            ['/opt/homebrew/bin/colima', 'stop', 'photoprism']])
                self.assertTrue(self.control.marker(service).exists())

    def test_failed_uninstall_restores_offline_vm_and_does_not_mark_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            service = self.photoprism_fixture(root)
            def execute(argv, *args):
                if argv == service['uninstall_command']:
                    raise RuntimeError('simulated Docker failure')
            with mock.patch.object(self.control, 'ROOT', root), \
                    mock.patch.object(pathlib.Path, 'home', return_value=root), \
                    mock.patch.object(self.control, 'profile_running', side_effect=[False, False, True]), \
                    mock.patch.object(self.control, 'run_argv', side_effect=execute) as run:
                with self.assertRaisesRegex(RuntimeError, 'simulated Docker failure'):
                    self.control.uninstall(service, True)
                self.assertEqual(run.call_args.args[0], ['/opt/homebrew/bin/colima', 'stop', 'photoprism'])
                self.assertFalse(self.control.marker(service).exists())

    def test_uninstall_running_profile_does_not_change_its_power_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            service = self.photoprism_fixture(root)
            with mock.patch.object(self.control, 'ROOT', root), \
                    mock.patch.object(self.control, 'profile_running', return_value=True), \
                    mock.patch.object(self.control, 'run_argv') as run:
                self.control.uninstall(service, True)
                self.assertEqual([c.args[0] for c in run.call_args_list], [service['uninstall_command']])


if __name__ == '__main__':
    unittest.main()

class LaunchAgentHealthTests(unittest.TestCase):
    def test_agent_without_http_health_is_stopped_when_unloaded(self):
        import control
        from unittest.mock import patch
        service={'id':'local-test-launchd','kind':'launchd','health':''}
        with patch.object(control,'launch_running',return_value=False):
            self.assertFalse(control.healthy(service))
        with patch.object(control,'launch_running',return_value=True):
            self.assertTrue(control.healthy(service))

    def test_loaded_but_inactive_agent_is_not_running(self):
        import control,subprocess
        from unittest.mock import patch
        service={'id':'local-test-launchd','kind':'launchd','label':'local.test','health':''}
        with patch.object(control.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'state = waiting\n','')):
            self.assertFalse(control.healthy(service))
