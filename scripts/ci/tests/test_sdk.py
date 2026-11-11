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


CI = Path(__file__).parents[1]
sys.path.insert(0, str(CI))
SPEC = importlib.util.spec_from_file_location("sdk", CI / "sdk.py")
sdk = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sdk)


def target(name, shard, subshard=""):
    return {"name": name, "properties": {"shard": shard, "subshard": subshard}}


def configuration(targets=None):
    if targets is None:
        targets = [target(f"Linux {shard}", shard) for shard in sorted(sdk.ANDROID_TOOL_SHARDS)]
    return {"enabled_branches": ["master", r"flutter-\d+\.\d+-candidate\.\d+"],
            "platform_properties": {"linux": {"properties": {"dependencies": [
                {"dependency": "android_sdk", "version": "version:udcv1"},
                {"dependency": "open_jdk", "version": "version:21"},
            ]}}}, "targets": targets}


def write_runner(root, variant, body="await runTests();"):
    path = root / f"dev/bots/suite_runners/run_android_{variant}_integration_tool_tests.dart"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"Future<void> android{variant.title()}IntegrationToolTestsRunner() async {{\n{body}\n}}\n")
    return path


def policy(config, branch="master", excluded=()):
    from official_ci import Policy
    return Policy(branch=branch, root_targets={item["name"] for item in config["targets"]} - set(excluded),
                  engine_targets=set())


class MatrixTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for variant in ("java11", "java17", "preview"):
            write_runner(self.root, variant)

    def matrix(self, config, **kwargs):
        return sdk.ci_matrix(config, self.root, policy=kwargs.pop("policy", policy(config)), **kwargs)

    def test_only_android_tool_suites_are_selected(self):
        config = configuration()
        for prefix in ("Linux", "Mac", "Mac_arm64", "Mac_x64", "Windows"):
            for shard in ("framework_tests", "tool_tests", "tool_tests_commands",
                          "tool_integration_tests", "web_tool_tests", "build_tests", "analyze"):
                config["targets"].append(target(f"{prefix} {shard}", shard, "1_3"))
        for prefix in ("Mac", "Windows"):
            for shard in sdk.ANDROID_TOOL_SHARDS:
                config["targets"].append(target(f"{prefix} {shard}", shard))
        entries = self.matrix(config)["include"]
        self.assertEqual({entry["shard"] for entry in entries}, sdk.ANDROID_TOOL_SHARDS)
        self.assertEqual(len(entries), len(sdk.ANDROID_TOOL_SHARDS))
        self.assertTrue(all(entry["source_target"].startswith("Linux ") for entry in entries))

    def test_jdk_and_sdk_inherit_platform_dependencies_with_target_overrides(self):
        for serialized in (False, True):
            with self.subTest(serialized=serialized):
                config = configuration([target("Linux preview", "android_preview_tool_integration_tests")])
                dependencies = [{"dependency": "open_jdk", "version": "version:17"}]
                config["targets"][0]["properties"]["dependencies"] = json.dumps(dependencies) if serialized else dependencies
                entry = self.matrix(config)["include"][0]
                self.assertEqual((entry["jdk"], entry["android_sdk"]), ("17", "version:udcv1"))
                dependencies.append({"dependency": "android_sdk", "version": "version:baklava_v1"})
                config["targets"][0]["properties"]["dependencies"] = dependencies
                self.assertEqual(self.matrix(config)["include"][0]["android_sdk"], "version:baklava_v1")

    def test_unsupported_jdk_format_fails(self):
        config = configuration()
        config["targets"][0]["properties"]["dependencies"] = [{"dependency": "open_jdk", "version": "unknown:21"}]
        with self.assertRaisesRegex(ValueError, "Unsupported JDK dependency"):
            self.matrix(config)

    def test_preview_requires_a_safe_sdk_version_from_the_target(self):
        for version in (None, "", "udcv1", "version:udcv1\nother/package latest"):
            with self.subTest(version=version):
                config = configuration([target("Linux preview", "android_preview_tool_integration_tests")])
                dependencies = config["platform_properties"]["linux"]["properties"]["dependencies"]
                dependencies[:] = [d for d in dependencies if d["dependency"] != "android_sdk"]
                if version is not None:
                    dependencies.append({"dependency": "android_sdk", "version": version})
                with self.assertRaisesRegex(ValueError, "Android SDK dependency"):
                    self.matrix(config)

    def test_yaml_preserves_unquoted_numeric_partition(self):
        path = self.root / "ci.yaml"
        path.write_text("targets:\n  - name: Linux Android preview\n    properties:\n      shard: android_preview_tool_integration_tests\n      subshard: 1_1\n")
        actual = sdk.read_configuration(path)
        self.assertEqual(actual["targets"][0]["properties"]["subshard"], "1_1")
        actual["platform_properties"] = configuration()["platform_properties"]
        actual["enabled_branches"] = ["master"]
        self.assertEqual(self.matrix(actual)["include"][0]["subshard"], "1_1")

    def test_all_numeric_partitions_follow_the_requested_ref(self):
        for total in (3, 5):
            with self.subTest(total=total):
                config = configuration([target(f"Linux Android {index}", "android_preview_tool_integration_tests", f"{index}_{total}")
                                        for index in range(1, total + 1)])
                entries = self.matrix(config)["include"]
                self.assertEqual({entry["subshard"] for entry in entries}, {f"{index}_{total}" for index in range(1, total + 1)})
                self.assertEqual(len(entries), total)

    def test_incomplete_or_conflicting_partition_fails(self):
        for subshards in (("1_3", "3_3"), ("1_2", "2_3"), ("0_2", "1_2")):
            with self.subTest(subshards=subshards):
                config = configuration([target(f"Linux Android {part}", "android_preview_tool_integration_tests", part)
                                        for part in subshards])
                with self.assertRaisesRegex(ValueError, "Incomplete or conflicting"):
                    self.matrix(config)

    def test_actual_empty_java11_runner_is_excluded_without_banning_java11(self):
        config = configuration([target("Linux Java11", "android_java11_tool_integration_tests")])
        write_runner(self.root, "java11", "// Empty until ci can be modified.\n/* no cases */")
        excluded = []
        matrix = self.matrix(config, unavailable=excluded)
        self.assertEqual(matrix, {"include": []})
        self.assertIn("empty function", excluded[0]["reason"])
        self.assertIn("empty function", "\n".join(sdk.plan_diagnostics(matrix, excluded)))
        write_runner(self.root, "java11", "await runJava11Tests();")
        self.assertEqual(len(self.matrix(config)["include"]), 1)

    def test_release_policy_and_removed_targets_are_applied_before_source_selection(self):
        config = configuration()
        preview = next(t for t in config["targets"] if t["properties"]["shard"] == "android_preview_tool_integration_tests")
        preview["bringup"] = True
        java11 = next(t for t in config["targets"] if t["properties"]["shard"] == "android_java11_tool_integration_tests")
        write_runner(self.root, "java11").unlink()
        selected_policy = policy(config, branch="flutter-3.35-candidate.0", excluded=[java11["name"]])
        excluded = []
        entries = self.matrix(config, policy=selected_policy, unavailable=excluded)["include"]
        self.assertEqual([entry["shard"] for entry in entries], ["android_java17_tool_integration_tests"])
        self.assertEqual(len(excluded), 2)

    def test_ref_without_android_tool_targets_has_an_empty_plan(self):
        config = configuration([target("Linux widgets", "framework_tests", "widgets")])
        matrix = self.matrix(config)
        self.assertEqual(matrix, {"include": []})
        self.assertEqual(len(sdk.plan_diagnostics(matrix)), len(sdk.ANDROID_TOOL_SHARDS))

    def test_plan_cli_needs_no_framework_source_and_reports_empty_matrices(self):
        from official_ci import Policy
        for targets, has_tests in (([], "false"), ([target("Linux Java 11", "android_java11_tool_integration_tests")], "true")):
            with self.subTest(has_tests=has_tests):
                config = configuration(targets)
                (self.root / ".ci.yaml").write_text(json.dumps(config))
                summary = self.root / "step-summary.md"
                stdout, stderr = io.StringIO(), io.StringIO()
                with mock.patch.object(sys, "argv", ["sdk.py", "--flutter-root", str(self.root), "--outputs", str(self.root / "results"), "plan"]), \
                        mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}), \
                        mock.patch.object(Policy, "load", return_value=policy(config)), \
                        redirect_stdout(stdout), redirect_stderr(stderr):
                    self.assertEqual(sdk.main(), 0)
                outputs = dict(line.split("=", 1) for line in stdout.getvalue().splitlines())
                self.assertEqual(outputs["has-tests"], has_tests)
                self.assertEqual(len(json.loads(outputs["matrix"])["include"]), len(targets))
                self.assertIn("Android tool plan:", summary.read_text())
                self.assertIn("::notice::", stderr.getvalue())
                self.assertTrue((self.root / "results/android-tools-plan.json").is_file())


class SdkEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "flutter"
        self.outputs = Path(self.directory.name) / "outputs"
        self.outputs.mkdir()
        for variant in ("java11", "java17", "preview"):
            write_runner(self.root, variant)
        self.runner = mock.Mock(root=self.root, outputs=self.outputs)
        self.environment = mock.patch.dict(os.environ, {
            "FLUTTER_PREBUILT_ENGINE_VERSION": "published-engine",
            "JAVA_HOME": "/jdk", "SUBSHARD": "inherited-partition",
            "ANDROID_HOME": "/runner-sdk", "ANDROID_SDK_ROOT": "/runner-sdk",
        }, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def preview(self, subshard="", sdk_version="version:udcv1", planned=""):
        config = configuration([target("Linux preview", "android_preview_tool_integration_tests", subshard)])
        config["platform_properties"]["linux"]["properties"]["dependencies"][0]["version"] = sdk_version
        (self.root / ".ci.yaml").write_text(json.dumps(config))
        sdk.run_sdk(self.runner, "android_preview_tool_integration_tests", subshard, planned)
        return self.runner.run.call_args.kwargs["env"]

    def test_unpartitioned_preview_omits_inherited_subshard(self):
        env = self.preview()
        self.assertNotIn("SUBSHARD", env)
        self.assertEqual(env["SHARD"], "android_preview_tool_integration_tests")

    def test_explicit_preview_partitions_are_preserved(self):
        for subshard in ("1_3", "2_3", "3_3"):
            with self.subTest(subshard=subshard):
                self.assertEqual(self.preview(subshard)["SUBSHARD"], subshard)

    def test_preview_installs_target_archive_and_scopes_sdk_environment_to_the_child(self):
        for version in ("version:udcv1", "version:baklava_v1"):
            with self.subTest(version=version):
                env = self.preview(sdk_version=version, planned=version)
                self.assertEqual((self.outputs / "android-sdk.ensure").read_text(),
                                 f"flutter/android/sdk/all/linux-amd64 {version}\n")
                self.assertEqual(env["ANDROID_HOME"], str(self.outputs / "android-preview-sdk"))
                self.assertEqual(env["ANDROID_SDK_ROOT"], env["ANDROID_HOME"])
                self.assertEqual(os.environ["ANDROID_SDK_ROOT"], "/runner-sdk")
                self.assertEqual(self.runner.manifest.call_args.kwargs["android_sdk"], version)

    def test_preview_rejects_a_version_that_differs_from_the_target(self):
        with self.assertRaisesRegex(ValueError, "differs from target CI"):
            self.preview(sdk_version="version:udcv1", planned="version:baklava_v1")
        self.runner.run.assert_not_called()

    def test_preview_missing_archive_fails_before_running_commands(self):
        with self.assertRaisesRegex(ValueError, "Android SDK dependency"):
            self.preview(sdk_version="")
        self.runner.run.assert_not_called()

    def test_empty_runner_cannot_be_forced_through_the_execution_entrypoint(self):
        write_runner(self.root, "java11", "")
        with self.assertRaisesRegex(ValueError, "runner is empty"):
            sdk.run_sdk(self.runner, "android_java11_tool_integration_tests", "")
        self.runner.run.assert_not_called()

    def test_android_setup_does_not_enable_or_precache_other_platforms(self):
        sdk.run_sdk(self.runner, "android_java11_tool_integration_tests", "")
        arguments = [arg for call in self.runner.run.call_args_list for arg in call.args[0]]
        for platform_flag in ("--enable-web", "--web", "--linux", "--macos", "--ios"):
            self.assertNotIn(platform_flag, arguments)
        self.assertIn("--android", arguments)
        self.assertFalse(self.runner.manifest.call_args.kwargs["native_engine_coverage"])

    def test_general_sdk_shards_are_rejected_before_running_commands(self):
        for shard in ("framework_tests", "tool_tests", "web_tool_tests", "build_tests", "analyze"):
            with self.subTest(shard=shard):
                with self.assertRaisesRegex(ValueError, "Unsupported Android tool shard"):
                    sdk.run_sdk(self.runner, shard, "")
                self.runner.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
