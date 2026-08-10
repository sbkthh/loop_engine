"""Tests for environment context (.loop/context.json) write and merge."""

import sys
import os
import json
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from setup import init_requirement, setup_requirement
from directives import build
from state import StateManager
from constants import SCORE
import registry

CONTEXT = {
    "databases": {
        "uat": {"host": "10.0.0.1", "port": 3306, "name": "wms_uat",
                "password_env": "DB_UAT_PASSWORD"}
    },
    "nacos": {"namespace": "cross-dock-v2", "data_ids": ["wms-inbound.yml"]}
}


class TestContextWrite(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "req")
        os.makedirs(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_init_requirement_writes_context(self):
        result = init_requirement(self.root, context=CONTEXT)
        self.assertEqual(result["context_path"],
                         os.path.join(self.root, ".loop", "context.json"))
        with open(result["context_path"]) as f:
            self.assertEqual(json.load(f), CONTEXT)

    def test_init_requirement_without_context_skips_file(self):
        result = init_requirement(self.root)
        self.assertIsNone(result["context_path"])
        self.assertFalse(os.path.exists(
            os.path.join(self.root, ".loop", "context.json")))

    def test_setup_requirement_passes_context(self):
        original_path = registry.REGISTRY_PATH
        registry.REGISTRY_PATH = os.path.join(self.tmp.name, "requirements.json")
        try:
            result = setup_requirement("t", self.root, "chg", [],
                                       context=CONTEXT)
            self.assertNotIn("error", result)
            ctx_path = os.path.join(self.root, ".loop", "context.json")
            self.assertTrue(os.path.exists(ctx_path))
            with open(ctx_path) as f:
                self.assertEqual(json.load(f), CONTEXT)
        finally:
            registry.REGISTRY_PATH = original_path


class TestDirectivesMerge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "req")
        os.makedirs(os.path.join(self.root, ".loop"))
        os.makedirs(os.path.join(self.root, "openspec", "changes", "chg",
                                 "specs", "mod"))
        self.module = {
            "change_id": "chg",
            "module_name": "mod",
            "spec_hash": "abc123",
            "maker_attempt": 0,
        }

    def tearDown(self):
        self.tmp.cleanup()

    def _spec_path(self):
        return os.path.join(self.root, "openspec", "changes", "chg",
                            "specs", "mod", "spec.md")

    def test_build_merges_context_json(self):
        with open(os.path.join(self.root, ".loop", "context.json"), "w") as f:
            json.dump(CONTEXT, f)
        result = build(SCORE, "chg/mod", self.module, self.root)
        self.assertEqual(result["directives"]["context"]["environment"],
                         CONTEXT)

    def test_build_without_context_has_no_environment_key(self):
        result = build(SCORE, "chg/mod", self.module, self.root)
        self.assertNotIn("environment",
                         result["directives"]["context"])

    def test_build_invalid_context_json_reports_error_not_crash(self):
        with open(os.path.join(self.root, ".loop", "context.json"), "w") as f:
            f.write("{not json")
        result = build(SCORE, "chg/mod", self.module, self.root)
        ctx = result["directives"]["context"]
        self.assertNotIn("environment", ctx)
        self.assertIn("environment_error", ctx)
        self.assertIn("invalid context.json", ctx["environment_error"])


if __name__ == "__main__":
    unittest.main()
