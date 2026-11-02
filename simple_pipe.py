"""Build immutable Compose releases and deploy them over pinned-host SSH."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def digest(path):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def config(path):
    data = json.loads(Path(path).read_text())
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,40}', data['project']):
        raise ValueError('Invalid project name')
    if not data['services']:
        raise ValueError('At least one service is required')
    for name, value in data['services'].items():
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]*', name):
            raise ValueError('Invalid service name')
        if set(value) != {'context', 'dockerfile'}:
            raise ValueError('Each service needs context and dockerfile')
    return data


def build(args):
    data = config(args.config)
    if not re.fullmatch(r'[0-9a-f]{40}', args.sha):
        raise ValueError('A full Git commit SHA is required')
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=False)
    images = {}
    for name, service in data['services'].items():
        tag = f"simple-pipe/{data['project']}-{name}:{args.sha}"
        run('docker', 'build', '--label', f'org.opencontainers.image.revision={args.sha}',
            '-t', tag, '-f', service['dockerfile'], service['context'])
        images[name] = tag
    run('docker', 'save', '-o', str(out / 'images.tar'), *images.values())
    manifest = {'format': 1, 'project': data['project'], 'sha': args.sha,
                'images': images, 'images_sha256': digest(out / 'images.tar')}
    (out / 'manifest.json').write_text(json.dumps(manifest))
    shutil.copyfile(Path(__file__).with_name('deploy.py'), out / 'deploy.py')
    with tarfile.open(out / 'release.tar.gz', 'w:gz') as archive:
        for name in ('manifest.json', 'images.tar', 'deploy.py'):
            archive.add(out / name, arcname=name)


def send(args):
    host = os.environ['DEPLOY_HOST']
    user = os.environ['DEPLOY_USER']
    directory = os.environ['DEPLOY_DIRECTORY']
    port = os.environ.get('DEPLOY_PORT', '22')
    if not re.fullmatch(r'[a-zA-Z0-9.:-]+', host) or host.startswith('-'):
        raise ValueError('Invalid host')
    if not re.fullmatch(r'[a-z_][a-z0-9_-]*', user):
        raise ValueError('Invalid SSH user')
    if not re.fullmatch(r'/[a-zA-Z0-9/_-]+', directory):
        raise ValueError('Deploy directory must be an absolute path without spaces')
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError('Invalid SSH port')
    run_id = os.environ['GITHUB_RUN_NUMBER']
    if not run_id.isdigit():
        raise ValueError('Invalid run number')
    # The SSH identity and known_hosts are prepared by the workflow, never logged.
    options = ['-i', os.environ['DEPLOY_KEY_FILE'], '-o', 'BatchMode=yes',
               '-o', 'StrictHostKeyChecking=yes', '-o',
               f"UserKnownHostsFile={os.environ['DEPLOY_KNOWN_HOSTS_FILE']}",
               '-o', 'ConnectTimeout=20']
    target = f'{user}@{host}'
    remote = run('ssh', *options, '-p', port, target,
                 'umask 077; mktemp -d /tmp/simple-pipe.XXXXXXXXXX',
                 capture_output=True, text=True).stdout.strip()
    if not re.fullmatch(r'/tmp/simple-pipe\.[a-zA-Z0-9]+', remote):
        raise ValueError('Unexpected remote temporary directory')
    try:
        run('scp', *options, '-P', port, str(Path(args.release).resolve()),
            f'{target}:{remote}/release.tar.gz')
        # Tar members originate from this toolkit, never from pull request artifacts.
        command = (f'cd {remote} && tar -xzf release.tar.gz && '
                   f'python3 deploy.py --directory {directory} --run {run_id}')
        run('ssh', *options, '-p', port, target, command)
    finally:
        run('ssh', *options, '-p', port, target, f'rm -rf -- {remote}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='command', required=True)
    build_parser = subs.add_parser('build')
    build_parser.add_argument('--config', default='.simple-pipe/project.json')
    build_parser.add_argument('--sha', required=True)
    build_parser.add_argument('--out', default='dist')
    send_parser = subs.add_parser('send')
    send_parser.add_argument('--release', required=True)
    args = parser.parse_args()
    {'build': build, 'send': send}[args.command](args)


if __name__ == '__main__':
    main()
