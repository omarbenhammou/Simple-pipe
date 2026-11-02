"""Server-side deployment transaction. Requires Linux, Python 3.11+, Compose v2."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def atomic_json(path, data):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, indent=2))
    temporary.replace(path)


def deploy(directory, number):
    root = Path(directory).resolve(strict=True)
    state = root / '.simple-pipe'
    state.mkdir(mode=0o700, exist_ok=True)
    # Hold this descriptor for the entire transaction, including recovery.
    with (state / 'lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        manifest = json.loads(Path('manifest.json').read_text())
        if manifest['format'] != 1 or not re.fullmatch(r'[0-9a-f]{40}', manifest['sha']):
            raise ValueError('Invalid release manifest')
        project = manifest['project']
        if not re.fullmatch(r'[a-z][a-z0-9-]{0,40}', project):
            raise ValueError('Invalid project')
        if (root / 'project-name').read_text().strip() != project:
            raise ValueError('Release belongs to a different project')
        images = manifest['images']
        if not images:
            raise ValueError('Empty release')
        for name, image in images.items():
            if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]*', name):
                raise ValueError('Invalid service')
            if image != f"simple-pipe/{project}-{name}:{manifest['sha']}":
                raise ValueError('Unexpected image tag')
        with Path('images.tar').open('rb') as stream:
            actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        if actual != manifest['images_sha256']:
            raise ValueError('Image checksum mismatch')
        current = state / 'current.json'
        previous = json.loads(current.read_text()) if current.exists() else None
        if previous and number <= previous['run']:
            print('Skipping a release older than or equal to the deployed run')
            return
        compose = ['docker', 'compose', '--project-directory', str(root),
                   '--env-file', str(root / '.env'), '-p', project,
                   '-f', str(root / 'compose.yml')]
        for hook in ('backup', 'health'):
            if not os.access(root / 'hooks' / hook, os.X_OK):
                raise ValueError(f'Mandatory executable hook missing: {hook}')
        # Capture actual running image IDs, including a pre-pipeline deployment.
        rollback = {}
        for name in images:
            container = run(*compose, 'ps', '-q', name, capture_output=True, text=True).stdout.strip()
            if not container or '\n' in container:
                raise ValueError(f'Expected one existing container for {name}; bootstrap it first')
            image_id = run('docker', 'inspect', '--format', '{{.Image}}', container,
                           capture_output=True, text=True).stdout.strip()
            rollback[name] = {'image': image_id}
        old = state / 'rollback.json'
        candidate = state / 'candidate.json'
        atomic_json(old, {'services': rollback})
        atomic_json(candidate, {'services': {n: {'image': i} for n, i in images.items()}})
        run('docker', 'load', '-i', str(Path('images.tar').resolve()))
        for image in images.values():
            revision = run('docker', 'inspect', '--format',
                           '{{index .Config.Labels "org.opencontainers.image.revision"}}',
                           image, capture_output=True, text=True).stdout.strip()
            if revision != manifest['sha']:
                raise ValueError('Image revision mismatch')
        env = dict(os.environ, SP_DIRECTORY=str(root), SP_SHA=manifest['sha'],
                   SP_OVERRIDE=str(candidate), SP_PROJECT=project)
        # Hooks are provisioned separately on the server, never imported from a release.
        run(str(root / 'hooks' / 'backup'), cwd=root, env=env, timeout=1800)
        switched = False
        try:
            migrate = root / 'hooks' / 'migrate'
            if migrate.exists():
                run(str(migrate), cwd=root, env=env, timeout=900)
            switched = True  # up may fail after partially replacing containers
            run(*compose, '-f', str(candidate), 'up', '-d', '--no-build', '--pull', 'never',
                '--no-deps', '--wait', '--wait-timeout', '180', *images)
            run(str(root / 'hooks' / 'health'), cwd=root, env=env, timeout=240)
        except (subprocess.SubprocessError, OSError):
            if switched:
                print('Deployment failed; restoring previous application images', flush=True)
                run(*compose, '-f', str(old), 'up', '-d', '--no-build', '--pull', 'never',
                    '--no-deps', '--wait', '--wait-timeout', '180', *images)
                run(str(root / 'hooks' / 'health'), cwd=root, env=env, timeout=240)
            raise
        atomic_json(state / 'active.json', json.loads(candidate.read_text()))
        atomic_json(current, {'run': number, 'sha': manifest['sha'], 'images': images})
        with (state / 'history.jsonl').open('a') as history:
            history.write(json.dumps({'run': number, 'sha': manifest['sha']}) + '\n')
        print(f"Deployed {project} at {manifest['sha']}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True)
    parser.add_argument('--run', type=int, required=True)
    args = parser.parse_args()
    if args.run < 1:
        parser.error('run must be positive')
    deploy(args.directory, args.run)
