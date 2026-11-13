"""Local AI agent monitoring: Claude Code, Codex, OpenClaw, Grok Bot.

Collects session activity from agent data directories, translates to a unified
event model, and pushes updates via WebSocket. No upstream forwarding; this is
a pure local observer.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any


MAX_TASK_LENGTH = 200
MAX_TOOL_TARGET_LENGTH = 120


def sanitize_text(text: str, max_length: int) -> str:
    """Strip secrets and truncate."""
    if not text:
        return ''
    # Remove common secret patterns
    sanitized = re.sub(r'\bsk-[A-Za-z0-9_-]{32,}', '[API_KEY]', text)
    sanitized = re.sub(r'\bBearer [A-Za-z0-9_.-]{16,}', 'Bearer [TOKEN]', sanitized)
    sanitized = re.sub(r'password["\']?\s*[:=]\s*["\']?[^\s"\']{6,}', 'password=[REDACTED]', sanitized, flags=re.IGNORECASE)
    if len(sanitized) > max_length:
        return sanitized[:max_length] + '…'
    return sanitized


class AgentCollector:
    """Base collector for one agent type."""

    def __init__(self, agent_type: str):
        self.agent_type = agent_type
        self._sessions = {}  # session_id -> session metadata
        self._file_positions = {}  # file_path -> byte_offset

    def collect(self) -> list[dict[str, Any]]:
        """Return new events since last collect. Override in subclass."""
        return []

    def snapshot(self) -> dict[str, Any]:
        """Return current state of all sessions."""
        return {
            'agent': self.agent_type,
            'sessions': list(self._sessions.values()),
        }


class ClaudeCodeCollector(AgentCollector):
    """Collect from ~/.claude/projects/*/session.jsonl and ~/.claude/sessions/*.json"""

    def __init__(self):
        super().__init__('claude-code')
        self.claude_home = Path.home() / '.claude'

    def _scan_sessions(self) -> list[Path]:
        """Find active session jsonl files."""
        projects = self.claude_home / 'projects'
        if not projects.exists():
            return []
        files = []
        for project_dir in projects.iterdir():
            if not project_dir.is_dir():
                continue
            for jsonl in project_dir.glob('*.jsonl'):
                files.append(jsonl)
        return files

    def _read_new_lines(self, path: Path) -> list[dict]:
        """Read lines added since last collect."""
        try:
            stat = path.stat()
            offset = self._file_positions.get(str(path), 0)
            if stat.st_size <= offset:
                return []
            with path.open('r', encoding='utf-8', errors='replace') as f:
                f.seek(offset)
                lines = f.readlines()
                self._file_positions[str(path)] = f.tell()
            records = []
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            return records
        except (OSError, ValueError):
            return []

    def _extract_events(self, records: list[dict], session_id: str, project: str) -> list[dict]:
        """Convert Claude Code records to unified events."""
        events = []
        for rec in records:
            rec_type = rec.get('type')
            ts = rec.get('timestamp', '')

            if rec_type == 'user':
                content = rec.get('message', {}).get('content', [])
                text = ' '.join(c.get('text', '') for c in content if c.get('type') == 'text')
                task = sanitize_text(text, MAX_TASK_LENGTH)
                events.append({
                    'agent': 'claude-code',
                    'session': session_id,
                    'project': project,
                    'type': 'task_start',
                    'task': task,
                    'ts': ts,
                })
                self._sessions[session_id] = {
                    'session': session_id,
                    'project': project,
                    'state': 'active',
                    'task': task,
                    'tool': None,
                    'target': None,
                    'ts': ts,
                }

            elif rec_type == 'assistant':
                content = rec.get('message', {}).get('content', [])
                for block in content:
                    if block.get('type') == 'tool_use':
                        tool = block.get('name', 'unknown')
                        input_data = block.get('input', {})
                        target = ''
                        if tool == 'Bash':
                            target = sanitize_text(input_data.get('command', ''), MAX_TOOL_TARGET_LENGTH)
                        elif tool in ('Read', 'Edit', 'Write'):
                            target = input_data.get('file_path', '')
                        elif tool == 'WebFetch':
                            target = input_data.get('url', '')
                        events.append({
                            'agent': 'claude-code',
                            'session': session_id,
                            'project': project,
                            'type': 'tool_call',
                            'tool': tool,
                            'target': sanitize_text(target, MAX_TOOL_TARGET_LENGTH),
                            'ts': ts,
                        })
                        if session_id in self._sessions:
                            self._sessions[session_id].update({
                                'tool': tool,
                                'target': sanitize_text(target, MAX_TOOL_TARGET_LENGTH),
                                'ts': ts,
                            })

            elif rec_type == 'api-request':
                usage = rec.get('params', {}).get('usage', {})
                if usage:
                    events.append({
                        'agent': 'claude-code',
                        'session': session_id,
                        'project': project,
                        'type': 'token_usage',
                        'tokens': usage,
                        'ts': ts,
                    })

        return events

    def collect(self) -> list[dict[str, Any]]:
        all_events = []
        for jsonl in self._scan_sessions():
            session_id = jsonl.stem
            project = jsonl.parent.name
            records = self._read_new_lines(jsonl)
            events = self._extract_events(records, session_id, project)
            all_events.extend(events)
        return all_events


class CodexCollector(AgentCollector):
    """Collect from ~/.codex/sessions/YYYY/MM/DD/*.jsonl"""

    def __init__(self):
        super().__init__('codex')
        self.codex_home = Path.home() / '.codex'

    def _scan_sessions(self) -> list[Path]:
        """Find recent rollout jsonl files."""
        sessions = self.codex_home / 'sessions'
        if not sessions.exists():
            return []
        files = []
        for year_dir in sessions.iterdir():
            if not year_dir.is_dir():
                continue
            for month_dir in year_dir.iterdir():
                if not month_dir.is_dir():
                    continue
                for day_dir in month_dir.iterdir():
                    if not day_dir.is_dir():
                        continue
                    for jsonl in day_dir.glob('rollout-*.jsonl'):
                        # Only recent files to avoid scanning history
                        if time.time() - jsonl.stat().st_mtime < 86400 * 7:
                            files.append(jsonl)
        return files

    def _read_new_lines(self, path: Path) -> list[dict]:
        """Read lines added since last collect."""
        try:
            stat = path.stat()
            offset = self._file_positions.get(str(path), 0)
            if stat.st_size <= offset:
                return []
            with path.open('r', encoding='utf-8', errors='replace') as f:
                f.seek(offset)
                lines = f.readlines()
                self._file_positions[str(path)] = f.tell()
            records = []
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            return records
        except (OSError, ValueError):
            return []

    def _extract_events(self, records: list[dict], session_id: str) -> list[dict]:
        """Convert Codex rollout events to unified events."""
        events = []
        for rec in records:
            rec_type = rec.get('type')
            payload = rec.get('payload', {})
            ts = rec.get('timestamp', '')

            if rec_type == 'event_msg' and payload.get('type') == 'task_started':
                turn_id = payload.get('turn_id', '')
                events.append({
                    'agent': 'codex',
                    'session': session_id,
                    'project': '',
                    'type': 'task_start',
                    'task': f"Turn {turn_id[:8]}",
                    'ts': ts,
                })
                self._sessions[session_id] = {
                    'session': session_id,
                    'project': '',
                    'state': 'active',
                    'task': f"Turn {turn_id[:8]}",
                    'tool': None,
                    'target': None,
                    'ts': ts,
                }

            elif rec_type == 'response_item' and payload.get('type') == 'custom_tool_call':
                tool_name = payload.get('name', 'unknown')
                tool_input = payload.get('input', '')
                events.append({
                    'agent': 'codex',
                    'session': session_id,
                    'project': '',
                    'type': 'tool_call',
                    'tool': tool_name,
                    'target': sanitize_text(str(tool_input), MAX_TOOL_TARGET_LENGTH),
                    'ts': ts,
                })
                if session_id in self._sessions:
                    self._sessions[session_id].update({
                        'tool': tool_name,
                        'target': sanitize_text(str(tool_input), MAX_TOOL_TARGET_LENGTH),
                        'ts': ts,
                    })

            elif rec_type == 'event_msg' and payload.get('type') == 'token_count':
                usage = payload.get('info', {}).get('last_token_usage', {})
                if usage:
                    events.append({
                        'agent': 'codex',
                        'session': session_id,
                        'project': '',
                        'type': 'token_usage',
                        'tokens': usage,
                        'ts': ts,
                    })

            elif rec_type == 'event_msg' and payload.get('type') == 'task_complete':
                if session_id in self._sessions:
                    self._sessions[session_id]['state'] = 'idle'
                events.append({
                    'agent': 'codex',
                    'session': session_id,
                    'project': '',
                    'type': 'task_complete',
                    'ts': ts,
                })

        return events

    def collect(self) -> list[dict[str, Any]]:
        all_events = []
        for jsonl in self._scan_sessions():
            # Extract session ID from filename: rollout-DATE-SESSION_ID.jsonl
            parts = jsonl.stem.split('-')
            if len(parts) >= 5:
                session_id = '-'.join(parts[4:])
            else:
                session_id = jsonl.stem
            records = self._read_new_lines(jsonl)
            events = self._extract_events(records, session_id)
            all_events.extend(events)
        return all_events


class AgentMonitor:
    """Unified monitor coordinating all agent collectors."""

    def __init__(self):
        self.collectors = [
            ClaudeCodeCollector(),
            CodexCollector(),
        ]
        self._lock = threading.Lock()
        self._subscribers = []  # list of (queue, generation)
        self._generation = 0
        self._running = False
        self._thread = None

    def start(self):
        """Start background collection loop."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._collect_loop, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop background collection."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)

    def _collect_loop(self):
        """Background thread: collect and broadcast events."""
        while self._running:
            try:
                events = []
                for collector in self.collectors:
                    events.extend(collector.collect())

                if events:
                    with self._lock:
                        self._generation += 1
                        dead = []
                        for q, gen in self._subscribers:
                            try:
                                q.put_nowait({'type': 'events', 'events': events, 'generation': self._generation})
                            except Exception:
                                dead.append((q, gen))
                        for item in dead:
                            self._subscribers.remove(item)

                time.sleep(0.5)
            except Exception:
                time.sleep(1)

    def snapshot(self) -> dict[str, Any]:
        """Return full current state."""
        agents = []
        for collector in self.collectors:
            agents.append(collector.snapshot())
        return {
            'schema_version': 1,
            'agents': agents,
            'generation': self._generation,
        }

    def subscribe(self, queue):
        """Add a subscriber queue for events."""
        with self._lock:
            self._subscribers.append((queue, self._generation))

    def unsubscribe(self, queue):
        """Remove a subscriber queue."""
        with self._lock:
            self._subscribers[:] = [(q, g) for q, g in self._subscribers if q is not queue]
