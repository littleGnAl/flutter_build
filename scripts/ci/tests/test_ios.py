import importlib.util
import json
from pathlib import Path
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
            runner = root / "engine/src/flutter/testing/ios_scenario_app/bin/run_ios_tests.dart"
            runner.parent.mkdir(parents=True)
            original = "if (args.length > 1 || args.contains('-h')) {}\naddOption('os-runtime'); addOption('os-version');\n"
            runner.write_text(original)
            runtime = {"identifier": "com.apple.CoreSimulator.SimRuntime.iOS-26-2",
                       "isAvailable": True, "version": "26.2"}
            with mock.patch.object(ios, "find_build", return_value=fixture_build()), \
                    mock.patch.object(ios, "build_engine", return_value="ci/ios_debug_unopt_sim_arm64"), \
                    mock.patch.object(ios.subprocess, "check_output", return_value=json.dumps({"runtimes": [runtime]})), \
                    mock.patch.object(ios, "run", side_effect=[7, 0]) as run:
                self.assertEqual(ios.run_native(root, results, 2, False), 1)
            self.assertEqual(run.call_count, 2)
            self.assertIn("--os-runtime=" + runtime["identifier"], run.call_args_list[1].args[0])
            self.assertEqual(runner.read_text(), original)
            self.assertTrue((results / "scenario-runner.patch").is_file())
            self.assertEqual(json.loads((results / "results.json").read_text()),
                             {"embedding": 7, "scenario": 0})

    def test_missing_required_task_does_not_silently_reduce_coverage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".ci.yaml").write_text("targets: []\n")
            with self.assertRaisesRegex(ValueError, "absent"):
                ios.simulator_tasks(root)

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
