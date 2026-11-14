"""Device grants, owned assets and recoverable task records shared across processes.

This module deliberately uses only the standard library: the 8888 gateway and
CLI can manage an offline Hub without importing its MCP runtime dependencies.
"""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import time
import uuid

from local_llm_deploy.storage import file_lock


SLUG = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$")
MAX_ASSET_BYTES = 512 * 1024 * 1024


class HubStateError(ValueError):
    pass


def _list(value, label):
    if not isinstance(value, list) or not value or len(value) > 128 or any(
            not isinstance(x, str) or not SLUG.fullmatch(x) for x in value):
        raise HubStateError(f"{label} 必须是非空标识列表")
    return sorted(set(value))


class HubState:
    def __init__(self, paths):
        self.root = Path(paths if isinstance(paths, (str, os.PathLike)) else paths.root) / 'data' / 'mcp-hub'
        self.db_path = self.root / 'state.sqlite3'

    @contextmanager
    def _connect(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = sqlite3.connect(self.db_path, timeout=10)
        os.chmod(self.db_path, 0o600)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('PRAGMA foreign_keys=ON')
        connection.executescript('''
            CREATE TABLE IF NOT EXISTS devices (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, digest TEXT UNIQUE NOT NULL,
                services TEXT NOT NULL, permissions TEXT NOT NULL,
                created_at REAL NOT NULL, revoked_at REAL, last_seen_at REAL);
            CREATE TABLE IF NOT EXISTS assets (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, service TEXT NOT NULL,
                name TEXT NOT NULL, path TEXT NOT NULL, bytes INTEGER NOT NULL,
                mime_type TEXT NOT NULL, sha256 TEXT NOT NULL, created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, service TEXT NOT NULL,
                tool TEXT NOT NULL, request_id TEXT NOT NULL, request_digest TEXT, prompt_id TEXT,
                status TEXT NOT NULL, result TEXT, error TEXT,
                created_at REAL NOT NULL, updated_at REAL NOT NULL,
                UNIQUE(owner, service, request_id));
            CREATE INDEX IF NOT EXISTS task_prompt ON tasks(service, prompt_id);
        ''')
        if 'request_digest' not in {row[1] for row in connection.execute('PRAGMA table_info(tasks)')}:
            try:
                connection.execute('ALTER TABLE tasks ADD COLUMN request_digest TEXT')
            except sqlite3.OperationalError as exc:
                if 'duplicate column' not in str(exc):
                    connection.close()
                    raise
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def internal_key(self):
        path = self.root / 'internal.key'
        with file_lock(self.root / '.internal.lock'):
            if path.exists():
                value = path.read_text().strip()
                if not re.fullmatch(r'[A-Za-z0-9_-]{64}', value):
                    raise HubStateError('Hub 内部密钥文件损坏，拒绝使用')
                return value
            value = secrets.token_urlsafe(48)
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, 'w') as stream:
                stream.write(value + '\n')
            return value

    @staticmethod
    def _device(row):
        return {k: json.loads(row[k]) if k in ('services', 'permissions') else row[k]
                for k in ('id', 'name', 'services', 'permissions', 'created_at', 'revoked_at', 'last_seen_at')}

    def create_device(self, name, services, permissions):
        if not isinstance(name, str) or not name.strip() or len(name) > 120:
            raise HubStateError('请填写 1–120 字的设备名称')
        services = _list(services, '服务')
        permissions = _list(permissions, '权限')
        if set(permissions) - {'read', 'generate'} or 'read' not in permissions:
            raise HubStateError('权限必须包含 read，可选 generate')
        token = 'mcp_hub_' + secrets.token_urlsafe(36)
        device_id = uuid.uuid4().hex
        with self._connect() as db:
            db.execute('INSERT INTO devices(id,name,digest,services,permissions,created_at) VALUES(?,?,?,?,?,?)',
                       (device_id, name.strip(), hashlib.sha256(token.encode()).hexdigest(),
                        json.dumps(services), json.dumps(permissions), time.time()))
            row = db.execute('SELECT * FROM devices WHERE id=?', (device_id,)).fetchone()
        return {'device': self._device(row), 'token': token}

    def list_devices(self):
        with self._connect() as db:
            return [self._device(r) for r in db.execute('SELECT * FROM devices ORDER BY created_at DESC')]

    def revoke_device(self, device_id):
        with self._connect() as db:
            return bool(db.execute('UPDATE devices SET revoked_at=? WHERE id=? AND revoked_at IS NULL',
                                   (time.time(), device_id)).rowcount)

    def authenticate(self, token, service=None, permission='read'):
        if not isinstance(token, str) or not token.startswith('mcp_hub_') or len(token) > 200:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self._connect() as db:
            row = db.execute('SELECT * FROM devices WHERE digest=? AND revoked_at IS NULL', (digest,)).fetchone()
            if not row:
                return None
            device = self._device(row)
            if (service and service not in device['services']) or permission not in device['permissions']:
                return None
            if not row['last_seen_at'] or time.time() - row['last_seen_at'] > 60:
                db.execute('UPDATE devices SET last_seen_at=? WHERE id=?', (time.time(), row['id']))
            return device

    def workspace(self, owner, service):
        if not SLUG.fullmatch(owner) or not SLUG.fullmatch(service):
            raise HubStateError('无效的工作目录标识')
        folder = self.root / 'workspaces' / service / owner
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        return folder.resolve()

    @staticmethod
    def safe_name(name):
        if not isinstance(name, str) or not name or len(name) > 200 or name in ('.', '..') or any(
                c in name for c in ('/', '\\', '\x00', '\r', '\n')):
            raise HubStateError('文件名无效；请只提供文件名，不含目录')
        return name

    def asset_from_file(self, owner, service, source, name=None):
        source = Path(source)
        if source.is_symlink() or not source.is_file():
            raise HubStateError('素材必须是普通文件')
        name = self.safe_name(name or source.name)
        size = source.stat().st_size
        if size > MAX_ASSET_BYTES:
            raise HubStateError('素材超过 512 MiB 限制')
        asset_id = uuid.uuid4().hex
        destination = self.root / 'assets' / asset_id / name
        destination.parent.mkdir(parents=True, mode=0o700)
        try:
            digest = hashlib.sha256()
            written = 0
            with source.open('rb') as incoming, destination.open('xb') as outgoing:
                while True:
                    chunk = incoming.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_ASSET_BYTES:
                        raise HubStateError('素材超过 512 MiB 限制')
                    digest.update(chunk)
                    outgoing.write(chunk)
            destination.chmod(0o600)
            with self._connect() as db:
                db.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?,?)',
                           (asset_id, owner, service, name, str(destination.resolve()), written,
                            mimetypes.guess_type(name)[0] or 'application/octet-stream', digest.hexdigest(), time.time()))
            return self.get_asset(asset_id, owner)
        except BaseException:
            shutil.rmtree(destination.parent, ignore_errors=True)
            raise

    def get_asset(self, asset_id, owner=None, *, include_path=False):
        with self._connect() as db:
            row = db.execute('SELECT * FROM assets WHERE id=?', (asset_id,)).fetchone()
        if not row or (owner is not None and row['owner'] != owner):
            raise HubStateError('素材不存在或不属于当前设备')
        result = dict(row)
        path = Path(result['path'])
        assets = (self.root / 'assets').resolve()
        if path.is_symlink() or not path.is_file() or assets not in path.resolve().parents:
            raise HubStateError('素材文件不可用')
        if not include_path:
            result.pop('path')
        result['download_path'] = '/assets/' + result['id']
        return result

    def list_assets(self, owner=None, limit=100):
        with self._connect() as db:
            sql = 'SELECT * FROM assets' + (' WHERE owner=?' if owner else '') + ' ORDER BY created_at DESC LIMIT ?'
            rows = db.execute(sql, (owner, min(1000, max(1, limit))) if owner else (min(1000, max(1, limit)),)).fetchall()
        return [{**{k: r[k] for k in r.keys() if k != 'path'}, 'download_path': '/assets/' + r['id']} for r in rows]

    @staticmethod
    def _task(row):
        result = dict(row)
        result['result'] = json.loads(result['result']) if result['result'] else None
        return result

    def begin_task(self, owner, service, tool, request_id=None, *, request_digest=None):
        request_id = request_id or uuid.uuid4().hex
        if not isinstance(request_id, str) or not SLUG.fullmatch(request_id):
            raise HubStateError('request_id 必须为最多 80 位字母、数字、连字符或下划线')
        now = time.time()
        with self._connect() as db:
            db.execute('INSERT OR IGNORE INTO tasks(id,owner,service,tool,request_id,request_digest,status,created_at,updated_at) '
                       'VALUES(?,?,?,?,?,?,?,?,?)', (uuid.uuid4().hex, owner, service, tool, request_id, request_digest, 'submitting', now, now))
            fresh = bool(db.execute('SELECT changes()').fetchone()[0])
            row = db.execute('SELECT * FROM tasks WHERE owner=? AND service=? AND request_id=?',
                             (owner, service, request_id)).fetchone()
            if row['tool'] != tool or row['request_digest'] != request_digest:
                raise HubStateError('request_id 已被不同参数的操作使用')
        return self._task(row), fresh

    def update_task(self, task_id, *, status=None, prompt_id=None, result=None, error=None):
        values = {'updated_at': time.time()}
        if status is not None:
            values['status'] = status
        if prompt_id is not None:
            values['prompt_id'] = prompt_id
        if result is not None:
            values['result'] = json.dumps(result, ensure_ascii=False)
        if error is not None:
            values['error'] = str(error)[:1000]
        with self._connect() as db:
            db.execute('UPDATE tasks SET ' + ','.join(k + '=?' for k in values) + ' WHERE id=?',
                       (*values.values(), task_id))
        return self.get_task(task_id)

    def get_task(self, task_id, owner=None, service=None):
        with self._connect() as db:
            sql = 'SELECT * FROM tasks WHERE (id=? OR prompt_id=?)'
            args = [task_id, task_id]
            if owner is not None:
                sql += ' AND owner=?'
                args.append(owner)
            if service:
                sql += ' AND service=?'
                args.append(service)
            row = db.execute(sql + ' ORDER BY created_at DESC LIMIT 1', args).fetchone()
        if not row:
            raise HubStateError('任务不存在或不属于当前设备')
        return self._task(row)

    def list_tasks(self, limit=50, owner=None):
        with self._connect() as db:
            sql = 'SELECT * FROM tasks' + (' WHERE owner=?' if owner else '') + ' ORDER BY created_at DESC LIMIT ?'
            values = (owner, min(500, max(1, limit))) if owner else (min(500, max(1, limit)),)
            return [self._task(r) for r in db.execute(sql, values)]

    def recover_submitting(self):
        with self._connect() as db:
            db.execute("UPDATE tasks SET status='uncertain', error=?,updated_at=? WHERE status='submitting'",
                       ('Hub 在提交期间停止；请核对远端队列，勿重复提交。', time.time()))
