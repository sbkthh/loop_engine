"""StateManager: load/save state.json, module CRUD, priority selection."""

import json
import logging
import os
import shutil
import tempfile
import time

from constants import STATE_FILE, CONTEXT_FILE, PRIORITY_ORDER, DRAFT
from spec_utils import coerce_roots, derive_plan_path

logger = logging.getLogger("loop")


class StateManager:
    def __init__(self, root_dir="."):
        self.root_dir = os.path.abspath(root_dir)
        self.loop_dir = os.path.join(self.root_dir, ".loop")
        self.state_path = os.path.join(self.root_dir, STATE_FILE)
        self.bak_path = self.state_path + ".bak"
        self.context_path = os.path.join(self.root_dir, CONTEXT_FILE)

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
            restored = self._restore_from_backup("state.json 丢失")
            if restored is not None:
                return self._migrate(restored)
            logger.warning("state.json 不存在且无备份，重建空状态（root=%s）",
                           self.root_dir)
            return self._migrate(self.init_state())
        try:
            with open(self.state_path) as f:
                return self._migrate(json.load(f))
        except ValueError:
            return self._migrate(self._recover_corrupt())

    @staticmethod
    def _migrate(state):
        """Reconstruct fields older state.json files predate, in memory only.

        Covers `project_root` -> canonical `project_roots` (the derived scalar
        is kept in sync = project_roots[0] so un-migrated readers keep working
        for one release) and the missing `ever_synced` flag. Disk files are not
        rewritten by this pass; both passes are idempotent.
        """
        for mod in state.get("modules", {}).values():
            roots = coerce_roots(mod.get("project_roots",
                                          mod.get("project_root")))
            mod["project_roots"] = roots
            mod["project_root"] = roots[0]
        StateManager._backfill_ever_synced(state)
        return state

    @staticmethod
    def _backfill_ever_synced(state):
        """Recover `ever_synced` for modules that synced before it existed.

        The flag (added 2026-09-08) gates the CLASSIFY_CHANGE light path, so a
        module that shipped earlier re-runs SCORE -> MAKER_STEP0 -> whole-
        document CHECKER on every later change. Reconstruct it from evidence
        rather than a one-off script: `last_synced` alone is NOT enough -- that
        is exactly the "no code yet but marked SYNCED" case f908a23 guarded
        against -- so a plan must still be on disk *and* a MAKER must have run.
        Read-only and per-load, so a module losing its plan self-corrects.
        """
        root = state.get("root_dir") or ""
        if not os.path.isabs(root):
            return
        for mod in state.get("modules", {}).values():
            if mod.get("ever_synced") or not mod.get("last_synced"):
                continue
            if mod.get("maker_attempt", 0) < 1:
                continue
            paths = [derive_plan_path(mod.get("change_id"),
                                      mod.get("module_name"), root)]
            stored = mod.get("plan_path")
            if stored:
                paths.append(stored if os.path.isabs(stored)
                             else os.path.join(root, stored))
            if any(os.path.exists(p) for p in paths):
                mod["ever_synced"] = True

    def _recover_corrupt(self):
        """Unparseable state.json: quarantine it, restore last good backup."""
        ts = int(time.time())
        corrupt_path = f"{self.state_path}.corrupt-{ts}"
        try:
            os.replace(self.state_path, corrupt_path)
        except OSError:
            corrupt_path = self.state_path
        logger.error("state.json 损坏，已隔离到 %s", corrupt_path)
        restored = self._restore_from_backup("state.json 损坏")
        if restored is not None:
            return restored
        logger.error("state.json 损坏且无备份，重建空状态（root=%s）",
                     self.root_dir)
        return self.init_state()

    def _restore_from_backup(self, reason):
        if not os.path.exists(self.bak_path):
            return None
        try:
            with open(self.bak_path) as f:
                state = json.load(f)
        except ValueError:
            logger.error("备份 %s 也损坏", self.bak_path)
            return None
        try:
            shutil.copy2(self.bak_path, self.state_path)
        except OSError:
            return None
        logger.warning("%s，已从备份 %s 恢复", reason, self.bak_path)
        return state

    def save(self, state):
        os.makedirs(self.loop_dir, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=self.loop_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            # rolling backup: previous good state, survives loss/corruption
            if os.path.exists(self.state_path):
                shutil.copy2(self.state_path, self.bak_path)
            os.replace(tmp_path, self.state_path)
        except Exception:
            os.unlink(tmp_path)
            raise

    @staticmethod
    def module_key(change_id, module_name):
        return f"{change_id}/{module_name}"

    @staticmethod
    def set_module_field(state, key, field, value):
        if key in state["modules"]:
            state["modules"][key][field] = value
        return state

    @staticmethod
    def add_module(state, key, change_id, module_name, project_root=".",
                   project_roots=None,
                   spec_hash=None, plan_hash=None, spec_norm_hash=None):
        roots = coerce_roots(project_roots if project_roots is not None
                             else project_root)
        state["modules"][key] = {
            "change_id": change_id,
            "module_name": module_name,
            "project_roots": roots,
            "project_root": roots[0],
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
    def select_next_module(state, only_key=None):
        modules = state.get("modules", {})
        for status in PRIORITY_ORDER:
            for key, module in modules.items():
                if only_key is not None and key != only_key:
                    continue
                if module["status"] == status:
                    return key, module
        return None
