"""管理后台页面测试：登录守卫 / 禁用提示 / 页面渲染 smoke"""
import unittest
from unittest import mock

from app import create_app

PAGES = ['/', '/sources', '/channels', '/monitor', '/logs', '/settings']


class AdminWebTest(unittest.TestCase):
    """页面路由：未启用 403、未登录 302、登录后 200 渲染"""

    def setUp(self):
        # 页面与 API 各自持有 ADMIN_PASSWORD 引用，需同时打补丁
        self.pass_patcher = mock.patch('admin.web.ADMIN_PASSWORD', 'webpass')
        self.pass_patcher.start()
        self.api_pass_patcher = mock.patch('admin.api.ADMIN_PASSWORD', 'webpass')
        self.api_pass_patcher.start()
        self.app = create_app()
        self.client = self.app.test_client()

    def tearDown(self):
        self.api_pass_patcher.stop()
        self.pass_patcher.stop()

    def _login(self):
        return self.client.post('/api/admin/login', json={'password': 'webpass'})

    def test_disabled_403_except_login_page(self):
        """ADMIN_PASSWORD 空：页面整体 403，登录页展示禁用提示"""
        with mock.patch('admin.web.ADMIN_PASSWORD', ''), \
             mock.patch('admin.api.ADMIN_PASSWORD', ''):
            for page in PAGES:
                resp = self.client.get(f'/admin{page}')
                self.assertEqual(resp.status_code, 403, page)
            login = self.client.get('/admin/login')
            self.assertEqual(login.status_code, 200)
            self.assertIn('未启用', login.get_data(as_text=True))

    def test_anonymous_redirects_to_login(self):
        """未登录访问页面：302 跳转 /admin/login"""
        for page in PAGES:
            resp = self.client.get(f'/admin{page}')
            self.assertEqual(resp.status_code, 302, page)
            self.assertIn('/admin/login', resp.headers['Location'])

    def test_login_page_redirects_when_logged_in(self):
        """已登录访问登录页：302 跳仪表盘"""
        self._login()
        resp = self.client.get('/admin/login')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/admin', resp.headers['Location'])

    def test_pages_render_after_login(self):
        """登录后 5 个页面渲染 200 且含页面标识"""
        self._login()
        markers = {
            '/': ['仪表盘', '最近健康时间线', '最近日志', 'trendChart', '频道数趋势'],
            '/sources': ['源管理', '新增源', '立即刷新'],
            '/channels': ['频道管理', '搜索频道名', '覆盖状态'],
            '/monitor': ['监控', '健康检测历史', '流探测明细', '仅看不可达'],
            '/logs': ['日志', '全部级别', '关键词'],
            '/settings': ['设置', 'min_channel_count', 'public_base_url', 'alert_enabled'],
        }
        for page, expects in markers.items():
            resp = self.client.get(f'/admin{page}')
            self.assertEqual(resp.status_code, 200, page)
            text = resp.get_data(as_text=True)
            for marker in expects:
                self.assertIn(marker, text, f"{page} 应包含 {marker}")

    def test_pages_escape_user_data_before_render(self):
        """模板含 esc() 工具：动态数据注入前转义（防 XSS 的基础约定）"""
        self._login()
        resp = self.client.get('/admin/channels')
        text = resp.get_data(as_text=True)
        # 共享工具必须在每个交互页就绪（channels 依赖 esc/renderPager/api）
        for helper in ('function esc(', 'function api(', 'function renderPager('):
            self.assertIn(helper, text)


if __name__ == '__main__':
    unittest.main()
