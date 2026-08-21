"""管理后台页面路由：/admin/*（Jinja2 模板 + Bootstrap CDN + 原生 fetch）

页面与数据分离：页面只渲染骨架与前端 JS，数据全部走 /api/admin/*（session 共用）。
守卫规则：ADMIN_PASSWORD 空 → 除登录页外整体 403（安全默认）；
未登录访问页面 → 302 跳转登录页；已登录访问登录页 → 302 跳转仪表盘。
"""
from flask import Blueprint, redirect, render_template, request, session, url_for

from config import ADMIN_PASSWORD

admin_web = Blueprint('admin_web', __name__)


@admin_web.before_request
def _require_login():
    """页面守卫：未启用 403；未登录（登录页除外）跳登录页"""
    if request.endpoint == 'admin_web.login_page':
        return None
    if not ADMIN_PASSWORD:
        return '管理功能未启用：请在 .env 配置 ADMIN_PASSWORD 后重启服务', 403
    if not session.get('admin'):
        return redirect(url_for('admin_web.login_page'))
    return None


@admin_web.route('/login')
def login_page():
    """登录页：已登录跳仪表盘；未启用时仅展示提示不渲染表单"""
    if ADMIN_PASSWORD and session.get('admin'):
        return redirect(url_for('admin_web.dashboard'))
    return render_template('admin/login.html', disabled=not ADMIN_PASSWORD)


@admin_web.route('/')
def dashboard():
    """仪表盘：状态卡片 + 最近健康时间线 + 最近日志"""
    return render_template('admin/index.html', active='dashboard')


@admin_web.route('/sources')
def sources_page():
    """源管理：公开源 + B 站房间 CRUD、立即刷新"""
    return render_template('admin/sources.html', active='sources')


@admin_web.route('/channels')
def channels_page():
    """频道管理：聚合后频道 + 覆盖状态（分页/搜索/禁用/改分组/改名）"""
    return render_template('admin/channels.html', active='channels')


@admin_web.route('/monitor')
def monitor_page():
    """监控：健康历史 + 流探测明细（过滤不可达）"""
    return render_template('admin/monitor.html', active='monitor')


@admin_web.route('/logs')
def logs_page():
    """日志：级别/关键词过滤 + 分页"""
    return render_template('admin/logs.html', active='logs')


@admin_web.route('/settings')
def settings_page():
    """设置：运行时配置（DB 优先、config 兜底）可视化编辑"""
    return render_template('admin/settings.html', active='settings')
