import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).parents[1] / "official_avd.py"


class OfficialAvdTest(unittest.TestCase):
    def emulator(self, root, exit_code=0):
        binary = root / "src/.android_emulator/fixture/emulator/emulator"
        binary.parent.mkdir(parents=True)
        binary.write_text(f"#!{sys.executable}\nimport sys\nprint('KVM check', *sys.argv[1:])\nsys.exit({exit_code})\n")
        binary.chmod(0o755)

    def test_test_result_and_logs_survive_failed_emulator_cleanup(self):
        for test_result in (0, 7):
            with self.subTest(test_result=test_result), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                commands = root / "bin"
                commands.mkdir()
                launcher = commands / "vpython3"
                launcher.write_text(f"#!{sys.executable}\nprint('android_35_google_apis_x64|emulator-5554 started (pid: 12345)')\n")
                launcher.chmod(0o755)
                adb = commands / "adb"
                adb.write_text(f"#!{sys.executable}\nimport sys\nprint('stopping', *sys.argv[1:])\nsys.exit(42)\n")
                adb.chmod(0o755)
                outputs = root / "results"
                self.emulator(root / "archive")
                env = {**os.environ, "PATH": str(commands) + os.pathsep + os.environ["PATH"],
                       "FLUTTER_CI_AVD_ROOT": str(root / "archive"),
                       "FLUTTER_CI_AVD_CONFIG": "android_35_google_apis_x64.textpb",
                       "FLUTTER_TEST_OUTPUTS_DIR": str(outputs)}
                result = subprocess.run([sys.executable, str(SCRIPT), "run", "--", sys.executable,
                                         "-c", "import os,sys; print(os.environ['ANDROID_SERIAL']); sys.exit(int(sys.argv[1]))",
                                         str(test_result)], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, test_result, result.stdout + result.stderr)
                self.assertIn("emulator-5554", result.stdout)
                self.assertIn("stopping -s emulator-5554 emu kill", (outputs / "avd/stop.log").read_text())
                self.assertEqual(json.loads((outputs / "avd/instance.json").read_text())["pid"], 12345)

    def test_failed_avd_start_never_runs_the_test_command(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launcher = root / "vpython3"
            launcher.write_text(f"#!{sys.executable}\nimport sys\nprint('official AVD failed to boot')\nsys.exit(17)\n")
            launcher.chmod(0o755)
            self.emulator(root)
            env = {**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"],
                   "FLUTTER_CI_AVD_ROOT": str(root), "FLUTTER_CI_AVD_CONFIG": "test.textpb",
                   "FLUTTER_TEST_OUTPUTS_DIR": str(root / "results")}
            # Only executable discovery matters; the failed launcher never calls adb.
            (root / "adb").symlink_to(sys.executable)
            marker = root / "test-ran"
            result = subprocess.run([sys.executable, str(SCRIPT), "run", "--", sys.executable, "-c",
                                     f"from pathlib import Path; Path({str(marker)!r}).touch()"],
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 17, result.stdout + result.stderr)
            self.assertFalse(marker.exists())
            self.assertIn("official AVD failed to boot", (root / "results/avd/emulator.log").read_text())

    def test_unavailable_acceleration_prevents_emulator_and_test_start(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.emulator(root, exit_code=3)
            (root / "adb").symlink_to(sys.executable)
            env = {**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"],
                   "FLUTTER_CI_AVD_ROOT": str(root), "FLUTTER_CI_AVD_CONFIG": "test.textpb",
                   "FLUTTER_TEST_OUTPUTS_DIR": str(root / "results")}
            marker = root / "test-ran"
            result = subprocess.run([sys.executable, str(SCRIPT), "run", "--", sys.executable, "-c",
                                     f"from pathlib import Path; Path({str(marker)!r}).touch()"],
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
            self.assertFalse(marker.exists())
            self.assertFalse((root / "results/avd/emulator.log").exists())
            self.assertIn("KVM check -accel-check", (root / "results/avd/acceleration.log").read_text())


if __name__ == "__main__":
    unittest.main()
