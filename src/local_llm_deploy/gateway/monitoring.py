"""Read-only system collection and stable monitor response shapes."""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from local_llm_deploy.observability import log

def collect_system_info(running, *, strict=False):
    """Collect CPU, memory, load average and per-process stats via macOS commands."""
    result = {
        "cpu": {"user": 0, "sys": 0, "idle": 100},
        "load_avg": [0, 0, 0],
        "memory": {"total_gb": 0, "used_gb": 0, "free_gb": 0, "wired_gb": 0},
        "processes": {"llama_server": [], "ollama": []},
        "cached_at": time.time(),
    }

    if strict:
        result['cpu'] = dict.fromkeys(('user', 'sys', 'idle'))
        result['memory'] = dict.fromkeys(('total_gb', 'used_gb', 'free_gb', 'wired_gb'))
        result['load_avg'] = [None, None, None]

    try:
        top_result = subprocess.run(
            ["top", "-l", "1", "-n", "0", "-s", "0"],
            capture_output=True, text=True, timeout=3 if strict else 10,
        )
        if strict and top_result.returncode:
            raise ValueError('System collector failed')
        top_out = top_result.stdout
        cpu_m = re.search(
            r"CPU usage:\s*([\d.]+)%\s*user,\s*([\d.]+)%\s*sys,\s*([\d.]+)%\s*idle",
            top_out,
        )
        if cpu_m:
            result["cpu"] = {
                "user": float(cpu_m.group(1)),
                "sys": float(cpu_m.group(2)),
                "idle": float(cpu_m.group(3)),
            }
        load_m = re.search(
            r"Load Avg:\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)", top_out
        )
        if load_m:
            result["load_avg"] = [
                float(load_m.group(1)),
                float(load_m.group(2)),
                float(load_m.group(3)),
            ]
        mem_m = re.search(
            r"PhysMem:\s*([\d.]+)([GMTK])\s*used.*?([\d.]+)([GMTK])\s*unused",
            top_out,
        )
        if mem_m:
            def _to_gb(val, unit):
                v = float(val)
                return {"T": v * 1024, "G": v, "M": v / 1024, "K": v / (1024 * 1024)}.get(unit, v)
            used = _to_gb(mem_m.group(1), mem_m.group(2))
            free = _to_gb(mem_m.group(3), mem_m.group(4))
            result["memory"]["used_gb"] = round(used, 1)
            result["memory"]["free_gb"] = round(free, 1)
            result["memory"]["total_gb"] = round(used + free, 1)
        wired_m = re.search(r"\((\d+[GMTK]?)\s*wired", top_out)
        if wired_m:
            w = wired_m.group(1)
            wm = re.match(r"([\d.]+)([GMTK])?", w)
            if wm:
                unit = wm.group(2) or "G"
                result["memory"]["wired_gb"] = round(
                    {"T": 1024, "G": 1, "M": 1/1024, "K": 1/(1024*1024)}.get(unit, 1) * float(wm.group(1)), 1
                )
    except Exception as e:
        if strict:
            raise ValueError('System readings are unavailable') from None
        log.debug(f"[system] top parse error: {e}")

    if strict:
        if result['cpu']['user'] is None or result['memory']['total_gb'] is None:
            raise ValueError('System readings are unavailable')
        return result

    running_pids = {info["pid"]: name for name, info in running.items()}
    running_ports = {name: info["port"] for name, info in running.items()}

    try:
        ps_out = subprocess.run(
            ["ps", "-eo", "pid,rss,%cpu,comm"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        for line in ps_out.strip().split("\n")[1:]:
            parts = line.split()
            if len(parts) < 4:
                continue
            try:
                pid = int(parts[0])
                rss_kb = int(parts[1])
                cpu_pct = float(parts[2])
            except (ValueError, IndexError):
                continue
            comm = " ".join(parts[3:])
            rss_gb = round(rss_kb / (1024 * 1024), 2)
            if "llama-server" in comm or "llama_server" in comm:
                entry = {"pid": pid, "rss_gb": rss_gb, "cpu_pct": cpu_pct}
                if pid in running_pids:
                    name = running_pids[pid]
                    entry["port"] = running_ports.get(name)
                    entry["model"] = name
                result["processes"]["llama_server"].append(entry)
            elif "ollama" in comm.lower():
                result["processes"]["ollama"].append(
                    {"pid": pid, "rss_gb": rss_gb, "cpu_pct": cpu_pct, "comm": os.path.basename(comm)}
                )
    except Exception as e:
        log.debug(f"[system] ps parse error: {e}")

    return result


class Monitoring:
    def __init__(self, discovery, scheduler, settings, *, collector=collect_system_info, clock=time.monotonic):
        self.discovery, self.scheduler, self.settings = discovery, scheduler, settings
        self.collector, self.clock = collector, clock
        self.lock = threading.Lock()
        self.cache = None
        self.timestamp = 0

    def system(self):
        with self.lock:
            if self.cache is not None and self.clock() - self.timestamp < self.settings.system_ttl:
                return self.cache
            running = {key: {'pid': model.pid, 'port': model.port} for key, model in self.discovery.models().items()}
            self.cache = self.collector(running)
            self.timestamp = self.clock()
            return self.cache

    def models(self):
        lanes, budgets = self.scheduler.snapshots()
        result = []
        for key, model in self.discovery.models().items():
            budget = budgets.get(key)
            item = {'name': key, 'model': model.alias, 'port': model.port,
                    'pid': model.pid, 'external': model.external, 'ollama': model.ollama,
                    'ollama_model': model.backend_model if model.ollama else None,
                    'queue': budget['queue_depth'] if budget else 0,
                    'capabilities': list(model.capabilities)}
            if budget and ('chat' in model.capabilities or budget.get('uncertain')):
                item['budget'] = budget
            result.append(item)
        return {'models': result, 'ollama': self.discovery.ollama_status(),
                'lanes': lanes, 'global': lanes['chat'],
                'unavailable_backends': dict(getattr(self.discovery, 'unavailable', {})),
                'uncertain_backends': [key for key, budget in budgets.items() if budget.get('uncertain')]}

    def openai_models(self):
        items = []
        seen = set()
        now = int(time.time())
        for model in self.discovery.models().values():
            for name in model.names:
                if name in seen:
                    continue
                seen.add(name)
                items.append({'id': name, 'object': 'model', 'created': now,
                              'owned_by': 'ollama' if model.ollama else 'local'})
        return {'object': 'list', 'data': items}
