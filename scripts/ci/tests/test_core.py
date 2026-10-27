import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

CI = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CI))


def module(name):
    spec = importlib.util.spec_from_file_location(name, CI / (name + ".py"))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


resolve_ref = module("resolve_ref")
prepare = module("prepare_checkout")
golden = module("golden")


class RefValidationTest(unittest.TestCase):
    def test_rejects_repositories_that_escape_github_owner_repository(self):
        for repository in ("https://evil.test/a/b", "owner/repo/extra", "../repo", "owner name/repo"):
            with self.subTest(repository=repository), self.assertRaises(ValueError):
                resolve_ref.validate_repository(repository)

    def test_rejects_empty_option_or_multiline_refs_before_running_git(self):
        for ref in ("", "--upload-pack=command", "main\nsha=spoofed", "two words"):
            with self.subTest(ref=ref), self.assertRaises(ValueError):
                resolve_ref.validate_ref(ref)

    def test_accepts_branch_tag_and_commit(self):
        for ref in ("feature/ios-scenes", "3.38.0", "a" * 40, "refs/tags/3.38.0"):
            self.assertEqual(resolve_ref.validate_ref(ref), ref)


class BootstrapManifestTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.git("init", "--quiet")
        self.git("config", "user.name", "CI test")
        self.git("config", "user.email", "ci@example.invalid")
        (self.root / "engine").mkdir()
        (self.root / "engine/source.cc").write_text("engine one")
        (self.root / "DEPS").write_text("dependencies")
        self.commit("baseline")

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.root), *args], text=True).strip()

    def commit(self, message):
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", message)
        return self.git("rev-parse", "HEAD")

    def test_bootstrap_hash_uses_official_baseline_not_fork_engine_changes(self):
        baseline = self.git("rev-parse", "HEAD")
        expected = prepare.baseline_engine(self.root, baseline)
        (self.root / "engine/source.cc").write_text("unpublished fork change")
        fork = self.commit("fork")
        self.assertEqual(prepare.baseline_engine(self.root, baseline), expected)
        self.assertNotEqual(prepare.baseline_engine(self.root, fork), expected)

    def test_release_engine_version_takes_priority_over_content_hash(self):
        path = self.root / "bin/internal/engine.version"
        path.parent.mkdir(parents=True)
        path.write_text("b" * 40 + "\n")
        revision = self.commit("release")
        self.assertEqual(prepare.baseline_engine(self.root, revision), "b" * 40)


class GoldenComparisonTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "flutter"
        self.root.mkdir()
        subprocess.run(["git", "init", "--quiet", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=CI",
                        "-c", "user.email=ci@example.invalid", "commit", "--allow-empty",
                        "--quiet", "-m", "fixture"], check=True)
        self.outputs = Path(self.temp.name) / "results"
        self.environ = patch.dict(os.environ, {"FLUTTER_BASELINE_SHA": "b" * 40, "UPDATE_GOLDENS": "true"})
        self.environ.start()
        self.runner = golden.Golden(self.root, self.outputs, "android_engine_vulkan_tests")
        self.driver = self.runner.project / "test_driver"
        self.driver.mkdir(parents=True)
        comparator = self.root / "dev/tools/android_driver_extensions/lib/src/goldens.dart"
        comparator.parent.mkdir(parents=True)
        comparator.write_text("  Future<bool> compare(Uint8List imageBytes, Uri golden) async {\n"
                              "    final io.File goldenFile = _getTestFilePath(golden);\n"
                              "    return true;\n  }\n")
        self.commands = []
        self.runner.checkout = lambda revision: self.commands.append(("checkout", revision))
        self.runner.android_command = lambda *args: self.commands.append(("android", args))
        self.runner.baseline_images = lambda: (self.driver / "reference.png").write_bytes(b"trusted baseline")

    def tearDown(self):
        self.environ.stop()
        self.temp.cleanup()

    def test_target_comparison_cannot_inherit_update_goldens(self):
        def run(command, **kwargs):
            self.assertNotIn("UPDATE_GOLDENS", kwargs["env"])
            self.assertEqual((self.driver / "reference.png").read_bytes(), b"trusted baseline")
            actuals = Path(kwargs["env"]["FLUTTER_GOLDEN_ACTUALS_DIR"])
            actuals.mkdir()
            (actuals / "reference.actual.png").write_bytes(b"trusted baseline")
            return 0
        self.runner.run = run
        self.assertEqual(self.runner.execute(), 0)
        report = json.loads((self.outputs / "manifest.json").read_text())
        self.assertEqual(report["baseline_sha"], "b" * 40)
        self.assertTrue(report["baseline_images"])

    def test_missing_reference_images_fails_instead_of_passing_empty_suite(self):
        self.runner.baseline_images = lambda: None
        with self.assertRaisesRegex(ValueError, "no reference screenshots"):
            self.runner.execute()

    def test_mutated_reference_image_is_rejected(self):
        def run(command, **kwargs):
            (self.driver / "reference.png").write_bytes(b"target silently updated golden")
            actuals = Path(kwargs["env"]["FLUTTER_GOLDEN_ACTUALS_DIR"])
            actuals.mkdir()
            (actuals / "reference.actual.png").write_bytes(b"target silently updated golden")
            return 0
        self.runner.run = run
        with self.assertRaisesRegex(ValueError, "modified"):
            self.runner.execute()

    def test_success_without_actual_comparisons_is_rejected(self):
        self.runner.run = lambda *args, **kwargs: 0
        with self.assertRaisesRegex(ValueError, "without comparing"):
            self.runner.execute()


class GoldenPlanTest(unittest.TestCase):
    def test_target_tool_dependencies_do_not_erase_inherited_avd(self):
        import yaml
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ci = {
                "platform_properties": {"linux_android_emu": {"properties": {
                    "dependencies": json.dumps([{"dependency": "android_virtual_device",
                                                "version": "android_36_google_apis_x64.textpb"}])}}},
                "targets": [{"name": "Linux_android_emu " + suite, "properties": {
                    "shard": suite, "dependencies": json.dumps([{"dependency": "open_jdk", "version": "25"}])}}
                    for suite in golden.SUITES],
            }
            (root / ".ci.yaml").write_text(yaml.safe_dump(ci))
            matrix = golden.source_matrix(root)["include"]
            self.assertEqual(len(matrix), 7)
            self.assertTrue(all(entry["api"] == "36" for entry in matrix))

    def test_instrumentation_uses_flat_source_engine_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            engine = root / "engine/src/out/android_debug_x64"
            engine.mkdir(parents=True)
            for artifact in ("flutter_embedding_debug", "x86_64_debug"):
                (engine / (artifact + ".pom")).write_text('<project xmlns="urn:maven"><version>1.0.0-source</version></project>')
                (engine / (artifact + ".jar")).write_bytes(b"source artifact")
                (engine / (artifact + ".maven-metadata.xml")).write_text("metadata")
            properties = golden.maven_repository(root, root / "maven")
            self.assertEqual(properties["ORG_GRADLE_PROJECT_local-engine-out"], str(engine))
            links = list((root / "maven").rglob("*.jar"))
            self.assertEqual(len(links), 2)
            self.assertTrue(all(path.read_bytes() == b"source artifact" for path in links))


if __name__ == "__main__":
    unittest.main()
