"""Bounded structured request logs. Credentials are never included."""
from __future__ import annotations

import json
import logging
import threading
import time
from urllib.parse import urlsplit

log = logging.getLogger('local_llm_deploy')


class BoundedCapture:
    def __init__(self, limit):
        self.limit = limit
        self.data = bytearray()
        self.truncated = False

    def append(self, chunk):
        available = max(0, self.limit - len(self.data))
        self.data.extend(chunk[:available])
        self.truncated |= len(chunk) > available


class AccessLogger:
    def __init__(self, path=None, *, capture_bytes=65536, log_body=False):
        self.path = path
        self.capture_bytes = capture_bytes
        self.log_body = log_body
        self.lock = threading.Lock()

    def write(self, *, request_id, path, method, model, capability, elapsed, status, body=None, response=None, outcome=None):
        if not self.path:
            return
        record = {'ts': time.time(), 'request_id': request_id, 'path': urlsplit(path).path,
                  'method': method, 'model_name': model, 'kind': capability,
                  'elapsed_sec': round(elapsed, 4), 'status': status, 'outcome': outcome}
        # Payload logging remains explicit and bounded. Never record multipart audio.
        if self.log_body and body and capability != 'asr':
            record['body'] = body[:self.capture_bytes].decode('utf-8', errors='replace')
            record['body_truncated'] = len(body) > self.capture_bytes
        if self.log_body and response is not None:
            record['response_body'] = bytes(response.data).decode('utf-8', errors='replace')
            record['response_truncated'] = response.truncated
        try:
            with self.lock, open(self.path, 'a', encoding='utf-8') as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + '\n')
        except OSError:
            log.warning('Cannot write gateway access log')
