import importlib.util
import pathlib
import tempfile
import unittest
from unittest import mock


class ReloadTests(unittest.TestCase):
    def setUp(self):
        path=pathlib.Path(__file__).with_name('supervisor.py')
        spec=importlib.util.spec_from_file_location('supervisor',path)
        self.supervisor=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.supervisor)

    def test_changed_configuration_is_written_only_after_child_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            root=pathlib.Path(directory)
            active=root/'config.yaml'; desired=root/'config.next.json'
            active.write_text('{"old":true}')
            desired.write_text('{"new":true}')
            process=mock.Mock()
            process.poll.return_value=None
            def stopped(timeout):
                self.assertEqual(active.read_text(),'{"old":true}')
                return 0
            process.wait.side_effect=stopped
            with mock.patch.object(self.supervisor,'spawn',return_value='replacement') as spawn:
                result=self.supervisor.reconcile(process,desired,active)
                self.assertEqual(result,'replacement')
                process.terminate.assert_called_once()
                self.assertEqual(active.read_text(),desired.read_text())
                spawn.assert_called_once()

    def test_unchanged_configuration_keeps_running_child(self):
        with tempfile.TemporaryDirectory() as directory:
            root=pathlib.Path(directory)
            active=root/'config.yaml'; desired=root/'config.next.json'
            active.write_text('{"same":true}'); desired.write_text(active.read_text())
            process=mock.Mock(); process.poll.return_value=None
            self.assertIs(self.supervisor.reconcile(process,desired,active),process)
            process.terminate.assert_not_called()

    def test_invalid_desired_configuration_preserves_running_child(self):
        with tempfile.TemporaryDirectory() as directory:
            root=pathlib.Path(directory)
            active=root/'config.yaml'; desired=root/'config.next.json'
            active.write_text('{"same":true}'); desired.write_text('{broken')
            process=mock.Mock(); process.poll.return_value=None
            with self.assertRaises(ValueError):
                self.supervisor.reconcile(process,desired,active)
            process.terminate.assert_not_called()
            self.assertEqual(active.read_text(),'{"same":true}')
