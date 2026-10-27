import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location("ios_ci", Path(__file__).parents[1] / "ios.py")
ios = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ios)


def fixture_build(extension=False):
    variant = "ci/ios_debug_unopt_sim_arm64" + ("_extension_safe" if extension else "")
    return {
        "gn": ["--ios", "--simulator", "--unoptimized", "--simulator-cpu", "arm64",
               "--rbe", "--xcode-symlinks", "--runtime-mode", "debug",
               "--target-dir", variant] + (["--darwin-extension-safe"] if extension else []),
        "ninja": {"config": variant, "targets": ["flutter/testing/ios_scenario_app", "ios_test_flutter"]},
    }


def fixture_native_runners(root, *, legacy_embedding=False, legacy_shell=False):
    src = root / "engine/src"
    testing = src / "flutter/testing"
    scenario = testing / "ios_scenario_app"
    (scenario / "bin").mkdir(parents=True)
    # Legacy contracts copied from littleGnAl/flutter@225e8f4e687e34a0f5c21c3f56d90e934b5ced76.
    embedding = """def run_objc_tests(ios_variant='ios_debug_sim_unopt', test_filter=None):
  new_simulator_name = 'IosUnitTestsSimulator'
  create_simulator = [
      'xcrun '
      'simctl '
      'create '
      '%s com.apple.CoreSimulator.SimDeviceType.iPhone-11' % new_simulator_name
  ]
  run_cmd(create_simulator, shell=True)
""" if legacy_embedding else "parser.add_argument('--ios-runtime')\n"
    if legacy_shell:
        shell = (" " + chr(92) + "\n").join([
            '\"$DART\"',
            '  --disable-dart-dev',
            '  testing/ios_scenario_app/bin/run_ios_tests.dart',
            '  \"$@\"',
        ]) + "\n"
    else:
        shell = '\"$DART\" \"$SCRIPT_DIR/bin/run_ios_tests.dart\" \"$@\"\n'
    dart = "if (args.length > 1 || args.contains('-h')) {}\naddOption('os-runtime'); addOption('os-version');\n"
    (testing / "run_tests.py").write_text(embedding)
    (scenario / "run_ios_tests.sh").write_text(shell)
    (scenario / "bin/run_ios_tests.dart").write_text(dart)
    return embedding, shell, dart


def fixture_335_tasks(root):
    directory = root / "dev/devicelab/bin/tasks"
    directory.mkdir(parents=True)
    missing = {"ios_universal_link_test", "uiscene_plugin_lifecycle_test_ios", "hello_world_impeller_ios_sdfs"}
    simulator = ["native_assets_ios_simulator", "hot_mode_dev_cycle_ios_simulator"]
    tasks = [task for task in ios.SIMULATOR_TASKS if task not in missing]
    baseline = ["module_test_ios", "build_ios_framework_module_test", "plugin_test_ios"]
    (root / ".ci.yaml").write_text("\n".join("      task_name: " + task for task in [*tasks, *baseline]))
    for task in [*tasks, *baseline]:
        source = "testWithNewIOSSimulator('sim', callback);" if task in simulator else "await task(physicalDeviceTask());"
        (directory / (task + ".dart")).write_text(source)
    return simulator, baseline


