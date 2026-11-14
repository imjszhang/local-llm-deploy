#!/usr/bin/env python3
"""Upload inputs and download MCP Hub assets on any LAN client (stdlib only)."""
from __future__ import annotations

import argparse
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
from urllib.parse import urlencode, urlsplit


def credential():
    filename = os.environ.get('MCP_HUB_TOKEN_FILE')
    value = Path(filename).expanduser().read_text().strip() if filename else os.environ.get('MCP_HUB_TOKEN', '')
    if not value or any(c.isspace() for c in value):
        raise ValueError('请通过 MCP_HUB_TOKEN_FILE 或 MCP_HUB_TOKEN 设置设备令牌')
    return value


def connection(url):
    parsed = urlsplit(url)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ('', '/')):
        raise ValueError('--url 必须是 Hub 的 HTTP(S) 基础地址，不含 /mcp 路径')
    cls = http.client.HTTPSConnection if parsed.scheme == 'https' else http.client.HTTPConnection
    return cls(parsed.hostname, parsed.port, timeout=120)


def error(response):
    response.read(65536)
    # Never print arbitrary response bodies or credentials from a misconfigured endpoint.
    raise ValueError(f'Hub 返回 HTTP {response.status}；请核对设备令牌、权限和素材 ID')


def upload(args, token):
    source = Path(args.file).expanduser()
    if not source.is_file() or source.stat().st_size > 512 * 1024 * 1024:
        raise ValueError('输入必须是小于 512 MiB 的文件')
    conn = connection(args.url)
    try:
        conn.putrequest('PUT', '/assets?' + urlencode({'service': args.service, 'filename': source.name}))
        conn.putheader('Authorization', 'Bearer ' + token)
        conn.putheader('Content-Type', 'application/octet-stream')
        conn.putheader('Content-Length', str(source.stat().st_size))
        conn.endheaders()
        with source.open('rb') as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                conn.send(chunk)
        response = conn.getresponse()
        if response.status != 201:
            error(response)
        result = json.loads(response.read(1024 * 1024))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        conn.close()


def download(args, token):
    if not args.asset_id.isalnum() or len(args.asset_id) > 80:
        raise ValueError('无效素材 ID')
    destination = Path(args.out).expanduser().resolve()
    if destination.exists():
        raise ValueError('输出文件已存在；请选择新的 --out 路径')
    if not destination.parent.is_dir():
        raise ValueError('输出目录不存在')
    conn = connection(args.url)
    temporary = None
    try:
        conn.request('GET', '/assets/' + args.asset_id, headers={'Authorization': 'Bearer ' + token})
        response = conn.getresponse()
        if response.status != 200:
            error(response)
        length = response.getheader('Content-Length')
        received = 0
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix='.mcp-download-', delete=False) as stream:
            temporary = Path(stream.name)
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                received += len(chunk)
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        if length is not None and received != int(length):
            raise ValueError('下载不完整，请重新下载')
        # Hard linking is atomic and refuses to overwrite a file created meanwhile.
        os.link(temporary, destination)
        print(json.dumps({'path': str(destination), 'bytes': received}, ensure_ascii=False))
    finally:
        conn.close()
        if temporary:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True, help='例如 http://服务器IP:8899')
    sub = parser.add_subparsers(dest='action', required=True)
    put = sub.add_parser('upload')
    put.add_argument('--service', required=True)
    put.add_argument('file')
    get = sub.add_parser('download')
    get.add_argument('asset_id')
    get.add_argument('--out', required=True)
    args = parser.parse_args()
    token = credential()
    (upload if args.action == 'upload' else download)(args, token)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, http.client.HTTPException) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
