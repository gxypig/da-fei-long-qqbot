"""File-backed SQLite regressions; all messages and identities are synthetic."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch


SOURCE = (Path(__file__).resolve().parents[1] / 'AstrBot' / 'data' / 'plugins'
          / 'astrbot_plugin_proactive_agent' / 'sent_memory.py')
SPEC = importlib.util.spec_from_file_location('sent_memory_read_path_subject', SOURCE)
SUBJECT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SUBJECT)
SentMemory = SUBJECT.SentMemory
REAL_CONNECT = sqlite3.connect


class SentMemoryReadPath(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='qqbot-sent-memory-test-')
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'sent.sqlite3'
        self.now = 1_700_000_000.0
        self.memory = SentMemory(self.path, clock=lambda: self.now)
        self.origin = 'test:FriendMessage:alice'

    def journal_mode(self, mode):
        with closing(REAL_CONNECT(self.path)) as db:
            self.assertEqual(db.execute('PRAGMA journal_mode=' + mode).fetchone()[0],
                             mode.lower())

    def test_hot_note_reads_committed_history_while_another_writer_holds_lock(self):
        self.memory.record(self.origin, 'current', 'committed test message', 'fixed')
        # Pending records belonging to somebody else must not trigger adoption.
        self.memory.record('test:FriendMessage:bob', None, 'other private message', 'fixed')
        for mode in ('DELETE', 'WAL'):
            with self.subTest(journal=mode):
                self.journal_mode(mode)
                with closing(REAL_CONNECT(self.path)) as writer:
                    writer.execute('BEGIN IMMEDIATE')
                    writer.execute('UPDATE sent SET text=? WHERE origin=?',
                                   ('uncommitted change', self.origin))
                    try:
                        # Demonstrate that a no-op UPDATE really does contend.
                        with closing(REAL_CONNECT(self.path, timeout=0.05)) as control:
                            with self.assertRaisesRegex(sqlite3.OperationalError, 'locked'):
                                control.execute('UPDATE sent SET cid=? WHERE origin=? AND cid IS NULL',
                                                ('current', self.origin))
                        note = self.memory.note(self.origin, 'current')
                        self.assertIn('committed test message', note)
                        self.assertNotIn('uncommitted change', note)
                        self.assertNotIn('other private message', note)
                    finally:
                        writer.rollback()

    def test_hot_read_uses_one_connection_without_mutation(self):
        self.memory.record(self.origin, 'current', 'stored test message', 'manual')
        statements = []

        def connect(*args, **kwargs):
            db = REAL_CONNECT(*args, **kwargs)
            db.set_trace_callback(statements.append)
            return db

        with patch.object(SUBJECT.sqlite3, 'connect', side_effect=connect) as opened:
            self.assertIn('stored test message', self.memory.note(self.origin, 'current'))
        self.assertEqual(opened.call_count, 1)
        verbs = {sql.lstrip().split()[0].upper() for sql in statements}
        self.assertFalse(verbs & {'BEGIN', 'UPDATE', 'INSERT', 'DELETE', 'REPLACE'})

    def test_pending_send_is_adopted_once_and_reset_stays_isolated(self):
        self.memory.record(self.origin, None, 'before first conversation', 'fixed')
        self.assertIn('before first conversation', self.memory.note(self.origin, 'first'))
        self.assertEqual(self.memory.note(self.origin, 'after-reset'), '')
        self.memory.record(self.origin, None, 'after reset pending send', 'discretionary')
        self.assertIn('after reset pending send', self.memory.note(self.origin, 'after-reset'))
        self.assertNotIn('before first conversation', self.memory.note(self.origin, 'after-reset'))
        self.assertNotIn('after reset pending send', self.memory.note(self.origin, 'first'))
        reloaded = SentMemory(self.path, clock=lambda: self.now)
        self.assertIn('before first conversation', reloaded.note(self.origin, 'first'))
        self.assertEqual(reloaded.note(self.origin, 'third'), '')

    def test_empty_read_does_not_cache_away_a_later_pending_send(self):
        self.assertEqual(self.memory.note(self.origin, 'current'), '')
        self.memory.record(self.origin, None, 'later confirmed send', 'fixed')
        self.assertIn('later confirmed send', self.memory.note(self.origin, 'current'))
        self.assertEqual(self.memory.note(self.origin, 'another'), '')

    def test_private_group_and_platform_scopes_do_not_share_adoptions(self):
        origins = [self.origin, 'test:FriendMessage:bob', 'test:GroupMessage:alice',
                   'test:GroupMessage:member_alice', 'other:FriendMessage:alice']
        for index, origin in enumerate(origins):
            self.memory.record(origin, None, 'only-scope-' + str(index), 'fixed')
        for index, origin in enumerate(origins):
            rows = self.memory.recent(origin, 'same-conversation-id')
            self.assertEqual([r['text'] for r in rows], ['only-scope-' + str(index)])

    def test_none_and_empty_conversation_ids_do_not_claim_pending_rows(self):
        self.memory.record(self.origin, None, 'pending', 'fixed')
        self.memory.record(self.origin, '', 'empty id', 'manual')
        self.assertEqual([r['text'] for r in self.memory.recent(self.origin, None)], ['pending'])
        self.assertEqual([r['text'] for r in self.memory.recent(self.origin, '')], ['empty id'])
        self.assertEqual([r['text'] for r in self.memory.recent(self.origin, 'first')], ['pending'])
        self.assertEqual([r['text'] for r in self.memory.recent(self.origin, '')], ['empty id'])

    def test_concurrent_claims_have_one_owner_and_never_move_scoped_history(self):
        for mode in ('DELETE', 'WAL'):
            with self.subTest(journal=mode):
                self.journal_mode(mode)
                self.assert_single_claim_owner(self.origin + '-' + mode)

    def assert_single_claim_owner(self, origin):
        self.memory.record(origin, None, 'pending', 'fixed')
        self.memory.record(origin, 'first', 'already first', 'manual')
        self.memory.record(origin, 'second', 'already second', 'manual')
        other = SentMemory(self.path, clock=lambda: self.now)
        barrier = threading.Barrier(2, timeout=5)

        class RacingConnection(sqlite3.Connection):
            def execute(db, sql, parameters=()):
                if sql.lstrip().upper().startswith('UPDATE SENT SET CID='):
                    # Both calls have decided to adopt before either writes.
                    barrier.wait()
                return super().execute(sql, parameters)

        def connect(*args, **kwargs):
            return REAL_CONNECT(*args, factory=RacingConnection, **kwargs)

        with patch.object(SUBJECT.sqlite3, 'connect', side_effect=connect):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(self.memory.recent, origin, 'first')
                second = pool.submit(other.recent, origin, 'second')
                results = {'first': first.result(timeout=10), 'second': second.result(timeout=10)}
        owners = [cid for cid, rows in results.items() if any(r['text'] == 'pending' for r in rows)]
        self.assertEqual(len(owners), 1)
        for cid, rows in results.items():
            self.assertIn('already ' + cid, [r['text'] for r in rows])
            self.assertNotIn('already ' + ('second' if cid == 'first' else 'first'),
                             [r['text'] for r in rows])
        with closing(REAL_CONNECT(self.path)) as db:
            self.assertEqual(db.execute('SELECT cid FROM sent WHERE origin=? AND text=?',
                                        (origin, 'pending')).fetchone(),
                             (owners[0],))
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone(), ('ok',))

    def test_required_adoption_waits_for_the_writer_instead_of_skipping_memory(self):
        for mode in ('DELETE', 'WAL'):
            with self.subTest(journal=mode):
                self.journal_mode(mode)
                origin = self.origin + '-' + mode
                self.memory.record(origin, None, 'must still adopt', 'fixed')
                started = threading.Event()

                class WaitingConnection(sqlite3.Connection):
                    def execute(db, sql, parameters=()):
                        if sql.lstrip().upper().startswith('UPDATE SENT SET CID='):
                            started.set()
                        return super().execute(sql, parameters)

                def connect(*args, **kwargs):
                    return REAL_CONNECT(*args, factory=WaitingConnection, **kwargs)

                with closing(REAL_CONNECT(self.path)) as writer:
                    writer.execute('BEGIN IMMEDIATE')
                    with patch.object(SUBJECT.sqlite3, 'connect', side_effect=connect):
                        with ThreadPoolExecutor(max_workers=1) as pool:
                            reading = pool.submit(self.memory.recent, origin, 'current')
                            try:
                                self.assertTrue(started.wait(timeout=2))
                                self.assertFalse(reading.done())
                            finally:
                                writer.rollback()
                            self.assertEqual([r['text'] for r in reading.result(timeout=5)],
                                             ['must still adopt'])
                self.assertEqual(self.memory.note(origin, 'another'), '')

    def test_delete_between_probe_and_claim_cannot_resurrect_a_message(self):
        self.memory.record(self.origin, None, 'removed before adoption', 'fixed')
        path, origin = self.path, self.origin
        deleted = []

        class DeletingConnection(sqlite3.Connection):
            def execute(db, sql, parameters=()):
                if sql.lstrip().upper().startswith('UPDATE SENT SET CID=') and not deleted:
                    with closing(REAL_CONNECT(path, timeout=0.2)) as other:
                        with other:
                            other.execute('DELETE FROM sent WHERE origin=?', (origin,))
                    deleted.append(True)
                return super().execute(sql, parameters)

        def connect(*args, **kwargs):
            return REAL_CONNECT(*args, factory=DeletingConnection, **kwargs)

        with patch.object(SUBJECT.sqlite3, 'connect', side_effect=connect):
            self.assertEqual(self.memory.note(self.origin, 'current'), '')
        self.assertTrue(deleted)
        self.assertEqual(self.memory.note(self.origin, 'another'), '')

    def test_deleted_history_is_not_served_from_a_cache(self):
        self.memory.record(self.origin, 'current', 'delete this fixture', 'manual')
        self.assertIn('delete this fixture', self.memory.note(self.origin, 'current'))
        with closing(REAL_CONNECT(self.path)) as db:
            with db:
                db.execute('DELETE FROM sent WHERE origin=?', (self.origin,))
        self.assertEqual(self.memory.note(self.origin, 'current'), '')

    def test_note_retains_full_fields_order_and_existing_eight_message_window(self):
        expected = []
        for index in range(10):
            self.now += 1
            text = 'fixture-' + str(index) + '\n' + '龙' * 800
            kind = 'manual' if index % 2 else 'fixed'
            self.memory.record(self.origin, 'current', text, kind)
            expected.append({'time': datetime.fromtimestamp(self.now).isoformat(timespec='seconds'),
                             'role': 'assistant', 'text': text, 'kind': kind})
        note = self.memory.note(self.origin, 'current')
        self.assertEqual(json.loads(note[note.index('[{'):]), expected[-8:])
        self.now += 7 * 86400 + 1
        self.assertEqual(self.memory.note(self.origin, 'current'), '')

    def test_record_retention_and_proactive_history_remain_recipient_scoped(self):
        self.memory.record(self.origin, 'old', 'expired', 'fixed')
        self.now += 7 * 86400 + 1
        self.memory.record('test:FriendMessage:bob', 'same', 'other recipient', 'fixed')
        for index in range(35):
            self.memory.record(self.origin, 'old' if index % 2 else 'new',
                               'fixture-' + str(index), 'fixed')
        self.memory.record(self.origin, 'new', 'manual excluded from proactive history', 'manual')
        with closing(REAL_CONNECT(self.path)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM sent WHERE origin=?',
                                        (self.origin,)).fetchone(), (30,))
            self.assertEqual(db.execute('SELECT count(*) FROM sent WHERE text=?',
                                        ('expired',)).fetchone(), (0,))
        self.assertEqual(self.memory.proactive_recent(self.origin),
                         ['fixture-' + str(i) for i in range(11, 35)])
        self.assertEqual(self.memory.proactive_recent('test:FriendMessage:bob'), ['other recipient'])


if __name__ == '__main__':
    unittest.main()
