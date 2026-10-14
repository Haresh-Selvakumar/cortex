"""Review an explicit publication allowlist; never print matched credential values.

This heuristic scan complements a manual image/content review. It does not claim
complete secret detection. Private traces/archives are always excluded from Git.
"""
import argparse
from collections import Counter
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = 'publication-manifest.json'
ROOT_FILES = {'.gitignore', '.env.example', 'pyproject.toml', 'requirements-build.txt',
              'README.md', 'START_HERE.md', 'LICENSE_NOTICE.md'}
ROOTS = {'cortex', 'tests', 'scripts', 'docs', 'examples', 'evaluation', 'deployment', 'submission'}
CONFIGS = {'cortex.example.json', 'cortex.public.example.json', 'cortex.public.offline.json', 'tools.example.json'}
EXCLUDED = {'submission/public_submission_v1/06-readback-evidence.jpg',
            'evaluation/extraction_v1/heldout_wording.json'}
TEXT_SUFFIXES = {'.py', '.md', '.txt', '.json', '.toml', '.html', '.js', '.css', '.yaml', '.conf'}
PATTERNS = {
    'private_key': re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----'),
    'provider_token': re.compile(rb'\b(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{22,}|AKIA[A-Z0-9]{16})\b'),
    'owner_url': re.compile(rb'(?:#owner=|[?&]bootstrap=)[A-Za-z0-9_-]{16,}'),
    'credential_url': re.compile(rb'https?://[^/\s"<>]+:[^/@\s"]+@'),
    'literal_secret_assignment': re.compile(rb'''(?i)(?:api[_-]?key|password|access[_-]?token|client[_-]?secret)["']?\s*[:=]\s*["'][A-Za-z0-9_./+=-]{20,}["']'''),
    'local_user_path': re.compile(rb'(?:[A-Z]:[\\/]+Users[\\/]+(?!Public[\\/]|YOUR_USER[\\/])[^\\/\s`"<>]+|/(?:Users|home)/[^/\s`"<>]+)'),
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def candidates():
    paths = [ROOT / name for name in ROOT_FILES]
    paths += [ROOT / 'config' / name for name in CONFIGS]
    for name in ROOTS:
        for path in (ROOT / name).rglob('*'):
            relative = path.relative_to(ROOT)
            if not path.is_file() or '__pycache__' in relative.parts or relative.as_posix() in EXCLUDED:
                continue
            if path.suffix in {'.pyc', '.pyo'}:
                continue
            paths.append(path)
    for path in paths:
        if path.is_symlink() or path.resolve() != path.absolute():
            raise ValueError('Symlink or redirected file in publication: ' + path.relative_to(ROOT).as_posix())
    return sorted(paths)


def environment_secrets():
    # Known local values only, never included in findings or reports.
    return [value.encode() for name, value in os.environ.items()
            if re.search(r'(?:API_KEY|ACCESS_TOKEN|AUTH_TOKEN|PASSWORD|SECRET|^GH_TOKEN$|^GITHUB_TOKEN$)', name)
            and len(value) >= 12]


def scan(data, name, secrets):
    findings = []
    for category, pattern in PATTERNS.items():
        for match in pattern.finditer(data):
            findings.append({'file': name, 'category': category,
                             'line': data[:match.start()].count(b'\n') + 1})
    if any(value in data for value in secrets):
        findings.append({'file': name, 'category': 'local_environment_secret', 'line': None})
    return findings


def image_metadata(data, name):
    """Reject image metadata payloads; pixel privacy still requires visual review."""
    if name.endswith('.png'):
        offset = 8
        while offset + 12 <= len(data):
            length = int.from_bytes(data[offset:offset + 4], 'big')
            if data[offset + 4:offset + 8] in {b'tEXt', b'zTXt', b'iTXt', b'eXIf'}:
                return True
            offset += 12 + length
    elif name.endswith('.jpg'):
        offset = 2
        while offset + 4 <= len(data) and data[offset] == 255:
            marker = data[offset + 1]
            if marker == 218:  # compressed pixels begin
                break
            if marker in {225, 237, 254}:  # EXIF/XMP, IPTC or comments
                return True
            offset += 2 + int.from_bytes(data[offset + 2:offset + 4], 'big')
    return False


def public_audit(paths):
    findings = []
    files = []
    secrets = environment_secrets()
    for path in paths:
        name = path.relative_to(ROOT).as_posix()
        data = path.read_bytes()
        if path.suffix in {'.jpg', '.png'} and name.startswith('submission/public_submission_v1/'):
            if image_metadata(data, name):
                findings.append({'file': name, 'category': 'image_metadata_requires_review', 'line': None})
        elif path.suffix not in TEXT_SUFFIXES and name not in ROOT_FILES and path.name not in {'Dockerfile', 'Dockerfile.dockerignore'}:
            findings.append({'file': name, 'category': 'unreviewed_file_type', 'line': None})
        findings.extend(scan(data, name, secrets))
        files.append({'path': name, 'bytes': len(data), 'sha256': digest(data)})
    return files, findings


def private_audit():
    """Read archives in memory. Do not extract or include matched text in reports."""
    findings = []
    counters = Counter()
    secrets = environment_secrets()

    def inspect(data, name, depth=0):
        counters['scanned_members'] += 1
        findings.extend(scan(data, name, secrets))
        if name.endswith(('.zip', '.whl')):
            if depth >= 4:
                raise ValueError('Archive nesting requires manual inspection')
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                if sum(entry.file_size for entry in archive.infolist()) > 150_000_000:
                    raise ValueError('Archive exceeds bounded review size')
                for entry in archive.infolist():
                    if not entry.is_dir():
                        inspect(archive.read(entry), name + '!' + entry.filename, depth + 1)

    for name in ('runs', 'snapshots', 'releases'):
        for path in (ROOT / name).rglob('*'):
            if path.is_file():
                counters['local_files'] += 1
                inspect(path.read_bytes(), path.relative_to(ROOT).as_posix())
    return {'scope': 'local-only runs, snapshots and releases, including nested archive members',
            'counts': dict(counters), 'finding_counts': dict(Counter(f['category'] for f in findings)),
            'locations': findings,
            'disposition': 'excluded regardless of scan results; originals preserved',
            'limitations': 'heuristic byte/text scan; binary pixels require separate visual review'}


def staged_audit(paths):
    manifest = json.loads((ROOT / MANIFEST).read_text(encoding='utf-8'))
    expected = {row['path']: row['sha256'] for row in manifest['files']}
    actual_paths = {p.relative_to(ROOT).as_posix() for p in paths}
    if set(expected) != actual_paths:
        raise ValueError('Manifest file set differs from current allowlist')
    staged = subprocess.run(['git', 'ls-files', '--stage', '-z'], cwd=ROOT,
                            check=True, capture_output=True).stdout.split(b'\0')
    found = {}
    for row in staged:
        if not row:
            continue
        header, raw_name = row.split(b'\t', 1)
        mode, object_id, stage = header.split()
        name = raw_name.decode('utf-8')
        if mode != b'100644' or stage != b'0':
            raise ValueError('Unexpected index mode/stage: ' + name)
        data = subprocess.run(['git', 'cat-file', 'blob', object_id.decode()], cwd=ROOT,
                              check=True, capture_output=True).stdout
        found[name] = digest(data)
    expected[MANIFEST] = digest((ROOT / MANIFEST).read_bytes())
    if expected != found:
        raise ValueError('Index differs from exact reviewed file hashes')
    for path in paths:
        if digest(path.read_bytes()) != expected[path.relative_to(ROOT).as_posix()]:
            raise ValueError('Working file changed after manifest review')
    print(json.dumps({'staged_files': len(found), 'exact_manifest_match': True}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write-manifest', action='store_true')
    parser.add_argument('--staged', action='store_true')
    parser.add_argument('--private-report', type=Path)
    args = parser.parse_args()
    paths = candidates()
    files, findings = public_audit(paths)
    print(json.dumps({'public_files': len(files), 'findings': findings}, indent=2))
    if findings:
        return 2
    if args.write_manifest:
        (ROOT / MANIFEST).write_text(json.dumps({'review_version': 'github-publication-v1',
            'limitations': 'Heuristic scans plus manual image/content review; not a secret-detection guarantee.',
            'manifest_self_excluded': True, 'files': files}, indent=2) + '\n', encoding='utf-8')
    if args.private_report:
        report_path = args.private_report.resolve()
        if ROOT / '.test_tmp' not in report_path.parents:
            raise ValueError('Private audit report must stay under ignored .test_tmp')
        report = private_audit()
        with report_path.open('x', encoding='utf-8') as output:
            json.dump(report, output, indent=2)
        print(json.dumps({k: v for k, v in report.items() if k != 'locations'}))
    if args.staged:
        staged_audit(paths)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
