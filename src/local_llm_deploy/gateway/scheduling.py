"""Atomic lane + model reservations owned by a request until upstream finishes."""
from __future__ import annotations

from dataclasses import dataclass
import json
import threading
import time


class QueueFull(Exception):
    pass


class BackendUncertain(Exception):
    pass


def _content_chars(value):
    if isinstance(value, str):
        return len(value)
    if isinstance(value, list):
        return sum(_content_chars(v) for v in value)
    if isinstance(value, dict):
        return sum(_content_chars(value.get(k)) for k in ('text', 'content', 'reasoning_content'))
    return 0


def estimate_kv_tokens(body, params, chars_per_token=2.5):
    try:
        data = json.loads(body or b'{}')
    except (ValueError, UnicodeDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    cap = next((data[k] for k in ('max_tokens', 'max_output_tokens', 'max_completion_tokens') if data.get(k) is not None), params.get('n_predict', 32768))
    if type(cap) is not int or cap <= 0:
        raise ValueError('Generation token limit must be a positive integer')
    characters = sum(_content_chars(data.get(k)) for k in ('messages', 'input', 'prompt', 'instructions', 'system'))
    return int(characters / chars_per_token) + cap


@dataclass
class Ticket:
    scheduler: object
    identifier: int
    model: str
    lane: str
    tokens: int
    priority: int
    state: str = 'queued'

    def acquire(self, timeout=0):
        return self.scheduler.acquire(self, timeout)

    def release(self):
        self.scheduler.release(self)

    def quarantine(self, reason):
        self.scheduler.quarantine(self, reason)


class Scheduler:
    """One process owns this scheduler; no lock is held across network I/O.

    Admission counts include active requests, preserving MAX_QUEUE_DEPTH's
    historical meaning. The monitor also exposes the actual waiting count.
    """
    def __init__(self, settings, *, clock=time.monotonic):
        self.settings, self.clock = settings, clock
        self.condition = threading.Condition()
        self.sequence = 0
        self.tickets = {}
        self.models = {}
        self.uncertain = {}
        self.limits = {'chat': settings.chat_concurrent, 'embed': settings.embed_concurrent,
                       'rerank': settings.rerank_concurrent, 'asr': settings.asr_concurrent}

    def register_models(self, specs):
        """Freeze existing budgets, adding new model limits only on first sight."""
        with self.condition:
            for key, spec in specs.items():
                cfg = spec.params
                self.models.setdefault(key, {
                    'total': int(cfg.get('ctx_size', 131072) * cfg.get('kv_budget_ratio', .9)),
                    'max_slots': max(1, cfg.get('max_concurrent', 1)),
                })

    def submit(self, model, capability, tokens=0, *, priority=0, params=None):
        lane = 'embed' if capability == 'embedding' else capability
        with self.condition:
            if model in self.uncertain:
                raise BackendUncertain(self.uncertain[model])
            scope = [t for t in self.tickets.values() if (t.model == model if lane == 'chat' else t.lane == lane)]
            if len(scope) >= self.settings.max_queue_depth:
                raise QueueFull('Inference queue is full; retry later')
            if model not in self.models:
                cfg = params or {}
                self.models[model] = {'total': int(cfg.get('ctx_size', 131072) * cfg.get('kv_budget_ratio', .9)),
                                      'max_slots': max(1, cfg.get('max_concurrent', 1))}
            self.sequence += 1
            ticket = Ticket(self, self.sequence, model, lane, tokens, priority)
            self.tickets[ticket.identifier] = ticket
            return ticket

    def _eligible(self, ticket):
        if ticket.model in self.uncertain:
            return False
        active = [t for t in self.tickets.values() if t.state in ('running', 'uncertain')]
        if sum(t.lane == ticket.lane for t in active) >= self.limits[ticket.lane]:
            return False
        same_model = [t for t in active if t.model == ticket.model]
        if ticket.lane == 'chat':
            budget = self.models[ticket.model]
            if len(same_model) >= budget['max_slots']:
                return False
            # The first request may exceed the estimate, as in the legacy gate.
            if same_model and sum(t.tokens for t in same_model) + ticket.tokens > budget['total']:
                return False
        return True

    def acquire(self, ticket, timeout=0):
        deadline = None if timeout is None else self.clock() + timeout
        with self.condition:
            while ticket.state == 'queued':
                if ticket.model in self.uncertain:
                    raise BackendUncertain(self.uncertain[ticket.model])
                eligible = [t for t in self.tickets.values() if t.state == 'queued' and t.lane == ticket.lane and self._eligible(t)]
                best = max(eligible, key=lambda t: (t.priority, -t.identifier), default=None)
                if best is ticket:
                    ticket.state = 'running'
                    return True
                remaining = None if deadline is None else deadline - self.clock()
                if remaining is not None and remaining <= 0:
                    return False
                self.condition.wait(remaining)
            return ticket.state == 'running'

    def release(self, ticket):
        with self.condition:
            if ticket.state == 'uncertain':
                # A transport error is not evidence that model computation ended.
                return
            if ticket.state != 'released':
                self.tickets.pop(ticket.identifier, None)
                ticket.state = 'released'
                self.condition.notify_all()

    def quarantine(self, ticket, reason):
        with self.condition:
            if ticket.state == 'released':
                return
            self.uncertain[ticket.model] = reason
            ticket.state = 'uncertain'
            self.condition.notify_all()

    def recover_model(self, model, *, confirmed_idle=False):
        """For lifecycle integration after independently confirming backend idle.

        No unauthenticated HTTP endpoint can clear an uncertain reservation.
        Restarting the backend and then the gateway is also a recovery path.
        """
        if not confirmed_idle:
            raise ValueError('Backend idle must be independently confirmed')
        with self.condition:
            self.uncertain.pop(model, None)
            for ticket in list(self.tickets.values()):
                if ticket.model == model and ticket.state == 'uncertain':
                    ticket.state = 'released'
                    self.tickets.pop(ticket.identifier)
            self.condition.notify_all()

    def snapshots(self):
        with self.condition:
            lanes = {}
            for lane, limit in self.limits.items():
                scope = [t for t in self.tickets.values() if t.lane == lane]
                lanes[lane] = {'active': sum(t.state in ('running', 'uncertain') for t in scope),
                               'max': limit, 'waiting': sum(t.state == 'queued' for t in scope),
                               'queue_depth': len(scope)}
            budgets = {}
            for key, limits in self.models.items():
                scope = [t for t in self.tickets.values() if t.model == key]
                active = [t for t in scope if t.state in ('running', 'uncertain')]
                budgets[key] = {**limits, 'used': sum(t.tokens for t in active),
                                'active_slots': len(active), 'queue_depth': len(scope),
                                'waiting': sum(t.state == 'queued' for t in scope)}
                if key in self.uncertain:
                    budgets[key]['uncertain'] = True
                    budgets[key]['recovery'] = 'Confirm backend idle, or restart backend then gateway'
            return lanes, budgets
