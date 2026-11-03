import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = (ROOT / ".github/workflows/test_android.yaml",
             ROOT / ".github/workflows/test_android_goldens.yaml")


def kvm_steps():
    import yaml
    for workflow in WORKFLOWS:
        document = yaml.safe_load(workflow.read_text())
        for name, job in document["jobs"].items():
            for step in job.get("steps", []):
                if step.get("name", "").startswith("Enable") and "KVM" in step["name"]:
                    yield f"{workflow.name}/{name}", step["run"]


class KvmAccessTest(unittest.TestCase):
    def test_emulators_require_hardware_acceleration(self):
        options = [line for workflow in WORKFLOWS for line in workflow.read_text().splitlines()
                   if "emulator-options:" in line]
        self.assertEqual(len(options), 3)
        for line in options:
            self.assertIn("-accel on", line)
            self.assertNotIn("-accel off", line)

    def run_step(self, script, *, present=True, chmod_status=0, chmod_effect=True):
        with tempfile.TemporaryDirectory() as directory:
            env = {**os.environ, "KVM_FIXTURE": directory,
                   "KVM_PRESENT": str(int(present)), "CHMOD_STATUS": str(chmod_status),
                   "CHMOD_EFFECT": str(int(chmod_effect))}
            # Run the actual workflow shell with device/system calls isolated.
            # udev triggers complete before their permission event is processed,
            # as permitted by udevadm trigger without --settle on Ubuntu 22.04.
            harness = r'''
test() {
  if [[ "$#" == 2 && "$2" == /dev/kvm ]]; then
    printf 'test %s %s\n' "$1" "$2" >> "$KVM_FIXTURE/calls"
    case "$1" in
      -c) [[ "$KVM_PRESENT" == 1 ]]; return ;;
      -r|-w) builtin test -f "$KVM_FIXTURE/accessible"; return ;;
    esac
  fi
  builtin test "$@"
}
sudo() {
  printf 'sudo %s\n' "$*" >> "$KVM_FIXTURE/calls"
  case "$1" in
    tee) cat ;;
    udevadm) return 0 ;;
    chmod)
      if [[ "$CHMOD_STATUS" != 0 ]]; then return "$CHMOD_STATUS"; fi
      if [[ "$CHMOD_EFFECT" == 1 ]]; then : > "$KVM_FIXTURE/accessible"; fi
      ;;
    *) return 99 ;;
  esac
}
export -f test sudo
set -e
'''
            result = subprocess.run(["bash", "-c", harness + script], cwd=ROOT,
                                    env=env, capture_output=True, text=True)
            calls = Path(directory, "calls").read_text()
            return result, calls

    def test_existing_device_becomes_accessible_before_immediate_check(self):
        steps = list(kvm_steps())
        self.assertEqual(len(steps), 3)
        for name, script in steps:
            with self.subTest(step=name):
                result, calls = self.run_step(script)
                self.assertEqual(result.returncode, 0, result.stderr + calls)

    def test_missing_device_and_permission_failures_remain_failures(self):
        for name, script in kvm_steps():
            with self.subTest(step=name, failure="missing device"):
                result, calls = self.run_step(script, present=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("sudo chmod", calls)
            with self.subTest(step=name, failure="chmod rejected"):
                result, _ = self.run_step(script, chmod_status=7)
                self.assertEqual(result.returncode, 7)
            with self.subTest(step=name, failure="still inaccessible"):
                result, _ = self.run_step(script, chmod_effect=False)
                self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
