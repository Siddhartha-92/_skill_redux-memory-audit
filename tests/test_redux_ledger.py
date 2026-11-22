import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / 'scripts' / 'redux_ledger.py'


def cli(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True)


class ReduxLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'historia.md'
        self.output = self.root / 'index'

    def test_large_file_covers_every_line_without_gaps_or_truncation(self):
        with self.source.open('w', encoding='utf-8', newline='') as f:
            for i in range(1, 64002):
                f.write(f'## Solicitud {i} — comprobante #{i:05d}\n')
        made = cli('prepare', self.source, '--out', self.output, '--max-lines', 701, '--max-bytes', 32000)
        self.assertEqual(made.returncode, 0, made.stderr)
        manifest = json.loads((self.output / 'manifest.json').read_text(encoding='utf-8'))
        self.assertGreater(len(manifest['chunks']), 90)
        self.assertEqual(manifest['line_count'], 64001)
        self.assertEqual(manifest['chunks'][0]['start_line'], 1)
        self.assertEqual(manifest['chunks'][-1]['end_line'], 64001)
        self.assertEqual(b''.join((self.output / c['file']).read_bytes() for c in manifest['chunks']), self.source.read_bytes())
        ver = cli('verify', self.output / 'manifest.json')
        self.assertEqual(ver.returncode, 2, ver.stdout + ver.stderr)
        self.assertIn('PARTIAL', ver.stdout)

    def test_state_machine_requires_evidence_and_preserves_progress(self):
        self.source.write_text('uno\ndos\ntres\n', encoding='utf-8')
        self.assertEqual(cli('prepare', self.source, '--out', self.output, '--max-lines', 2).returncode, 0)
        m = self.output / 'manifest.json'
        self.assertNotEqual(cli('mark', m, 'C0001', 'verified', '--proof', 'notion:test').returncode, 0)
        self.assertEqual(cli('mark', m, 'C0001', 'reviewed').returncode, 0)
        self.assertNotEqual(cli('mark', m, 'C0001', 'reconciled').returncode, 0)
        self.assertEqual(cli('mark', m, 'C0001', 'reconciled', '--proof', 'diff:sin cambios').returncode, 0)
        self.assertEqual(cli('mark', m, 'C0001', 'verified', '--proof', 'notion:relectura-1').returncode, 0)
        self.assertEqual(cli('prepare', self.source, '--out', self.output, '--max-lines', 2).returncode, 0)
        self.assertEqual(json.loads(m.read_text())['chunks'][0]['status'], 'verified')
        self.assertEqual(cli('verify', m).returncode, 2)  # the other chunk pending
        self.assertEqual(cli('mark', m, 'C0002', 'reviewed').returncode, 0)
        self.assertEqual(cli('mark', m, 'C0002', 'reconciled', '--proof', 'diff:sin cambios').returncode, 0)
        self.assertEqual(cli('mark', m, 'C0002', 'verified', '--proof', 'notion:relectura-2').returncode, 0)
        check = cli('verify', m)
        self.assertEqual(check.returncode, 0, check.stdout+check.stderr)
        self.assertIn('COMPLETE', check.stdout)

    def test_modified_input_or_chunk_is_detected(self):
        self.source.write_text('1\n2\n3\n', encoding='utf-8')
        self.assertEqual(cli('prepare', self.source, '--out', self.output).returncode, 0)
        m = self.output / 'manifest.json'
        manifest = json.loads(m.read_text())
        (self.output / manifest['chunks'][0]['file']).write_text('garbled', encoding='utf-8')
        self.assertEqual(cli('verify', m).returncode, 1)
        self.source.write_text('4\n5\n6\n', encoding='utf-8')
        self.assertNotEqual(cli('prepare', self.source, '--out', self.output).returncode, 0)

    def test_very_long_line_not_silently_dropped(self):
        self.source.write_text('a' * 10000 + '\nnext\n', encoding='utf-8')
        self.assertEqual(cli('prepare', self.source, '--out', self.output, '--max-bytes', 2000).returncode, 0)
        m = json.loads((self.output / 'manifest.json').read_text())
        self.assertTrue(m['chunks'][0]['oversized_line'])
        self.assertEqual(b''.join((self.output / c['file']).read_bytes() for c in m['chunks']), self.source.read_bytes())

    def test_multibyte_and_non_newline_terminal(self):
        self.source.write_text('Año—2026\nñandú', encoding='utf-8')
        self.assertEqual(cli('prepare', self.source, '--out', self.output, '--max-lines', 1).returncode, 0)
        m = json.loads((self.output / 'manifest.json').read_text())
        self.assertEqual(m['line_count'], 2)
        self.assertEqual(b''.join((self.output / c['file']).read_bytes() for c in m['chunks']), self.source.read_bytes())


if __name__ == '__main__':
    unittest.main()
