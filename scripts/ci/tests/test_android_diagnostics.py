import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import android


class AndroidDiagnosticsTest(unittest.TestCase):
    def test_live_logcat_survives_failure_and_collector_is_stopped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adb = root / 'adb'
            adb.write_text(f'#!{sys.executable}\nimport os, time\n'
                           'print(f"LIVE_LOGCAT pid={os.getpid()}", flush=True)\n'
                           'time.sleep(60)\n')
            adb.chmod(0o755)
            outputs = root / 'results'
            failure = subprocess.CalledProcessError(7, ['upstream-test'])
            with mock.patch.object(android.shutil, 'which', return_value=str(adb)):
                with self.assertRaises(subprocess.CalledProcessError) as caught:
                    with android.monitor_android(outputs, interval=0.02):
                        log = outputs / 'logcat-live.log'
                        deadline = time.monotonic() + 3
                        while time.monotonic() < deadline and 'LIVE_LOGCAT' not in log.read_text():
                            time.sleep(0.01)
                        self.assertIn('LIVE_LOGCAT', log.read_text())
                        pid = int(log.read_text().split('pid=')[1].splitlines()[0])
                        raise failure
            self.assertIs(caught.exception, failure)
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
            self.assertIn('LIVE_LOGCAT', log.read_text())
            self.assertIn('timestamp', (outputs / 'runner-resources.jsonl').read_text())

    def test_missing_adb_and_spawn_error_do_not_override_test_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            for adb in (None, str(Path(directory) / 'missing-adb')):
                with self.subTest(adb=adb), mock.patch.object(android.shutil, 'which', return_value=adb):
                    failure = RuntimeError('original test failure')
                    with self.assertRaises(RuntimeError) as caught:
                        with android.monitor_android(Path(directory) / 'results'):
                            raise failure
                    self.assertIs(caught.exception, failure)

    def test_dry_run_does_not_start_collectors(self):
        with tempfile.TemporaryDirectory() as directory:
            outputs = Path(directory) / 'results'
            with mock.patch.object(android.subprocess, 'Popen') as spawn:
                with android.monitor_android(outputs, enabled=False):
                    pass
            spawn.assert_not_called()
            self.assertFalse(outputs.exists())


if __name__ == '__main__':
    unittest.main()
