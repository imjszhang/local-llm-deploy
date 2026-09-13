"""Private, bounded chat history with transactional revisions and lazy SQLite I/O."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time
from contextlib import contextmanager
from urllib.parse import urlsplit

from .routing import RoutingError

MAX_BYTES = 8 * 1024 * 1024
MAX_SESSIONS = 1000
MAX_SAFE_INTEGER = 9007199254740991
IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}')


def _invalid(message='Invalid chat history payload'):
    raise RoutingError(400, message)


def _object(value, required, optional=()):
    if not isinstance(value, dict) or set(value) - set(required) - set(optional) or set(required) - set(value):
        _invalid()
    return value


def _text(value, limit=1024 * 1024):
    if not isinstance(value, str) or len(value) > limit or '\x00' in value:
        _invalid()
    try:
        if len(value.encode('utf-8')) > limit:
            _invalid()
    except UnicodeError:
        _invalid()
    return value


def _id(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        _invalid('Invalid chat session or message identifier')
    return value


def _number(value, low=0, high=MAX_SAFE_INTEGER, integer=False):
    if type(value) not in (int, float):
        _invalid()
    try:
        valid = math.isfinite(value) and low <= value <= high
    except OverflowError:
        valid = False
    if not valid or (integer and type(value) is not int):
        _invalid()


def _boolean(value):
    if type(value) is not bool:
        _invalid()


def _array(value, limit):
    if not isinstance(value, list) or len(value) > limit:
        _invalid()
    return value


def _effort(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,23}', value) or value == 'none':
        _invalid()


def _parameters(value):
    _object(value, (), ('temperature', 'top_p', 'max_tokens', 'seed', 'thinking',
                        'reasoning_effort', 'reasoning_budget_tokens'))
    for key, item in value.items():
        if key == 'thinking':
            _boolean(item)
        elif key == 'reasoning_effort':
            _effort(item)
        elif key == 'temperature':
            _number(item, high=2)
        elif key == 'top_p':
            _number(item, high=1)
            if item == 0:
                _invalid()
        elif key in ('max_tokens', 'reasoning_budget_tokens'):
            _number(item, low=1, high=131072, integer=True)
        else:
            _number(item, integer=True)


def _controls(value):
    if value is None:
        return
    _object(value, ('thinking', 'reasoning_efforts', 'reasoning_budget', 'default_thinking',
                    'default_effort', 'source'))
    _boolean(value['thinking'])
    _boolean(value['reasoning_budget'])
    if value['default_thinking'] is not None:
        _boolean(value['default_thinking'])
    efforts = _array(value['reasoning_efforts'], 8)
    for effort in efforts:
        _effort(effort)
    if len(set(efforts)) != len(efforts) or value['source'] != 'configured':
        _invalid()
    if value['default_effort'] is not None and value['default_effort'] not in efforts:
        _invalid()
    if not value['thinking'] and (efforts or value['reasoning_budget'] or value['default_thinking'] is True or value['default_effort'] is not None):
        _invalid()


def _request(value):
    _object(value, ('model', 'backend', 'parameters', 'messages'), ('chat_controls',))
    _text(value['model'], 512)
    _text(value['backend'], 128)
    _parameters(value['parameters'])
    for message in _array(value['messages'], 2001):
        _object(message, ('role', 'content'))
        if message['role'] not in ('system', 'user', 'assistant'):
            _invalid()
        _text(message['content'], 4 * 1024 * 1024)
    if 'chat_controls' in value:
        _controls(value['chat_controls'])


def validate_session(value, identifier):
    _object(value, ('id', 'title', 'model', 'system', 'parameters', 'turns', 'draft'))
    if _id(value['id']) != identifier:
        _invalid('Session identifier does not match URL')
    _text(value['title'], 512)
    _text(value['model'], 512)
    _text(value['system'])
    _text(value['draft'])
    _parameters(value['parameters'])
    turn_ids, answer_ids = set(), set()
    for turn in _array(value['turns'], 1000):
        _object(turn, ('id', 'user', 'answers', 'selected'))
        turn_id = _id(turn['id'])
        if turn_id in turn_ids:
            _invalid('Duplicate turn identifier')
        turn_ids.add(turn_id)
        _text(turn['user'])
        answers = _array(turn['answers'], 100)
        _number(turn['selected'], high=max(0, len(answers) - 1), integer=True)
        for answer in answers:
            _object(answer, ('id', 'content', 'reasoning', 'status', 'adopted', 'startedAt', 'request'),
                    ('error', 'finishReason', 'firstContentMs', 'durationMs', 'usage'))
            answer_id = _id(answer['id'])
            if answer_id in answer_ids:
                _invalid('Duplicate answer identifier')
            answer_ids.add(answer_id)
            _text(answer['content'], 4 * 1024 * 1024)
            _text(answer['reasoning'], 4 * 1024 * 1024)
            if answer['status'] not in ('waiting', 'streaming', 'complete', 'stopped', 'error'):
                _invalid()
            _boolean(answer['adopted'])
            _number(answer['startedAt'], integer=True)
            for field in ('firstContentMs', 'durationMs'):
                if field in answer:
                    _number(answer[field], high=365 * 24 * 60 * 60 * 1000)
            for field in ('error', 'finishReason'):
                if field in answer:
                    _text(answer[field], 8192)
            if 'usage' in answer:
                _object(answer['usage'], (), ('prompt_tokens', 'completion_tokens', 'total_tokens'))
                for count in answer['usage'].values():
                    _number(count, integer=True)
            _request(answer['request'])
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    if len(encoded.encode('utf-8')) > MAX_BYTES:
        raise RoutingError(413, 'Chat session exceeds 8 MiB limit')
    return encoded


def parse_payload(body):
    if not body:
        _invalid('Chat history request body is required')
    if len(body) > MAX_BYTES:
        raise RoutingError(413, 'Chat history request exceeds 8 MiB limit')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                _invalid('Duplicate JSON field')
            result[key] = value
        return result
    try:
        return json.loads(body.decode('utf-8'), object_pairs_hook=pairs,
                          parse_constant=lambda _: _invalid())
    except (ValueError, UnicodeError, RecursionError):
        _invalid('Invalid chat history JSON')


def chat_target(path, method):
    parsed = urlsplit(path)
    if parsed.query or parsed.fragment or '?' in path or '#' in path:
        _invalid('Chat history endpoints do not accept query parameters')
    if parsed.path == '/chat-api/v1/sessions':
        if method != 'GET':
            raise RoutingError(405, 'Method not allowed')
        return None
    match = re.fullmatch(r'/chat-api/v1/sessions/([A-Za-z0-9][A-Za-z0-9_-]{0,127})', parsed.path)
    if not match:
        raise RoutingError(404, 'Unknown chat history endpoint')
    if method not in ('GET', 'PUT', 'DELETE'):
        raise RoutingError(405, 'Method not allowed')
    return match.group(1)


class ChatStore:
    def __init__(self, paths, *, clock=time.time, max_sessions=MAX_SESSIONS):
        self.path = Path(paths.root) / 'data' / 'chat-history.sqlite3'
        self.clock, self.max_sessions = clock, max_sessions
        self._lock = threading.RLock()

    @contextmanager
    def _connect(self):
        connection = None
        try:
            # A fixed path and private directory protect the DB plus WAL/SHM.
            # Reject symlinks rather than changing permissions on their target.
            with self._lock:
                directory = self.path.parent
                if directory.is_symlink():
                    raise OSError('Invalid history directory')
                directory.mkdir(mode=0o700, parents=False, exist_ok=True)
                directory.chmod(0o700)
                descriptor = os.open(str(self.path), os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
                try:
                    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                        raise OSError('Invalid history file')
                    os.fchmod(descriptor, 0o600)
                finally:
                    os.close(descriptor)
                connection = sqlite3.connect(str(self.path), timeout=3, isolation_level=None)
                connection.row_factory = sqlite3.Row
                connection.execute('PRAGMA busy_timeout=3000')
                connection.execute('PRAGMA journal_mode=WAL')
                connection.execute('PRAGMA secure_delete=ON')
                connection.execute('BEGIN IMMEDIATE')
                version = connection.execute('PRAGMA user_version').fetchone()[0]
                if version not in (0, 1):
                    raise sqlite3.DatabaseError('Unsupported history schema')
                if version == 0:
                    connection.execute('''CREATE TABLE IF NOT EXISTS sessions (
                        id TEXT PRIMARY KEY, revision INTEGER NOT NULL, created_at INTEGER NOT NULL,
                        updated_at INTEGER NOT NULL, title TEXT NOT NULL, model TEXT NOT NULL,
                        payload TEXT, deleted INTEGER NOT NULL DEFAULT 0)''')
                columns = [row['name'] for row in connection.execute('PRAGMA table_info(sessions)')]
                if columns != ['id', 'revision', 'created_at', 'updated_at', 'title', 'model', 'payload', 'deleted']:
                    raise sqlite3.DatabaseError('Invalid history schema')
                if version == 0:
                    connection.execute('PRAGMA user_version=1')
                connection.commit()
            yield connection
        except (OSError, sqlite3.Error):
            raise RoutingError(503, 'Local chat history is unavailable', code='history_unavailable') from None
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _document(row):
        try:
            session = json.loads(row['payload'])
        except (ValueError, TypeError, RecursionError):
            raise RoutingError(503, 'Stored chat session is unavailable', code='history_unavailable') from None
        return {'schema_version': 1, 'session': session, 'revision': row['revision'], 'updated_at': row['updated_at']}

    def list(self):
        with self._connect() as connection:
            rows = connection.execute('''SELECT id, title, model, revision, created_at, updated_at
                FROM sessions WHERE deleted=0 ORDER BY updated_at DESC, id LIMIT ?''', (self.max_sessions + 1,)).fetchall()
            if len(rows) > self.max_sessions:
                raise RoutingError(503, 'Chat history exceeds the supported session limit')
            return {'schema_version': 1, 'sessions': [dict(row) for row in rows]}

    def get(self, identifier):
        _id(identifier)
        with self._connect() as connection:
            row = connection.execute('SELECT * FROM sessions WHERE id=? AND deleted=0', (identifier,)).fetchone()
            if row is None:
                raise RoutingError(404, 'Chat session not found')
            return self._document(row)

    def put(self, identifier, value):
        _id(identifier)
        _object(value, ('schema_version', 'session', 'revision'))
        if type(value['schema_version']) is not int or value['schema_version'] != 1:
            _invalid('Unsupported chat history schema')
        revision = value['revision']
        _number(revision, high=MAX_SAFE_INTEGER - 1, integer=True)
        payload = validate_session(value['session'], identifier)
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT revision, created_at, updated_at, deleted FROM sessions WHERE id=?', (identifier,)).fetchone()
            if (row is None and revision != 0) or (row is not None and (row['deleted'] or revision != row['revision'])):
                raise RoutingError(409, 'Chat session changed or was deleted; reload before saving', code='revision_conflict')
            now = int(self.clock() * 1000)
            if row is None:
                if connection.execute('SELECT COUNT(*) FROM sessions WHERE deleted=0').fetchone()[0] >= self.max_sessions:
                    raise RoutingError(507, 'Chat history has reached the 1000-session limit; delete a session before creating another', code='history_limit')
                connection.execute('INSERT INTO sessions VALUES (?, 1, ?, ?, ?, ?, ?, 0)',
                                   (identifier, now, now, value['session']['title'], value['session']['model'], payload))
            else:
                now = max(now, row['updated_at'] + 1)
                connection.execute('UPDATE sessions SET revision=?, updated_at=?, title=?, model=?, payload=? WHERE id=?',
                                   (revision + 1, now, value['session']['title'], value['session']['model'], payload, identifier))
            result = connection.execute('SELECT * FROM sessions WHERE id=?', (identifier,)).fetchone()
            connection.commit()
            return self._document(result)

    def delete(self, identifier, value):
        _id(identifier)
        _object(value, ('revision',))
        _number(value['revision'], low=1, high=MAX_SAFE_INTEGER - 1, integer=True)
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT revision, updated_at, deleted FROM sessions WHERE id=?', (identifier,)).fetchone()
            if row is None or row['deleted']:
                raise RoutingError(404, 'Chat session not found')
            if value['revision'] != row['revision']:
                raise RoutingError(409, 'Chat session changed; reload before deleting', code='revision_conflict')
            now = max(int(self.clock() * 1000), row['updated_at'] + 1)
            # Keep only a revision tombstone so an old pending create/update
            # cannot silently resurrect a deleted session.
            connection.execute("UPDATE sessions SET revision=revision+1, updated_at=?, title='', model='', payload=NULL, deleted=1 WHERE id=?",
                               (now, identifier))
            connection.commit()
            return {'deleted': True, 'id': identifier}
