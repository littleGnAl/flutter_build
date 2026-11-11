import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).parents[1]))

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
        "tests": [
            {"script": "flutter/testing/run_tests.py", "parameters": ["--variant", variant, "--type", "objc", "--ios-variant", variant]},
            {"script": "flutter/testing/ios_scenario_app/run_ios_tests.sh", "parameters": [variant]},
        ],
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


def native_policy():
    policy = mock.Mock(branch="master")
    policy.engine_enabled.return_value = True
    return policy


def fixture_policy(root, branch="flutter-3.35-candidate.0", removed=()):
    targets = json.loads((root / ".ci.yaml").read_text())["targets"]
    return ios.Policy(branch=branch, root_targets={t["name"] for t in targets} - set(removed),
                      engine_targets={"Mac mac_unopt"}, source_sha="", official_sha="", default_sha="")


def fixture_335_tasks(root):
    directory = root / "dev/devicelab/bin/tasks"
    directory.mkdir(parents=True)
    missing = {"ios_universal_link_test", "uiscene_plugin_lifecycle_test_ios", "hello_world_impeller_ios_sdfs",
               "module_uiscene_test_ios"}
    simulator = ["native_assets_ios_simulator", "hot_mode_dev_cycle_ios_simulator"]
    tasks = [task for task in ios.SIMULATOR_TASKS if task not in missing]
    baseline = ["module_test_ios", "build_ios_framework_module_test", "plugin_test_ios"]
    targets = []
    for task in [*tasks, *baseline]:
        prefix = "Mac_x64" if task == "hot_mode_dev_cycle_ios_simulator" else "Mac_arm64"
        targets.append({"name": prefix + " " + task, "properties": {"task_name": task}})
        source = "testWithNewIOSSimulator('sim', callback);" if task in simulator else "await task(physicalDeviceTask());"
        (directory / (task + ".dart")).write_text(source)
    configuration = {
        "enabled_branches": ["master", "flutter-.*-candidate.*"],
        "platform_properties": {"mac_x64": {"properties": {"cpu": "x86"}},
                                "mac_arm64": {"properties": {"cpu": "arm64"}}},
        "targets": targets,
    }
    (root / ".ci.yaml").write_text(json.dumps(configuration))
    engine = root / "engine/src/flutter"
    (engine / "ci/builders").mkdir(parents=True)
    (engine / "ci/builders/mac_unopt.json").write_text(json.dumps({"builds": [fixture_build(), fixture_build(True)]}))
    (engine / ".ci.yaml").write_text(json.dumps({"enabled_branches": configuration["enabled_branches"],
        "targets": [{"name": "Mac mac_unopt", "properties": {"config_name": "mac_unopt"}}]}))
    return simulator, baseline


