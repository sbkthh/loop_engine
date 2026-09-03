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
                        read_checker_test_command,
                        coerce_roots, resolve_project_root,
                        resolve_project_roots,
                        read_test_commands,
                        read_checker_test_commands,
                        read_maker_test_commands)
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


class TestCheckerTestCommandRepoRoot(unittest.TestCase):
    """Bug α regression: -pl scoping must be computed against the OWNING
    REPO root, not the requirement root. Requirement-root callers used to
    strip <req_root>/ and get the repo name as the first segment."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.req_root = self.tmp.name
        self.repo = os.path.join(self.req_root, "kunhe-wms")
        os.makedirs(os.path.join(self.repo, "inventory-service", "src/main/java"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_repo_root_produces_module_name(self):
        files = [os.path.join(
            self.repo, "inventory-service/src/main/java/Foo.java")]
        cmd = read_checker_test_command("mvn clean test", self.repo, files)
        self.assertEqual(cmd, "mvn test -pl inventory-service -am")

    def test_requirement_root_would_yield_repo_name(self):
        # Documents the bug shape: same call but with req_root instead of
        # repo_root produces -pl kunhe-wms (wrong). The fix is on the caller
        # side (directives passes module.project_root, not root_dir).
        files = [os.path.join(
            self.repo, "inventory-service/src/main/java/Foo.java")]
        cmd = read_checker_test_command("mvn clean test", self.req_root, files)
        self.assertEqual(cmd, "mvn test -pl kunhe-wms -am")

    def test_cross_repo_files_are_ignored_when_outside_repo(self):
        # Files belonging to a different repo shouldn't leak into -pl.
        other = os.path.join(self.req_root, "opc-sna/whatever/src/main/java/Bar.java")
        mine = os.path.join(
            self.repo, "inventory-service/src/main/java/Foo.java")
        cmd = read_checker_test_command("mvn clean test", self.repo, [mine, other])
        self.assertEqual(cmd, "mvn test -pl inventory-service -am")

    def test_no_matching_files_omits_pl(self):
        cmd = read_checker_test_command("mvn clean test", self.repo, [])
        self.assertEqual(cmd, "mvn test")


class TestResolveProjectRootsPlural(unittest.TestCase):
    """Commit 4 canonical resolver: returns every bound repo path."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        os.makedirs(os.path.join(self.root, "kunhe-wms"))
        os.makedirs(os.path.join(self.root, "opc-sna"))
        self._orig = spec_utils.registry.list_requirements

    def tearDown(self):
        spec_utils.registry.list_requirements = self._orig
        self.tmp.cleanup()

    def _stub(self, mapping_value, projects=None):
        spec_utils.registry.list_requirements = lambda: [{
            "root": self.root,
            "projects": projects if projects is not None
                        else [{"name": "kunhe-wms"}, {"name": "opc-sna"}],
            "module_to_project": {"inventory": mapping_value},
        }]

    def test_list_mapping_returns_all_repos_in_order(self):
        self._stub(["opc-sna", "kunhe-wms"])
        self.assertEqual(
            resolve_project_roots(self.root, "inventory"),
            [os.path.join(self.root, "opc-sna"),
             os.path.join(self.root, "kunhe-wms")])

    def test_scalar_mapping_returns_single_element_list(self):
        self._stub("kunhe-wms")
        self.assertEqual(
            resolve_project_roots(self.root, "inventory"),
            [os.path.join(self.root, "kunhe-wms")])

    def test_unmapped_module_falls_back_to_module_name(self):
        # no explicit mapping; project named "inventory" doesn't exist
        spec_utils.registry.list_requirements = lambda: [{
            "root": self.root,
            "projects": [{"name": "kunhe-wms"}],
            "module_to_project": {},
        }]
        self.assertEqual(resolve_project_roots(self.root, "inventory"), [])

    def test_source_repo_used_when_worktree_missing(self):
        src = os.path.join(self.tmp.name, "src-kunhe-wms")
        os.makedirs(src)
        spec_utils.registry.list_requirements = lambda: [{
            "root": self.root,
            "projects": [{"name": "ghost-wms", "source": src}],
            "module_to_project": {"inventory": ["ghost-wms"]},
        }]
        self.assertEqual(resolve_project_roots(self.root, "inventory"),
                         [os.path.abspath(src)])

    def test_duplicate_entries_deduped(self):
        self._stub(["kunhe-wms", "kunhe-wms", "opc-sna"])
        self.assertEqual(
            resolve_project_roots(self.root, "inventory"),
            [os.path.join(self.root, "kunhe-wms"),
             os.path.join(self.root, "opc-sna")])


class TestPerRepoCommandHelpers(unittest.TestCase):
    """Commit 4 helpers that fan a single-command API out to per-repo dicts."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.repo_a = os.path.join(self.root, "kunhe-wms")
        self.repo_b = os.path.join(self.root, "opc-sna")
        os.makedirs(os.path.join(self.repo_a, "inventory/src/main/java"))
        os.makedirs(os.path.join(self.repo_b, "consumer/src/main/java"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_test_commands_maps_abs_repo_to_cmd(self):
        cmds = read_test_commands([self.repo_a, self.repo_b])
        self.assertEqual(cmds[os.path.abspath(self.repo_a)], "mvn clean test")
        self.assertEqual(cmds[os.path.abspath(self.repo_b)], "mvn clean test")

    def test_checker_per_repo_buckets_files_by_prefix(self):
        cmd_by_repo = {os.path.abspath(self.repo_a): "mvn clean test",
                       os.path.abspath(self.repo_b): "mvn clean test"}
        files = [
            os.path.join(self.repo_a, "inventory/src/main/java/Foo.java"),
            os.path.join(self.repo_b, "consumer/src/main/java/Bar.java"),
        ]
        out = read_checker_test_commands(cmd_by_repo,
                                         [self.repo_a, self.repo_b], files)
        self.assertEqual(out[os.path.abspath(self.repo_a)],
                         "mvn test -pl inventory -am")
        self.assertEqual(out[os.path.abspath(self.repo_b)],
                         "mvn test -pl consumer -am")

    def test_maker_per_repo_uses_plan_modules(self):
        plan = os.path.join(self.root, "plan.md")
        with open(plan, "w", encoding="utf-8") as f:
            f.write("- 改 inventory/src/main/java/Foo.java\n"
                    "- 改 consumer/src/main/java/Bar.java\n")
        cmd_by_repo = {os.path.abspath(self.repo_a): "mvn clean test",
                       os.path.abspath(self.repo_b): "mvn clean test"}
        out = read_maker_test_commands(cmd_by_repo,
                                       [self.repo_a, self.repo_b], plan)
        # Each repo scopes to the maven modules that exist under it.
        self.assertEqual(out[os.path.abspath(self.repo_a)],
                         "mvn clean test -pl inventory -am")
        self.assertEqual(out[os.path.abspath(self.repo_b)],
                         "mvn clean test -pl consumer -am")
