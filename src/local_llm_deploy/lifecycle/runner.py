"""Foreground launchd supervisor: acquire installation locks before any model code runs."""
from __future__ import annotations

from dataclasses import asdict, replace
import json
import os
from pathlib import Path

from .types import LifecycleError, ServiceSpec


PAYLOAD_ENV = "LOCAL_LLM_SERVICE_SPEC"


def encode_spec(spec: ServiceSpec) -> str:
    # This payload is stored only in the owner-readable plist, never PID metadata.
    return json.dumps(asdict(spec), default=str, separators=(",", ":"))


def decode_spec(payload: str) -> ServiceSpec:
    data = json.loads(payload)
    for field in ("cwd", "log_path", "pid_path", "model_path", "api_key_file"):
        if data.get(field) is not None:
            data[field] = Path(data[field])
    data["argv"] = tuple(data["argv"])
    return ServiceSpec(**data)


def main() -> int:
    from ..config import load_specs, project_paths
    from .manager import foreground_service
    payload = os.environ.pop(PAYLOAD_ENV, None)
    if not payload:
        raise LifecycleError("服务 runner 缺少由 LaunchAgent 提供的启动描述")
    spec = decode_spec(payload)
    paths = project_paths(spec.env.get("LOCAL_LLM_ROOT", str(spec.cwd)))
    if spec.pid_path != paths.run / (spec.key + ".pid"):
        raise LifecycleError("runner PID 路径与所属项目不一致")
    models = load_specs(paths.registry) if paths.registry.exists() else {}
    env = dict(spec.env)
    env["LOCAL_LLM_MANAGED_INSTANCE"] = "1"
    return foreground_service(replace(spec, management="process", supervisor_label=spec.label, env=env), paths, models=models)


if __name__ == "__main__":
    raise SystemExit(main())