def fixture_module_uiscene_task(root):
    # Official target and simulator contract at flutter/flutter@bc9b0371.
    task = "module_uiscene_test_ios"
    path = root / ".ci.yaml"
    configuration = json.loads(path.read_text())
    configuration["targets"].append({
        "name": "Mac " + task,
        "recipe": "devicelab/devicelab_drone",
        "properties": {"task_name": task},
    })
    path.write_text(json.dumps(configuration))
    (root / "dev/devicelab/bin/tasks" / (task + ".dart")).write_text("""
await testWithNewIOSSimulator(
  'TestAdd2AppSim',
  deviceTypeId: 'com.apple.CoreSimulator.SimDeviceType.iPad-Pro-11-inch-3rd-generation',
  (String deviceId) async { await testNativeApp(deviceId); },
);
""")
    return task


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
                self.assertEqual(ios.run_native(root, results, 2, False, policy=native_policy()), 1)
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
                self.assertEqual(ios.run_native(root, results, 2, False, policy=native_policy()), 1)
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
                self.assertEqual(ios.run_native(root, results, 2, False, policy=native_policy()), 0)
            self.assertEqual((src / "flutter/testing/ios_scenario_app/run_ios_tests.sh").read_text(), original_shell)
            self.assertEqual((src / "flutter/testing/ios_scenario_app/bin/run_ios_tests.dart").read_text(), original_dart)

    def test_unknown_legacy_simulator_contract_fails_before_patching(self):
        with self.assertRaisesRegex(ValueError, "unsupported simulator creation"):
            ios.embedding_runtime_options("def run_objc_tests(): pass\n",
                                          "com.apple.CoreSimulator.SimRuntime.iOS-18-2")
        with self.assertRaisesRegex(ValueError, "Invalid iOS simulator runtime"):
            ios.embedding_runtime_options("legacy runner", "runtime; arbitrary shell command")

    def test_old_ref_preserves_only_available_official_tasks_and_cpu(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            simulator, baseline = fixture_335_tasks(root)
            policy = fixture_policy(root)
            unavailable = []
            self.assertEqual(ios.simulator_tasks(root, unavailable=unavailable, policy=policy), simulator)
            self.assertEqual(ios.baseline_tasks(root, policy=policy), baseline)
            entries = ios.task_entries(root, ios.SIMULATOR_TASKS, simulator=True, policy=policy)
            runners = {entry["task"]: entry["runner"] for entry in entries}
            self.assertEqual(runners["native_assets_ios_simulator"], "macos-15")
            self.assertEqual(runners["hot_mode_dev_cycle_ios_simulator"], "macos-15-intel")
            reasons = {item["task"]: item["reason"] for item in unavailable}
            self.assertEqual(reasons["uiscene_plugin_lifecycle_test_ios"], "task_file_absent")
            self.assertEqual(reasons["channels_integration_test_ios"], "requires_physical_device_in_this_ref")

    def test_plan_emits_cpu_matrices_and_empty_job_guards(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            simulator, baseline = fixture_335_tasks(root)
            policy = fixture_policy(root)
            results, output, summary = root / "results", root / "outputs", root / "summary"
            argv = [str(Path(ios.__file__)), "plan", "--flutter-root", str(root), "--results", str(results)]
            with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(summary)}), \
                    mock.patch.object(ios.Policy, "load", return_value=policy), mock.patch.object(sys, "argv", argv):
                self.assertEqual(ios.main(), 0)
            values = dict(line.split("=", 1) for line in output.read_text().splitlines())
            self.assertEqual([entry["task"] for entry in json.loads(values["baseline"])["include"]], baseline)
            self.assertEqual([entry["task"] for entry in json.loads(values["simulator"])["include"]], simulator)
            self.assertEqual(values["has-simulator"], "true")
            self.assertEqual(values["has-native"], "true")
            self.assertIn("plugin_test_darwin", summary.read_text())
            empty = ios.Policy(branch="master", root_targets=set(), engine_targets=set(),
                               source_sha="", official_sha="", default_sha="")
            with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}):
                ios.plan_tasks(root, results, policy=empty)
            values = dict(line.split("=", 1) for line in output.read_text().splitlines())
            for name in ("simulator", "baseline", "native"):
                self.assertEqual(values["has-" + name], "false")
                self.assertEqual(json.loads(values[name]), {"include": []})

    def test_release_policy_removes_bringup_and_retired_tasks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture_335_tasks(root)
            path = root / ".ci.yaml"
            config = json.loads(path.read_text())
            name = "Mac_x64 hot_mode_dev_cycle_ios_simulator"
            next(target for target in config["targets"] if target["name"] == name)["bringup"] = True
            path.write_text(json.dumps(config))
            removed = "Mac_arm64 module_test_ios"
            policy = fixture_policy(root, removed={removed})
            self.assertEqual(ios.simulator_tasks(root, policy=policy), ["native_assets_ios_simulator"])
            self.assertNotIn("module_test_ios", ios.baseline_tasks(root, policy=policy))
            self.assertIn("hot_mode_dev_cycle_ios_simulator", ios.simulator_tasks(root, policy=fixture_policy(root, branch="master")))

    def test_unscheduled_new_simulator_source_does_not_add_a_case(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture_335_tasks(root)
            task = root / "dev/devicelab/bin/tasks/new_simulator_test.dart"
            task.write_text("testWithNewIOSSimulator('sim', callback);")
            policy = fixture_policy(root)
            self.assertNotIn("new_simulator_test", ios.simulator_tasks(root, policy=policy))
            path = root / ".ci.yaml"
            config = json.loads(path.read_text())
            config["targets"].append({"name": "Mac_arm64 new_simulator_test", "properties": {"task_name": "new_simulator_test"}})
            path.write_text(json.dumps(config))
            with self.assertRaisesRegex(ValueError, "build-mode review"):
                ios.simulator_tasks(root, policy=fixture_policy(root))

    def test_module_uiscene_plans_and_runs_only_when_officially_scheduled(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            fixture_335_tasks(root)
            task = fixture_module_uiscene_task(root)
            policy = fixture_policy(root, branch="master")
            results = root / "results"
            plan = ios.plan_tasks(root, results, policy=policy)
            entries = [entry for entry in plan["simulator"]["include"] if entry["task"] == task]
            self.assertEqual(entries, [{"task": task, "source_target": "Mac " + task,
                                       "runner": "macos-15", "cpu": "arm64", "id": "Mac-" + task}])
            self.assertNotIn(task, [entry["task"] for entry in plan["baseline"]["include"]])
            argv = [str(Path(ios.__file__)), "simulator", "--flutter-root", str(root),
                    "--results", str(results), "--task", task, "--source-target", "Mac " + task]
            with mock.patch.dict(os.environ), \
                    mock.patch.object(ios.sys, "platform", "darwin"), \
                    mock.patch.object(ios.platform, "machine", return_value="arm64"), \
                    mock.patch.object(ios.Policy, "load", return_value=policy), \
                    mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(ios, "run", side_effect=[0, 0, 9]) as run:
                self.assertEqual(ios.main(), 1)
            self.assertEqual(run.call_count, 3)
            self.assertEqual(run.call_args.args[0], [root / "bin/cache/dart-sdk/bin/dart",
                "bin/test_runner.dart", "test", "--exit", "--task", task,
                "--results-file", str(results / (task + "-devicelab.json"))])
            unscheduled = fixture_policy(root, branch="master", removed={"Mac " + task})
            self.assertNotIn(task, ios.simulator_tasks(root, policy=unscheduled))

    def test_native_scope_respects_builder_and_per_test_branch_filters(self):
        build = fixture_build()
        build["tests"][1]["test_if"] = "master$"
        self.assertEqual(set(ios.native_suites(build, mock.Mock(branch="flutter-3.35-candidate.0"))), {"embedding"})
        self.assertEqual(set(ios.native_suites(build, native_policy())), {"embedding", "scenario"})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            policy = native_policy()
            policy.engine_enabled.return_value = False
            self.assertEqual(ios.native_matrix(root, policy), {"include": []})
            with mock.patch.object(ios, "build_engine") as build:
                with self.assertRaisesRegex(ValueError, "does not schedule"):
                    ios.run_native(root, root, 2, False, policy=policy)
                build.assert_not_called()

    def test_simulator_executes_only_selected_official_task_without_extra_smoke(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture_335_tasks(root)
            results = root / "results"
            results.mkdir()
            task = "hot_mode_dev_cycle_ios_simulator"
            with mock.patch.object(ios.sys, "platform", "darwin"), \
                    mock.patch.object(ios.platform, "machine", return_value="x86_64"), \
                    mock.patch.object(ios, "build_engine") as build, \
                    mock.patch.object(ios, "run", side_effect=[0, 0, 9]) as run:
                self.assertEqual(ios.run_devicelab(root, results, task, "Mac_x64 " + task,
                                                 simulator=True, policy=fixture_policy(root)), 1)
            build.assert_not_called()
            self.assertEqual(run.call_count, 3)
            command = run.call_args.args[0]
            self.assertIn(task, command)
            self.assertNotIn("--local-engine", command)
            self.assertEqual(json.loads((results / "results.json").read_text()), {task: 9})
            self.assertEqual(json.loads((results / "coverage.json").read_text())["cpu"], "x64")
            self.assertFalse((root / "bin/flutter.ci-ios-original").exists())

    def test_task_execution_rejects_wrong_cpu_and_unscheduled_target_before_commands(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture_335_tasks(root)
            task = "hot_mode_dev_cycle_ios_simulator"
            with mock.patch.object(ios.sys, "platform", "darwin"), \
                    mock.patch.object(ios.platform, "machine", return_value="arm64"), \
                    mock.patch.object(ios, "run") as run:
                with self.assertRaisesRegex(ValueError, "requires macOS/x86_64"):
                    ios.run_devicelab(root, root, task, "Mac_x64 " + task, simulator=True, policy=fixture_policy(root))
                with self.assertRaisesRegex(ValueError, "not scheduled"):
                    ios.run_devicelab(root, root, task, "Mac_arm64 " + task, simulator=True, policy=fixture_policy(root))
                run.assert_not_called()

    def test_baseline_preserves_upstream_modes_and_has_no_local_engine(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture_335_tasks(root)
            results = root / "results"
            results.mkdir()
            with mock.patch.object(ios.sys, "platform", "darwin"), \
                    mock.patch.object(ios.platform, "machine", return_value="arm64"), \
                    mock.patch.object(ios, "build_engine") as build, \
                    mock.patch.object(ios, "run", return_value=0) as run:
                self.assertEqual(ios.run_devicelab(root, results, "module_test_ios", "Mac_arm64 module_test_ios",
                                                 policy=fixture_policy(root)), 0)
            build.assert_not_called()
            self.assertNotIn("--local-engine", run.call_args.args[0])
            self.assertEqual(json.loads((results / "coverage.json").read_text())["engine_origin"],
                             "selected-framework-and-tools-with-baseline-engine")


if __name__ == "__main__":
    unittest.main()
