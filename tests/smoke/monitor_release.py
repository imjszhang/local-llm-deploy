"""Opt-in old -> candidate -> old monitor release rehearsal using isolated gateways.

Only source files and static assets are copied. Every registry, credential,
backend and gateway used by this rehearsal is a temporary fixture; no active
static directory, model process, LaunchAgent or deployment configuration changes.
"""
from __future__ import annotations

import argparse
import hashlib
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time


FIXTURE_KEY = 'monitor-release-fixture-only'
FIXTURE_MODEL = 'release-fixture'


class Assets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        path = attrs.get('src') if tag == 'script' else attrs.get('href') if tag == 'link' and attrs.get('rel') in ('stylesheet', 'modulepreload') else None
        if path:
            if not path.startswith('/') or path.startswith('//') or '..' in PurePosixPath(path).parts:
                raise ValueError('Rehearsal only accepts local static asset references')
            self.paths.append(path)


class BackendFixture(BaseHTTPRequestHandler):
    def respond(self, value):
        raw = json.dumps(value).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == '/api/version':
            self.respond({'version': 'release-fixture'})
        elif self.path in ('/api/tags', '/api/ps'):
            self.respond({'models': []})
        elif self.path == '/v1/models':
            self.respond({'object': 'list', 'data': [{'id': FIXTURE_MODEL, 'object': 'model'}]})
        else:
            self.respond({'status': 'ok'})

    def do_POST(self):
        value = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
        self.respond({'id': 'fixture-request', 'object': 'chat.completion', 'model': value.get('model'),
                      'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'release fixture response'},
                                   'finish_reason': 'stop'}]})

    def log_message(self, *args):
        pass


# -I -S prevents user site packages, editable installations and startup hooks
# from choosing a different copy of the gateway than the staged source.
CHILD = r'''
import json, signal, sys
from pathlib import Path
root, control, backend_port, gateway_port = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve(), int(sys.argv[3]), int(sys.argv[4])
sys.path.insert(0, str(root / 'src'))
import local_llm_deploy
assert (root / 'src') in Path(local_llm_deploy.__file__).resolve().parents
from local_llm_deploy.config import ProjectPaths, load_specs
from local_llm_deploy.gateway.app import GatewayContext, create_server
from local_llm_deploy.gateway.discovery import Discovery
from local_llm_deploy.gateway.monitoring import Monitoring
from local_llm_deploy.gateway.scheduling import Scheduler
from local_llm_deploy.gateway.settings import GatewaySettings
paths = ProjectPaths(root)
settings = GatewaySettings(ollama_host='http://127.0.0.1:' + str(backend_port), ollama_auto_discover=False,
                           knowledge_url='http://127.0.0.1:' + str(backend_port) + '/knowledge',
                           access_log=None, log_body=False)
specs = load_specs(paths.registry)
discovery = Discovery(paths, specs, settings, observe=lambda *args: {})
scheduler = Scheduler(settings)
system = {'cpu': {'user': 0, 'sys': 2, 'idle': 98},
          'memory': {'total_gb': 64, 'used_gb': 12, 'free_gb': 52, 'wired_gb': 2}, 'load_avg': [1, 1, 1]}
monitoring = Monitoring(discovery, scheduler, settings, collector=lambda running: dict(system))
context = GatewayContext(paths, specs=specs, discovery=discovery, settings=settings,
                         scheduler=scheduler, monitoring=monitoring)
if hasattr(context, 'monitor_api'):
    from local_llm_deploy.gateway.monitor_api import MonitorAPI
    context.monitor_api.close()
    context.monitor_api = MonitorAPI(context, system_collector=lambda: dict(system))
server = create_server(context, host='127.0.0.1', port=gateway_port)
control.write_text(json.dumps({'port': server.server_port, 'module': str(Path(local_llm_deploy.__file__).resolve()),
                               'python': sys.version.split()[0]}))
def stop(signum, frame):
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
try:
    server.serve_forever(poll_interval=.05)
finally:
    server.server_close()
'''


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def source_digest(root):
    entries = [{'path': str(path.relative_to(root)), 'sha256': digest(path.read_bytes())}
               for path in sorted((root / 'src').rglob('*.py'))]
    return {'files': len(entries), 'sha256': digest(json.dumps(entries, sort_keys=True).encode())}


