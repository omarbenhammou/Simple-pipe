import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

if sys.platform == 'win32':
    sys.modules['fcntl'] = types.SimpleNamespace(LOCK_EX=2, flock=lambda *args: None)

import deploy
import simple_pipe


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.old_cwd = Path.cwd()
        os.chdir(self.temp.name)
        self.addCleanup(os.chdir, self.old_cwd)
        self.root = Path('server').resolve()
        (self.root / 'hooks').mkdir(parents=True)
        for name in ('backup', 'health', 'migrate'):
            (self.root / 'hooks' / name).write_text('#!/bin/sh\nexit 0\n')
            (self.root / 'hooks' / name).chmod(0o700)
        (self.root / 'project-name').write_text('demo')
        Path('images.tar').write_bytes(b'images')
        self.sha = 'a' * 40
        self.manifest = {'format': 1, 'project': 'demo', 'sha': self.sha,
                         'images': {'web': f'simple-pipe/demo-web:{self.sha}'},
                         'images_sha256': simple_pipe.digest('images.tar')}
        self.save()
        self.calls = []
        self.fail_health = False

    def save(self):
        Path('manifest.json').write_text(json.dumps(self.manifest))

    def fake_run(self, *args, **kwargs):
        self.calls.append(args)
        if 'ps' in args:
            output = 'container-id'
        elif '{{.Image}}' in args:
            output = 'sha256:old-image'
        elif 'inspect' in args:
            output = self.sha
        else:
            output = ''
        if args[0] == str(self.root / 'hooks' / 'health') and self.fail_health:
            self.fail_health = False
            raise subprocess.CalledProcessError(1, args)
        return subprocess.CompletedProcess(args, 0, stdout=output)

    def execute(self):
        with patch.object(deploy, 'run', self.fake_run), patch.object(deploy.os, 'access', return_value=True):
            deploy.deploy(self.root, 2)

    def test_orders_backup_migration_switch_and_records_success(self):
        self.execute()
        backup = next(i for i, c in enumerate(self.calls) if Path(c[0]).name == 'backup')
        migrate = next(i for i, c in enumerate(self.calls) if Path(c[0]).name == 'migrate')
        switch = next(i for i, c in enumerate(self.calls) if 'up' in c)
        self.assertLess(backup, migrate)
        self.assertLess(migrate, switch)
        self.assertEqual(json.loads((self.root / '.simple-pipe/current.json').read_text())['sha'], self.sha)

    def test_health_failure_restores_actual_image_and_does_not_promote(self):
        self.fail_health = True
        with self.assertRaises(subprocess.CalledProcessError):
            self.execute()
        switches = [c for c in self.calls if 'up' in c]
        self.assertEqual(len(switches), 2)
        self.assertIn(str(self.root / '.simple-pipe/rollback.json'), switches[-1])
        self.assertFalse((self.root / '.simple-pipe/current.json').exists())
        rollback = json.loads((self.root / '.simple-pipe/rollback.json').read_text())
        self.assertEqual(rollback['services']['web']['image'], 'sha256:old-image')

    def test_corrupted_archive_is_rejected_before_docker(self):
        Path('images.tar').write_bytes(b'corrupted')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            self.execute()
        self.assertEqual(self.calls, [])

    def test_cross_project_is_rejected(self):
        (self.root / 'project-name').write_text('different')
        with self.assertRaisesRegex(ValueError, 'different project'):
            self.execute()

    def test_stale_run_is_skipped(self):
        state = self.root / '.simple-pipe'
        state.mkdir()
        (state / 'current.json').write_text(json.dumps({'run': 3}))
        self.execute()
        self.assertEqual(self.calls, [])

    def test_backup_failure_never_switches(self):
        def fail(*args, **kwargs):
            if Path(args[0]).name == 'backup':
                raise subprocess.CalledProcessError(1, args)
            return self.fake_run(*args, **kwargs)
        with patch.object(deploy, 'run', fail), patch.object(deploy.os, 'access', return_value=True):
            with self.assertRaises(subprocess.CalledProcessError):
                deploy.deploy(self.root, 2)
        self.assertFalse(any('up' in c for c in self.calls))

    def test_partial_compose_failure_triggers_rollback(self):
        failed = False
        def fail(*args, **kwargs):
            nonlocal failed
            if 'up' in args and not failed:
                failed = True
                self.calls.append(args)
                raise subprocess.CalledProcessError(1, args)
            return self.fake_run(*args, **kwargs)
        with patch.object(deploy, 'run', fail), patch.object(deploy.os, 'access', return_value=True):
            with self.assertRaises(subprocess.CalledProcessError):
                deploy.deploy(self.root, 2)
        self.assertEqual(len([c for c in self.calls if 'up' in c]), 2)


class ConfigTests(unittest.TestCase):
    def test_empty_services_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            path.write_text('{"project":"demo","services":{}}')
            with self.assertRaises(ValueError):
                simple_pipe.config(path)

    def test_example_config_validates(self):
        self.assertEqual(simple_pipe.config(Path(__file__).resolve().parents[1] / 'examples/project.json')['project'], 'my-app')

    def test_send_rejects_shell_injection_before_ssh(self):
        env = {'DEPLOY_HOST': 'host;whoami', 'DEPLOY_USER': 'ubuntu',
               'DEPLOY_DIRECTORY': '/srv/app'}
        with patch.dict(os.environ, env), patch.object(simple_pipe, 'run') as command:
            with self.assertRaises(ValueError):
                simple_pipe.send(argparse.Namespace(release='release.tar.gz'))
            command.assert_not_called()


if __name__ == '__main__':
    unittest.main()
