"""Homepage lists mounted apps and external service proxies."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from local_llm_deploy.gateway.home import render_home


class HomePageTests(unittest.TestCase):
    def test_external_services_appear_beside_apps_without_upstream(self):
        page = render_home(
            {'video': SimpleNamespace(alias='视频库', key='video', endpoint='/video/', kind='video')},
            {'civ2': SimpleNamespace(alias='civ2agent', key='civ2', prefix='/services/civ2', upstream='http://192.168.0.75:4732')},
        ).decode()
        self.assertIn('href="/video/"', page)
        self.assertIn('href="/services/civ2/"', page)
        self.assertIn('外部服务', page)
        self.assertNotIn('192.168.0.75', page)
        secret = SimpleNamespace(alias='civ2agent', key='civ2', prefix='/services/civ2',
                                 raw={'home_query': 'token=saved-token'})
        hidden = render_home({}, {'civ2': secret}).decode()
        self.assertIn('href="/services/civ2/?token=saved-token"', hidden)
        self.assertIn('<code>/services/civ2/</code>', hidden)
        self.assertLess(page.index('civ2agent'), page.index('视频库'))
