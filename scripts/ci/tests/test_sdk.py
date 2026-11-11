import importlib.util
import io
import json
import os
from pathlib import Path
from contextlib import redirect_stderr, redirect_stdout
import sys
import tempfile
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location("sdk", Path(__file__).parents[1] / "sdk.py")
sdk = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sdk)


def target(name, shard, subshard=""):
    return {"name": name, "properties": {"shard": shard, "subshard": subshard}}


def configuration():
    return {"targets": [target(f"Linux {shard}", shard) for shard in sorted(sdk.ANDROID_TOOL_SHARDS)]}


class MatrixTests(unittest.TestCase):
    def test_only_android_tool_suites_are_selected(self):
        config = configuration()
        for prefix in ("Linux", "Mac", "Mac_arm64", "Mac_x64", "Windows"):
            for shard in ("framework_tests", "tool_tests", "tool_tests_commands",
                          "tool_integration_tests", "web_tool_tests", "build_tests", "analyze"):
                config["targets"].append(target(f"{prefix} {shard}", shard, "1_3"))
        for prefix in ("Mac", "Windows"):
            for shard in sdk.ANDROID_TOOL_SHARDS:
                config["targets"].append(target(f"{prefix} {shard}", shard))
        entries = sdk.ci_matrix(config)["include"]
        self.assertEqual({entry["shard"] for entry in entries}, sdk.ANDROID_TOOL_SHARDS)
        self.assertEqual(len(entries), len(sdk.ANDROID_TOOL_SHARDS))
        self.assertTrue(all(entry["source_target"].startswith("Linux ") for entry in entries))
        self.assertTrue(all(entry["runner"] == "ubuntu-22.04" for entry in entries))

    def test_jdk_comes_from_the_requested_ci_target(self):
        for serialized in (False, True):
            with self.subTest(serialized=serialized):
                config = configuration()
                dependencies = [{"dependency": "open_jdk", "version": "version:21"}]
                config["targets"][0]["properties"]["dependencies"] = json.dumps(dependencies) if serialized else dependencies
                entries = sdk.ci_matrix(config)["include"]
                entry = next(entry for entry in entries if entry["source_target"] == config["targets"][0]["name"])
                self.assertEqual(entry["jdk"], "21")

    def test_unsupported_jdk_format_fails(self):
        config = configuration()
        config["targets"][0]["properties"]["dependencies"] = [{"dependency": "open_jdk", "version": "unknown:21"}]
        with self.assertRaisesRegex(ValueError, "Unsupported JDK dependency"):
            sdk.ci_matrix(config)

    def test_yaml_preserves_unquoted_numeric_partition(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ci.yaml"
            path.write_text("targets:\n  - name: Linux Android preview\n    properties:\n      shard: android_preview_tool_integration_tests\n      subshard: 1_1\n")
            actual = sdk.read_configuration(path)
            self.assertEqual(actual["targets"][0]["properties"]["subshard"], "1_1")
            self.assertEqual(sdk.ci_matrix(actual)["include"][0]["subshard"], "1_1")

    def test_all_numeric_partitions_follow_the_requested_ref(self):
        for total in (3, 5):
            with self.subTest(total=total):
                config = {"targets": [target(f"Linux Android {index}", "android_preview_tool_integration_tests", f"{index}_{total}")
                                      for index in range(1, total + 1)]}
                entries = sdk.ci_matrix(config)["include"]
                self.assertEqual({entry["subshard"] for entry in entries}, {f"{index}_{total}" for index in range(1, total + 1)})
                self.assertEqual(len(entries), total)

    def test_incomplete_or_conflicting_partition_fails(self):
        for subshards in (("1_3", "3_3"), ("1_2", "2_3"), ("0_2", "1_2")):
            with self.subTest(subshards=subshards):
                config = {"targets": [target(f"Linux Android {part}", "android_preview_tool_integration_tests", part)
                                      for part in subshards]}
                with self.assertRaisesRegex(ValueError, "Incomplete or conflicting"):
                    sdk.ci_matrix(config)

    def test_legacy_java11_entry_is_retained_and_missing_modern_suites_are_reported(self):
        # Flutter 3.35.7 schedules Java 11 rather than the newer Java 17 shard.
        config = {"targets": [target("Linux Android Java 11", "android_java11_tool_integration_tests")]}
        matrix = sdk.ci_matrix(config)
        self.assertEqual([entry["shard"] for entry in matrix["include"]], ["android_java11_tool_integration_tests"])
        diagnostics = "\n".join(sdk.plan_diagnostics(matrix))
        self.assertIn("android_java17_tool_integration_tests: not configured", diagnostics)
        self.assertIn("android_java11_tool_integration_tests instead", diagnostics)
        self.assertNotIn("tool_tests_commands", diagnostics)

    def test_ref_without_android_tool_targets_has_an_empty_plan(self):
        config = {"targets": [target("Linux widgets", "framework_tests", "widgets")]}
        matrix = sdk.ci_matrix(config)
        self.assertEqual(matrix, {"include": []})
        self.assertEqual(len(sdk.plan_diagnostics(matrix)), len(sdk.ANDROID_TOOL_SHARDS))

    def test_plan_cli_needs_no_framework_source_and_reports_if_tests_exist(self):
        for targets, has_tests in (([], "false"), ([target("Linux Java 11", "android_java11_tool_integration_tests")], "true")):
            with self.subTest(has_tests=has_tests), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / ".ci.yaml").write_text(json.dumps({"targets": targets}))
                summary = root / "step-summary.md"
                stdout, stderr = io.StringIO(), io.StringIO()
                with mock.patch.object(sys, "argv", ["sdk.py", "--flutter-root", str(root), "plan"]), \
                        mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}), \
                        redirect_stdout(stdout), redirect_stderr(stderr):
                    self.assertEqual(sdk.main(), 0)
                outputs = dict(line.split("=", 1) for line in stdout.getvalue().splitlines())
                self.assertEqual(outputs["has-tests"], has_tests)
                self.assertEqual(len(json.loads(outputs["matrix"])["include"]), len(targets))
                self.assertIn("Android tool plan:", summary.read_text())
                self.assertIn("android_java17_tool_integration_tests: not configured", summary.read_text())
                self.assertIn("::notice::", stderr.getvalue())


