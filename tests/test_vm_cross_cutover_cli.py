"""Operator gates tested on temporary directories, without touching systemd."""
import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


spec = importlib.util.spec_from_file_location('vm_cross_cutover', Path(__file__).resolve().parents[1] / 'tools/vm_cross_cutover.py')
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


class CutoverCliTests(unittest.TestCase):
    def test_rollback_marker_blocks_preparation_and_finish_before_opening_databases(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / 'test'
            archive.mkdir()
            (archive / 'journal').mkdir()
            (archive / 'rollback-intent').write_text('cross:freeze:test')
            for phase in ('prepare', 'finish'):
                with self.subTest(phase=phase), patch.object(cli, 'ROOT', root), \
                     patch.object(cli, 'MARKER', root / 'service-start-interlock'), \
                     patch.object(cli, '_private_dir'), \
                     patch.object(cli, 'SharedPaperStore', side_effect=AssertionError('must not open')):
                    with self.assertRaisesRegex(SystemExit, 'Rollback has begun'):
                        cli._run(SimpleNamespace(operation='cross:freeze:test', phase=phase))


if __name__ == '__main__':
    unittest.main()
