"""Offline publication checks with constructed fake secrets, no provider requests."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('publication_audit', ROOT / 'scripts/publication_audit.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class PublicationTests(unittest.TestCase):
    def test_credential_findings_do_not_contain_matched_values(self):
        secret = ('sk-' + 'synthetic' * 5).encode()
        findings = audit.scan(b'api_key="' + secret + b'"', 'fixture.txt', [])
        self.assertEqual({row['category'] for row in findings}, {'provider_token', 'literal_secret_assignment'})
        self.assertNotIn(secret.decode(), str(findings))

    def test_exact_environment_secret_match_is_redacted(self):
        secret = b'constructed-fixture-value'
        self.assertEqual(audit.scan(secret, 'fixture', [secret]),
                         [{'file': 'fixture', 'category': 'local_environment_secret', 'line': None}])

    def test_owner_links_are_private_but_placeholders_are_allowed(self):
        self.assertEqual(audit.scan(b'OPENAI_API_KEY=\nhttps://YOUR_DOMAIN\n', 'example', []), [])
        findings = audit.scan(b'https://localhost/#owner=' + b'x' * 32, 'fixture', [])
        self.assertEqual(findings[0]['category'], 'owner_url')

    def test_image_metadata_detection_stops_before_jpeg_pixels(self):
        self.assertTrue(audit.image_metadata(b'\xff\xd8\xff\xe1\x00\x08private', 'x.jpg'))
        self.assertFalse(audit.image_metadata(b'\xff\xd8\xff\xda\x00\x08\xff\xe1pixels', 'x.jpg'))
        self.assertTrue(audit.image_metadata(b'\x89PNG\r\n\x1a\n' + (6).to_bytes(4, 'big') + b'tEXtsecret' + b'0000', 'x.png'))

    def test_allowlist_excludes_private_roots_and_reserved_fixtures(self):
        paths = {p.relative_to(ROOT).as_posix() for p in audit.candidates()}
        self.assertIn('tests/test_cortex.py', paths)
        self.assertIn('submission/public_submission_v1/05-file-completion.jpg', paths)
        self.assertFalse(paths & audit.EXCLUDED)
        self.assertFalse(any(p.startswith(('runs/', 'snapshots/', '.cortex/')) for p in paths))

    def test_staged_extra_file_cannot_pass_manifest_comparison(self):
        import json
        import subprocess
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(['git', 'init', '--quiet', str(root)], check=True)
            public = root / 'public.txt'
            public.write_bytes(b'public\n')
            manifest = {'files': [{'path': public.name, 'sha256': audit.digest(public.read_bytes())}]}
            (root / audit.MANIFEST).write_text(json.dumps(manifest), encoding='utf-8')
            (root / 'unreviewed.txt').write_text('must not ship', encoding='utf-8')
            subprocess.run(['git', '-c', 'core.autocrlf=false', '-C', str(root), 'add', '--', public.name, audit.MANIFEST, 'unreviewed.txt'], check=True)
            with patch.object(audit, 'ROOT', root):
                with self.assertRaisesRegex(ValueError, 'Index differs'):
                    audit.staged_audit([public])