class IOSCITests(unittest.TestCase):
    def test_local_flags_remove_remote_options_and_values(self):
        flags = ios.local_gn_flags([
            "--ios", "--rbe", "--rbe-server-address", "socket", "--rbe-platform=mac",
            "--xcode-symlinks", "--target-dir", "ci/ios_debug", "--no-lto",
        ])
        self.assertEqual(flags, ["--ios", "--target-dir", "ci/ios_debug", "--no-lto",
                                 "--no-rbe", "--no-goma"])

    def test_selects_normal_and_extension_build_independently(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            builder = root / "engine/src/flutter/ci/builders/mac_unopt.json"
            builder.parent.mkdir(parents=True)
            builder.write_text(json.dumps({"builds": [fixture_build(), fixture_build(True)]}))
            self.assertEqual(ios.find_build(root, "ios")["ninja"]["config"],
                             "ci/ios_debug_unopt_sim_arm64")
            self.assertTrue(ios.find_build(root, "ios", True)["ninja"]["config"].endswith("extension_safe"))

    def test_ambiguous_builder_fails_instead_of_testing_wrong_variant(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            builder = root / "engine/src/flutter/ci/builders/mac_unopt.json"
            builder.parent.mkdir(parents=True)
            builder.write_text(json.dumps({"builds": [fixture_build(), fixture_build()]}))
            with self.assertRaisesRegex(ValueError, "Expected one"):
                ios.find_build(root, "ios")

    def test_app_variants_avoid_nested_xcode_engine_root(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(ios, "run") as run:
            root = Path(temporary)
            variant = ios.build_engine(root, fixture_build(), root / "results", 2, app_artifacts=True)
            self.assertEqual(variant, "ios_debug_unopt_sim_arm64")
            self.assertIn("ios_debug_unopt_sim_arm64", run.call_args_list[0].args[0])
            self.assertNotIn("ci/ios_debug_unopt_sim_arm64", run.call_args_list[0].args[0])
            self.assertIn("default", run.call_args_list[1].args[0])

    def test_runtime_chooses_latest_available_compatible_ios(self):
        device = "com.apple.CoreSimulator.SimDeviceType.iPhone-SE-3rd-generation"
        def runtime(version, available=True, compatible=True):
            return {"identifier": "com.apple.CoreSimulator.SimRuntime.iOS-" + version.replace(".", "-"),
                    "version": version, "isAvailable": available,
                    "supportedDeviceTypes": [{"identifier": device if compatible else "another-device"}]}
        selected = ios.select_runtime({"runtimes": [runtime("26.2"), runtime("26.10"),
                                                          runtime("27.0", False), runtime("28.0", True, False)]})
        self.assertEqual(selected["version"], "26.10")

    def test_no_compatible_runtime_is_explicit_failure(self):
        with self.assertRaisesRegex(ValueError, "No available"):
            ios.select_runtime({"runtimes": []})

    def test_scenario_patch_changes_only_obsolete_argument_guard(self):
        old = "if (args.length > 1 || args.contains('-h')) {\n  exit(1);\n}\nassert(testPassed);\n"
        new = ios.patch_scenario_text(old)
        self.assertEqual(new, "if (args.contains('-h')) {\n  exit(1);\n}\nassert(testPassed);\n")
        self.assertEqual(ios.patch_scenario_text(new), new)

    def test_embedding_failure_still_runs_scenario_and_restores_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            results = root / "results"
            results.mkdir()
            _, _, original = fixture_native_runners(root)
            runner = root / "engine/src/flutter/testing/ios_scenario_app/bin/run_ios_tests.dart"
            runtime = {"identifier": "com.apple.CoreSimulator.SimRuntime.iOS-26-2",
                       "isAvailable": True, "version": "26.2"}
            with mock.patch.object(ios, "find_build", return_value=fixture_build()), \
                    mock.patch.object(ios, "build_engine", return_value="ci/ios_debug_unopt_sim_arm64"), \
                    mock.patch.object(ios.subprocess, "check_output", return_value=json.dumps({"runtimes": [runtime]})), \
                    mock.patch.object(ios, "run", side_effect=[7, 0]) as run:
                self.assertEqual(ios.run_native(root, results, 2, False), 1)
            self.assertEqual(run.call_count, 2)
            self.assertIn("--ios-runtime", run.call_args_list[0].args[0])
            self.assertIn("--os-runtime=" + runtime["identifier"], run.call_args_list[1].args[0])
            self.assertEqual(run.call_args_list[1].kwargs["cwd"], root / "engine/src")
            self.assertFalse((results / "embedding-runner.patch").exists())
            self.assertEqual(runner.read_text(), original)
            self.assertTrue((results / "scenario-runner.patch").is_file())
            self.assertEqual(json.loads((results / "results.json").read_text()),
                             {"embedding": 7, "scenario": 0})

    def test_legacy_embedding_selects_runtime_without_unsupported_cli_flag(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            results = root / "results"
            results.mkdir()
            original, _, _ = fixture_native_runners(root, legacy_embedding=True)
            runner = root / "engine/src/flutter/testing/run_tests.py"
            runtime = {"identifier": "com.apple.CoreSimulator.SimRuntime.iOS-26-2",
                       "isAvailable": True, "version": "26.2"}

            def execute(command, **kwargs):
                if "flutter/testing/run_tests.py" in command:
                    self.assertNotIn("--ios-runtime", command)
                    patched = runner.read_text()
                    self.assertIn("iPhone-11 " + runtime["identifier"], patched)
                    compile(patched, str(runner), "exec")
                    return 7
                return 0

            with mock.patch.object(ios, "find_build", return_value=fixture_build()), \
                    mock.patch.object(ios, "build_engine", return_value="ci/ios_debug_unopt_sim_arm64"), \
                    mock.patch.object(ios.subprocess, "check_output", return_value=json.dumps({"runtimes": [runtime]})), \
                    mock.patch.object(ios, "run", side_effect=execute):
                self.assertEqual(ios.run_native(root, results, 2, False), 1)
            self.assertEqual(runner.read_text(), original)
            self.assertTrue((results / "embedding-runner.patch").is_file())

    def test_legacy_scenario_shell_runs_from_flutter_source_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            results = root / "results"
            results.mkdir()
            _, original_shell, original_dart = fixture_native_runners(root, legacy_shell=True)
            src = root / "engine/src"
            runtime = {"identifier": "com.apple.CoreSimulator.SimRuntime.iOS-18-2",
                       "isAvailable": True, "version": "18.2"}

            def execute(command, **kwargs):
                if command[0].endswith("run_ios_tests.sh"):
                    self.assertEqual(kwargs["cwd"], src / "flutter")
                    self.assertTrue((kwargs["cwd"] / "testing/ios_scenario_app/bin/run_ios_tests.dart").is_file())
                    self.assertTrue(Path(command[0]).is_file())
                return 0

            with mock.patch.object(ios, "find_build", return_value=fixture_build()), \
                    mock.patch.object(ios, "build_engine", return_value="ci/ios_debug_unopt_sim_arm64"), \
                    mock.patch.object(ios.subprocess, "check_output", return_value=json.dumps({"runtimes": [runtime]})), \
                    mock.patch.object(ios, "run", side_effect=execute):
                self.assertEqual(ios.run_native(root, results, 2, False), 0)
            self.assertEqual((src / "flutter/testing/ios_scenario_app/run_ios_tests.sh").read_text(), original_shell)
            self.assertEqual((src / "flutter/testing/ios_scenario_app/bin/run_ios_tests.dart").read_text(), original_dart)

    def test_unknown_legacy_simulator_contract_fails_before_patching(self):
        with self.assertRaisesRegex(ValueError, "unsupported simulator creation"):
            ios.embedding_runtime_options("def run_objc_tests(): pass\n",
                                          "com.apple.CoreSimulator.SimRuntime.iOS-18-2")
        with self.assertRaisesRegex(ValueError, "Invalid iOS simulator runtime"):
            ios.embedding_runtime_options("legacy runner", "runtime; arbitrary shell command")

    def test_missing_required_task_does_not_silently_reduce_coverage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".ci.yaml").write_text("targets: []\n")
            with self.assertRaisesRegex(ValueError, "Required core simulator task"):
                ios.simulator_tasks(root)

    def test_old_ref_runs_existing_simulator_tasks_and_reports_unavailable_features(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            simulator, _ = fixture_335_tasks(root)
            self.assertEqual(ios.simulator_tasks(root), simulator)
            unavailable = []
            self.assertEqual(ios.simulator_tasks(root, unavailable=unavailable), simulator)
            reasons = {item["task"]: item["reason"] for item in unavailable}
            self.assertEqual(reasons["uiscene_plugin_lifecycle_test_ios"], "task_file_absent")
            self.assertEqual(reasons["channels_integration_test_ios"], "requires_physical_device_in_this_ref")

    def test_old_ref_plan_emits_existing_baseline_matrix_and_coverage_summary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            simulator, baseline = fixture_335_tasks(root)
            results, output, summary = root / "results", root / "outputs", root / "summary"
            environment = {**os.environ, "GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(summary)}
            result = subprocess.run([
                sys.executable, str(Path(ios.__file__)), "plan", "--flutter-root", str(root),
                "--results", str(results),
            ], env=environment, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            values = dict(line.split("=", 1) for line in output.read_text().splitlines())
            self.assertEqual(json.loads(values["baseline"]), {"task": baseline})
            coverage = json.loads((results / "ios-plan.json").read_text())
            self.assertEqual(coverage["simulator_tasks"], simulator)
            self.assertIn({"task": "plugin_test_darwin", "reason": "task_file_absent"}, coverage["unavailable"])
            self.assertIn("plugin_test_darwin", summary.read_text())
            self.assertIn("unavailable", summary.read_text())

    def test_missing_core_task_still_fails_when_other_simulator_tasks_exist(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture_335_tasks(root)
            (root / "dev/devicelab/bin/tasks/native_assets_ios_simulator.dart").unlink()
            with self.assertRaisesRegex(ValueError, "Required core simulator task.*native_assets_ios_simulator"):
                ios.simulator_tasks(root)

    def test_absent_ci_declaration_is_reported_and_empty_baseline_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture_335_tasks(root)
            ci = root / ".ci.yaml"
            ci.write_text(ci.read_text().replace("      task_name: channels_integration_test_ios", ""))
            unavailable = []
            ios.simulator_tasks(root, unavailable=unavailable)
            self.assertIn({"task": "channels_integration_test_ios", "reason": "not_configured_in_ci"}, unavailable)
            ci.write_text("targets: []\n")
            with self.assertRaisesRegex(ValueError, "No configured baseline"):
                ios.baseline_tasks(root)

    def test_devicelab_uses_local_host_and_target_and_continues_after_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            results = root / "results"
            results.mkdir()
            binary = root / "bin/flutter"
            binary.parent.mkdir()
            binary.write_text("#!/bin/sh\necho original\n")
            with mock.patch.object(ios, "simulator_tasks", return_value=["task_a", "task_b"]), \
                    mock.patch.object(ios, "find_build"), \
                    mock.patch.object(ios, "build_engine", side_effect=["ios_local_sim_arm64", "host_local_arm64"]), \
                    mock.patch.object(ios, "run", side_effect=[0, 9, 0, 0, 0]) as run:
                self.assertEqual(ios.run_devicelab(root, results, 2), 1)
            for call in run.call_args_list[1:3]:
                command = call.args[0]
                self.assertIn("--local-engine", command)
                self.assertIn("ios_local_sim_arm64", command)
                self.assertIn("--local-engine-host", command)
                self.assertIn("host_local_arm64", command)
            statuses = json.loads((results / "results.json").read_text())
            self.assertEqual(statuses["task_a"], 9)
            self.assertEqual(statuses["task_b"], 0)
            self.assertEqual(binary.read_text(), "#!/bin/sh\necho original\n")
            self.assertTrue((results / "source-flutter-launcher.patch").is_file())

    def test_source_guard_checks_actual_mode_and_engine_selection(self):
        config = {"root": "/checkout/flutter", "ios": "ios_debug_sim_arm64", "host": "host_debug_arm64"}
        flags = ["--local-engine-src-path=/checkout/flutter/engine/src",
                 "--local-engine=ios_debug_sim_arm64", "--local-engine-host=host_debug_arm64"]
        self.assertEqual(ios.validate_source_command([*flags, "build", "ios", "--simulator"], config, {}), "debug")
        with self.assertRaisesRegex(ValueError, "requested profile"):
            ios.validate_source_command([*flags, "run", "--profile"], config, {})
        with self.assertRaisesRegex(ValueError, "requested release"):
            ios.validate_source_command([*flags, "build", "ios"], config, {})
        with self.assertRaisesRegex(ValueError, "did not select"):
            ios.validate_source_command(["run", "--debug"], config, {})

    def test_source_guard_checks_xcode_assemble_and_allows_config_only(self):
        config = {"root": "/checkout/flutter", "ios": "ios_debug_sim_arm64", "host": "host_debug_arm64"}
        flags = ["--local-engine-src-path", "/checkout/flutter/engine/src",
                 "--local-engine", "ios_debug_sim_arm64", "--local-engine-host", "host_debug_arm64"]
        self.assertIsNone(ios.validate_source_command([*flags, "build", "ios", "--config-only"], config, {}))
        self.assertEqual(ios.validate_source_command([*flags, "assemble", "-dBuildMode=debug"], config,
                                                    {"SDK_NAME": "iphonesimulator26.2"}), "debug")
        with self.assertRaisesRegex(ValueError, "physical"):
            ios.validate_source_command([*flags, "assemble", "-dBuildMode=debug"], config,
                                        {"SDK_NAME": "iphoneos26.2"})
        with self.assertRaisesRegex(ValueError, "requested release"):
            ios.validate_source_command([*flags, "assemble", "-dBuildMode=release"], config, {})

    def test_multimode_compatibility_does_not_inject_debug_local_engine(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            results = root / "results"
            results.mkdir()
            (root / ".ci.yaml").write_text("      task_name: module_test_ios\n")
            source = root / "dev/devicelab/bin/tasks/module_test_ios.dart"
            source.parent.mkdir(parents=True)
            source.write_text("await task(moduleTest);\n")
            with mock.patch.object(ios, "build_engine") as build, \
                    mock.patch.object(ios, "run", return_value=0) as run:
                self.assertEqual(ios.run_devicelab(root, results, 2, "module_test_ios"), 0)
            build.assert_not_called()
            self.assertNotIn("--local-engine", run.call_args.args[0])
            self.assertEqual(json.loads((results / "coverage.json").read_text())["engine_origin"],
                             "selected-framework-and-tools-with-baseline-engine")

    def test_new_official_simulator_task_requires_mode_review(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "dev/devicelab/bin/tasks"
            directory.mkdir(parents=True)
            tasks = [*ios.SIMULATOR_TASKS, "new_simulator_test", "module_test_ios"]
            (root / ".ci.yaml").write_text("\n".join("      task_name: " + task for task in tasks))
            for task in tasks:
                (directory / (task + ".dart")).write_text("testWithNewIOSSimulator('sim', callback);")
            with self.assertRaisesRegex(ValueError, "build-mode review"):
                ios.simulator_tasks(root)
            (directory / "new_simulator_test.dart").unlink()
            self.assertNotIn("module_test_ios", ios.simulator_tasks(root))


if __name__ == "__main__":
    unittest.main()
