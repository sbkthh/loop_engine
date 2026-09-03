"""Tests for spec_utils.py — spec hashing and normalization."""

import hashlib
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from spec_utils import (compute_spec_hash, compute_spec_norm_hash,
                        normalize_spec,
                        count_plan_existing_claims,
                        read_maker_test_command,
                        coerce_roots, resolve_project_root)
import spec_utils


class TestNormalizeSpec(unittest.TestCase):
    def test_html_comment_stripped(self):
        a = normalize_spec("# T\n\n## S\n\n<!-- note -->\n1. x\n")
        b = normalize_spec("# T\n\n## S\n\n1. x\n")
        self.assertEqual(a, b)

    def test_multiline_html_comment_stripped(self):
        a = normalize_spec("# T\n<!--\nmulti\nline\n-->\n## S\n")
        b = normalize_spec("# T\n## S\n")
        self.assertEqual(a, b)

    def test_crlf_and_trailing_whitespace_normalized(self):
        a = normalize_spec("# T\r\n\r\n## S\r\n1. x  \r\n")
        b = normalize_spec("# T\n\n## S\n1. x\n")
        self.assertEqual(a, b)

    def test_surrounding_blank_lines_trimmed(self):
        a = normalize_spec("\n\n# T\n\n## S\n\n\n")
        b = normalize_spec("# T\n\n## S\n")
        self.assertEqual(a, b)

    def test_prose_change_not_normalized_away(self):
        self.assertNotEqual(normalize_spec("# T\n\n1. create item\n"),
                            normalize_spec("# T\n\n1. create item quickly\n"))


class TestSpecHashes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "spec.md")
        self.base = "# Test\n\n## Scenarios\n\n1. Create item\n"
        with open(self.path, "w") as f:
            f.write(self.base)

    def tearDown(self):
        self.tmp.cleanup()

    def test_raw_hash_changes_norm_hash_stable_on_comment(self):
        raw_before = compute_spec_hash(self.path)
        with open(self.path, "a") as f:
            f.write("\n<!-- review note -->\n")
        self.assertNotEqual(compute_spec_hash(self.path), raw_before)
        expected = hashlib.md5(
            normalize_spec(self.base).encode("utf-8")).hexdigest()
        self.assertEqual(compute_spec_norm_hash(self.path), expected)

    def test_substantive_change_alters_norm_hash(self):
        with open(self.path, "a") as f:
            f.write("\n## New Scenario\n")
        expected = hashlib.md5(
            normalize_spec(self.base).encode("utf-8")).hexdigest()
        self.assertNotEqual(compute_spec_norm_hash(self.path), expected)

    def test_missing_file_returns_none(self):
        missing = os.path.join(self.tmp.name, "nope.md")
        self.assertIsNone(compute_spec_hash(missing))
        self.assertIsNone(compute_spec_norm_hash(missing))


class TestPlanExistingClaimCount(unittest.TestCase):
    """count_plan_existing_claims feeds the gap_audit completeness check."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _plan(self, text):
        path = os.path.join(self.root, "plan.md")
        with open(path, "w") as f:
            f.write(text)
        return path

    def test_count_existing_claims(self):
        path = self._plan(
            "- C1: 已有，见 Foo.java:2\n"
            "- C2: 无需变更，见 Foo.java:3\n"
            "- C3: 新增方法\n")
        self.assertEqual(count_plan_existing_claims(path), 2)

    def test_missing_plan_file(self):
        missing = os.path.join(self.root, "missing.md")
        self.assertEqual(count_plan_existing_claims(missing), 0)


if __name__ == "__main__":
    unittest.main()


class TestMakerTestCommand(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        for m in ("mod-a", "mod-b"):
            os.makedirs(os.path.join(self.root, m, "src/main/java"))
        self.plan = os.path.join(self.root, "plan.md")
        with open(self.plan, "w", encoding="utf-8") as f:
            f.write("- 改 mod-a/src/main/java/Foo.java\n"
                    "- 测 mod-b/src/test/java/FooTest.java\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_scopes_to_plan_modules_and_keeps_clean(self):
        self.assertEqual(
            read_maker_test_command("mvn clean test", self.root, self.plan),
            "mvn clean test -pl mod-a,mod-b -am")

    def test_relative_plan_resolved_against_root(self):
        self.assertEqual(
            read_maker_test_command("mvn clean test", self.root, "plan.md"),
            "mvn clean test -pl mod-a,mod-b -am")

    def test_missing_plan_falls_back(self):
        self.assertEqual(
            read_maker_test_command("mvn clean test", self.root, None),
            "mvn clean test")
        self.assertEqual(
            read_maker_test_command(
                "mvn clean test", self.root,
                os.path.join(self.root, "nope.md")),
            "mvn clean test")

    def test_nonexistent_module_dir_filtered_out(self):
        with open(self.plan, "w", encoding="utf-8") as f:
            f.write("- 改 ghost/src/main/java/Foo.java\n")
        self.assertEqual(
            read_maker_test_command("mvn clean test", self.root, self.plan),
            "mvn clean test")

    def test_already_scoped_cmd_untouched(self):
        self.assertEqual(
            read_maker_test_command(
                "mvn clean test -pl mod-a -am", self.root, self.plan),
            "mvn clean test -pl mod-a -am")


class TestCoerceRoots(unittest.TestCase):
    """Commit 1 helper: canonical project_root(s) normalizer."""

    def test_none_becomes_unbound_sentinel(self):
        self.assertEqual(coerce_roots(None), ["."])

    def test_scalar_promoted_to_list(self):
        self.assertEqual(coerce_roots("./kunhe-wms"), ["./kunhe-wms"])

    def test_list_dedup_preserves_order(self):
        self.assertEqual(coerce_roots(["./b", "./a", "./b"]), ["./b", "./a"])

    def test_blank_and_non_string_entries_collapse(self):
        self.assertEqual(coerce_roots(["", "  ", None]), ["."])

    def test_empty_list_becomes_sentinel(self):
        self.assertEqual(coerce_roots([]), ["."])


class TestResolveProjectRootMappingShape(unittest.TestCase):
    """resolve_project_root tolerates scalar- and list-valued
    module_to_project entries; list resolves to first project (M1 shim,
    plural resolver comes in Commit 4)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        os.makedirs(os.path.join(self.root, "kunhe-wms"))
        os.makedirs(os.path.join(self.root, "opc-sna"))
        self._orig = spec_utils.registry.list_requirements

    def tearDown(self):
        spec_utils.registry.list_requirements = self._orig
        self.tmp.cleanup()

    def _stub(self, mapping_value):
        spec_utils.registry.list_requirements = lambda: [{
            "root": self.root,
            "projects": [{"name": "kunhe-wms"}, {"name": "opc-sna"}],
            "module_to_project": {"inventory": mapping_value},
        }]

    def test_scalar_mapping_still_resolves(self):
        self._stub("kunhe-wms")
        self.assertEqual(
            resolve_project_root(self.root, "inventory"),
            os.path.join(self.root, "kunhe-wms"))

    def test_list_mapping_resolves_to_first(self):
        self._stub(["opc-sna", "kunhe-wms"])
        self.assertEqual(
            resolve_project_root(self.root, "inventory"),
            os.path.join(self.root, "opc-sna"))

    def test_empty_list_falls_back_to_module_name(self):
        # empty list means "no explicit project mapping"; the resolver
        # then treats the module name itself as the project name, matching
        # pre-multi-repo behavior.
        self._stub([])
        self.assertIsNone(resolve_project_root(self.root, "inventory"))
