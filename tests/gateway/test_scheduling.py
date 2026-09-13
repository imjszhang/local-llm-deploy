"""Resource invariants, priority and recovery without inference dependencies."""
from __future__ import annotations

import unittest
from local_llm_deploy.gateway.scheduling import Scheduler, QueueFull, BackendUncertain, estimate_kv_tokens
from local_llm_deploy.gateway.settings import GatewaySettings


class SchedulingTests(unittest.TestCase):
    def test_lanes_do_not_block_auxiliary_work(self):
        scheduler = Scheduler(GatewaySettings())
        chat = scheduler.submit('chat', 'chat', 50)
        other = scheduler.submit('other', 'chat', 50)
        embed = scheduler.submit('embed', 'embedding')
        rerank = scheduler.submit('rerank', 'rerank')
        self.assertTrue(chat.acquire())
        self.assertFalse(other.acquire())
        self.assertTrue(embed.acquire())
        self.assertTrue(rerank.acquire())
        chat.release()
        self.assertTrue(other.acquire())
        for ticket in (chat, other, embed, rerank):
            ticket.release()
        self.assertTrue(all(v['active'] == v['queue_depth'] == 0 for v in scheduler.snapshots()[0].values()))

    def test_lane_and_budget_acquired_atomically(self):
        scheduler = Scheduler(GatewaySettings(chat_concurrent=2))
        params = {'ctx_size': 100, 'kv_budget_ratio': 1, 'max_concurrent': 2}
        running = scheduler.submit('a', 'chat', 80, params=params)
        self.assertTrue(running.acquire())
        blocked = scheduler.submit('a', 'chat', 30, priority=1)
        unrelated = scheduler.submit('b', 'chat', 20)
        self.assertFalse(blocked.acquire())
        self.assertTrue(unrelated.acquire())
        self.assertEqual(scheduler.snapshots()[0]['chat']['active'], 2)
        running.release()
        self.assertTrue(blocked.acquire())

    def test_stream_priority_and_fifo_survive_keepalive_polling(self):
        scheduler = Scheduler(GatewaySettings())
        running = scheduler.submit('a', 'chat', 1)
        running.acquire()
        batch = scheduler.submit('a', 'chat', 1)
        stream1 = scheduler.submit('a', 'chat', 1, priority=1)
        stream2 = scheduler.submit('a', 'chat', 1, priority=1)
        for _ in range(3):
            self.assertFalse(stream1.acquire())
        running.release()
        self.assertFalse(batch.acquire())
        self.assertFalse(stream2.acquire())
        self.assertTrue(stream1.acquire())
        stream1.release()
        self.assertTrue(stream2.acquire())
        stream2.release()
        self.assertTrue(batch.acquire())

    def test_admission_limit_and_idempotent_release(self):
        scheduler = Scheduler(GatewaySettings(max_queue_depth=2))
        first = scheduler.submit('a', 'chat', 5)
        second = scheduler.submit('a', 'chat', 5)
        first.acquire()
        with self.assertRaises(QueueFull):
            scheduler.submit('a', 'chat', 5)
        first.release()
        first.release()
        second.release()
        lanes, models = scheduler.snapshots()
        self.assertEqual(lanes['chat']['active'], 0)
        self.assertEqual(models['a']['used'], 0)
        self.assertEqual(models['a']['queue_depth'], 0)

    def test_uncertain_backend_retains_global_capacity_until_confirmed(self):
        scheduler = Scheduler(GatewaySettings())
        ticket = scheduler.submit('a', 'chat', 10)
        ticket.acquire()
        ticket.quarantine('transport ended before backend completion')
        ticket.release()
        self.assertEqual(scheduler.snapshots()[0]['chat']['active'], 1)
        with self.assertRaises(BackendUncertain):
            scheduler.submit('a', 'chat', 5)
        other = scheduler.submit('b', 'chat', 5)
        self.assertFalse(other.acquire())
        with self.assertRaises(ValueError):
            scheduler.recover_model('a')
        scheduler.recover_model('a', confirmed_idle=True)
        self.assertTrue(other.acquire())
        other.release()
        self.assertEqual(scheduler.snapshots()[0]['chat']['active'], 0)

    def test_large_first_estimate_does_not_deadlock(self):
        scheduler = Scheduler(GatewaySettings())
        ticket = scheduler.submit('a', 'chat', 1000, params={'ctx_size': 100})
        self.assertTrue(ticket.acquire())
        ticket.release()

    def test_estimates_support_protocols_and_reject_bad_limits(self):
        self.assertEqual(estimate_kv_tokens(b'{"input":"abcd","max_output_tokens":4}', {}, 2), 6)
        self.assertEqual(estimate_kv_tokens(b'{"messages":[{"content":[{"text":"abcd"}]}],"max_tokens":4}', {}, 2), 6)
        for body in (b'{"max_tokens":"5"}', b'{"max_tokens":-1}', b'{"max_tokens":true}'):
            with self.assertRaises(ValueError):
                estimate_kv_tokens(body, {})


if __name__ == '__main__':
    unittest.main()