def copy_tree(source, destination):
    if source.is_symlink() or not source.is_dir():
        raise ValueError('Expected a real source directory')
    for path in source.rglob('*'):
        if '__pycache__' in path.parts or path.suffix == '.pyc':
            continue
        if path.is_symlink():
            raise ValueError('Source and static symlinks are not accepted in release rehearsal')
        target = destination / path.relative_to(source)
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


def archive_previous(source, revision, destination):
    raw = subprocess.check_output(['git', '-C', str(source), 'archive', '--format=tar', revision, 'src', 'static'])
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:') as archive:
        for member in archive.getmembers():
            relative = PurePosixPath(member.name)
            if relative.is_absolute() or '..' in relative.parts or relative.parts[0] not in ('src', 'static'):
                raise ValueError('Archive contains an unexpected path')
            target = destination.joinpath(*relative.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as stream:
                    target.write_bytes(stream.read())
            else:
                raise ValueError('Archive links and special files are not accepted')


def request(port, path, method='GET', body=None, *, authorized=True):
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
    try:
        headers = {'Authorization': 'Bearer ' + FIXTURE_KEY} if authorized else {}
        if body is not None:
            body = json.dumps(body)
            headers['Content-Type'] = 'application/json'
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


def port_is_closed(port):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=.25):
            return False
    except OSError:
        return True