class SdkEnvironmentTests(unittest.TestCase):
    def preview_environment(self, subshard):
        with tempfile.TemporaryDirectory() as directory:
            runner = mock.Mock(root=Path(directory) / "flutter", outputs=Path(directory))
            with mock.patch.dict(os.environ, {
                "FLUTTER_PREBUILT_ENGINE_VERSION": "published-engine",
                "JAVA_HOME": "/jdk",
                "SUBSHARD": "inherited-partition",
            }, clear=True):
                sdk.run_sdk(runner, "android_preview_tool_integration_tests", subshard)
            return runner.run.call_args.kwargs["env"]

    def test_unpartitioned_preview_omits_subshard_to_run_all_target_tests(self):
        # The target's selectIndexOfTotalSubshard returns all tests when this
        # variable is absent, but rejects an empty string or inherited value.
        env = self.preview_environment("")
        self.assertNotIn("SUBSHARD", env)
        self.assertEqual(env["SHARD"], "android_preview_tool_integration_tests")

    def test_explicit_preview_partitions_are_preserved(self):
        for subshard in ("1_3", "2_3", "3_3"):
            with self.subTest(subshard=subshard):
                self.assertEqual(self.preview_environment(subshard)["SUBSHARD"], subshard)

    def test_android_setup_does_not_enable_or_precache_other_platforms(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = mock.Mock(root=Path(directory) / "flutter", outputs=Path(directory))
            with mock.patch.dict(os.environ, {
                "FLUTTER_PREBUILT_ENGINE_VERSION": "published-engine",
                "JAVA_HOME": "/jdk",
            }, clear=True):
                sdk.run_sdk(runner, "android_java11_tool_integration_tests", "")
            commands = [call.args[0] for call in runner.run.call_args_list]
            self.assertEqual([command[1:] for command in commands[:-1]], [
                ["config", "--jdk-dir=/jdk"], ["precache", "--android"], ["update-packages"],
            ])
            self.assertEqual(commands[-1][1:], ["--enable-asserts", "dev/bots/test.dart"])
            self.assertEqual(runner.manifest.call_args.kwargs["kind"], "android-tools")
            self.assertFalse(runner.manifest.call_args.kwargs["native_engine_coverage"])

    def test_general_sdk_shards_are_rejected_before_running_commands(self):
        for shard in ("framework_tests", "tool_tests", "web_tool_tests", "build_tests", "analyze"):
            with self.subTest(shard=shard):
                runner = mock.Mock()
                with self.assertRaisesRegex(ValueError, "Unsupported Android tool shard"):
                    sdk.run_sdk(runner, shard, "")
                runner.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
