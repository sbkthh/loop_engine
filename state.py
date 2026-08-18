"""StateManager: load/save state.json, module CRUD, priority selection."""

import json
import os
import tempfile

from constants import STATE_FILE, PRIORITY_ORDER, DRAFT


class StateManager:
    def __init__(self, root_dir="."):
        self.root_dir = os.path.abspath(root_dir)
        self.loop_dir = os.path.join(self.root_dir, ".loop")
        self.state_path = os.path.join(self.root_dir, STATE_FILE)

    def init_state(self):
        os.makedirs(self.loop_dir, exist_ok=True)
        state = {
            "version": 1,
            "root_dir": self.root_dir,
            "current": {"module": None, "action": None, "attempt": 0},
            "modules": {},
            "gray_drafts": [],
            "trace": [],
            "audit_trail": [],
        }
        self.save(state)
        return state

    def load(self):
        if not os.path.exists(self.state_path):
            return self.init_state()
        with open(self.state_path) as f:
            return json.load(f)

    def save(self, state):
        os.makedirs(self.loop_dir, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=self.loop_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, self.state_path)
        except Exception:
            os.unlink(tmp_path)
            raise

    @staticmethod
    def module_key(change_id, module_name):
        return f"{change_id}/{module_name}"

    @staticmethod
    def get_module(state, key):
        return state["modules"].get(key)

    @staticmethod
    def set_module_field(state, key, field, value):
        if key in state["modules"]:
            state["modules"][key][field] = value
        return state

    @staticmethod
    def add_module(state, key, change_id, module_name, project_root=".",
                   spec_hash=None, plan_hash=None, spec_norm_hash=None):
        state["modules"][key] = {
            "change_id": change_id,
            "module_name": module_name,
            "project_root": project_root,
            "status": DRAFT,
            "spec_hash": spec_hash,
            "spec_norm_hash": spec_norm_hash,
            "plan_hash": plan_hash,
            "maker_attempt": 0,
            "review_fix_attempt": 0,
            "files_created": [],
            "files_modified": [],
            "plan_path": None,
            "last_synced": None,
        }
        return state["modules"][key]

    @staticmethod
    def find_mid_progress(state):
        current = state.get("current", {})
        module_key = current.get("module")
        action = current.get("action")
        if not module_key or not action:
            return None
        module = state["modules"].get(module_key)
        if not module:
            return None
        return module_key, module, action

    @staticmethod
    def set_current(state, module_key, action, attempt=0):
        state["current"] = {
            "module": module_key,
            "action": action,
            "attempt": attempt,
        }

    @staticmethod
    def clear_current(state):
        state["current"] = {"module": None, "action": None, "attempt": 0}

    @staticmethod
    def select_next_module(state):
        modules = state.get("modules", {})
        for status in PRIORITY_ORDER:
            for key, module in modules.items():
                if module["status"] == status:
                    return key, module
        return None
