import importlib.util
import json
from pathlib import Path
import tempfile
import sys
from types import SimpleNamespace
import unittest
from unittest import mock


spec = importlib.util.spec_from_file_location("android_ci", Path(__file__).parents[1] / "android.py")
android = importlib.util.module_from_spec(spec)
spec.loader.exec_module(android)


class AndroidConfigurationTest(unittest.TestCase):
    def test_command_failure_retains_exit_status_and_captured_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.dict(android.os.environ, {"FLUTTER_TEST_OUTPUTS_DIR": str(root / "results")}):
                runner = android.Android(root)
            with self.assertRaises(android.subprocess.CalledProcessError) as result:
                runner.run([sys.executable, "-c", "print('failure-detail'); raise SystemExit(7)"])
            self.assertEqual(result.exception.returncode, 7)
            self.assertIn("failure-detail", (root / "results/android-run.log").read_text())

    def test_remote_execution_is_removed_without_losing_target_flags(self):
        result = android.local_gn(["--android", "--android-cpu=x64", "--rbe", "--no-goma"])
        self.assertEqual(result, ["--android", "--android-cpu=x64", "--no-lto"])

    def test_page_size_is_preserved_in_avd_target(self):
        self.assertEqual(android.avd_configuration("android_37_google_apis_ps16k_x64.textpb"),
                         {"api": "37.0", "target": "google_apis_ps16k", "arch": "x86_64"})
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            android.avd_configuration("android_37_unknown_arm64.textpb")

    def test_task_dependencies_do_not_hide_platform_avd_and_new_tasks_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".ci.yaml").write_text("fixture")
            extension = root / "packages/flutter_tools/gradle/src/main/kotlin/FlutterExtension.kt"
            extension.parent.mkdir(parents=True)
            extension.write_text('val ndkVersion: String = "28.2.13676358"')
            tasks = root / "dev/devicelab/bin/tasks"
            tasks.mkdir(parents=True)
            targets = [{"name": f"Linux {task}", "properties": {
                "task_name": task, "dependencies": [{"dependency": "open_jdk", "version": "version:25"}]}}
                for task in android.BUILD_TASKS]
            for task in android.ADAPTED_TASKS:
                (tasks / f"{task}.dart").touch()
                targets.append({"name": f"Linux_mokey {task}", "properties": {"task_name": task}})
            targets.append({"name": "Linux_android_emu android views", "properties": {
                "task_name": "android_views", "dependencies": [{"dependency": "goldctl", "version": "v1"}]}})
            ci = {"targets": targets, "platform_properties": {"linux_android_emu": {"properties": {
                "dependencies": [{"dependency": "android_virtual_device",
                                  "version": "android_36_google_apis_x64.textpb"}]}}}}
            with mock.patch.dict("sys.modules", {"yaml": SimpleNamespace(safe_load=lambda _: ci)}):
                plan = android.source_plan(root)
                self.assertEqual(plan["emulator"]["include"][0]["api"], "36")
                self.assertEqual(plan["emulator"]["include"][0]["ndk"], "28.2.13676358")
                targets.append({"name": "Linux_android_emu newly_added", "properties": {"task_name": "newly_added"}})
                with self.assertRaisesRegex(ValueError, "New Android emulator task"):
                    android.source_plan(root)


class EngineSelectionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for mode in ("debug", "profile", "release"):
            for variant in (f"ci/android_{mode}_x64", f"ci/host_{mode}_test"):
                output = self.root / "engine/src/out" / variant
                output.mkdir(parents=True)
                (output / "flutter-ci-build.json").write_text(json.dumps({"sha": "a" * 40}))
                (self.root / "engine/src/out" / variant.removeprefix("ci/")).symlink_to(variant)
        self.git = mock.patch.object(android.subprocess, "check_output", return_value="a" * 40)
        self.git.start()
        self.addCleanup(self.git.stop)

    def test_debug_profile_release_calls_use_the_corresponding_source_engines(self):
        for mode in ("debug", "profile", "release"):
            with self.subTest(mode=mode):
                args, selected = android.flutter_arguments(["run", f"--{mode}", "-d", "emulator-5554"],
                                                           self.root, ["debug", "profile", "release"])
                self.assertEqual(selected, mode)
                self.assertIn(f"--local-engine=android_{mode}_x64", args)
                self.assertIn(f"--local-engine-host=host_{mode}_test", args)
                self.assertEqual(args[-4:], ["run", f"--{mode}", "-d", "emulator-5554"])

    def test_apk_build_defaults_to_release_but_test_defaults_to_debug(self):
        self.assertEqual(android.flutter_arguments(["build", "apk"], self.root, ["release"])[1], "release")
        self.assertEqual(android.flutter_arguments(["install"], self.root, ["release"])[1], "release")
        self.assertEqual(android.flutter_arguments(["test", "integration_test"], self.root, ["debug"])[1], "debug")

    def test_missing_modes_and_stale_artifacts_fail_instead_of_using_baseline(self):
        with self.assertRaisesRegex(ValueError, "only"):
            android.flutter_arguments(["run", "--profile"], self.root, ["debug"])
        manifest = self.root / "engine/src/out/ci/android_debug_x64/flutter-ci-build.json"
        manifest.write_text(json.dumps({"sha": "b" * 40}))
        with self.assertRaisesRegex(ValueError, "belongs to"):
            android.flutter_arguments(["drive"], self.root, ["debug"])

    def test_explicit_fixed_local_engine_cannot_override_mode_selection(self):
        with self.assertRaisesRegex(ValueError, "Do not mix"):
            android.flutter_arguments(["--local-engine=wrong", "run"], self.root, ["debug"])

    def test_preparation_commands_remain_unchanged(self):
        for args in (["pub", "get"], ["create", "app"], ["build", "apk", "--config-only"],
                     ["install", "--uninstall-only"]):
            self.assertEqual(android.flutter_arguments(args, self.root, []), (args, None))

    def test_incomplete_transferred_runtime_fails_before_starting_tests(self):
        with self.assertRaisesRegex(ValueError, "Incomplete source runtime artifact"):
            android.Android(self.root).validate_app(["debug"])

    def test_wrapper_restores_original_and_accepts_new_checkout_entrypoint(self):
        binary = self.root / "bin/flutter"
        binary.parent.mkdir()
        binary.write_text("#!/bin/sh\necho upstream-original\n")
        with mock.patch.dict(android.os.environ, {"FLUTTER_TEST_OUTPUTS_DIR": str(self.root / "results")}):
            runner = android.Android(self.root)
        runner.install_wrapper(["debug"])
        runner.install_wrapper(["debug", "release"])
        runner.restore_wrapper()
        self.assertEqual(binary.read_text(), "#!/bin/sh\necho upstream-original\n")
        runner.install_wrapper(["debug"])
        binary.write_text("#!/bin/sh\necho new-checkout\n")
        runner.install_wrapper(["debug"])
        runner.restore_wrapper()
        self.assertEqual(binary.read_text(), "#!/bin/sh\necho new-checkout\n")


if __name__ == "__main__":
    unittest.main()
