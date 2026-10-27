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
    targets = [target(f"Linux {shard}", shard) for shard in sdk.SDK_SHARDS - {"framework_tests"}]
    targets += [target("Linux framework", "framework_tests", "widgets")]
    return {"targets": targets}


FRAMEWORK = "await selectSubshard(<String, ShardRunner>{'widgets': runWidgets, 'libraries': runLibraries, 'slow': runSlow, 'misc': runMisc, 'impeller': runImpeller,});"


class MatrixTests(unittest.TestCase):
    def test_legacy_commands_and_missing_new_suites_are_supported(self):
        # Flutter 3.35.7 (225e8f4) schedules commands under tool_tests and has
        # no Java 17 integration shard. Preserve its actual command names.
        config = configuration()
        config["targets"] = [item for item in config["targets"] if item["properties"]["shard"]
                             not in {"tool_tests_commands", "android_java17_tool_integration_tests"}]
        config["targets"].append(target("Linux tool_tests_commands", "tool_tests", "commands"))
        entries = sdk.ci_matrix(config, FRAMEWORK)["include"]
        self.assertTrue(any(entry["shard"] == "tool_tests" and entry["subshard"] == "commands" for entry in entries))
        self.assertFalse(any(entry["shard"] == "tool_tests_commands" for entry in entries))

    def test_jdk_comes_from_the_requested_ci_target(self):
        config = configuration()
        config["targets"][0]["properties"]["dependencies"] = '[{"dependency":"open_jdk","version":"version:21"}]'
        entries = sdk.ci_matrix(config, FRAMEWORK)["include"]
        entry = next(entry for entry in entries if entry["source_target"] == config["targets"][0]["name"])
        self.assertEqual(entry["jdk"], "21")

    def test_yaml_preserves_unquoted_numeric_partition(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ci.yaml"
            path.write_text("targets:\n  - name: Linux tools\n    properties:\n      shard: tool_tests_commands\n      subshard: 1_1\n")
            actual = sdk.read_configuration(path)
            self.assertEqual(actual["targets"][0]["properties"]["subshard"], "1_1")

    def test_discovers_new_framework_partitions_without_dropping_any(self):
        entries = sdk.ci_matrix(configuration(), FRAMEWORK)["include"]
        actual = {entry["subshard"] for entry in entries if entry["shard"] == "framework_tests"}
        self.assertEqual(actual, {"widgets", "libraries", "slow", "misc", "impeller"})
        self.assertEqual(next(entry["runner"] for entry in entries if entry["subshard"] == "impeller"), "macos-15")

    def test_uses_full_native_mac_partition_instead_of_duplicate_x64(self):
        config = configuration()
        for prefix in ("Mac_x64", "Mac_arm64"):
            for index in range(1, 4):
                config["targets"].append(target(f"{prefix} build {index}", "build_tests", f"{index}_3"))
        entries = [entry for entry in sdk.ci_matrix(config, FRAMEWORK)["include"] if entry["runner"] == "macos-15" and entry["shard"] == "build_tests"]
        self.assertEqual(len(entries), 3)
        self.assertTrue(all(entry["source_target"].startswith("Mac_arm64") for entry in entries))

    def test_incomplete_partition_fails_instead_of_reporting_partial_coverage(self):
        config = configuration()
        config["targets"] = [item for item in config["targets"] if item["properties"]["shard"] != "build_tests"]
        config["targets"].extend([target("Linux build1", "build_tests", "1_3"), target("Linux build3", "build_tests", "3_3")])
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            sdk.ci_matrix(config, FRAMEWORK)

    def test_missing_core_suite_is_an_explicit_compatibility_error(self):
        config = configuration()
        config["targets"] = [item for item in config["targets"] if item["properties"]["shard"] != "analyze"]
        with self.assertRaisesRegex(ValueError, "analyze"):
            sdk.ci_matrix(config, FRAMEWORK)

    def test_legacy_java11_entry_is_retained_and_missing_modern_suites_are_reported(self):
        config = configuration()
        config["targets"] = [item for item in config["targets"] if item["properties"]["shard"]
                             not in {"tool_tests_commands", "android_java17_tool_integration_tests"}]
        config["targets"].append(target("Linux commands", "tool_tests", "commands"))
        matrix = sdk.ci_matrix(config, FRAMEWORK)
        self.assertTrue(any(entry["shard"] == "android_java11_tool_integration_tests" for entry in matrix["include"]))
        diagnostics = "\n".join(sdk.plan_diagnostics(matrix))
        self.assertIn("tool_tests_commands: not configured", diagnostics)
        self.assertIn("legacy tool_tests/commands", diagnostics)
        self.assertIn("android_java17_tool_integration_tests: not configured", diagnostics)
        self.assertIn("android_java11_tool_integration_tests instead", diagnostics)

    def test_plan_cli_reports_absent_suites_without_polluting_matrix_output(self):
        config = configuration()
        config["targets"] = [item for item in config["targets"] if item["properties"]["shard"]
                             not in sdk.OPTIONAL_SDK_SHARDS]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".ci.yaml").write_text(json.dumps(config))
            runner = root / "dev/bots/suite_runners/run_framework_tests.dart"
            runner.parent.mkdir(parents=True)
            runner.write_text(FRAMEWORK)
            summary = root / "step-summary.md"
            stdout, stderr = io.StringIO(), io.StringIO()
            with mock.patch.object(sys, "argv", ["sdk.py", "--flutter-root", str(root), "plan"]), \
                    mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}), \
                    redirect_stdout(stdout), redirect_stderr(stderr):
                self.assertEqual(sdk.main(), 0)
            self.assertTrue(stdout.getvalue().startswith("matrix="))
            json.loads(stdout.getvalue().removeprefix("matrix="))
            self.assertIn("android_java17_tool_integration_tests: not configured", summary.read_text())
            self.assertIn("::notice::", stderr.getvalue())


