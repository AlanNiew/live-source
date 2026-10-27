"""音乐接入测试：上游转发、歌单管理、/api/music/* 路由

全部 mock 网络请求（mock MusicUtils 的 _request_get/_request_post），
歌单 json 用临时目录，不触碰真实音乐服务。
"""
import json
import os
import tempfile
import unittest
from unittest import mock

from core.music import MusicUtils
from app import create_app

# 上游标准响应构造器
def _ok(data):
    """构造 code=200 的上游响应 mock"""
    resp = mock.Mock()
    resp.json.return_value = {'code': 200, 'message': 'success', 'data': data}
    return resp


def _err(code=500, message='boom'):
    """构造非 200 的上游响应 mock"""
    resp = mock.Mock()
    resp.json.return_value = {'code': code, 'message': message, 'data': None}
    return resp


class MusicForwardTest(unittest.TestCase):
    """上游转发：search / player_url / playlist_detail / lyrics"""

    def test_search_ok(self):
        """搜索成功：返回 (200, message, 歌曲列表)"""
        tracks = [{'music_id': '186016', 'music_name': '稻香'}]
        with mock.patch.object(MusicUtils, '_request_get', return_value=_ok(tracks)) as get:
            code, message, data = MusicUtils.search('周杰伦', 10)
        self.assertEqual(code, 200)
        self.assertEqual(data, tracks)
        # 参数正确拼到上游
        self.assertEqual(get.call_args[0][0], '/search')
        self.assertEqual(get.call_args[1]['params'],
                         {'keyword': '周杰伦', 'limit': 10})

    def test_player_url_ok(self):
        """播放地址：返回网易 CDN 直链"""
        with mock.patch.object(MusicUtils, '_request_get',
                               return_value=_ok({'url': 'http://music.163.com/song/media/outer/url?id=186016.mp3'})):
            code, _, data = MusicUtils.player_url('186016', 'exhigh')
        self.assertEqual(code, 200)
        self.assertTrue(data['url'].startswith('http'))

    def test_playlist_detail_post(self):
        """歌单详情走 POST /playlist，携带分页参数"""
        payload = {'playlist': {'id': '123'}, 'tracks': [], 'total': 100}
        with mock.patch.object(MusicUtils, '_request_post', return_value=_ok(payload)) as post:
            code, _, data = MusicUtils.playlist_detail('123', page_num=2, page_size=20)
        self.assertEqual(code, 200)
        self.assertEqual(post.call_args[0][0], '/playlist')
        self.assertEqual(post.call_args[1]['json_body'],
                         {'id': '123', 'pageNum': 2, 'pageSize': 20})

    def test_upstream_error_passthrough(self):
        """上游业务错误（如歌曲无版权 code!=200）原样透传"""
        with mock.patch.object(MusicUtils, '_request_get', return_value=_err(404, '歌曲不存在')):
            code, message, data = MusicUtils.player_url('99999', 'exhigh')
        self.assertEqual(code, 404)
        self.assertEqual(message, '歌曲不存在')

    def test_upstream_network_error(self):
        """上游网络异常：返回 code=0 与异常信息（调用方转 502）"""
        with mock.patch.object(MusicUtils, '_request_get',
                               side_effect=Exception('connection refused')):
            code, message, data = MusicUtils.search('a')
        self.assertEqual(code, 0)
        self.assertIn('connection refused', message)


class PlaylistManageTest(unittest.TestCase):
    """歌单增删与汇总"""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.path_patcher = mock.patch(
            'core.music.MUSIC_PLAYLISTS_PATH', os.path.join(self.tmp_dir, 'music_playlists.json'))
        self.path_patcher.start()

    def tearDown(self):
        self.path_patcher.stop()

    def test_add_remove_playlist(self):
        """动态歌单添加/删除，按 id 去重"""
        with mock.patch.object(MusicUtils, 'playlist_detail',
                               return_value=(200, 'success', {'playlist': {'name': '我喜欢的音乐'}})):
            playlists = MusicUtils.add_playlist('123', None)
        self.assertEqual(playlists, [{'id': '123', 'name': '我喜欢的音乐'}])

        # 重复添加同 id：更新名称
        with mock.patch.object(MusicUtils, 'playlist_detail',
                               return_value=(200, 'success', {'playlist': {'name': '新名字'}})):
            playlists = MusicUtils.add_playlist('123', None)
        self.assertEqual(len(playlists), 1)
        self.assertEqual(playlists[0]['name'], '新名字')

        removed, playlists = MusicUtils.remove_playlist('123')
        self.assertTrue(removed)
        self.assertEqual(playlists, [])

    def test_remove_missing_playlist(self):
        """删除不存在的歌单：返回 (False, 原列表)"""
        removed, playlists = MusicUtils.remove_playlist('nope')
        self.assertFalse(removed)

    def test_add_playlist_upstream_fail(self):
        """上游拉不到歌单名称：抛 ValueError（路由转 404）"""
        with mock.patch.object(MusicUtils, 'playlist_detail', return_value=(404, '歌单不存在', None)):
            with self.assertRaises(ValueError):
                MusicUtils.add_playlist('99999', None)

    def test_list_playlists_merges_static_dynamic(self):
        """汇总列表 = 静态 config + 动态 json 合并去重（动态优先）"""
        with mock.patch('core.music.MUSIC_PLAYLISTS',
                        [{'id': '1', 'name': '静态歌单'}, {'id': '2', 'name': '静态2'}]):
            MusicUtils.save_playlists([{'id': '2', 'name': '动态改名的2'}])
            playlists = MusicUtils.list_playlists()
        by_id = {p['id']: p['name'] for p in playlists}
        self.assertEqual(by_id['1'], '静态歌单')
        self.assertEqual(by_id['2'], '动态改名的2')  # 动态覆盖静态
        self.assertEqual(len(playlists), 2)

    def test_load_corrupted_file(self):
        """json 损坏：返回空列表不抛异常"""
        with open(os.path.join(self.tmp_dir, 'music_playlists.json'), 'w') as f:
            f.write('{{{bad json')
        self.assertEqual(MusicUtils.load_playlists(), [])


