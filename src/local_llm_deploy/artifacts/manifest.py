from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from ..config import ConfigError, project_paths
from ..storage import atomic_json, file_lock


class Manifest:
    def __init__(self, paths=None):
        self.paths = paths or project_paths()
        self.path = self.paths.models / ".manifest.json"

    def load(self):
        if not self.path.exists():
            return {"version": 1, "entries": []}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version", 1) != 1 or not isinstance(data.get("entries"), list):
            raise ConfigError(f"无效 manifest: {self.path}")
        if any(not isinstance(e, dict) or not e.get("path") or not e.get("model_key") for e in data["entries"]):
            raise ConfigError("manifest 条目必须有 model_key 和 path")
        return data

    def update(self, operation):
        with file_lock(self.path.with_suffix(".lock")):
            data = self.load()
            operation(data)
            atomic_json(self.path, data)

    def entries(self, key=None, quant=None):
        entries = self.load()["entries"]
        return [e for e in entries if (key is None or e["model_key"] == key)
                and (quant is None or e.get("quant") == quant)]

    def absolute(self, entry):
        path = Path(entry["path"])
        return path if path.is_absolute() else self.paths.root / path

    def record(self, key, quant, path, *, revision=None, source=None):
        from .paths import inside_models
        path = Path(path).resolve()
        if not inside_models(path, self.paths):
            raise ConfigError("登记路径必须在 models/ 子目录内")
        relative = str(path.relative_to(self.paths.root))
        def change(data):
            now = datetime.now(timezone.utc).isoformat()
            entry = next((e for e in data["entries"] if e["model_key"] == key
                          and e["path"] == relative and e.get("quant") == quant), None)
            if entry is None:
                entry = {"model_key": key, "path": relative, "created_at": now}
                data["entries"].append(entry)
            entry.update(quant=quant, updated_at=now)
            if revision is not None:
                entry["revision"] = revision
            if source is not None:
                entry["source"] = source
        self.update(change)

    def remove_paths(self, targets):
        targets = {Path(p).resolve() for p in targets}
        self.update(lambda data: data.update(entries=[e for e in data["entries"] if self.absolute(e).resolve() not in targets]))
