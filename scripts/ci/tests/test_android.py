import importlib.util
import json
from pathlib import Path
import tempfile
import sys
import textwrap
from types import SimpleNamespace
import unittest
from unittest import mock


spec = importlib.util.spec_from_file_location("android_ci", Path(__file__).parents[1] / "android.py")
android = importlib.util.module_from_spec(spec)
spec.loader.exec_module(android)


class AndroidConfigurationTest(unittest.TestCase):
    def policy(self, excluded=(), engine=("linux_unopt", "linux_android_emulator")):
        return SimpleNamespace(
            targets=lambda ci: [target for target in ci["targets"] if target["name"] not in excluded],
            rejection=lambda target, ci: "not scheduled by the official policy" if target["name"] in excluded else None,
            engine_enabled=lambda root, name: name in engine,
        )

    def old_ref_fixture(self, root):
        """Relevant contract from littleGnAl/flutter@225e8f4 (Flutter 3.35)."""
        (root / ".ci.yaml").write_text("older-ref fixture")
        extension = root / "packages/flutter_tools/gradle/src/main/kotlin/FlutterExtension.kt"
        extension.parent.mkdir(parents=True)
        extension.write_text('val ndkVersion: String = "27.0.12077973"')
        tasks = root / "dev/devicelab/bin/tasks"
        tasks.mkdir(parents=True)
        absent_builds = {"android_java17_build_android_host_app_with_module_aar",
                         "plugin_test_android_variants", "plugin_test_android_standard"}
        build_names = [t for t in android.BUILD_TASKS if t not in absent_builds and t != "plugin_test"]
        build_names.append("plugin_test")
        targets = [{"name": f"Linux {task}", "properties": {
            "task_name": task, "dependencies": [{"dependency": "open_jdk", "version": "version:21"}]}}
            for task in build_names]
        emulator_names = ["android_views", "android_display_cutout", "android_defines_test",
                          "external_textures_integration_test", "native_assets_android"]
        targets += [{"name": f"Linux_android_emu {task}", "properties": {"task_name": task}}
                    for task in emulator_names]
        # Official physical tasks must not become emulator jobs merely because
        # their task source and .ci entries exist.
        physical_tasks = ["channels_integration_test", "integration_ui_driver", "flavors_test"]
        targets += [{"name": f"Linux_mokey {task}", "properties": {"task_name": task}}
                    for task in physical_tasks]
        for task in build_names + emulator_names + physical_tasks:
            (tasks / f"{task}.dart").write_text("Future<void> main() async { await runTests(); }\n")
        (tasks / "android_java11_dependency_smoke_tests.dart").write_text('''
Future<void> main() async {
  // Empty until removal of the Java 11 CI configuration.
  await task(() async {
    return TaskResult.success(null);
  });
}
''')
        return {"targets": targets, "platform_properties": {"linux_android_emu": {"properties": {
            "dependencies": [{"dependency": "android_virtual_device",
                              "version": "android_36_google_apis_x64.textpb"},
                             {"dependency": "open_jdk", "version": "version:21"}]}}}}

    def test_old_ref_plan_uses_available_ci_tasks_and_reports_future_features(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ci = self.old_ref_fixture(root)
            with mock.patch.dict("sys.modules", {"yaml": SimpleNamespace(safe_load=lambda _: ci)}):
                plan = android.source_plan(root, self.policy())
            builds = {item["task"] for item in plan["build"]["include"]}
            self.assertIn("plugin_test", builds)
            self.assertNotIn("plugin_test_android_standard", builds)
            self.assertNotIn("android_java11_dependency_smoke_tests", builds)
            self.assertEqual(len(builds), 15)
            unavailable = {item["task"] for item in plan["coverage"]["unavailable"]}
            self.assertIn("android_java17_build_android_host_app_with_module_aar", unavailable)
            self.assertIn("android_intent_parsing_test", unavailable)
            self.assertEqual(plan["emulator"]["include"][0]["jdk"], "21")
            self.assertEqual(len(plan["emulator"]["include"]), 5)
            self.assertTrue(all(not item["id"].endswith("-adapted") for item in plan["emulator"]["include"]))

    def test_old_ref_with_groovy_extension_reads_its_ndk(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ci = self.old_ref_fixture(root)
            (root / "packages/flutter_tools/gradle/src/main/kotlin/FlutterExtension.kt").unlink()
            extension = root / "packages/flutter_tools/gradle/src/main/groovy/flutter.groovy"
            extension.parent.mkdir(parents=True)
            extension.write_text("class FlutterExtension { static String ndkVersion = '26.3.11579264' }")
            with mock.patch.dict("sys.modules", {"yaml": SimpleNamespace(safe_load=lambda _: ci)}):
                plan = android.source_plan(root, self.policy())
            self.assertEqual(plan["emulator"]["include"][0]["ndk"], "26.3.11579264")

    def test_policy_excludes_stale_and_bringup_targets_before_source_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ci = self.old_ref_fixture(root)
            (root / "dev/devicelab/bin/tasks/android_views.dart").unlink()
            excluded = {"Linux plugin_test", "Linux_android_emu android_views"}
            with mock.patch.dict("sys.modules", {"yaml": SimpleNamespace(safe_load=lambda _: ci)}):
                plan = android.source_plan(root, self.policy(excluded, engine=("linux_unopt",)))
                self.assertNotIn("plugin_test", {item["task"] for item in plan["build"]["include"]})
                self.assertNotIn("android_views", {item["task"] for item in plan["emulator"]["include"]})
                self.assertEqual(plan["embedding"], {"include": [{"suite": "java"}]})
                unavailable = {item["task"]: item for item in plan["coverage"]["unavailable"]}
                self.assertIn("policy", unavailable["android_views"]["reason"])
                self.assertIn("policy", unavailable["plugin_test"]["reason"])
                # A source file missing from an enabled task remains an error.
                with self.assertRaisesRegex(ValueError, "source file"):
                    android.source_plan(root, self.policy())

    def test_empty_official_schedule_returns_empty_matrices(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ci = self.old_ref_fixture(root)
            excluded = {target["name"] for target in ci["targets"]}
            with mock.patch.dict("sys.modules", {"yaml": SimpleNamespace(safe_load=lambda _: ci)}):
                plan = android.source_plan(root, self.policy(excluded, engine=()))
            for group in ("build", "emulator", "embedding"):
                self.assertEqual(plan[group], {"include": []})

    def test_java11_task_is_kept_when_the_target_version_has_real_tests(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ci = self.old_ref_fixture(root)
            task = root / "dev/devicelab/bin/tasks/android_java11_dependency_smoke_tests.dart"
            task.write_text("Future<void> main() async { await runDependencySmokeTests(); }\n")
            with mock.patch.dict("sys.modules", {"yaml": SimpleNamespace(safe_load=lambda _: ci)}):
                plan = android.source_plan(root, self.policy())
            self.assertIn("android_java11_dependency_smoke_tests",
                          {item["task"] for item in plan["build"]["include"]})

    def test_only_empty_main_and_unconditional_success_placeholders_are_noops(self):
        self.assertTrue(android.task_is_noop("Future<void> main() async {}"))
        self.assertTrue(android.task_is_noop("void main() {/* no cases */}"))
        self.assertFalse(android.task_is_noop("Future<void> main() async { await test(); }"))
        self.assertFalse(android.task_is_noop("Future<void> main() async { await task(() async { "
                                             "await build(); return TaskResult.success(null); }); }"))

    def test_old_host_build_uses_ref_sdk_target_and_const_finder(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            builders = root / "engine/src/flutter/ci/builders"
            builders.mkdir(parents=True)
            for filename, name, targets in (
                ("linux_android_debug_engine.json", "ci/android_debug_x64", ["flutter"]),
                ("linux_host_engine_test.json", "ci/host_debug_test",
                 ["flutter/build/dart:copy_dart_sdk", "flutter/shell/testing"]),
            ):
                (builders / filename).write_text(json.dumps({"builds": [{
                    "name": name, "gn": ["--target-dir", name], "ninja": {"targets": targets}}]}))
            const_finder = root / "engine/src/flutter/tools/const_finder/BUILD.gn"
            const_finder.parent.mkdir(parents=True)
            const_finder.touch()
            runner = android.Android(root, dry_run=True)
            with mock.patch.object(runner, "run") as run:
                runner.build_app("debug")
            targets = run.call_args_list[-1].args[0]
            self.assertIn("flutter/build/dart:copy_dart_sdk", targets)
            self.assertIn("flutter/tools/const_finder", targets)

    def test_command_failure_retains_exit_status_and_captured_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.dict(android.os.environ, {"FLUTTER_TEST_OUTPUTS_DIR": str(root / "results")}):
                runner = android.Android(root)
            with self.assertRaises(android.subprocess.CalledProcessError) as result:
                runner.run([sys.executable, "-c", "print('failure-detail'); raise SystemExit(7)"])
            self.assertEqual(result.exception.returncode, 7)
            self.assertIn("failure-detail", (root / "results/android-run.log").read_text())

    def test_java_entrypoint_does_not_inherit_a_conflicting_host_sdk_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            src = root / "engine/src"
            entrypoint = src / "flutter/testing/run_tests.py"
            entrypoint.parent.mkdir(parents=True)
            fixture = Path(__file__).parent / "fixtures/flutter_225e8f4_java_tests.py"
            entrypoint.write_text(fixture.read_text() + textwrap.dedent('''
                import subprocess
                import sys

                BUILDROOT_DIR = os.path.abspath(os.path.join(__file__, '..', '..', '..'))
                OUT_DIR = os.path.join(BUILDROOT_DIR, 'out')
                is_windows = lambda: False
                is_mac = lambda: False

                def run_cmd(command, cwd, env):
                    subprocess.run(command, cwd=cwd, env=env, check=True)

                run_java_tests(None, sys.argv[-1])
            '''))
            gradle_build = src / "flutter/shell/platform/android/test_runner/build.gradle"
            gradle_build.parent.mkdir(parents=True)
            original = 'jvmArgs "-Xmx8g"\nmaxParallelForks availableProcessors\n'
            gradle_build.write_text(original)
            gradle = src / "flutter/third_party/gradle/bin/gradle"
            gradle.parent.mkdir(parents=True)
            # Only the final Gradle process is fake. The real target-ref Java
            # entrypoint still chooses its SDK and JDK in a child process.
            gradle.write_text(f"#!{sys.executable}\n" + textwrap.dedent('''
                import json
                import os
                from pathlib import Path

                variables = ('ANDROID_HOME', 'ANDROID_SDK_ROOT', 'JAVA_HOME')
                env = {key: os.environ[key] for key in variables if key in os.environ}
                Path(os.environ['JAVA_TEST_CAPTURE']).write_text(json.dumps(env))
                sdk_root = os.environ.get('ANDROID_SDK_ROOT')
                if sdk_root and sdk_root != os.environ['ANDROID_HOME']:
                    raise SystemExit('Several environment variables and/or system properties '
                                     'contain different paths to the SDK.')
            '''))
            gradle.chmod(0o755)
            captured = root / "gradle-env.json"
            for sdk_root in ("/usr/local/lib/android/sdk", None):
                with self.subTest(host_sdk_root=sdk_root), mock.patch.dict(android.os.environ, {
                    "ANDROID_HOME": "/usr/local/lib/android/sdk",
                    "JAVA_HOME": "/host/jdk25",
                    "JAVA_TEST_CAPTURE": str(captured),
                    "FLUTTER_TEST_OUTPUTS_DIR": str(root / "results"),
                }):
                    if sdk_root:
                        android.os.environ["ANDROID_SDK_ROOT"] = sdk_root
                    else:
                        android.os.environ.pop("ANDROID_SDK_ROOT", None)
                    inherited = dict(android.os.environ)
                    android.Android(root).java(build=False)
                    env = json.loads(captured.read_text())
                    self.assertNotIn("ANDROID_SDK_ROOT", env)
                    self.assertEqual(env["ANDROID_HOME"], str(src / "flutter/third_party/android_tools/sdk"))
                    self.assertEqual(Path(env["JAVA_HOME"]).resolve(),
                                     src / "flutter/third_party/java/openjdk")
                    self.assertEqual(dict(android.os.environ), inherited)
                    self.assertEqual(gradle_build.read_text(), original)

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
            for task in android.BUILD_TASKS:
                (tasks / f"{task}.dart").touch()
            targets.append({"name": "Linux_android_emu android views", "properties": {
                "task_name": "android_views", "dependencies": [{"dependency": "goldctl", "version": "v1"}]}})
            (tasks / "android_views.dart").touch()
            ci = {"targets": targets, "platform_properties": {"linux_android_emu": {"properties": {
                "dependencies": [{"dependency": "android_virtual_device",
                                  "version": "android_36_google_apis_x64.textpb"},
                                 {"dependency": "open_jdk", "version": "version:25"}]}}}}
            with mock.patch.dict("sys.modules", {"yaml": SimpleNamespace(safe_load=lambda _: ci)}):
                plan = android.source_plan(root, self.policy())
                self.assertEqual(plan["emulator"]["include"][0]["api"], "36")
                self.assertEqual(plan["emulator"]["include"][0]["ndk"], "28.2.13676358")
                targets.append({"name": "Linux_android_emu newly_added", "properties": {"task_name": "newly_added"}})
                with self.assertRaisesRegex(ValueError, "New Android emulator task"):
                    android.source_plan(root, self.policy())


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
