"""Registry: global requirement registry CRUD at ~/.qoder/loop_engine/requirements.json."""

import json
import os
import tempfile
import datetime

REGISTRY_PATH = os.path.join(
    os.path.expanduser("~/.qoder/loop_engine"), "requirements.json"
)


def load():
    if not os.path.exists(REGISTRY_PATH):
        return {"requirements": []}
    with open(REGISTRY_PATH) as f:
        data = json.load(f)
    if "requirements" not in data:
        data["requirements"] = []
    return data


def save(data):
    registry_dir = os.path.dirname(REGISTRY_PATH)
    os.makedirs(registry_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=registry_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, REGISTRY_PATH)
    except Exception:
        os.unlink(tmp_path)
        raise


def add_requirement(name, root, projects=None, description=None):
    data = load()
    for r in data["requirements"]:
        if r["name"] == name:
            raise ValueError(f"Requirement already registered: {name}")
    entry = {
        "name": name,
        "root": os.path.abspath(root),
        "registered_at": datetime.datetime.now().isoformat(),
    }
    if projects:
        entry["projects"] = projects
    if description:
        entry["description"] = description
    data["requirements"].append(entry)
    save(data)
    return entry


def remove_requirement(name):
    data = load()
    before = len(data["requirements"])
    data["requirements"] = [r for r in data["requirements"] if r["name"] != name]
    if len(data["requirements"]) < before:
        save(data)
        return True
    return False


def rename_requirement(old_name, new_name):
    data = load()
    for r in data["requirements"]:
        if r["name"] == new_name:
            raise ValueError(f"Requirement already registered: {new_name}")
    target = next((r for r in data["requirements"] if r["name"] == old_name), None)
    if not target:
        return False
    target["name"] = new_name
    save(data)
    return True


def list_requirements():
    return load()["requirements"]


def find_requirement(name):
    for r in load()["requirements"]:
        if r["name"] == name:
            return r
    return None
