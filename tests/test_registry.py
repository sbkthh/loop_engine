"""Tests for registry.py — load empty, add, duplicate, remove, list, find."""

import sys
import os
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import registry


class TestRegistry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._original_path = registry.REGISTRY_PATH
        registry.REGISTRY_PATH = os.path.join(self.tmp.name, "requirements.json")

    def tearDown(self):
        registry.REGISTRY_PATH = self._original_path
        self.tmp.cleanup()

    def test_load_empty_no_file(self):
        data = registry.load()
        self.assertEqual(data, {"requirements": []})

    def test_add_requirement(self):
        entry = registry.add_requirement("test-req", "/tmp/test")
        self.assertEqual(entry["name"], "test-req")
        self.assertEqual(entry["root"], "/tmp/test")
        self.assertIn("registered_at", entry)
        self.assertEqual(len(registry.list_requirements()), 1)

    def test_add_requirement_with_projects(self):
        projects = [{"name": "proj-a", "source": "/path/to/proj-a"}]
        entry = registry.add_requirement("multi-req", "/tmp/multi", projects=projects)
        self.assertIn("projects", entry)
        self.assertEqual(entry["projects"], projects)

    def test_add_requirement_with_description(self):
        entry = registry.add_requirement("desc-req", "/tmp/desc",
                                         description="战略备货系统升级相关需求")
        self.assertEqual(entry["description"], "战略备货系统升级相关需求")
        found = registry.find_requirement("desc-req")
        self.assertEqual(found["description"], "战略备货系统升级相关需求")

    def test_add_requirement_without_description(self):
        entry = registry.add_requirement("plain-req", "/tmp/plain")
        self.assertNotIn("description", entry)

    def test_add_duplicate_raises(self):
        registry.add_requirement("dup", "/tmp/dup")
        with self.assertRaises(ValueError):
            registry.add_requirement("dup", "/tmp/other")

    def test_remove_requirement(self):
        registry.add_requirement("removable", "/tmp/rem")
        self.assertTrue(registry.remove_requirement("removable"))
        self.assertEqual(len(registry.list_requirements()), 0)

    def test_remove_not_found(self):
        self.assertFalse(registry.remove_requirement("ghost"))
        self.assertEqual(len(registry.list_requirements()), 0)

    def test_rename_requirement(self):
        registry.add_requirement("old-name", "/tmp/req")
        self.assertTrue(registry.rename_requirement("old-name", "new-name"))
        self.assertIsNone(registry.find_requirement("old-name"))
        renamed = registry.find_requirement("new-name")
        self.assertIsNotNone(renamed)
        self.assertEqual(renamed["root"], "/tmp/req")

    def test_rename_not_found(self):
        self.assertFalse(registry.rename_requirement("ghost", "other"))

    def test_rename_to_existing_raises(self):
        registry.add_requirement("req-a", "/tmp/a")
        registry.add_requirement("req-b", "/tmp/b")
        with self.assertRaises(ValueError):
            registry.rename_requirement("req-a", "req-b")

    def test_list_requirements(self):
        registry.add_requirement("req-a", "/tmp/a")
        registry.add_requirement("req-b", "/tmp/b")
        requirements = registry.list_requirements()
        self.assertEqual(len(requirements), 2)

    def test_find_requirement(self):
        registry.add_requirement("findable", "/tmp/find")
        entry = registry.find_requirement("findable")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["name"], "findable")

    def test_find_requirement_not_found(self):
        self.assertIsNone(registry.find_requirement("ghost"))

    def test_save_atomic_no_temp(self):
        registry.add_requirement("temp-test", "/tmp/temp")
        registry_dir = os.path.dirname(registry.REGISTRY_PATH)
        tmps = [f for f in os.listdir(registry_dir) if f.endswith(".tmp")]
        self.assertEqual(len(tmps), 0)

    def test_add_project(self):
        registry.add_requirement("req", "/tmp/req",
                                 projects=[{"name": "p1", "source": "/src/p1"}])
        entry = registry.add_project("req", "p2", "/src/p2", branch="feature/x")
        self.assertEqual(entry["name"], "p2")
        self.assertEqual(entry["source"], "/src/p2")
        self.assertEqual(entry["branch"], "feature/x")
        projects = registry.find_requirement("req")["projects"]
        self.assertEqual(len(projects), 2)
        self.assertEqual(projects[1]["name"], "p2")

    def test_add_project_no_branch(self):
        registry.add_requirement("req", "/tmp/req")
        entry = registry.add_project("req", "p1", "/src/p1")
        self.assertNotIn("branch", entry)

    def test_add_project_requirement_not_found(self):
        with self.assertRaises(ValueError):
            registry.add_project("ghost", "p1", "/src/p1")

    def test_add_project_duplicate(self):
        registry.add_requirement("req", "/tmp/req",
                                 projects=[{"name": "p1", "source": "/src/p1"}])
        with self.assertRaises(ValueError):
            registry.add_project("req", "p1", "/src/other")

    def test_add_project_initializes_missing_list(self):
        """Requirements registered without projects get the list created."""
        registry.add_requirement("bare", "/tmp/bare")
        registry.add_project("bare", "p1", "/src/p1")
        projects = registry.find_requirement("bare")["projects"]
        self.assertEqual(len(projects), 1)


if __name__ == '__main__':
    unittest.main()
