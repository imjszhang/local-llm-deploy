#!/usr/bin/env python3
"""Run the external Video Remake app under launchd and hot-register its gateway entry."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import plistlib
import shutil
import socket
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


LABEL = "com.local-llm-deploy.video-remake"


def request_json(url, *, method="GET", data=None, api_key=None):
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    body = None if data is None else json.dumps(data, ensure_ascii=False).encode()
    with urlopen(Request(url, data=body, headers=headers, method=method), timeout=5) as response:
        return json.load(response)


def main():
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=root.parent.parent / "projects" / "video-remake")
    parser.add_argument("--node", default=shutil.which("node"))
    parser.add_argument("--port", type=int, default=8786)
    parser.add_argument("--gateway", default="http://127.0.0.1:8888")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("This installer uses macOS launchd; run the Node app directly on other systems.")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    repo = args.repo.expanduser().resolve()
    entry = repo / "cli" / "web.js"
    if not entry.is_file():
        parser.error(f"Video Remake web entry is missing: {entry}")
    if not args.node:
        parser.error("Node.js 22 or later is required; use --node to select it.")
    node = str(Path(args.node).expanduser().absolute())
    version = subprocess.check_output([node, "--version"], text=True).strip()
    if int(version.lstrip("v").split(".", 1)[0]) < 22:
        parser.error("Node.js 22 or later is required.")
    api_key = (root / ".api-key").read_text().strip()
    # Authenticate before installing a process. Never put the key in argv, a URL, or a plist.
    gateway = args.gateway.rstrip("/")
    request_json(gateway + "/gateway-api/v1/apps", api_key=api_key)

    upstream = f"http://127.0.0.1:{args.port}"
    logs = root / "logs"
    logs.mkdir(exist_ok=True)
    document = {
        "Label": LABEL,
        "ProgramArguments": [node, str(entry)],
        "WorkingDirectory": str(repo),
        "EnvironmentVariables": {
            "REMAKE_HOST": "127.0.0.1", "REMAKE_PORT": str(args.port), "REMAKE_ROOT": str(repo),
            "PATH": str(Path(node).parent) + ":/usr/bin:/bin",
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 5,
        "StandardOutPath": str(logs / "video-remake.stdout.log"),
        "StandardErrorPath": str(logs / "video-remake.stderr.log"),
    }
    plist = Path.home() / "Library" / "LaunchAgents" / (LABEL + ".plist")
    target = f"gui/{os.getuid()}/{LABEL}"
    loaded = subprocess.run(["launchctl", "print", target], capture_output=True, text=True).returncode == 0
    if plist.exists():
        saved = plistlib.loads(plist.read_bytes())
        if saved != document:
            raise RuntimeError(f"An existing {LABEL} has different settings. Inspect {plist} before replacing it.")
    elif loaded:
        raise RuntimeError(f"{LABEL} is already loaded without the expected plist; leaving it unchanged.")
    if not loaded:
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", args.port)) == 0:
                raise RuntimeError(f"Port {args.port} is already occupied; leaving that process unchanged.")
        plist.parent.mkdir(parents=True, exist_ok=True)
        temporary = plist.with_suffix(".plist.tmp")
        temporary.write_bytes(plistlib.dumps(document))
        temporary.chmod(0o600)
        temporary.replace(plist)
        subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)], check=True)

    deadline = time.monotonic() + 20
    while True:
        try:
            request_json(upstream + "/api/health")
            break
        except (URLError, ValueError, OSError):
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Video Remake did not become healthy; inspect {logs / 'video-remake.stderr.log'}")
            time.sleep(0.25)

    mounted = request_json(gateway + "/gateway-api/v1/apps/video-remake", method="PUT", api_key=api_key,
                           data={"alias": "Video Remake", "prefix": "/remake", "upstream": upstream,
                                 "description": "复刻项目、成片与审阅"})
    request_json(gateway + "/remake/api/health")
    print(json.dumps({"url": gateway + mounted["endpoint"], "launch_agent": LABEL,
                      "plist": str(plist), "log_directory": str(logs)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except HTTPError as exc:
        print(f"HTTP {exc.code}: could not register or verify Video Remake.", file=sys.stderr)
        raise SystemExit(1)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
