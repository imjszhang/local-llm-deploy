"""History persists only explicit chat data, with revisions and private local files."""
from __future__ import annotations

import copy
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from local_llm_deploy.config import ProjectPaths
from local_llm_deploy.gateway.chat_store import ChatStore, MAX_BYTES, MAX_SESSIONS, parse_payload
from local_llm_deploy.gateway.routing import RoutingError


def session(identifier='session-1'):
    return {'id': identifier, 'title': '测试对话', 'model': 'local-model', 'system': 'Be concise',
            'parameters': {'temperature': 0.5, 'thinking': True, 'reasoning_effort': 'low', 'reasoning_budget_tokens': 128},
            'draft': '尚未发送的草稿', 'turns': [{'id': 'turn-1', 'user': 'question', 'selected': 1, 'answers': [
                {'id': 'answer-1', 'content': 'prior answer', 'reasoning': 'prior reasoning', 'status': 'complete',
                 'adopted': False, 'startedAt': 123, 'durationMs': 42.5, 'firstContentMs': 2.5,
                 'finishReason': 'stop', 'usage': {'prompt_tokens': 12, 'completion_tokens': 5, 'total_tokens': 17},
                 'request': {'model': 'prior-model', 'backend': 'llama_cpp', 'parameters': {'thinking': True},
                             'messages': [{'role': 'user', 'content': 'question'}],
                             'chat_controls': {'thinking': True, 'reasoning_efforts': ['low', 'medium', 'xhigh'],
                                               'reasoning_budget': True, 'default_thinking': False,
                                               'default_effort': 'xhigh', 'source': 'configured'}}},
                {'id': 'answer-2', 'content': 'partial', 'reasoning': 'partial reasoning', 'status': 'streaming',
                 'adopted': False, 'startedAt': 456,
                 'request': {'model': 'local-model', 'backend': 'ollama', 'parameters': {},
                             'messages': [{'role': 'user', 'content': 'question'}]}}
            ]}]}


class ChatStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = ProjectPaths(Path(self.tmp.name))
        self.now = [1_800_000_000.]
        self.store = ChatStore(self.paths, clock=lambda: self.now[0])

    def put(self, value=None, revision=0, store=None):
        value = value or session()
        return (store or self.store).put(value['id'], {'schema_version': 1, 'session': value, 'revision': revision})

    def assert_status(self, status, action):
        with self.assertRaises(RoutingError) as caught:
            action()
        self.assertEqual(caught.exception.status, status)

    def test_lazy_private_database_and_reopen_round_trip(self):
        self.assertFalse(self.store.path.parent.exists())
        created = self.put()
        self.assertEqual(created, {'schema_version': 1, 'session': session(), 'revision': 1, 'updated_at': 1_800_000_000_000})
        reopened = ChatStore(self.paths)
        self.assertEqual(reopened.get('session-1'), created)
        self.assertEqual(reopened.list(), {'schema_version': 1, 'sessions': [{
            'id': 'session-1', 'title': '测试对话', 'model': 'local-model', 'revision': 1,
            'created_at': created['updated_at'], 'updated_at': created['updated_at']}]})
        self.assertEqual(self.store.path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
        with self.store._connect() as connection:
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 1)
            self.assertEqual(connection.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
            self.assertEqual(connection.execute('PRAGMA busy_timeout').fetchone()[0], 3000)
            connection.execute('BEGIN IMMEDIATE')
            connection.execute('UPDATE sessions SET title=title')
            for suffix in ('-wal', '-shm'):
                path = Path(str(self.store.path) + suffix)
                if path.exists():
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_updates_are_ordered_revisioned_and_old_writes_cannot_revive_deletion(self):
        created = self.put()
        changed = session()
        changed['title'], changed['draft'] = 'renamed', 'new draft'
        changed['turns'][0]['answers'][1]['status'] = 'stopped'
        updated = self.put(changed, revision=1)
        self.assertEqual(updated['revision'], 2)
        self.assertGreater(updated['updated_at'], created['updated_at'])
        self.assertEqual(updated['session'], changed)
        self.assert_status(409, lambda: self.put(changed, revision=1))
        self.assert_status(409, lambda: self.store.delete('session-1', {'revision': 1}))
        self.assertEqual(self.store.delete('session-1', {'revision': 2}), {'deleted': True, 'id': 'session-1'})
        self.assertEqual(self.store.list()['sessions'], [])
        self.assert_status(404, lambda: self.store.get('session-1'))
        self.assert_status(404, lambda: self.store.delete('session-1', {'revision': 2}))
        for revision in (0, 1, 2, 3):
            self.assert_status(409, lambda: self.put(changed, revision=revision))
        with closing(sqlite3.connect(str(self.store.path))) as connection, connection:
            self.assertEqual(connection.execute('SELECT title, model, payload, deleted FROM sessions').fetchone(), ('', '', None, 1))
        self.assert_status(409, lambda: self.put(session('missing'), revision=1))

    def test_session_limit_never_silently_drops_history_and_deletion_frees_space(self):
        self.assertEqual(MAX_SESSIONS, 1000)
        limited = ChatStore(self.paths, max_sessions=2)
        self.put(session('first'), store=limited)
        self.put(session('second'), store=limited)
        self.assert_status(507, lambda: self.put(session('third'), store=limited))
        self.assertEqual(len(limited.list()['sessions']), 2)
        limited.delete('first', {'revision': 1})
        self.put(session('third'), store=limited)
        self.assertEqual({row['id'] for row in limited.list()['sessions']}, {'second', 'third'})

    def test_concurrent_update_has_exactly_one_winner(self):
        self.put()
        barrier = threading.Barrier(2)
        outcomes = []
        def update(title):
            item = session()
            item['title'] = title
            barrier.wait()
            try:
                self.put(item, revision=1, store=ChatStore(self.paths))
                outcomes.append(200)
            except RoutingError as exc:
                outcomes.append(exc.status)
        threads = [threading.Thread(target=update, args=(title,)) for title in ('one', 'two')]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(sorted(outcomes), [200, 409])
        self.assertEqual(self.store.get('session-1')['revision'], 2)

    def test_unknown_fields_credentials_and_invalid_values_do_not_create_database(self):
        candidates = []
        for field in ('credential', 'headers', 'authorization', 'token'):
            value = session()
            value[field] = 'synthetic-secret'
            candidates.append(value)
            nested = session()
            nested['turns'][0]['answers'][0]['request'][field] = 'synthetic-secret'
            candidates.append(nested)
        for field, invalid in (('title', 'x' * 513), ('model', []), ('parameters', {'temperature': True}),
                               ('parameters', {'temperature': float('nan')}), ('parameters', {'seed': -1}),
                               ('parameters', {'reasoning_effort': 'none'}), ('turns', {}), ('draft', '\ud800')):
            value = session()
            value[field] = invalid
            candidates.append(value)
        wrong_id = session('other')
        self.assert_status(400, lambda: self.store.put('session-1', {'schema_version': 1, 'session': wrong_id, 'revision': 0}))
        selected = session()
        selected['turns'][0]['selected'] = 2
        candidates.append(selected)
        duplicate = session()
        duplicate['turns'].append(copy.deepcopy(duplicate['turns'][0]))
        candidates.append(duplicate)
        for value in candidates:
            with self.subTest(value_keys=list(value)):
                self.assert_status(400, lambda: self.put(value))
        for revision in (None, True, -1, 0.5, '0', 10 ** 1000):
            self.assert_status(400, lambda: self.put(revision=revision))
        self.assertFalse(self.store.path.parent.exists())

    def test_oversized_and_noncanonical_json_is_rejected(self):
        for body in (b'{"revision":0,"revision":1}', b'{"x":NaN}', b'\xff', b'{', b'[' * 1100, b''):
            self.assert_status(400, lambda: parse_payload(body))
        self.assert_status(413, lambda: parse_payload(b' ' * (MAX_BYTES + 1)))
        large = session()
        large['turns'][0]['answers'][0]['content'] = 'x' * (4 * 1024 * 1024)
        large['turns'][0]['answers'][0]['reasoning'] = 'x' * (4 * 1024 * 1024)
        self.assert_status(413, lambda: self.put(large))
        self.assertFalse(self.store.path.parent.exists())

    def test_failed_transaction_and_unknown_schema_preserve_existing_history(self):
        self.put()
        self.assert_status(409, lambda: self.put(session(), revision=20))
        self.assertEqual(self.store.get('session-1')['revision'], 1)
        with closing(sqlite3.connect(str(self.store.path))) as connection, connection:
            connection.execute('PRAGMA user_version=99')
        self.assert_status(503, lambda: self.store.list())
        with closing(sqlite3.connect(str(self.store.path))) as connection, connection:
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 99)
            self.assertEqual(connection.execute('SELECT revision FROM sessions').fetchone()[0], 1)

    def test_unrecognized_unversioned_table_is_not_migrated_or_overwritten(self):
        self.store.path.parent.mkdir()
        with closing(sqlite3.connect(str(self.store.path))) as connection, connection:
            connection.execute('CREATE TABLE sessions (unrelated TEXT)')
            connection.execute("INSERT INTO sessions VALUES ('keep')")
        self.assert_status(503, lambda: self.store.list())
        with closing(sqlite3.connect(str(self.store.path))) as connection, connection:
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 0)
            self.assertEqual(connection.execute('SELECT unrelated FROM sessions').fetchone()[0], 'keep')

    @unittest.skipUnless(hasattr(os, 'O_NOFOLLOW'), 'Requires no-follow filesystem support')
    def test_symlink_storage_paths_are_rejected(self):
        target = self.paths.root / 'unrelated'
        target.mkdir()
        self.store.path.parent.symlink_to(target, target_is_directory=True)
        self.assert_status(503, lambda: self.store.list())
        self.assertFalse((target / self.store.path.name).exists())
        self.store.path.parent.unlink()
        self.store.path.parent.mkdir()
        other = target / 'file'
        other.write_text('untouched')
        self.store.path.symlink_to(other)
        self.assert_status(503, lambda: self.store.list())
        self.assertEqual(other.read_text(), 'untouched')


if __name__ == '__main__':
    unittest.main()
