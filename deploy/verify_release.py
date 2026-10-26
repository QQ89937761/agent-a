"""Fail a release when its packaged files do not match the reviewed source."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

# Also works when invoked as `python deploy/verify_release.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from deploy.release_files import release_data_files


TEXT_SUFFIXES = {'.py', '.ps1', '.sh', '.html', '.css', '.js', '.md', '.toml',
                 '.json', '.yml', '.yaml', '.txt', '.service', '.conf', '.bat', '.cmd'}


def digest(path: Path, *, normalize_text: bool = False) -> str:
    if normalize_text and path.suffix in TEXT_SUFFIXES:
        return hashlib.sha256(path.read_bytes().replace(b'\r\n', b'\n')).hexdigest()
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def verify_files(expected: dict[str, Path], actual: dict[str, str], label: str,
                 *, normalize_text: bool = False) -> int:
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    stale = sorted(name for name in set(expected) & set(actual)
                   if digest(expected[name], normalize_text=normalize_text) != actual[name])
    if missing or extra or stale:
        raise RuntimeError(f'{label}: missing={missing}, unexpected={extra}, stale={stale}')
    return len(expected)


def verify_frozen_modules(expected: dict[str, Path], archive) -> int:
    missing = sorted(set(expected) - set(archive.toc))
    if missing:
        raise RuntimeError(f'Python application modules missing from frozen archive: {missing}')
    # Compare executable code too: a current payload beside stale bytecode is not a valid release.
    stale = [name for name, source in expected.items()
             if archive.extract(name) != compile(source.read_bytes(), str(source), 'exec',
                                                  dont_inherit=True, optimize=0)]
    if stale:
        raise RuntimeError(f'Python application bytecode differs from source: {sorted(stale)}')
    return len(expected)


def tree_hashes(root: Path, *, normalize_text: bool = False) -> dict[str, str]:
    if not root.is_dir():
        raise RuntimeError(f'Missing release directory: {root}')
    result = {}
    for path in root.rglob('*'):
        if path.is_symlink():
            raise RuntimeError(f'Unexpected release symlink: {path}')
        if path.is_file():
            result[path.relative_to(root).as_posix()] = digest(path, normalize_text=normalize_text)
    return result


def verify(root: Path, resources: Path, *, compare_staging: bool = True,
           reference_revision: str | None = None) -> dict:
    if reference_revision:
        # Read the immutable released commit, even when the publisher has been repaired since.
        work = root / 'work'
        work.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='release-reference-', dir=work) as tmp:
            subprocess.run(['git', 'clone', '--shared', '--no-checkout', '--quiet', str(root), tmp], check=True)
            subprocess.run(['git', '-C', tmp, '-c', 'core.autocrlf=false',
                            'checkout', '--detach', '--quiet', reference_revision], check=True)
            return _verify(Path(tmp), resources, compare_staging=False, dependency_root=root)
    return _verify(root, resources, compare_staging=compare_staging, dependency_root=root)


def _verify(root: Path, resources: Path, *, compare_staging: bool, dependency_root: Path) -> dict:
    from PyInstaller.archive.readers import CArchiveReader
    root, resources = root.resolve(), resources.resolve()
    package = json.loads((root / 'frontend/package.json').read_text(encoding='utf-8-sig'))
    expected_assets = {}
    for source, destination in release_data_files(root):
        source = Path(source)
        expected_assets[(Path(destination) / source.name).as_posix()] = source
    internal = resources / 'backend/_internal'
    actual_assets = {}
    for name in ('server-payload', 'frontend', 'docs'):
        directory = internal / name
        if directory.is_dir():
            actual_assets.update({f'{name}/{key}': value for key, value in tree_hashes(directory, normalize_text=not compare_staging).items()})
    for name in set(expected_assets) - set(actual_assets):
        path = internal / name
        if path.is_file():
            actual_assets[name] = digest(path, normalize_text=not compare_staging)
    asset_count = verify_files(expected_assets, actual_assets, 'cloud payload and assets', normalize_text=not compare_staging)
    for name in ('config.toml', 'ui_token.txt', 'data', 'model-cache', 'work'):
        if (internal / name).exists() or (internal / 'server-payload' / name).exists():
            raise RuntimeError(f'Private development resource in package: {name}')

    backend_actual = tree_hashes(resources / 'backend')
    runtime_actual = tree_hashes(resources / 'plugin-runtime')
    backend_count, runtime_count = len(backend_actual), len(runtime_actual)
    for name in ('python/python.exe', 'node/node.exe', 'node/npm.cmd', 'runtime.json'):
        if name not in runtime_actual:
            raise RuntimeError(f'Bundled runtime missing: {name}')
    if compare_staging:
        backend_source = root / 'dist/xiaoa-backend'
        backend_count = verify_files(
            {name: backend_source / name for name in tree_hashes(backend_source)},
            backend_actual, 'frozen backend copy',
        )
        runtime_source = root / 'work/plugin-runtime'
        runtime_count = verify_files(
            {name: runtime_source / name for name in tree_hashes(runtime_source)},
            runtime_actual, 'bundled runtimes copy',
        )
    # Independent CI binaries need not match local DLL hashes. Their application
    # sources, ASAR and frozen code are still checked exactly below.

    # Use the already installed Electron builder dependency; no extraction into the user's app.
    script = r'''
const asar = require('./frontend/node_modules/@electron/asar');
const crypto = require('node:crypto');
const archive = process.argv[1];
const files = {};
const normalizeText = process.argv[2] === 'true';
for (const entry of asar.listPackage(archive)) {
  const name = entry.replace(/\\/g, '/').replace(/^\/+/, '');
  if (!asar.statFile(archive, name).files) {
    const content = asar.extractFile(archive, name);
    files[name] = crypto.createHash('sha256').update(normalizeText && /\.(js|html|css|json)$/.test(name) ? content.toString('utf8').replace(/\r\n/g, '\n') : content).digest('hex');
  }
}
console.log(JSON.stringify({files, package: JSON.parse(asar.extractFile(archive, 'package.json').toString())}));
'''
    result = json.loads(subprocess.check_output(
        ['node', '-e', script, str(resources / 'app.asar'), str(not compare_staging).lower()], cwd=dependency_root, text=True, encoding='utf-8',
    ))
    expected_frontend = {name: root / 'frontend' / name for name in package['build']['files']}
    actual_frontend = dict(result['files'])
    actual_frontend.pop('package.json', None)  # electron-builder removes dev/build metadata.
    frontend_count = verify_files(expected_frontend, actual_frontend, 'Electron ASAR', normalize_text=not compare_staging)
    if result['package']['version'] != package['version']:
        raise RuntimeError('Packaged frontend version mismatch')

    archive = CArchiveReader(str(resources / 'backend/xiaoa-backend.exe'))
    modules = archive.open_embedded_archive('PYZ.pyz')
    required_modules = {}
    for source in expected_assets.values():
        relative = source.relative_to(root)
        if relative.parts[0] == 'backend' and source.suffix == '.py':
            parts = list(relative.with_suffix('').parts)
            if parts[-1] == '__init__':
                # Empty, unused package markers need no bytecode; their source hash is checked above.
                if not source.read_text(encoding='utf-8-sig').strip():
                    continue
                parts.pop()
            required_modules['.'.join(parts)] = source
    verify_frozen_modules(required_modules, modules)
    return dict(version=package['version'], frontend_files=frontend_count,
                cloud_assets=asset_count, backend_files=backend_count, runtime_files=runtime_count,
                application_modules=len(required_modules))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('resources', nargs='?', type=Path,
                        default=Path('frontend/dist/win-unpacked/resources'))
    args = parser.parse_args()
    print('RELEASE_CONTENTS_VERIFIED ' + json.dumps(verify(Path(__file__).resolve().parent.parent, args.resources)))