def run_stage(name, source, active, temporary, backend_port, port, candidate):
    if active.exists():
        shutil.rmtree(active)
    active.mkdir()
    copy_tree(source / 'src', active / 'src')
    copy_tree(source / 'static', active / 'static')
    (active / 'models.json').write_text(json.dumps({FIXTURE_MODEL: {
        'external_backend': True, 'alias': FIXTURE_MODEL, 'default_port': backend_port,
        'host': '127.0.0.1', 'capabilities': ['chat'],
    }}))
    (active / '.api-key').write_text(FIXTURE_KEY + '\n')
    control, log = temporary / (name + '.json'), temporary / (name + '.log')
    env = {'PATH': os.defpath, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1',
           'TMPDIR': str(temporary)}
    result = {'name': name, 'source': source_digest(active), 'assets': [], 'checks': [], 'cleanup': {}}
    process = None
    actual_port = port
    try:
        with log.open('w', encoding='utf-8') as stream:
            process = subprocess.Popen([sys.executable, '-I', '-S', '-c', CHILD, str(active), str(control),
                                        str(backend_port), str(port)], cwd=active, env=env, stdout=stream, stderr=stream)
            result['pid'] = process.pid
            deadline = time.monotonic() + 10
            while not control.is_file():
                if process.poll() is not None:
                    raise RuntimeError('Isolated gateway exited during startup: ' + log.read_text())
                if time.monotonic() >= deadline:
                    raise RuntimeError('Isolated gateway startup timed out')
                time.sleep(.02)
            runtime = json.loads(control.read_text())
            actual_port = runtime['port']
            result.update(runtime=runtime, port=actual_port)

            def check(condition, label):
                result['checks'].append({'name': label, 'passed': bool(condition)})
                if not condition:
                    raise AssertionError(name + ': ' + label)

            status, headers, html = request(actual_port, '/monitor.html', authorized=False)
            check(status == 200 and html == (active / 'static/monitor.html').read_bytes(), 'served matching HTML')
            result['html_sha256'] = digest(html)
            check((b'id="app"' in html) == candidate, 'expected previous or Vue page generation')
            if candidate:
                check(headers.get('Cache-Control') == 'no-cache', 'candidate HTML revalidates')
            parser = Assets()
            parser.feed(html.decode())
            check(bool(parser.paths), 'HTML references local assets')
            manifest = json.loads((active / 'static/monitor-manifest.json').read_text()) if candidate else None
            manifest_files = {entry['path']: entry for entry in manifest['files']} if manifest else {}
            for asset in parser.paths:
                status, headers, content = request(actual_port, asset, authorized=False)
                expected = active / 'static' / asset.lstrip('/')
                check(status == 200 and expected.is_file() and content == expected.read_bytes(), 'matching asset ' + asset)
                if candidate:
                    entry = manifest_files.get(asset.lstrip('/'), {})
                    check(entry.get('sha256') == digest(content), 'manifest asset hash ' + asset)
                    check(headers.get('Cache-Control') == 'public, max-age=31536000, immutable', 'immutable asset ' + asset)
                result['assets'].append({'path': asset, 'sha256': digest(content), 'bytes': len(content), 'status': status})
            check(request(actual_port, '/', authorized=False)[0] == 200, 'root entry retained')
            check(request(actual_port, '/knowledge', authorized=False)[0] == 301, 'knowledge entry retained')
            status, _, raw = request(actual_port, '/api/models', authorized=False)
            public = json.loads(raw)
            check(status == 200 and {'models', 'ollama', 'lanes', 'global'} <= public.keys(), 'legacy public models contract')
            check(any(row['name'] == FIXTURE_MODEL for row in public['models']), 'fixture backend discovered')
            status, _, raw = request(actual_port, '/monitor-api/v1/snapshot')
            result['monitor_api_status'] = status
            if candidate:
                value = json.loads(raw)
                check(status == 200 and value.get('schema_version') == 1, 'candidate monitor API v1')
                deadline = time.monotonic() + 5
                while value['sources']['discovery']['last_success_at'] is None and time.monotonic() < deadline:
                    time.sleep(.03)
                    status, _, raw = request(actual_port, '/monitor-api/v1/snapshot')
                    value = json.loads(raw)
                row = next((row for row in value['models'] if row['key'] == FIXTURE_MODEL), None)
                check(bool(row and row['routing']['available']), 'candidate snapshot confirms fixture route')
                check(value['system']['cpu']['user'] == 0, 'candidate uses isolated system readings')
                check(request(actual_port, '/monitor-api/v1/snapshot', authorized=False)[0] == 401, 'candidate monitor API requires fixture credential')
                result['monitor_schema_version'] = value['schema_version']
            else:
                check(status == 404, 'previous source has no monitor API v1')
                result['monitor_schema_version'] = None
            status, _, raw = request(actual_port, '/v1/chat/completions', 'POST', {
                'model': FIXTURE_MODEL, 'messages': [{'role': 'user', 'content': 'fixture request'}]})
            chat = json.loads(raw)
            check(status == 200 and chat['choices'][0]['message']['content'] == 'release fixture response',
                  'inference routing contract against fixture only')
            result['status'] = 'passed'
    except Exception as exc:
        result['status'], result['error'] = 'failed', str(exc)
    finally:
        if process:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
                    result['cleanup']['forced_kill'] = True
            result['cleanup']['exit_code'] = process.returncode
            result['cleanup']['child_reaped'] = process.poll() is not None
        result['cleanup']['port_closed'] = port_is_closed(actual_port) if actual_port else True
        if (not result['cleanup'].get('child_reaped') or not result['cleanup']['port_closed']
                or result['cleanup'].get('exit_code') != 0 or result['cleanup'].get('forced_kill')):
            result['status'] = 'failed'
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', help='Explicitly launch temporary fixture gateways')
    parser.add_argument('--previous-revision', required=True, help='Previous committed release to archive')
    parser.add_argument('--candidate-static', type=Path, required=True, help='Built candidate publication directory')
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--project-root', type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args(argv)
    if not args.run:
        parser.error('--run is required to start this isolated release rehearsal')
    project, candidate = args.project_root.resolve(), args.candidate_static.resolve()
    if not (candidate / 'monitor-manifest.json').is_file():
        parser.error('--candidate-static must contain monitor-manifest.json')
    previous = subprocess.check_output(['git', '-C', str(project), 'rev-parse', '--verify', '--end-of-options',
                                        args.previous_revision + '^{commit}'], text=True).strip()
    current = subprocess.check_output(['git', '-C', str(project), 'rev-parse', 'HEAD'], text=True).strip()
    report = {'schema_version': 1, 'previous_revision': previous, 'candidate_head': current,
              'candidate_source': 'current working tree (fingerprinted per stage)', 'python': sys.version.split()[0],
              'stages': [], 'status': 'failed', 'cleanup': {}, 'isolation': {
                  'source_selection': 'src and static only; no live registry or credential files copied',
                  'registry': 'temporary fixture', 'credentials': 'temporary fixture',
                  'backend': 'temporary localhost HTTP fixture', 'observations': 'empty injected provider',
                  'system': 'injected fixture', 'node_required': False, 'active_deployment_mutated': False}}
    temporary_path = None
    try:
        with tempfile.TemporaryDirectory(prefix='local-llm-monitor-release-') as temporary_name:
            temporary = temporary_path = Path(temporary_name)
            old, new, active = temporary / 'previous', temporary / 'candidate', temporary / 'active'
            old.mkdir()
            new.mkdir()
            archive_previous(project, previous, old)
            copy_tree(project / 'src', new / 'src')
            copy_tree(candidate, new / 'static')
            if not (new / 'static/index.html').is_file():
                shutil.copy2(old / 'static/index.html', new / 'static/index.html')
            backend = ThreadingHTTPServer(('127.0.0.1', 0), BackendFixture)
            backend_thread = threading.Thread(target=backend.serve_forever, daemon=True)
            backend_thread.start()
            try:
                port = 0
                for name, source, is_candidate in (('previous', old, False), ('candidate', new, True), ('rollback', old, False)):
                    stage = run_stage(name, source, active, temporary, backend.server_port, port, is_candidate)
                    stage['code_revision'] = current if is_candidate else previous
                    report['stages'].append(stage)
                    print(f"[{stage['status'].upper()}] {name}: API {stage.get('monitor_api_status')}, {len(stage['assets'])} verified assets", flush=True)
                    if stage['status'] != 'passed':
                        raise RuntimeError(stage.get('error', 'Stage cleanup failed'))
                    port = stage['port']
                before, _, rollback = report['stages']
                report['rollback_identical'] = (before['source'] == rollback['source'] and
                                                before['html_sha256'] == rollback['html_sha256'] and
                                                before['assets'] == rollback['assets'])
                if not report['rollback_identical']:
                    raise RuntimeError('Rollback did not restore identical source and static resources')
                report['same_gateway_port'] = len({stage['port'] for stage in report['stages']}) == 1
                if not report['same_gateway_port']:
                    raise RuntimeError('Release stages did not reuse the same temporary gateway port')
                report['status'] = 'passed'
            finally:
                backend_port = backend.server_port
                backend.shutdown()
                backend.server_close()
                backend_thread.join(timeout=3)
                report['cleanup']['backend_thread_stopped'] = not backend_thread.is_alive()
                report['cleanup']['backend_port_closed'] = port_is_closed(backend_port)
    except Exception as exc:
        report['status'], report['error'] = 'failed', str(exc)
        print(str(exc), file=sys.stderr)
    finally:
        report['cleanup']['temporary_directory_removed'] = temporary_path is not None and not temporary_path.exists()
        report['cleanup']['all_gateway_children_reaped'] = bool(report['stages']) and all(
            stage['cleanup'].get('child_reaped') for stage in report['stages'])
        if not all(report['cleanup'].values()):
            report['status'] = 'failed'
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print(f'Report: {args.report.resolve()}')
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
