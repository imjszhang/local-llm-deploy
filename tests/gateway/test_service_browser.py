"""Browser credentials and redirects stay scoped to mounted proxy services."""
from __future__ import annotations

from email.message import Message
from types import SimpleNamespace
import unittest

from local_llm_deploy.config import normalize_proxy
from local_llm_deploy.gateway.service_browser import (
    ServiceWriter, browser_authorized, local_browser_navigation, session_cookie, upstream_headers,
)


class ServiceBrowserTests(unittest.TestCase):
    def setUp(self):
        self.spec = normalize_proxy('tool', {
            'type': 'proxy', 'upstream': 'http://192.0.2.20:8080', 'browser': True,
        })

    def handler(self, *, host='127.0.0.1:8888', navigation=False, **headers):
        message = Message()
        message['Host'] = host
        if navigation:
            message['Sec-Fetch-Mode'] = 'navigate'
            message['Sec-Fetch-Dest'] = 'document'
            message['Sec-Fetch-Site'] = headers.pop('Sec-Fetch-Site', 'none')
        for name, value in headers.items():
            message[name] = value
        return SimpleNamespace(
            headers=message, command='GET', client_address=('127.0.0.1', 12345),
            server=SimpleNamespace(server_port=8888), connection=object(),
            context=SimpleNamespace(auth=SimpleNamespace(key=lambda: 'test-key')),
        )

    def test_navigation_requires_loopback_host_and_peer(self):
        self.assertTrue(local_browser_navigation(self.handler(navigation=True)))
        self.assertFalse(local_browser_navigation(self.handler(navigation=True, host='example.com:8888')))
        self.assertFalse(local_browser_navigation(self.handler(navigation=True, host='127.0.0.1:9999')))
        self.assertFalse(local_browser_navigation(self.handler(navigation=True, **{'X-Forwarded-For': '127.0.0.1'})))
        remote = self.handler(navigation=True)
        remote.client_address = ('192.0.2.10', 1234)
        self.assertFalse(local_browser_navigation(remote))
        post = self.handler(navigation=True)
        post.command = 'POST'
        self.assertFalse(local_browser_navigation(post))
        duplicate = self.handler(navigation=True)
        duplicate.headers['Host'] = '127.0.0.1:8888'
        self.assertFalse(local_browser_navigation(duplicate))

    def test_navigation_rejects_cross_site_and_foreign_origin(self):
        request = self.handler(navigation=True, **{
            'Sec-Fetch-Site': 'cross-site', 'Referer': 'http://127.0.0.1:8888/',
        })
        self.assertFalse(browser_authorized(request, self.spec))
        request = self.handler(navigation=True, Origin='https://evil.example')
        self.assertFalse(browser_authorized(request, self.spec))
        self.assertFalse(local_browser_navigation(request))

    def test_cookie_is_scoped_to_service_and_rejects_foreign_origin(self):
        cookie = session_cookie(self.handler(), self.spec)
        self.assertIn('; Path=/services/tool/;', cookie)
        self.assertNotIn('Domain=', cookie)
        token = cookie.split(';')[0]
        self.assertTrue(browser_authorized(self.handler(Cookie=token), self.spec))
        other = normalize_proxy('other', self.spec.raw)
        self.assertFalse(browser_authorized(self.handler(Cookie=token), other))
        self.assertFalse(browser_authorized(self.handler(Cookie=token, Origin='http://evil.example'), self.spec))

    def test_same_origin_metadata_maps_without_gateway_cookie(self):
        request = self.handler(
            Origin='http://127.0.0.1:8888',
            Cookie=session_cookie(self.handler(), self.spec).split(';')[0] + '; upstream=ok',
            **{'Sec-Fetch-Site': 'same-origin'},
        )
        result = upstream_headers(request, self.spec)
        self.assertEqual(result['Origin'], 'http://192.0.2.20:8080')
        self.assertEqual(result['Sec-Fetch-Site'], 'same-origin')
        self.assertEqual(result['Cookie'], 'upstream=ok')
        request = self.handler(Origin='https://evil.example', **{'Sec-Fetch-Site': 'cross-site'})
        result = upstream_headers(request, self.spec)
        self.assertEqual(result['Origin'], 'https://evil.example')
        self.assertEqual(result['Sec-Fetch-Site'], 'cross-site')

    def test_response_urls_and_media_length_remain_usable(self):
        calls = []
        target = SimpleNamespace(start=lambda status, headers, **kwargs: calls.append((status, headers, kwargs)))
        writer = ServiceWriter(target, self.spec)
        for value, expected in (
                ('/api/view?x=1', '/services/tool/api/view?x=1'),
                ('http://192.0.2.20:8080/api/view?x=1', '/services/tool/api/view?x=1'),
                ('//192.0.2.20:8080/api/view?x=1', '/services/tool/api/view?x=1'),
                ('http://192.0.2.20:8080', '/services/tool/'),
                ('http://192.0.2.20:8080?x=1', '/services/tool/?x=1'),
                ('https://example.com/', 'https://example.com/')):
            self.assertEqual(dict(writer.map_headers({'Location': value}))['Location'], expected)
        mapped = dict(writer.map_headers({'Set-Cookie': 'session=ok; Path=/api; Domain=192.0.2.20'}))
        self.assertIn('Path=/services/tool/api', mapped['Set-Cookie'])
        self.assertNotIn('Domain=', mapped['Set-Cookie'])
        writer.start(206, {'Content-Length': '16', 'Content-Range': 'bytes 0-15/100'})
        self.assertEqual(calls[-1][2], {'content_length': 16})
        writer.start(200, {'Content-Length': '16', 'Transfer-Encoding': 'chunked'})
        self.assertEqual(calls[-1][2], {'content_length': None})


if __name__ == '__main__':
    unittest.main()
