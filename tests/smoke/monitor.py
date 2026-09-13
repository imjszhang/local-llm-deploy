"""Explicit read-only smoke of a candidate gateway against existing local services.

Only metadata GET/HEAD requests are issued. The candidate binds an ephemeral
loopback port; managed services and the existing gateway are never restarted.
Reports contain only check names, model names, states and counts.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
import http.client
import json
import math
from pathlib import Path
import threading
import time
from urllib.parse import quote

from local_llm_deploy.config import ProjectPaths, load_specs
from local_llm_deploy.gateway.app import GatewayContext, create_server
from local_llm_deploy.gateway.auth import ApiAuth
from local_llm_deploy.gateway.settings import GatewaySettings
from local_llm_deploy.lifecycle.observe import observe_instances, same_identity


@dataclass(frozen=True)
class CandidatePaths(ProjectPaths):
    static_override: Path | None = None

    @property
    def static(self):
        return self.static_override or super().static


def verify_json(value):
    """Reject non-finite readings and accidentally exposed internal payloads."""
    if isinstance(value, dict):
        forbidden = {'generated', 'reasoning', 'api_key', 'api_key_file', 'argv', 'env', 'command', 'model_path'}
        if forbidden.intersection(value):
            raise AssertionError('unexpected_private_fields')
        for item in value.values():
            verify_json(item)
    elif isinstance(value, list):
        for item in value:
            verify_json(item)
    elif isinstance(value, float) and not math.isfinite(value):
        raise AssertionError('non_finite_reading')


def verify_source(source):
    if not isinstance(source, dict) or type(source.get('stale')) is not bool:
        raise AssertionError('invalid_source_state')
    for field in ('last_attempt_at', 'last_success_at'):
        value = source.get(field)
        if value is not None and (type(value) not in (int, float) or value < 0):
            raise AssertionError('invalid_source_timestamp')
    threshold = source.get('stale_after_ms')
    if type(threshold) not in (int, float) or threshold < 0:
        raise AssertionError('invalid_source_threshold')
    if source.get('error') is not None and not isinstance(source['error'], dict):
        raise AssertionError('invalid_source_error')


def settled(source):
    return source.get('last_success_at') is not None or source.get('error') is not None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', required=True)
    parser.add_argument('--project-root', type=Path, required=True)
    parser.add_argument('--static-dir', type=Path, help='Optional staged static publication; runtime observations still use project-root')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args(argv)
    paths = CandidatePaths(args.project_root, args.static_dir.resolve() if args.static_dir else None)
    report = {'status': 'failed', 'checks': [], 'models': []}
    context = server = thread = None
    before = {}
    current_check = 'initialize_candidate'
    def check(name, valid, **counts):
        report['checks'].append({'name': name, 'status': 'passed' if valid else 'failed', **counts})
        print(f"[{'OK' if valid else 'FAIL'}] {name}", flush=True)
        if not valid:
            raise AssertionError(name)

    try:
        specs = load_specs(paths.registry)
        observed = observe_instances(paths, specs)
        before = {key: item.identity for key, item in observed.items() if item.identity is not None}
        check('existing_gateway_identity_observed', 'serve-ui' in before, observed_process_count=len(before))
        settings = replace(GatewaySettings.from_env(), access_log=None, log_body=False)
        context = GatewayContext(paths, settings=settings, specs=specs)
        server = create_server(context, host='127.0.0.1', port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        # Credentials stay in this function's memory; neither headers nor bodies
        # nor raw exception messages are logged or included in the report.
        credential = ApiAuth(paths.api_key).key()
        def request(path, *, method='GET', protected=True):
            permitted = path == '/monitor-api/v1/snapshot' or path.startswith('/monitor-api/v1/models/')
            if protected and (not permitted or '?' in path):
                raise AssertionError('request_not_read_only_metadata')
            headers = {'Authorization': 'Bearer ' + credential} if protected and credential else {}
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            try:
                connection.request(method, path, headers=headers)
                response = connection.getresponse()
                body = response.read(2 * 1024 * 1024 + 1)
                if response.status != 200 or len(body) > 2 * 1024 * 1024:
                    raise AssertionError('candidate_http_failure')
                if protected:
                    if credential and credential.encode() in body:
                        raise AssertionError('credential_exposed')
                    value = json.loads(body)
                    verify_json(value)
                    return value
                return body
            finally:
                connection.close()

        current_check = 'candidate_static_get_head'
        html = request('/monitor.html', protected=False)
        check(current_check, b'id="app"' in html and b'/monitor-assets/' in html)
        check('candidate_static_head', request('/monitor.html', method='HEAD', protected=False) == b'')
        current_check = 'initial_monitor_snapshot'
        snapshot = request('/monitor-api/v1/snapshot')
        deadline = time.monotonic() + 40
        while not all(settled(source) for source in snapshot['sources'].values()):
            if time.monotonic() >= deadline:
                raise AssertionError('snapshot_collection_deadline')
            time.sleep(.2)
            snapshot = request('/monitor-api/v1/snapshot')
        check('snapshot_version_and_sources', snapshot.get('schema_version') == 1 and set(snapshot['sources']) == {'system', 'catalog', 'discovery'})
        for source in snapshot['sources'].values():
            verify_source(source)
        registered = {row['key'] for row in snapshot['models'] if row['registered']}
        keys = [row['key'] for row in snapshot['models']]
        check('complete_model_catalog', registered == set(specs) and len(set(keys)) == len(keys),
              registered_count=len(registered), discovered_count=len(keys) - len(registered), total_count=len(keys))
        system = snapshot['system']
        valid_missing = system is None and snapshot['sources']['system']['error'] is not None
        valid_system = isinstance(system, dict) and {'cpu', 'memory', 'load_avg'} <= set(system)
        check('system_readings_or_explicit_missing', valid_missing or valid_system)
        report['system_status'] = 'unavailable' if valid_missing else ('stale' if snapshot['sources']['system']['stale'] else 'ready')
        check('candidate_has_no_inference_tickets', all(lane['active'] == lane['waiting'] == lane['queue_depth'] == 0 for lane in snapshot['lanes'].values()))
        current_check = 'basic_model_details'
        pending = {row['key']: row for row in snapshot['models']}
        details = {}
        deadline = time.monotonic() + max(45, min(120, 8 * len(pending)))
        while pending:
            for key in list(pending):
                detail = request('/monitor-api/v1/models/' + quote(key, safe=''))
                verify_source(detail['source'])
                if detail.get('schema_version') != 1 or detail.get('key') != key:
                    raise AssertionError('invalid_model_detail_identity')
                states = {section: detail[section]['state'] for section in ('health', 'metrics', 'slots', 'process', 'ollama')}
                allowed = {'loading', 'ready', 'stale', 'unauthorized', 'unsupported', 'error'}
                if not set(states.values()) <= allowed:
                    raise AssertionError('invalid_detail_section_state')
                if not settled(detail['source']) or 'loading' in states.values():
                    continue
                row = pending.pop(key)
                for feature in ('metrics', 'slots'):
                    if not row['monitoring_support'][feature] and states[feature] != 'unsupported':
                        raise AssertionError('unsupported_feature_was_probed')
                details[key] = states
                report['models'].append({'name': key, 'backend': row['backend'],
                    'lifecycle': row['lifecycle']['state'], 'availability': row['availability']['state'],
                    'routing': 'ready' if row['routing']['available'] is True else 'unavailable' if row['routing']['available'] is False else 'unknown',
                    'detail_states': states})
            if pending:
                if time.monotonic() >= deadline:
                    raise AssertionError('detail_collection_deadline')
                time.sleep(.25)
        check('all_models_basic_details_validated', len(details) == len(keys), detail_count=len(details))
        check('advanced_output_not_requested_or_returned', True)
        report['status'] = 'passed'
    except Exception as error:
        # Only class and the current check name are reported; exceptions can
        # otherwise contain registry paths, backend bodies or inline credentials.
        report['status'] = 'failed'
        report['checks'].append({'name': current_check, 'status': 'failed', 'error_kind': type(error).__name__})
        print(f'[FAIL] {current_check} ({type(error).__name__})', flush=True)
    finally:
        if server:
            server.shutdown()
            server.server_close()
        if context:
            context.monitor_api.close()
            for worker in context.monitor_api.threads:
                worker.join(timeout=3)
        if thread:
            thread.join(timeout=2)
        if before:
            unchanged = all(same_identity(identity) for identity in before.values())
            report['checks'].append({'name': 'original_process_identities_unchanged',
                                     'status': 'passed' if unchanged else 'failed', 'process_count': len(before)})
            print(f"[{'OK' if unchanged else 'FAIL'}] original_process_identities_unchanged", flush=True)
            if not unchanged:
                report['status'] = 'failed'
        report['models'].sort(key=lambda row: row['name'])
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