class MusicApiRouteTest(unittest.TestCase):
    """/api/music/* 路由：转发与鉴权"""

    def setUp(self):
        self.app = create_app()
        self.client = self.app.test_client()

    def test_playlists_get(self):
        """GET 歌单列表：无鉴权"""
        with mock.patch.object(MusicUtils, 'list_playlists',
                               return_value=[{'id': '1', 'name': 'A'}]):
            resp = self.client.get('/api/music/playlists')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['playlists'][0]['id'], '1')

    def test_playlist_detail_route(self):
        """歌单详情路由：转发上游 data"""
        payload = {'playlist': {'id': '1', 'name': 'A'}, 'tracks': [], 'total': 0}
        with mock.patch.object(MusicUtils, 'playlist_detail',
                               return_value=(200, 'success', payload)):
            resp = self.client.get('/api/music/playlist?id=1&pageNum=2&pageSize=10')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['data']['playlist']['name'], 'A')

    def test_playlist_detail_missing_id(self):
        """缺 id 参数：400"""
        resp = self.client.get('/api/music/playlist')
        self.assertEqual(resp.status_code, 400)

    def test_playlist_detail_upstream_fail(self):
        """上游失败：502 且带错误信息"""
        with mock.patch.object(MusicUtils, 'playlist_detail',
                               return_value=(0, '连接失败', None)):
            resp = self.client.get('/api/music/playlist?id=1')
        self.assertEqual(resp.status_code, 502)

    def test_play_route(self):
        """播放地址路由：返回网易直链"""
        with mock.patch.object(MusicUtils, 'player_url',
                               return_value=(200, 'success', {'url': 'http://cdn/x.mp3'})):
            resp = self.client.get('/api/music/play?id=186016&level=exhigh')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['url'], 'http://cdn/x.mp3')

    def test_play_no_url(self):
        """上游返回空 url（无版权/失效）：502"""
        with mock.patch.object(MusicUtils, 'player_url',
                               return_value=(200, 'success', {'url': ''})):
            resp = self.client.get('/api/music/play?id=1')
        self.assertEqual(resp.status_code, 502)

    def test_add_playlist_requires_token(self):
        """POST 添加歌单：无 token 401"""
        resp = self.client.post('/api/music/playlists', json={'id': '123'})
        self.assertEqual(resp.status_code, 401)

    def test_add_playlist_invalid_id(self):
        """POST 添加歌单：id 非纯数字 400"""
        from config import API_TOKEN
        resp = self.client.post('/api/music/playlists', json={'id': 'abc'},
                                headers={'Authorization': f'Bearer {API_TOKEN}'})
        self.assertEqual(resp.status_code, 400)

    def test_add_playlist_ok(self):
        """POST 添加歌单：带 token 成功"""
        from config import API_TOKEN
        with mock.patch.object(MusicUtils, 'add_playlist',
                               return_value=[{'id': '123', 'name': 'X'}]):
            resp = self.client.post('/api/music/playlists', json={'id': '123'},
                                    headers={'Authorization': f'Bearer {API_TOKEN}'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['playlists'][0]['id'], '123')

    def test_delete_playlist_requires_token(self):
        """DELETE 删除歌单：无 token 401"""
        resp = self.client.delete('/api/music/playlists/123')
        self.assertEqual(resp.status_code, 401)

    def test_delete_playlist_ok(self):
        """DELETE 删除歌单：带 token 成功"""
        from config import API_TOKEN
        with mock.patch.object(MusicUtils, 'remove_playlist', return_value=(True, [])):
            resp = self.client.delete('/api/music/playlists/123',
                                      headers={'Authorization': f'Bearer {API_TOKEN}'})
        self.assertEqual(resp.status_code, 200)

    def test_delete_playlist_not_found(self):
        """DELETE 删除不存在的歌单：404"""
        from config import API_TOKEN
        with mock.patch.object(MusicUtils, 'remove_playlist', return_value=(False, [])):
            resp = self.client.delete('/api/music/playlists/123',
                                      headers={'Authorization': f'Bearer {API_TOKEN}'})
        self.assertEqual(resp.status_code, 404)


if __name__ == '__main__':
    unittest.main()
