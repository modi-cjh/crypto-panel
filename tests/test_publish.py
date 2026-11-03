import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from publish_snapshot import publish_once, main

NOW = datetime(2026, 11, 3, 1, 0, tzinfo=timezone.utc)


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


class PublishTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        self.remote, self.repo = base / 'remote.git', base / 'repo'
        subprocess.run(['git', 'init', '-q', '--bare', '-b', 'master', str(self.remote)], check=True)
        subprocess.run(['git', 'clone', '-q', str(self.remote), str(self.repo)], check=True, capture_output=True)
        for key, value in (('user.name', 'test'), ('user.email', 'test@example.invalid'), ('commit.gpgsign', 'false')):
            git(self.repo, 'config', key, value)
        git(self.repo, 'checkout', '-q', '-b', 'master')
        (self.repo / 'paper').mkdir()
        (self.repo / 'paper/account.json').write_text('{"v":0}\n')
        (self.repo / 'index.html').write_text('x\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-q', '-m', 'init')
        git(self.repo, 'push', '-q', 'origin', 'master')

    def generator(self, body, code=0):
        script = f"from pathlib import Path\nPath('paper/account.json').write_text({body!r})\nraise SystemExit({code})\n"
        return [sys.executable, '-c', script]

    def test_publishes_only_paper_json(self):
        (self.repo / 'index.html').write_text('local edit\n')
        git(self.repo, 'add', 'index.html')  # 已暂存的其他改动不能被带进快照提交
        result = publish_once(self.repo, 'master', self.generator('{"v":1}\n'), now=NOW, sleep=lambda s: None)
        self.assertEqual(result, 'published')
        self.assertEqual(git(self.remote, 'log', '-1', '--format=%s', 'master'), 'snapshot 2026-11-03T01:00:00Z')
        self.assertEqual(git(self.remote, 'show', '--name-only', '--format=', 'master'), 'paper/account.json')
        self.assertEqual(git(self.remote, 'show', 'master:paper/account.json'), '{"v":1}')

    def test_unchanged_and_failed_generation_do_not_commit(self):
        head = git(self.repo, 'rev-parse', 'HEAD')
        self.assertEqual(publish_once(self.repo, 'master', self.generator('{"v":0}\n')), 'unchanged')
        self.assertEqual(publish_once(self.repo, 'master', [sys.executable, '-c', 'raise SystemExit(1)']), 'skipped')
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), head)

    def test_pulls_remote_changes_before_publishing(self):
        other = self.repo.parent / 'other'
        subprocess.run(['git', 'clone', '-q', str(self.remote), str(other)], check=True, capture_output=True)
        git(other, 'config', 'user.name', 'test'); git(other, 'config', 'user.email', 'test@example.invalid')
        (other / 'index.html').write_text('merged PR\n')
        git(other, 'commit', '-q', '-am', 'code change'); git(other, 'push', '-q', 'origin', 'master')
        self.assertEqual(publish_once(self.repo, 'master', self.generator('{"v":2}\n'), now=NOW), 'published')
        self.assertEqual(git(self.remote, 'show', 'master:index.html'), 'merged PR')

    def test_dry_run_commits_locally_and_wrong_branch_refuses(self):
        self.assertEqual(publish_once(self.repo, 'master', self.generator('{"v":3}\n'), push=False, now=NOW), 'committed')
        self.assertNotEqual(git(self.repo, 'rev-parse', 'HEAD'), git(self.remote, 'rev-parse', 'master'))
        git(self.repo, 'checkout', '-q', '-b', 'feature')
        with self.assertRaises(SystemExit):
            publish_once(self.repo, 'master', self.generator('{"v":4}\n'))

    def test_interval_floor(self):
        with self.assertRaises(SystemExit):
            main(['--interval', '60', '--once'])


if __name__ == '__main__':
    unittest.main()
