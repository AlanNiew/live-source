"""流探测测试：可达/拒绝/超时判定、宽松 vs 严格口径"""
import http.server
import socketserver
import threading
import unittest

from core.probing import probe_stream


class _Handler(http.server.BaseHTTPRequestHandler):
    """本地 HTTP 服务：模拟可达流（200 + 1000 字节内容）"""

    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Length', '1000')
        self.end_headers()
        self.wfile.write(b'x' * 1000)

    def log_message(self, *args):
        pass


class ProbeStreamTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.srv = socketserver.TCPServer(('127.0.0.1', 0), _Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_reachable_http(self):
        """可达流：200 且读到数据 -> True"""
        self.assertTrue(probe_stream(f"http://127.0.0.1:{self.port}/a.m3u8"))

    def test_connection_refused(self):
        """连接拒绝 -> False"""
        self.assertFalse(probe_stream("http://127.0.0.1:9/refused.m3u8"))

    def test_bad_url(self):
        """非法 URL -> False"""
        self.assertFalse(probe_stream("not-a-url"))

    def test_strict_vs_loose_403(self):
        """403 严格口径不可达、宽松口径可达（判据差异）"""
        # 起一个返回 403 的临时服务
        class ForbiddenHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b'forbidden')

            def log_message(self, *args):
                pass

        srv = socketserver.TCPServer(('127.0.0.1', 0), ForbiddenHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]
        url = f"http://127.0.0.1:{port}/x.m3u8"
        self.assertFalse(probe_stream(url))                    # 严格：403 不可达
        self.assertTrue(probe_stream(url, accept_403=True))    # 宽松：403 可达
        srv.shutdown()
        srv.server_close()


if __name__ == '__main__':
    unittest.main()