class StrictGoldenTests(unittest.TestCase):
    def source(self):
        content = "    log('Skipping \"$golden\" test: $reason');\n"
        content += "      await update(golden, imageBytes);\n      return true;\n    }\n\n    final List<int> goldenBytes\n"
        content += "    if (result.passed) {\n      result.dispose();\n      return true;\n"
        for exception in ("io.OSError", "io.SocketException", "FormatException"):
            content += f"    }} on {exception} catch (_) {{\n      return FlutterSkippingFileComparator(\n"
        return content

    def test_skip_missing_and_all_network_fallbacks_fail_closed(self):
        patched = sdk.strict_goldens(self.source())
        self.assertEqual(patched.count("FLUTTER_STRICT_GOLDENS"), 6)
        self.assertEqual(patched.count("rethrow;"), 3)
        self.assertIn("has no baseline", patched)
        self.assertIn("comparison skipped", patched)
        self.assertIn("Strict golden compared:", patched)
        self.assertNotIn("io.Platform.environment", patched)

    def test_legacy_local_comparator_still_fails_on_missing_baseline(self):
        # Flutter 3.35.7 used a separate ComparisonResult declaration and did
        # not await update(); these are not reasons to disable strict goldens.
        source = self.source().replace(
            "      await update(golden, imageBytes);\n      return true;\n    }\n\n    final List<int> goldenBytes",
            "      update(golden, imageBytes);\n      return true;\n    }\n\n    ComparisonResult result;\n    final List<int> goldenBytes",
        )
        patched = sdk.strict_goldens(source)
        self.assertIn("has no baseline", patched)
        self.assertEqual(patched.count("FLUTTER_STRICT_GOLDENS"), 6)
        self.assertIn("await update(golden, imageBytes);", patched)

    def test_changed_or_duplicate_contract_is_not_partially_patched(self):
        for source in (self.source().replace("goldenBytes", "differentName"), self.source() + self.source()):
            with self.subTest(source=source):
                with self.assertRaisesRegex(ValueError, "partial strict patch"):
                    sdk.strict_goldens(source)


class HostTests(unittest.TestCase):
    def test_metal_variant_is_removed_without_removing_dart_cases_or_software_backends(self):
        source = "  impeller_backends = ['', 'vulkan', 'opengles']\n  if is_mac():\n    impeller_backends.append('metal')\n  for test in dart_tests:\n    run(test)\n"
        actual = sdk.software_dart_backends(source)
        self.assertIn("impeller_backends = ['', 'vulkan', 'opengles']", actual)
        self.assertIn("for test in dart_tests:", actual)
        self.assertNotIn("append('metal')", actual)
        with self.assertRaisesRegex(ValueError, "Cannot verify"):
            sdk.software_dart_backends(source.replace("append('metal')", "append('different')"))

    def test_removes_remote_build_requirements_and_keeps_instrumentation(self):
        original = ["--target-dir", "ci/host_debug_unopt", "--asan", "--dart-debug", "--rbe", "--xcode-symlinks", "--no-goma"]
        actual = sdk.local_gn(original)
        self.assertNotIn("--rbe", actual)
        self.assertNotIn("--xcode-symlinks", actual)
        self.assertIn("--no-rbe", actual)
        self.assertIn("--asan", actual)
        self.assertIn("--dart-debug", actual)

    def test_gpu_filter_discovers_new_cpu_suites_and_excludes_only_gpu(self):
        source = """def run_cc_tests(
make_test('old_unittests')
make_test('new_unittests')
run_engine_executable(build_dir, 'impeller_unittests')
run_engine_executable(build_dir, 'impeller_dart_unittests')
def run_engine_benchmarks(
"""
        self.assertEqual(sdk.mac_engine_filter(source), ["new_unittests", "old_unittests"])

    def test_unsupported_host_or_missing_builder_fails_without_building(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "Unsupported host"):
                sdk.host_configuration(Path(directory), "debug", "Darwin", "x86_64")


if __name__ == "__main__":
    unittest.main()
