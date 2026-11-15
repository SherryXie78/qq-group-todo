"""手机端待办台：一个极小的 HTTP 服务，读取 tasks 表并支持勾选完成。"""

from __future__ import annotations

import datetime as dt
import hmac
import hashlib
import json
import logging
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie, CookieError
from pathlib import Path
from typing import Any

from . import confidence
from . import conflicts
from . import ics
from . import observe
from . import restapi
from . import taskstatus
from . import theme
from .config import Settings
from .manual_tasks import create_manual_task
from .task_plan import card_brief, phase_when
from .store import Store
from .timeutil import iso, now_local, parse_iso
from .temporal import countdown,BEIJING
from .temporal import clock as source_clock
from .task_review import TaskReview
from .daily_advice import DailyAdvice
from .calendar_view import calendar_payload, courses, save_course
from . import android_install
from . import pet
from .qq_session import QQSession

LOGGER = logging.getLogger(__name__)

MAX_BODY_BYTES = 64 * 1024
APP_VERSION = '2026.11.01.3'

MANIFEST_JSON = json.dumps(
    {
        "name": "群消息待办",
        "short_name": "群待办",
        "description": "QQ 群通知摘要与个人待办台",
        "id": "/",
        "start_url": "/?app=1",
        "scope": "/",
        "display": "standalone",
        "display_override": ["standalone"],
        "background_color": "#0E1117",
        "theme_color": "#0E1117",
        "orientation": "portrait",
        "icons": [
            {
                "src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"
            },
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"}
        ],
    },
    ensure_ascii=False,
)

ICON_SVG = """<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 512 512\">
  <defs>
    <linearGradient id=\"g\" x1=\"0\" y1=\"0\" x2=\"1\" y2=\"1\">
      <stop offset=\"0\" stop-color=\"#4f7cff\"/>
      <stop offset=\"1\" stop-color=\"#8b5cf6\"/>
    </linearGradient>
  </defs>
  <rect width=\"512\" height=\"512\" rx=\"112\" fill=\"#0E1117\"/>
  <rect x=\"64\" y=\"64\" width=\"384\" height=\"384\" rx=\"96\" fill=\"url(#g)\"/>
  <path d=\"M168 264l58 58 122-142\" fill=\"none\" stroke=\"#fff\" stroke-width=\"36\" stroke-linecap=\"round\" stroke-linejoin=\"round\"/>
</svg>"""

_custom_icon = Path(__file__).resolve().parents[2] / 'assets' / 'app-icon.svg'
if _custom_icon.is_file():
    ICON_SVG = _custom_icon.read_text(encoding='utf8')

SERVICE_WORKER_JS = """const CACHE = 'qq-digest-shell-v17';
const DATA = 'qq-digest-data-v8';
const ASSETS = ['/', '/manifest.webmanifest', '/icon.svg', '/icon-192.png', '/icon-512.png', '/vendor/fullcalendar-6.1.20.min.js','/vendor/canvas-confetti-1.9.4.js'];
const DATA_PATHS = ['/api/tasks', '/api/notices', '/api/notifications', '/api/deadlines', '/api/meta','/api/calendar/events','/api/courses','/api/pet','/api/pet/avatar'];
self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(ASSETS)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(key => key.startsWith('qq-digest-shell-') && key !== CACHE).map(key => caches.delete(key)))).then(() => self.clients.claim()));
});
async function network(request) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 5000);
  try { return await fetch(request, {signal: controller.signal, cache: 'no-store'}); }
  finally { clearTimeout(timeout); }
}
function stamp(response, origin) {
  const headers = new Headers(response.headers);
  headers.set('X-QQ-Digest-Cache', origin);
  if (origin === 'network') headers.set('X-QQ-Digest-Fetched-At', String(Date.now()));
  return new Response(response.body, {status: response.status, statusText: response.statusText, headers});
}
async function dataResponse(request) {
  try {
    const response = await network(request);
    if (response.status >= 500) throw new Error('server unavailable');
    const fresh = stamp(response, 'network');
    if (fresh.ok) { const cache = await caches.open(DATA); await cache.put(request, fresh.clone()); }
    return fresh;
  } catch (_) {
    const cached = await caches.match(request);
    if (cached) return stamp(cached, 'offline');
    return new Response(JSON.stringify({error:'连接不到电脑，且尚无离线缓存。请检查电脑和 Tailscale。'}), {status:503, headers:{'Content-Type':'application/json','X-QQ-Digest-Cache':'offline'}});
  }
}
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.origin !== self.location.origin) return;
  if (DATA_PATHS.includes(url.pathname)) {
    event.respondWith(dataResponse(event.request));
    return;
  }
  if (url.pathname.startsWith('/api/')) return;
  event.respondWith(network(event.request).then(async response => {
    if (response.status >= 500) throw new Error('server unavailable');
    if (response.ok && (url.pathname === '/' || ASSETS.includes(url.pathname))) {
      const cache = await caches.open(CACHE);
      await cache.put(url.pathname === '/' ? '/' : event.request, response.clone());
    }
    return response;
  }).catch(async () => (await caches.match(url.pathname === '/' ? '/' : event.request)) || (event.request.mode === 'navigate' ? await caches.match('/') : Response.error())));
});
"""
PAGE_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0E1117">
<script>
/* 主题引导：在首帧前按 localStorage 的 qq_digest_theme（auto/light/dark）定好 data-theme，避免闪白/闪黑。
   必须是内联同步脚本（不能等 DOMContentLoaded），否则会先按深色画一帧再切主题。 */
(function () {
  var KEY = 'qq_digest_theme';
  var query = window.matchMedia('(prefers-color-scheme: light)');
  function mode() {
    try {
      var value = localStorage.getItem(KEY);
      if (value === 'light' || value === 'dark') return value;
    } catch (error) {}
    return 'auto';
  }
  function apply(next) {
    var dark = next === 'dark' || (next !== 'light' && !query.matches);
    document.documentElement.dataset.theme = dark ? 'dark' : 'light';
    var color = document.querySelector('meta[name="theme-color"]');
    if (color) color.setAttribute('content', dark ? '#0E1117' : '#F5F7FA');
    var bar = document.querySelector('meta[name="apple-mobile-web-app-status-bar-style"]');
    if (bar) bar.setAttribute('content', dark ? 'black-translucent' : 'default');
  }
  apply(mode());
  var followSystem = function () { if (mode() === 'auto') apply('auto'); };
  if (query.addEventListener) query.addEventListener('change', followSystem);
  else if (query.addListener) query.addListener(followSystem);
  window.qqTheme = {
    mode: mode,
    apply: apply,
    set: function (next) {
      try { localStorage.setItem(KEY, next); } catch (error) {}
      apply(next);
    }
  };
})();
</script>
<link rel="manifest" href="/manifest.webmanifest">
<script src="/vendor/fullcalendar-6.1.20.min.js" defer></script>
<link rel="icon" href="/icon.svg" type="image/svg+xml">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>群消息待办</title>
<style>
:root{
__THEME_DARK__
}
/* 浅色主题只覆盖同名 token，其余规则不动；JS 在 head 里按 跟随系统/浅色/深色 设置 data-theme */
[data-theme="light"]{
__THEME_LIGHT__
}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html{width:100%;max-width:100%;overflow-x:clip;background:var(--bg)}
body{width:100%;max-width:100%;margin:0;overflow-x:clip;background:linear-gradient(180deg,var(--bg-top),var(--bg) 340px);color:var(--text);font:15px/1.5 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;padding-bottom:calc(102px + env(safe-area-inset-bottom))}
header{width:100%;min-width:0;padding:calc(12px + env(safe-area-inset-top)) 14px 10px;position:relative;background:linear-gradient(180deg,var(--glass-1),var(--glass-2));border-bottom:1px solid var(--line);z-index:5}
.hero{width:100%;min-width:0;max-width:100%;border-radius:10px;padding:15px 15px 13px;background:linear-gradient(135deg,var(--hero-1),var(--hero-2) 58%,var(--hero-3));border:1px solid var(--line-strong);box-shadow:0 12px 28px var(--shadow)}
.hero-top{display:flex;min-width:0;align-items:flex-start;justify-content:space-between;gap:12px}
.hero h1{min-width:0;margin:0;font-size:19px;line-height:1.25;font-weight:700;letter-spacing:0}
.hero p{margin:6px 0 0;color:var(--muted);font-size:12px;line-height:1.45}
.progress-label{flex:0 0 auto;font-size:12px;font-weight:650;color:var(--text);background:var(--card-2);border:1px solid var(--line);border-radius:99px;padding:3px 8px}
.bar{height:4px;border-radius:99px;background:var(--bar);margin-top:12px;overflow:hidden}
.bar>i{display:block;height:100%;width:0;background:var(--accent-soft);border-radius:99px;transition:width .35s ease}
.stats{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}
.stat{display:none;font-size:11px;color:var(--muted);background:var(--card-2);border:1px solid var(--line);border-radius:99px;padding:3px 8px}
.stat.show{display:inline-block}
.stats{align-items:center}
.manual-add{display:inline-flex;align-items:center;justify-content:center;width:32px;height:32px;padding:0;border:1px solid var(--line);border-radius:50%;background:var(--card-2);color:var(--accent);font:inherit;font-size:24px;line-height:1;cursor:pointer}
.manual-add:hover{background:var(--tint-academic)}
.manual-add:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
.manual-dialog{box-sizing:border-box;width:min(520px,calc(100% - 24px));max-height:calc(100dvh - 32px);overflow:auto;padding:24px;border:1px solid var(--line);border-radius:18px;background:var(--card);color:var(--text);box-shadow:0 16px 60px var(--shadow)}
.manual-dialog::backdrop{background:rgba(0,0,0,.45)}
.manual-dialog h2{font-size:20px;margin:0 0 8px}
.manual-dialog .manual-hint{font-size:12px;line-height:1.6;color:var(--muted);margin:0 0 16px}
.manual-dialog label{display:grid;gap:7px;font-size:13px;margin:13px 0;color:var(--muted);min-width:0}
.manual-dialog input,.manual-dialog textarea{box-sizing:border-box;width:100%;min-width:0;padding:11px;border:1px solid var(--line);border-radius:9px;background:var(--card-2);color:var(--text);font:inherit;font-size:14px}
.manual-dialog textarea{resize:vertical;min-height:96px}
.manual-dialog input:focus,.manual-dialog textarea:focus{outline:2px solid var(--accent);outline-offset:2px}
.manual-dates{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.manual-dialog .actions{justify-content:flex-end}
.manual-feedback{font-size:12px;line-height:1.6;color:var(--muted);margin-top:8px;min-height:18px}
.manual-error{color:var(--urgent-ink)}
@media(max-width:390px){.manual-dialog{padding:18px}.manual-dates{grid-template-columns:1fr;gap:0}}
main{width:100%;min-width:0;padding:4px 14px 30px}
.section{width:100%;min-width:0;max-width:100%;margin-top:12px}
.section-head{display:flex;min-width:0;align-items:center;justify-content:space-between;gap:10px;margin:0 0 8px;padding:0 2px}
.section-title{display:flex;align-items:center;gap:7px;margin:0;font-size:13px;font-weight:650;color:var(--text)}
.section-title:before{content:"";width:6px;height:6px;border-radius:50%;background:var(--academic);box-shadow:0 0 0 3px var(--tint-academic)}
.section.overdue .section-title:before{background:var(--urgent-soft);box-shadow:0 0 0 3px var(--tint-urgent)}
.section.done .section-title:before{background:var(--info);box-shadow:none}
.count-pill{font-size:11px;color:var(--muted);background:var(--card-2);border:1px solid var(--line);border-radius:99px;padding:2px 7px}
ul{width:100%;min-width:0;list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:6px}
.task{position:relative;display:flex;width:100%;min-width:0;max-width:100%;gap:10px;padding:10px 11px;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden;transition:background .18s ease,border-color .18s ease,opacity .18s ease,transform .12s ease}
.task:before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--info)}
.task.urgent:before{background:var(--urgent-soft)}
.task.action:before{background:var(--action)}
.task.academic:before{background:var(--academic)}
.task.overdue{background:linear-gradient(90deg,var(--overdue-1),var(--card) 45%);border-color:var(--line-urgent)}
.task.overdue:before{background:var(--urgent-soft)}
.task.done{opacity:.48}
.task.done .t{text-decoration:line-through}
.check{position:relative;flex:0 0 auto;width:29px;height:29px;margin:0;padding:0;border-radius:50%;border:2px solid var(--check-line);background-color:var(--card-2);cursor:pointer;transition:transform .16s ease,border-color .16s ease,background-color .16s ease}
.check:before{content:"";position:absolute;inset:3px;border-radius:50%;border:1px solid var(--line)}
.check:after{content:"";position:absolute;left:8px;top:5px;width:7px;height:12px;border:2.5px solid var(--muted);border-top:0;border-left:0;border-radius:1px;transform:rotate(42deg) scale(1);opacity:.5;transition:transform .16s ease,opacity .16s ease}
.check:hover{border-color:var(--accent-soft);background-color:var(--card)}
.check:active{transform:scale(.92)}
/* 勾选圆点本身只有 29px，用一层透明子元素把命中区撑到 45px（视觉不变，避免误触标题/群名） */
.check-hit{position:absolute;left:50%;top:50%;width:45px;height:45px;transform:translate(-50%,-50%);border-radius:50%}
.candidate-mark{position:relative;flex:0 0 auto;display:grid;place-items:center;width:29px;height:29px;margin:0;border-radius:50%;border:2px solid var(--line-academic);background-color:var(--tint-academic);color:var(--academic-ink);font-size:12px;font-weight:750}
.section-head{cursor:default}
details.section>summary{cursor:pointer}
.task.done .check{border-color:transparent;background-color:var(--accent);background-image:none;box-shadow:none}
.task.done .check:after{transform:rotate(42deg) scale(1.06);opacity:1;border-color:var(--accent-ink)}
.body{min-width:0;max-width:100%;flex:1}
.card-top{display:flex;align-items:center;flex-wrap:wrap;gap:6px;margin-bottom:4px}
.tag{font-size:10px;font-weight:700;letter-spacing:0;border-radius:99px;padding:2px 7px;border:1px solid var(--line);color:var(--muted);background:var(--card-2)}
.tag.urgent{color:var(--urgent-ink);background:var(--tint-urgent);border-color:var(--line-urgent)}
.tag.action{color:var(--action-ink);background:var(--tint-action);border-color:var(--line-action)}
.tag.academic{color:var(--academic-ink);background:var(--tint-academic);border-color:var(--line-academic)}
.tag.candidate{color:var(--academic-ink);background:var(--tint-academic);border-color:var(--line-academic)}
.deadline-chip{display:inline-flex;align-items:center;font-size:11px;color:var(--action-ink);background:var(--tint-action);border:1px solid var(--line-action);border-radius:99px;padding:2px 7px}
.deadline-chip.over{color:var(--urgent-ink);background:var(--tint-urgent);border-color:var(--line-urgent)}
.snooze-chip{font-size:10px;font-weight:600;color:var(--academic-ink);background:var(--tint-academic);border:1px solid var(--line-academic);border-radius:99px;padding:2px 7px}
.duplicate-note{margin-top:7px;font-size:11px;color:var(--dim)}
.t{display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;overflow:hidden;font-size:15px;font-weight:650;line-height:1.45;letter-spacing:0;overflow-wrap:anywhere;word-break:break-word}
.context{display:flex;flex-wrap:wrap;gap:6px;margin-top:7px}
.ctx{font-size:11px;color:var(--muted);background:var(--card-2);border:1px solid var(--line);border-radius:6px;padding:3px 6px;overflow-wrap:anywhere}
.group-chip{background:var(--card-2);border:1px solid var(--line);border-radius:99px;padding:2px 7px}
.confidence{font-size:12px;color:var(--muted);margin-top:6px}
details{margin-top:7px}
summary{display:flex;align-items:center;gap:5px;font-size:12px;color:var(--dim);cursor:pointer;list-style:none;user-select:none}
summary::-webkit-details-marker{display:none}
summary:after{content:"›";font-size:16px;line-height:1;transform:rotate(0);transition:transform .16s ease}
details[open] summary:after{transform:rotate(90deg)}
details p{margin:7px 0 0;font-size:12px;line-height:1.6;color:var(--muted);border-left:2px solid rgba(255,255,255,.1);padding-left:8px;white-space:pre-wrap;overflow-wrap:anywhere}
/* 卡片内的折叠开关整行可点（命中区 >=44px）；配色不再用最暗的 --dim */
details:not(.section){margin-top:9px}
details:not(.section)>summary{justify-content:space-between;gap:8px;min-height:44px;padding:6px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card-2);font-size:12px;color:var(--muted);touch-action:manipulation;transition:background .15s ease,border-color .15s ease,transform .12s ease}
details:not(.section)>summary:after{font-size:16px;color:var(--dim)}
details:not(.section)>summary:active{background:var(--card);transform:scale(.995)}
details:not(.section)>summary:focus-visible{outline:2px solid var(--accent-soft);outline-offset:2px}
details:not(.section)>p{margin-top:9px;max-height:45vh;overflow:auto;overscroll-behavior:contain}
/* 一行操作条：详细 / 原文 / 纠错 并排，避免每张卡被三个 44px 行撑高 */
.toggle-row{display:flex;flex-wrap:wrap;gap:6px;margin-top:9px}
.toggle-row>details{flex:1 1 0;min-width:0;margin:0}
.toggle-row>details>summary{min-height:44px;padding:6px 8px;justify-content:center;gap:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.toggle-row>details[open]{flex:1 1 100%}
.toggle-row>details[open]>summary{justify-content:space-between}
.toggle-row>details.correction{flex:0 0 auto;min-width:56px;margin:0;padding-top:0;border-top:0}
.toggle-row>details.correction>summary{border-color:transparent;background:transparent;color:var(--dim)}
/* 外观开关（设置页） */
.theme-row{display:flex;gap:6px;margin-top:6px}
.theme-btn{flex:1 1 0;min-height:44px;padding:6px 8px;border:1px solid var(--line);border-radius:8px;background:var(--card-2);color:var(--muted);font:inherit;font-size:12px;cursor:pointer}
.theme-btn.active{border-color:var(--accent);background:var(--tint-academic);color:var(--text);font-weight:650}
.theme-btn:active{transform:scale(.98)}
.group-panel{padding:16px;margin-bottom:22px;border:1px solid var(--line);border-radius:14px;background:var(--card)}
.group-hint{font-size:12px;line-height:1.7;color:var(--muted);margin:0 0 14px}
.group-fields{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.group-fields[hidden]{display:none}
#history-form label{display:block;margin:12px 0;font-size:14px}
#history-form select,#history-form input{display:block;width:100%;margin-top:6px;padding:10px;border-radius:10px;border:1px solid var(--line);background:var(--panel);color:var(--text)}
#history-status{line-height:1.7;font-size:14px}
#history-results .card{margin-top:14px;padding:16px;overflow-wrap:anywhere}
#history-results details{font-size:13px;line-height:1.8}
.group-fields label{font-size:12px;color:var(--muted);display:grid;gap:7px;min-width:0}
.group-fields input{box-sizing:border-box;width:100%;min-width:0;padding:11px;border:1px solid var(--line);border-radius:8px;background:var(--card-2);color:var(--text);font:inherit;font-size:14px}
.group-fields input:focus{outline:2px solid var(--accent);outline-offset:2px}
.group-entry{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:14px 0;border-top:1px solid var(--line)}
.group-entry>div:first-child{min-width:0;overflow-wrap:anywhere;font-size:14px}
.group-entry small{display:block;color:var(--muted);margin-top:5px}
.group-entry .actions{flex-shrink:0;margin:0;gap:5px}
.group-status{font-size:12px;line-height:1.6;color:var(--muted);min-height:20px;margin:12px 0}
.btn:disabled{opacity:.5;cursor:wait}
@media(max-width:430px){.group-fields{grid-template-columns:1fr}.group-entry{align-items:flex-start;flex-wrap:wrap}}
.detail-list{margin:7px 0 0 18px;padding:0;font-size:12px;line-height:1.55;color:var(--muted)}
.detail-list li{margin:4px 0;overflow-wrap:anywhere}
.actions{display:flex;flex-wrap:wrap;gap:7px;margin-top:10px}
.btn{border:1px solid var(--line);background:var(--card-2);color:var(--text);font:inherit;font-size:12px;border-radius:8px;padding:9px 11px;cursor:pointer}
.btn.primary{border-color:transparent;background:var(--accent);color:var(--accent-ink)}
.btn.ghost{color:var(--muted)}
.correction{margin-top:10px;padding-top:9px;border-top:1px solid var(--line)}
.correction summary{color:var(--muted);font-weight:650}
.correction-panel{padding-top:2px}
.correction-hint{margin-top:6px;font-size:11px;line-height:1.45;color:var(--dim)}
.correct-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px;margin-top:8px}
.correct-row{display:flex;min-width:0;align-items:center;gap:7px;margin-top:7px}
.correct-btn{min-width:0;padding:9px 10px;border:1px solid var(--line);border-radius:9px;background:var(--card-2);color:var(--text);font:inherit;font-size:12px;font-weight:600;cursor:pointer;transition:transform .15s ease,background .15s ease,border-color .15s ease}
.correct-btn.primary{border-color:transparent;background:var(--accent);color:var(--accent-ink)}
.correct-btn.ghost{color:var(--muted);background:transparent}
.correct-btn:active{transform:scale(.97)}
.correct-select,.correct-date{flex:1;min-width:0;height:38px;padding:6px 8px;border:1px solid var(--line);border-radius:9px;background-color:var(--card-2);color:var(--text);font:inherit;font-size:12px}
.correct-select{appearance:none;padding-right:22px;background-image:linear-gradient(45deg,transparent 50%,#8f9bb2 50%),linear-gradient(135deg,#8f9bb2 50%,transparent 50%);background-position:calc(100% - 13px) 14px,calc(100% - 9px) 14px;background-size:4px 4px,4px 4px;background-repeat:no-repeat}
.task:target{box-shadow:0 0 0 2px var(--line-academic)}
.empty{color:var(--dim);font-size:13px;padding:10px 2px}
.tabs{position:fixed;left:50%;bottom:calc(9px + env(safe-area-inset-bottom));display:flex;gap:4px;width:calc(100% - 28px);max-width:440px;padding:6px;transform:translateX(-50%);border:1px solid var(--line-strong);border-radius:25px;background:var(--nav-bg);box-shadow:0 14px 34px var(--shadow);backdrop-filter:blur(24px) saturate(160%);-webkit-backdrop-filter:blur(24px) saturate(160%);isolation:isolate;z-index:10}
.tabs button{position:relative;z-index:2;flex:1;min-width:0;height:54px;padding:5px 2px;border:0;background:transparent;color:var(--dim);font-family:inherit;font-size:10px;font-weight:600;cursor:pointer;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;transition:color .18s ease,transform .18s ease}
.tabs button.active{color:var(--text)}
.tabs button:active{transform:scale(.96)}
.tabs svg{position:relative;z-index:2;width:21px;height:21px;stroke:currentColor;fill:none;stroke-width:1.9;stroke-linecap:round;stroke-linejoin:round;transition:stroke-width .18s ease}
.tabs button>span:last-child{position:relative;z-index:2;line-height:1.15}
.tabs button.active svg{stroke-width:2.25}
.tabs .dot{position:absolute;z-index:1;inset:5px 4px;border:1px solid transparent;border-radius:18px;background:transparent;transition:background .2s ease,border-color .2s ease}
.tabs button.active .dot{border-color:var(--line-strong);background:var(--nav-active)}
.notice{padding:12px;background:var(--card);border:1px solid var(--line);border-radius:10px}
.notice h3{margin:0;font-size:14px;font-weight:650}
.notice p{margin:6px 0 0;font-size:12px;color:var(--muted);white-space:pre-wrap}
.kv{display:flex;justify-content:space-between;gap:12px;padding:11px 0;border-bottom:1px solid var(--line);font-size:13px}
.kv span:last-child{color:var(--muted);text-align:right}
.insight-list{margin:6px 0 0;padding:0 0 0 16px;list-style:disc}
.insight-list li{margin:6px 0;font-size:12px;line-height:1.5;color:var(--muted)}
.offline{position:fixed;top:0;left:0;right:0;z-index:20;padding:9px 14px;background:var(--offline-bg);color:var(--offline-ink);font-size:13px;text-align:center;border-bottom:1px solid var(--line)}
.install-btn{position:fixed;right:12px;bottom:78px;z-index:19;padding:10px 15px;border-radius:14px;border:1px solid transparent;background:var(--accent);color:var(--accent-ink);font-size:13px;font-weight:600;box-shadow:0 8px 20px var(--shadow)}
body.offline-mode{padding-top:40px}
.sync-panel{margin-top:12px;padding:12px;border:1px solid var(--line);border-radius:14px;background:var(--card);display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.sync-panel strong{font-size:14px}.sync-panel small{display:block;font-size:11px;color:var(--muted);line-height:1.6}.sync-panel .sync-copy{flex:1;min-width:180px}
.sync-panel[data-state="live"]{border-color:var(--line-action)}.sync-panel[data-state="cached"]{border-color:var(--line-urgent)}
.sync-panel .btn{min-height:40px;flex:none}.sync-panel .sync-update{border-color:var(--accent)}
.task-fact{display:flex;align-items:baseline;gap:10px;margin-top:9px;line-height:1.6;font-size:14px;overflow-wrap:anywhere}
.task-fact b{flex:none;color:var(--muted);font-size:12px;width:28px}.task-fact.time-fact span{font-weight:700;color:var(--text)}
.milestones{margin:14px 0 6px;padding:12px;background:var(--card-2);border:1px solid var(--line);border-radius:14px}
.milestones h4{margin:0 0 10px;font-size:12px;color:var(--muted)}.milestone{display:flex;align-items:center;gap:10px;padding:10px 0;border-top:1px solid var(--line)}
.milestone-copy{flex:1;min-width:0}.milestone-copy strong{display:block;font-size:14px;line-height:1.5}.milestone-copy small{display:block;font-size:12px;line-height:1.65;color:var(--muted);margin-top:4px}
.milestone.done strong{text-decoration:line-through;color:var(--muted)}.milestone .btn{min-height:40px;padding:7px 10px;flex:none;font-size:12px}.body .t{white-space:normal;overflow-wrap:anywhere;font-size:18px;line-height:1.5;margin-top:8px}
__FEATURE_CSS__
</style>
</head>
<body>
<div id="offline" class="offline" role="status" hidden>离线：只读缓存，写操作已禁用</div>
<button id="install" class="install-btn" type="button" hidden>安装到手机</button>
<header>
  <div class="hero">
    <div class="hero-top">
      <h1 id="headline">加载中…</h1>
      <span class="progress-label" id="progressLabel">0%</span>
    </div>
    <p id="subline"></p>
    <p id="collector-status" role="alert" hidden style="font-size:12px;line-height:1.6;color:var(--urgent-ink)"></p>
    <div id="sync-panel" class="sync-panel" data-state="connecting" role="status" aria-live="polite">
      <div class="sync-copy"><strong id="sync-label">正在连接电脑…</strong><small id="sync-detail">正在核对最新待办</small></div>
      <button id="sync-now" class="btn primary" type="button">立即同步</button>
      <button id="connection-help" class="btn" type="button">连接帮助</button>
      <button id="app-update" class="btn sync-update" type="button" hidden>更新界面</button>
    </div>
    <details id="qq-login-panel" class="qq-login-panel" data-qq-card>
      <summary><strong>QQ 登录与采集</strong><span data-qq-field="title">正在检测…</span></summary>
      <div class="qq-login-body">
        <p class="group-hint" data-qq-field="account"></p>
        <p data-qq-field="message">正在检查电脑版 QQ 的原账号连接。</p>
        <div class="qq-login-actions"><button class="btn primary" type="button" data-qq-action="open_login">在电脑打开 QQ 登录</button><button class="btn" type="button" data-qq-action="recheck">重新检测</button><button class="btn" type="button" data-qq-action="reconnect">恢复采集</button></div>
        <p class="group-hint">登录在电脑的普通 QQ 窗口中完成；手机这里只发起打开请求。请使用原账号，无需发送测试消息。</p>
        <small data-qq-field="checked"></small><p class="qq-login-feedback" data-qq-field="action" role="status" hidden></p>
      </div>
    </details>
    <div class="bar"><i id="progress"></i></div>
    <div class="stats">
      <span class="stat" id="stat-open"></span>
      <span class="stat" id="stat-today"></span>
      <span class="stat" id="stat-overdue"></span>
      <span class="stat" id="stat-done"></span>
      <button id="manual-add" class="manual-add" type="button" title="手动添加待办" aria-label="手动添加待办">＋</button>
    </div>
    <div id="manual-feedback" class="manual-feedback" role="status" aria-live="polite" hidden></div>
  </div>
</header>
<dialog id="manual-dialog" class="manual-dialog" aria-labelledby="manual-title">
  <form id="manual-form">
    <h2 id="manual-title">手动添加待办</h2>
    <p class="manual-hint">补录以前的任务或遗漏事项，保存后直接进入待办。</p>
    <label>任务名称<input id="manual-summary" name="summary" maxlength="200" required placeholder="例如：提交班级活动报名表" autocomplete="off"></label>
    <div class="manual-dates">
      <label>截止日期（可选）<input id="manual-date" name="deadline_date" type="date"></label>
      <label>截止时间（可选）<input id="manual-time" name="deadline_time" type="time"></label>
    </div>
    <p class="manual-hint">按北京时间保存。只填日期时按当天 23:59；不填日期则不设截止时间，也可补录已过期任务。</p>
    <label>所属群（可选）<input id="manual-group" name="group" list="manual-group-options" maxlength="80" placeholder="选择已有群或输入群昵称" autocomplete="off"></label>
    <datalist id="manual-group-options"></datalist>
    <label>通知原文或备注（可选）<textarea id="manual-note" name="note" maxlength="8000" placeholder="可粘贴以前的群通知，方便以后核对"></textarea></label>
    <p id="manual-error" class="manual-feedback manual-error" role="alert" hidden></p>
    <div class="actions">
      <button id="manual-cancel" class="btn ghost" type="button">取消</button>
      <button id="manual-save" class="btn primary" type="submit">保存到待办</button>
    </div>
  </form>
</dialog>
<main>
  <section id="tab-tasks"></section>
  <section id="tab-notices" hidden></section>
  __PET_HTML__
  <section id="tab-calendar" hidden><div class="section-head"><h2 class="section-title">我的日历</h2><button id="course-add" class="btn primary">添加课程</button></div><p class="group-hint">北京时间 · 课程与活动按时间展示，截止日期是提醒点。课程表尚需本人提供或录入。</p><div id="calendar-error" role="status"></div><div id="calendar-view"></div><div id="calendar-undated"></div><div id="course-list"></div></section>
  <section id="tab-settings" hidden></section>
  <section id="tab-history" hidden>
    <div class="section-head"><h2 class="section-title">历史消息审查</h2></div>
    <div class="group-panel">
      <p class="group-hint">选择一个监控群，补查以前或离线时错过的消息。先读取记录，再交给 DeepSeek 审查；任务会保留原文依据。</p>
      <form id="history-form">
        <label>群聊<select id="history-group" class="correct-select" required aria-label="选择审查群聊"></select></label>
        <label>时间范围<select id="history-range" class="correct-select" aria-label="历史时间范围"><option value="7">最近一周</option><option value="30">最近一个月</option><option value="90">最近三个月</option><option value="custom">自选日期</option></select></label>
        <div id="history-dates" class="group-fields" hidden><label>起始日期<input id="history-start" type="date"></label><label>结束日期<input id="history-end" type="date"></label></div>
        <button id="history-read" class="btn primary" type="submit">读取历史记录</button>
      </form>
      <p class="group-hint">QQ 可能无法返回全部历史；单次最多读取 6000 个消息序号。只有原文明确了截止日期，且在今天或之后的任务会自动补入待办。没有明确时刻的日期暂设 23:59。</p>
      <p id="history-status" role="status" aria-live="polite">选择群聊和时间范围后开始。</p>
      <button id="history-review" class="btn primary" hidden>AI 审查并补入符合条件的任务</button>
      <button id="history-cancel" class="btn" hidden>取消本次操作</button>
      <div id="history-results"></div>
    </div>
  </section>
</main>
<nav class="tabs">
  <button data-tab="tasks" class="active" aria-label="待办">
    <span class="dot"></span>
    <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="4" y="4" width="16" height="16" rx="4"/><path d="m8 12 2.6 2.6L16.5 9"/></svg>
    <span>待办</span>
  </button>
  <button data-tab="pet" aria-label="宠物">
    <span class="dot"></span>
    <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="7" cy="6" r="2"/><circle cx="17" cy="6" r="2"/><circle cx="3.5" cy="11" r="1.5"/><circle cx="20.5" cy="11" r="1.5"/><path d="M7 20c-5 0-2-5 0-7 3-4 7-4 10 0 2 2 5 7 0 7-2 0-3-1-5-1s-3 1-5 1Z"/></svg>
    <span>宠物</span>
  </button>
  <button data-tab="history" aria-label="历史审查"><span class="dot"></span><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 10a8 8 0 1 1 1 8M4 4v6h6M12 7v5l3 2"/></svg><span>历史审查</span></button>
  <button data-tab="calendar" aria-label="日历"><span class="dot"></span><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="5" width="18" height="16" rx="3"/><path d="M7 3v4m10-4v4M3 11h18"/></svg><span>日历</span></button>
  <button data-tab="settings" aria-label="设置">
    <span class="dot"></span>
    <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-4V21a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9A1.7 1.7 0 0 0 3 14H2.8v-4H3a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1A1.7 1.7 0 0 0 9 4.6 1.7 1.7 0 0 0 10 3V2.8h4V3a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.2v4H21a1.7 1.7 0 0 0-1.6 1Z"/></svg>
    <span>设置</span>
  </button>
</nav>
<script>
var KEY = 'qq_digest_token';
var params = new URLSearchParams(location.search);
if (params.get('token')) {
  localStorage.setItem(KEY, params.get('token'));
  // 别把 token 留在地址栏 / 历史记录 / Referer 里，只存在 localStorage
  params.delete('token');
  var rest = params.toString();
  history.replaceState(null, '', location.pathname + (rest ? '?' + rest : '') + location.hash);
}
var token = localStorage.getItem(KEY) || '';
var CLIENT_VERSION = '__APP_VERSION__';
var serverReachable = false;
var lastSynced = Number(localStorage.getItem('qq_digest_last_sync') || 0);
var swRegistration = null;
function showUpdate() { document.getElementById('app-update').hidden = false; }

var canRunSW = location.protocol === 'https:' || location.hostname === 'localhost' || location.hostname === '127.0.0.1';
if ('serviceWorker' in navigator && canRunSW) {
  window.addEventListener('load', function () {
    navigator.serviceWorker.register('/sw.js', {updateViaCache:'none'}).then(function(registration) {
      swRegistration = registration;
      registration.update().catch(function(){});
      registration.addEventListener('updatefound', function() {
        var worker=registration.installing;
        if(worker)worker.addEventListener('statechange',function(){if(worker.state==='installed' && navigator.serviceWorker.controller)showUpdate();});
      });
    }).catch(function () {});
  });
  navigator.serviceWorker.addEventListener('controllerchange', showUpdate);
}

// 离线只读：断网时页面读缓存、写操作直接拦下
var offlineBanner = document.getElementById('offline');
function setOnline(state) {
  document.body.classList.toggle('offline-mode', !state);
  if (offlineBanner) offlineBanner.hidden = state;
  if (typeof updatePetAvailability === 'function') updatePetAvailability();
}
setOnline(navigator.onLine !== false);
window.addEventListener('online', function () { loadTasks(); });
window.addEventListener('offline', function () { serverReachable=false; setOnline(false); drawSync('cached'); });
function isOffline() { return navigator.onLine === false || !serverReachable; }
function drawSync(state, fetchedAt) {
  var panel=document.getElementById('sync-panel');panel.dataset.state=state;
  var label=document.getElementById('sync-label');
  label.textContent=state==='live'?'已与电脑同步':state==='connecting'?'正在连接电脑…':'离线缓存 · 尚未同步';
  var stamp=fetchedAt || lastSynced;
  document.getElementById('sync-detail').textContent=(stamp?'北京时间更新于 '+new Date(stamp).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'尚未取得最新数据')+' · 界面 '+CLIENT_VERSION;
  if(state==='cached') document.getElementById('sync-detail').textContent+='；请确认电脑已唤醒、两端 Tailscale 已连接。';
  setOnline(state!=='cached');
}
function connectionHelp() {
  var dialog=el('dialog','manual-dialog');dialog.appendChild(el('h2','','手机与电脑保持连接'));
  dialog.appendChild(el('p','manual-hint','先确认电脑已唤醒且待办正在运行。手机打开 Tailscale，保持“已连接”，然后回到待办点“立即同步”。'));
  dialog.appendChild(el('p','manual-hint','小米手机：在 Tailscale 的应用设置中允许后台自启动，省电策略选“无限制”；VPN 设置如提供“始终开启”，可由你开启。菜单名称可能随系统版本变化。'));
  dialog.appendChild(el('p','manual-hint','手机有网络不等于私人连接已通。电脑休眠期间不能同步新数据，恢复后页面会自动重试；离线缓存会明确标注时间。'));
  var open=el('a','btn primary','打开手机 Tailscale');open.href='intent:#Intent;action=android.intent.action.MAIN;category=android.intent.category.LAUNCHER;package=com.tailscale.ipn;end';dialog.appendChild(open);
  var close=el('button','btn','知道了');close.onclick=function(){dialog.close();};dialog.appendChild(close);
  dialog.addEventListener('close',function(){dialog.remove();});document.body.appendChild(dialog);dialog.showModal();
}
document.getElementById('connection-help').onclick=connectionHelp;
document.getElementById('sync-now').onclick=function(){loadTasks();if(swRegistration)swRegistration.update().catch(function(){});};
document.getElementById('app-update').onclick=function(){if(isOffline())return;location.reload();};

// 安装引导：支持时显示按钮，点了调起浏览器安装
var installBtn = document.getElementById('install');
var deferredInstall = null;
window.addEventListener('beforeinstallprompt', function (event) {
  event.preventDefault();
  deferredInstall = event;
  if (installBtn && /Android|iPhone|iPad/i.test(navigator.userAgent)) installBtn.hidden = false;
});
if (installBtn) {
  installBtn.onclick = function () {
    if (!deferredInstall) return;
    deferredInstall.prompt();
    deferredInstall = null;
    installBtn.hidden = true;
  };
}
window.addEventListener('appinstalled', function () {
  deferredInstall = null;
  if (installBtn) installBtn.hidden = true;
});

function appInstallGuide(){
 var dialog=el('dialog','manual-dialog');dialog.appendChild(el('h2','','安装独立应用视图'));
 var nativeApp=/GroupTodoNative/.test(navigator.userAgent);
 if(nativeApp){dialog.appendChild(el('p','manual-hint','当前已使用安卓独立版，私人连接已内置。可从应用右上角“设置 → 内置连接”管理登录与连接。'));var done=el('button','btn','关闭');done.onclick=function(){dialog.close();};dialog.appendChild(done);dialog.addEventListener('close',function(){dialog.remove();});document.body.appendChild(dialog);dialog.showModal();return;}
 var android=el('a','btn primary','安装内置私人连接的安卓独立版');android.href='/android';dialog.appendChild(android);
 dialog.appendChild(el('p','manual-hint','安卓独立版把待办和私人连接放在一个应用里。请按安装页指引登录；验收成功后再卸载旧 Tailscale。'));
 var standalone=window.matchMedia('(display-mode: standalone)').matches || navigator.standalone;
 dialog.appendChild(el('p','manual-hint',standalone?'当前已在独立应用视图中。':'当前为浏览器页面。请在 Edge 或 Chrome 中打开，然后选择菜单“添加到主屏幕 → 安装应用”。小米浏览器创建的网页快捷方式可能仍显示浏览器顶部。'));
 var open=el('a','btn','用手机 Edge 打开待办');
 var base=location.origin+'/?token='+encodeURIComponent(token);
 open.href='intent://'+base.slice('https://'.length)+'#Intent;scheme=https;package=com.microsoft.emmx;end';
 if(location.protocol==='https:')dialog.appendChild(open);
 var copy=el('button','btn','复制自己的连接地址');copy.onclick=function(){navigator.clipboard.writeText(base).then(function(){copy.textContent='已复制，请粘贴到 Edge 或 Chrome';}).catch(function(){copy.textContent='复制失败，请从电脑重新扫码';});};dialog.appendChild(copy);
 var download=el('a','btn','Edge 官方安装说明');download.href='https://learn.microsoft.com/zh-cn/deployedge/microsoft-edge-install-mobile-china';download.target='_blank';download.rel='noreferrer';dialog.appendChild(download);
 var chrome=el('a','btn primary','用手机 Chrome 打开待办');chrome.href='intent://'+base.slice('https://'.length)+'#Intent;scheme=https;package=com.android.chrome;end';dialog.appendChild(chrome);
 var close=el('button','btn','关闭');close.onclick=function(){dialog.close();};dialog.appendChild(close);
 dialog.addEventListener('close',function(){dialog.remove();});document.body.appendChild(dialog);dialog.showModal();
}

function api(path, options) {
  options = options || {};
  options.headers = Object.assign({'X-Token': token,'X-Client-Version':CLIENT_VERSION,'X-Display-Mode':/GroupTodoNative/.test(navigator.userAgent)?'native':window.matchMedia('(display-mode: standalone)').matches?'standalone':'browser'}, options.headers || {});
  options.cache='no-store';
  var controller=new AbortController();options.signal=controller.signal;
  var timeout=setTimeout(function(){controller.abort();},12000);
  return fetch(path, options).then(function (response) {
    return response.json().then(function(data) {
      if (!response.ok) {if(response.status===401)throw new Error('授权已失效，请在电脑设置里重新扫码连接。');throw new Error(data.error || ('HTTP ' + response.status));}
      if(path==='/api/tasks') {
        serverReachable=response.headers.get('X-QQ-Digest-Cache')!=='offline';
        var fetched=Number(response.headers.get('X-QQ-Digest-Fetched-At')||0);
        if(serverReachable){lastSynced=Date.now();localStorage.setItem('qq_digest_last_sync',String(lastSynced));}
        drawSync(serverReachable?'live':'cached',serverReachable?lastSynced:fetched);
        if(serverReachable && data.sync && data.sync.version!==CLIENT_VERSION)showUpdate();
      }
      return data;
    });
  }).finally(function(){clearTimeout(timeout);});
}

function el(tag, cls, text) {
  var node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function actionButton(label, action, cls) {
  var button = el('button', 'btn ' + (cls || ''), label);
  button.onclick = function () { sendAction(button.__task, action); };
  return button;
}


var CATEGORY_LABELS = {urgent: '紧急', action: '待办', academic: '学业', info: '通知'};

function correctionButton(label, correction, cls, value) {
  var button = el('button', 'correct-btn ' + (cls || ''), label);
  button.onclick = function () {
    var actual = typeof value === 'function' ? value() : value;
    sendCorrection(button.__task, correction, actual);
  };
  return button;
}

function correctionPanel(task) {
  var button=el('button','btn correction-trigger','纠错');button.type='button';button.onclick=function(){openCorrection(task);};return button;
}
/* 折叠开关：短标签给眼睛看，完整名称给读屏和长按提示看 */
function toggleSummary(label, short) {
  var node = el('summary', '', short);
  node.setAttribute('aria-label', label);
  node.title = label;
  return node;
}
function taskNode(task) {
  var status = task.status || (task.done ? 'done' : 'open');
  var candidate = status === 'candidate';
  var category = CATEGORY_LABELS[task.category] ? task.category : 'info';
  var overdue = Boolean(task.overdue && !task.done);
  var classes = ['task', category];
  if (candidate) classes.push('candidate');
  if (task.done) classes.push('done');
  if (overdue) classes.push('overdue');
  var li = el('li', classes.join(' '));
  li.id = 'task-' + task.id;
  if (candidate) {
    var candidateMark = el('span', 'candidate-mark', '?');
    candidateMark.setAttribute('aria-hidden', 'true');
    li.appendChild(candidateMark);
  } else {
    var check = el('button', 'check');
    var checkHit = el('span', 'check-hit');
    checkHit.setAttribute('aria-hidden', 'true');
    check.appendChild(checkHit);
    check.setAttribute('aria-label', task.done ? '恢复待办' : '标记完成');
    check.setAttribute('aria-pressed', task.done ? 'true' : 'false');
    check.title = task.done ? '恢复待办' : '标记完成';
    check.onclick = function () { toggleTask(task); };
    li.appendChild(check);
  }
  var body = el('div', 'body');
  var top = el('div', 'card-top');
  top.appendChild(el('span', 'tag ' + category, CATEGORY_LABELS[category]));
  if (task.source === 'manual') top.appendChild(el('span', 'group-chip', '手动添加'));
  if (task.source === 'history_review') top.appendChild(el('span', 'group-chip', '历史审查'));
  if (candidate) top.appendChild(el('span', 'tag candidate', '待确认'));
  var plan=task.presentation || {},phases=plan.milestones || [];
  if (task.deadline_text && !phases.length) top.appendChild(el('span', 'deadline-chip' + (overdue ? ' over' : ''), task.deadline_text));
  if (task.snoozed) top.appendChild(el('span', 'snooze-chip', '稍后 ' + (task.snooze_text || '')));
  if (task.groups && task.groups.length) top.appendChild(el('span', 'group-chip', task.groups.join('、')));
  body.appendChild(top);
  if(task.countdown)body.appendChild(el('div','countdown'+(overdue?' over':''),task.countdown));
  var brief=task.brief || {};
  body.appendChild(el('div', 't', brief.what || task.action || task.summary || task.text || ''));
  [['时间',brief.when || '原文未明确时间'],['地点',brief.where || '原文未注明地点']].forEach(function(pair){var row=el('div','task-fact'+(pair[0]==='时间'?' time-fact':''));row.appendChild(el('b','',pair[0]));row.appendChild(el('span','',pair[1]));body.appendChild(row);});
  if(phases.length){
    var timeline=el('div','milestones');var doneCount=phases.filter(function(p){return p.done || task.done;}).length;
    timeline.appendChild(el('h4','','阶段进度 '+doneCount+'/'+phases.length));
    phases.forEach(function(phase){var done=phase.done || task.done;var row=el('div','milestone'+(done?' done':''));var copy=el('div','milestone-copy');copy.appendChild(el('strong','',phase.label));copy.appendChild(el('small','',phase.when || '时间待核对'));row.appendChild(copy);var button=el('button','btn'+(done?'':' primary'),done?'已完成':'完成阶段');button.setAttribute('aria-label',(done?'恢复':'完成')+phase.label);button.onclick=function(){sendAction(task,done?'stage_reopen':'stage_done',{milestone_id:phase.id});};row.appendChild(button);timeline.appendChild(row);});body.appendChild(timeline);
  }
  if (task.duplicate_summary) {
    body.appendChild(el('div', 'duplicate-note', '与「' + task.duplicate_summary + '」重复'));
  }
  if (candidate) {
    body.appendChild(el('div', 'confidence', (task.confidence_text || '') + ' · ' + (task.confidence_reason || '')));
    if (task.confidence_why) body.appendChild(el('div', 'confidence', '依据：' + task.confidence_why));
    var actions = el('div', 'actions');
    var confirm = actionButton('确认待办', 'confirm', 'primary');
    var dismiss = actionButton('忽略', 'dismiss', 'ghost');
    var snooze = actionButton('明天提醒', 'snooze', 'ghost');
    confirm.__task = task; dismiss.__task = task; snooze.__task = task;
    actions.appendChild(confirm); actions.appendChild(dismiss); actions.appendChild(snooze);
    body.appendChild(actions);
  }
  // 卡片里的折叠开关并成一行（详细 / 原文 / 纠错）：短标签省高度，完整名称放 aria-label + title
  var toggles = el('div', 'toggle-row');
  if ((task.details && task.details.length) || task.audience || task.condition) {
    var detailBox = el('details');
    detailBox.appendChild(toggleSummary('详细要求', '详细'));
    var detailList = el('ul', 'detail-list');
    if(task.audience)detailList.appendChild(el('li','','适用对象：'+task.audience));
    if(task.condition)detailList.appendChild(el('li','','条件：'+task.condition));
    (task.details || []).forEach(function (value) { detailList.appendChild(el('li', '', value)); });
    detailBox.appendChild(detailList);
    toggles.appendChild(detailBox);
  }
  var fullText = String(task.source_text || '').trim();
  var evidenceText = String(task.evidence || '').trim();
  if (fullText || (evidenceText && evidenceText !== task.summary)) {
    var detail = el('details');
    var noteLabel = task.source === 'manual' ? '补充' : '原文';
    var noteTitle = task.source === 'manual' ? '查看手动补充的原文或备注' : '查看完整原文';
    var detailSummary = toggleSummary(noteTitle, noteLabel);
    detail.appendChild(detailSummary);
    detail.appendChild(el('p', '', fullText || evidenceText));
    (task.extra_sources||[]).forEach(function(source){detail.appendChild(el('h4','','关联通知依据'));detail.appendChild(el('p','',source.quote));});
    detail.addEventListener('toggle', function () {
      detailSummary.textContent = detail.open ? '收起' : noteLabel;
      detailSummary.setAttribute('aria-label', detail.open ? '收起' + noteLabel : noteTitle);
    });
    toggles.appendChild(detail);
  }
  toggles.appendChild(correctionPanel(task));
  if(task.review){var labels={queued:'正在排队修正',running:'DeepSeek 正在修正，原待办暂时保留',done:'已根据你的说明修正',failed:'修正未成功',needs_input:'需要你补充说明',interrupted:'上次修正已中断',undone:'上次修正已撤回'};var feedback=el('p','review-feedback'+(['failed','needs_input','interrupted'].includes(task.review.status)?' error':''),(labels[task.review.status]||'')+(task.review.error?'：'+task.review.error:''));body.appendChild(feedback);}
  body.appendChild(toggles);
  li.appendChild(body);
  return li;
}

function section(title, tasks, options) {
  tasks = tasks || [];
  options = options || {};
  if (!tasks.length) return null;
  var wrap = el(options.collapsed ? 'details' : 'section', 'section ' + (options.className || ''));
  var head = el(options.collapsed ? 'summary' : 'div', 'section-head');
  head.appendChild(el('h2', 'section-title', title));
  head.appendChild(el('span', 'count-pill', tasks.length + ' 件'));
  wrap.appendChild(head);
  var list = el('ul');
  tasks.forEach(function (task) { list.appendChild(taskNode(task)); });
  wrap.appendChild(list);
  return wrap;
}

function showStat(id, text, show) {
  var node = document.getElementById(id);
  node.textContent = text;
  node.classList.toggle('show', Boolean(show));
}

function render(data) {
  if (data.pet) renderPet(data.pet);
  var progress = Number(data.progress || 0);
  document.getElementById('headline').textContent = data.headline || '今天没有待办';
  document.getElementById('subline').textContent = data.subline || '';
  document.getElementById('progressLabel').textContent = progress + '%';
  document.getElementById('progress').style.width = progress + '%';
  var stats = data.stats || {};
  showStat('stat-open', '未完成 ' + (stats.open || 0), (stats.open || 0) > 0);
  showStat('stat-overdue', '已逾期 ' + (stats.overdue || 0), (stats.overdue || 0) > 0);
  showStat('stat-done', '已完成 ' + (stats.done || 0), true);
  showStat('stat-today','今日 '+((data.today||[]).filter(function(t){return !t.overdue;}).length)+' 件',true);
  var intake = data.intake || {};
  var collector = data.collector || {};
  var warning=document.getElementById('collector-status');warning.hidden=!isOffline()&&collector.online;
  warning.textContent=isOffline()?'当前显示离线缓存，恢复私人连接后会重新同步。':collector.online?'':collector.message||'QQ 采集未连接，请展开“QQ 登录与采集”处理。';
  renderQQSession(collector);
  var today = data.today || [];
  var overdue = today.filter(function (task) { return task.overdue; });
  var dueToday = today.filter(function (task) { return !task.overdue; });
  var blocks = [
    section('已过期', overdue, {className: 'overdue'}),
    section('今天', dueToday),
    section('待确认', data.candidates || []),
    section('本周', data.week || []),
    section('以后', data.later || []),
    section('已完成', data.done || [], {className: 'done', collapsed: true})
  ].filter(Boolean);
  var root = document.getElementById('tab-tasks');
  root.textContent = '';
  if (!blocks.length) {
    root.appendChild(el('div', 'empty', '今天没有需要处理的事项'));
  } else {
    blocks.forEach(function (block) { root.appendChild(block); });
  }
  if (location.hash) {
    var focused = document.querySelector(location.hash);
    if (focused) setTimeout(function () { focused.scrollIntoView({block: 'center'}); }, 40);
  }
}

function sendAction(task, action, detail) {
  if (isOffline()) { alert('离线状态：只读缓存，暂不能修改；连上网再试。'); return; }
  api('/api/tasks/' + task.id, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(Object.assign({action: action},detail || {}))
  }).then(function(result){if(result.reward)showPetReward(result.reward);return loadTasks();}).catch(function (error) { alert('操作失败：' + error.message); });
}

function sendCorrection(task, correction, value) {
  if (isOffline()) { alert('离线状态：只读缓存，暂不能纠错；连上网再试。'); return; }
  api('/api/tasks/' + task.id, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action: 'correct', correction: correction, value: value || ''})
  }).then(loadTasks).catch(function (error) { alert('纠错失败：' + error.message); });
}

function toggleTask(task) {
  sendAction(task, task.done ? 'reopen' : 'done');
}

var taskLoad=null;
function loadTasks() {
  if(taskLoad)return taskLoad;
  document.getElementById('sync-now').disabled=true;
  if(!serverReachable)drawSync('connecting');
  taskLoad=api('/api/tasks').then(render).catch(function(error){
    serverReachable=false;drawSync('cached');
    document.getElementById('subline').textContent=error.name==='AbortError'?'连接超时，正在等待电脑和私人网络恢复。':error.message;
  }).finally(function(){taskLoad=null;document.getElementById('sync-now').disabled=false;});
  return taskLoad;
}
function resumeSync(){if(document.visibilityState==='hidden')return;loadTasks();if(swRegistration)swRegistration.update().catch(function(){});}
document.addEventListener('visibilitychange',resumeSync);
window.addEventListener('pageshow',resumeSync);
window.addEventListener('focus',resumeSync);

var manualDialog = document.getElementById('manual-dialog');
var manualForm = document.getElementById('manual-form');
var manualSave = document.getElementById('manual-save');
var manualCancel = document.getElementById('manual-cancel');
var manualBusy = false;
var manualRequestId = '';
var manualLastBody = '';
document.getElementById('manual-add').onclick = function () {
  document.getElementById('manual-error').hidden = true;
  manualDialog.showModal();
  document.getElementById('manual-summary').focus();
  api('/api/groups').then(function (data) {
    var list = document.getElementById('manual-group-options'); list.textContent = '';
    (data.groups || []).forEach(function (item) { var option = el('option'); option.value = item.name; list.appendChild(option); });
  }).catch(function () {});
};
manualCancel.onclick = function () { if (!manualBusy) manualDialog.close(); };
manualDialog.addEventListener('cancel', function (event) { if (manualBusy) event.preventDefault(); });
manualForm.onsubmit = function (event) {
  event.preventDefault(); if (manualBusy) return;
  var errorBox = document.getElementById('manual-error');
  function failed(message) { errorBox.textContent = message; errorBox.hidden = false; }
  if (isOffline()) { failed('当前离线，请恢复连接后再保存。已填写内容会保留。'); return; }
  var payload = Object.fromEntries(new FormData(manualForm).entries());
  payload.summary = payload.summary.trim();
  if (!payload.summary) { failed('请填写任务名称。'); return; }
  if (payload.deadline_time && !payload.deadline_date) { failed('填写截止时间时，请同时选择截止日期。'); return; }
  var bodyKey = JSON.stringify(payload);
  if (!manualRequestId || bodyKey !== manualLastBody) { manualRequestId = crypto.randomUUID(); manualLastBody = bodyKey; }
  payload.client_id = manualRequestId;
  manualBusy = true; manualSave.disabled = true; manualCancel.disabled = true; errorBox.hidden = true;
  manualSave.textContent = '正在保存……';
  fetch('/api/tasks/manual', {method:'POST', headers:{'X-Token':token, 'Content-Type':'application/json'}, body:JSON.stringify(payload)})
    .then(function (response) { return response.json().then(function (data) { if (!response.ok) throw new Error(data.error || '保存未成功'); return data; }); })
    .then(function () {
      manualDialog.close(); manualForm.reset(); manualRequestId = ''; manualLastBody = '';
      document.querySelector('[data-tab="tasks"]').click();
      var message = document.getElementById('manual-feedback');
      message.textContent = '已添加到待办，可查看补充内容或勾选完成。'; message.hidden = false;
      loadTasks();
    })
    .catch(function (error) { failed('保存未完成：' + error.message + '。填写内容已保留，可重试。'); })
    .finally(function () { manualBusy = false; manualSave.disabled = false; manualCancel.disabled = false; manualSave.textContent = '保存到待办'; });
};

function loadNotices() {
  var root = document.getElementById('tab-notices');
  root.textContent = '';
  api('/api/notices').then(function (data) {
    var head = el('div', 'section-head');
    head.appendChild(el('h2', 'section-title', '最近推送'));
    root.appendChild(head);
    if (!data.items.length) {
      root.appendChild(el('div', 'empty', '还没有记录'));
      return;
    }
    var list = el('ul');
    data.items.forEach(function (item) {
      var card = el('li', 'notice');
      card.appendChild(el('h3', '', item.summary || item.kind || '摘要'));
      card.appendChild(el('p', '', item.body || ''));
      list.appendChild(card);
    });
    root.appendChild(list);
  });
}

/* 外观：跟随系统 / 浅色 / 深色。真正生效在 head 的内联脚本里，这里只负责改设置并即时应用 */
function themeRow() {
  var wrap = el('div', 'theme-row');
  var current = (window.qqTheme && window.qqTheme.mode()) || 'auto';
  [['auto', '跟随系统'], ['light', '浅色'], ['dark', '深色']].forEach(function (item) {
    var button = el('button', 'theme-btn' + (item[0] === current ? ' active' : ''), item[1]);
    button.type = 'button';
    button.onclick = function () {
      if (window.qqTheme) window.qqTheme.set(item[0]);
      wrap.querySelectorAll('.theme-btn').forEach(function (other) { other.classList.remove('active'); });
      button.classList.add('active');
    };
    wrap.appendChild(button);
  });
  return wrap;
}
function loadSettings() {
  var root = document.getElementById('tab-settings');
  root.textContent = '';
  root.appendChild(qqLoginCard());
  loadGroupManager(root);
  var tools=el('div','group-panel');root.appendChild(tools);
  tools.appendChild(el('h2','section-title','使用与手机日历'));
  var mobileButton=el('button','btn primary','手机连接与扫码');mobileButton.onclick=openMobileSetup;tools.appendChild(mobileButton);
  var appButton=el('button','btn','安装独立应用视图');appButton.onclick=appInstallGuide;tools.appendChild(appButton);
  tools.appendChild(el('p','group-hint','先预览并选择需要的日程，再导出给支持 .ics 的手机日历。导入后可离线查看；当前尚未连接自动同步。'));
  var calendarButton=el('button','btn primary','预览并导出手机日历');calendarButton.onclick=openCalendarPreview;tools.appendChild(calendarButton);
  var correctionsButton=el('button','btn','纠错记录与撤回');correctionsButton.onclick=openCorrectionsLog;tools.appendChild(correctionsButton);
  var stopButton=el('button','btn ghost','停止消息助手');stopButton.onclick=function(){
    if(!window.confirm('停止消息采集与待办工作台？普通 QQ 会继续运行。下次双击桌面图标可重新启动。'))return;
    stopButton.disabled=true;api('/api/app/stop',{method:'POST'}).then(function(){stopButton.textContent='已请求停止，可以关闭窗口';}).catch(function(error){stopButton.disabled=false;stopButton.textContent=error.message;});
  };tools.appendChild(stopButton);
  api('/api/meta').then(function (data) {
    var themeHead = el('div', 'section-head');
    themeHead.appendChild(el('h2', 'section-title', '外观'));
    root.appendChild(themeHead);
    root.appendChild(themeRow());
    var head = el('div', 'section-head');
    head.appendChild(el('h2', 'section-title', '运行信息'));
    root.appendChild(head);
    var rows = [
      ['推送通道', (data.channels || []).join('、') || '无'],
      ['截止提醒', data.reminders || '未开启'],
      ['推送预算', data.push_budget || '不限'],
      ['夜间静默', data.quiet_hours || '未设置'],
      ['待办统计', '共 ' + data.stats.total + ' 件，未完成 ' + data.stats.open + ' 件'],
      ['服务时间', data.started_at || '未知']
    ];
    rows.forEach(function (row) {
      var kv = el('div', 'kv');
      kv.appendChild(el('span', '', row[0]));
      kv.appendChild(el('span', '', row[1]));
      root.appendChild(kv);
    });
    var insights = data.insights || [];
    if (insights.length) {
      var tipHead = el('div', 'section-head');
      tipHead.appendChild(el('h2', 'section-title', '纠错提示'));
      root.appendChild(tipHead);
      var tipList = el('ul', 'insight-list');
      insights.forEach(function (text) { tipList.appendChild(el('li', '', text)); });
      root.appendChild(tipList);
    }
  });
}

function loadGroupManager(root) {
  var head = el('div', 'section-head');
  head.appendChild(el('h2', 'section-title', '监控群管理'));
  root.appendChild(head);
  var panel = el('div', 'group-panel'); root.appendChild(panel);
  panel.appendChild(el('p', 'group-hint', '添加你现有 QQ 账号已经加入的群。保存后立即收集新消息，由 DeepSeek 整理。以前或离线错过的消息，请到“历史审查”选择范围补查。'));
  var form = el('form'); panel.appendChild(form);
  var fields = el('div', 'group-fields'); form.appendChild(fields);
  var nameLabel = el('label', '', '群昵称');
  var name = el('input'); name.id = 'group-name'; name.required = true; name.maxLength = 80;
  name.placeholder = '例如：新加入的课程群'; name.autocomplete = 'off';
  nameLabel.appendChild(name); fields.appendChild(nameLabel);
  var idLabel = el('label', '', '群聊号');
  var groupId = el('input'); groupId.id = 'group-number'; groupId.required = true;
  groupId.maxLength = 12; groupId.inputMode = 'numeric'; groupId.pattern = '[1-9][0-9]{4,11}';
  groupId.placeholder = '输入群聊号'; groupId.autocomplete = 'off';
  idLabel.appendChild(groupId); fields.appendChild(idLabel);
  var actions = el('div', 'actions'); form.appendChild(actions);
  var save = el('button', 'btn primary', '添加群'); save.type = 'submit'; save.disabled = true; actions.appendChild(save);
  var cancel = el('button', 'btn ghost', '取消修改'); cancel.type = 'button'; cancel.hidden = true; actions.appendChild(cancel);
  var status = el('p', 'group-status', '正在读取已保存的群……'); status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite'); panel.appendChild(status);
  var list = el('div'); panel.appendChild(list);
  var busy = false;
  function reset() { form.reset(); groupId.readOnly = false; save.textContent = '添加群'; cancel.hidden = true; }
  cancel.onclick = reset;
  function draw(data) {
    list.textContent = '';
    if (!data.groups.length) list.appendChild(el('p', 'group-hint', '尚未监控任何群。添加后才会收集消息。'));
    data.groups.forEach(function (item) {
      var row = el('div', 'group-entry'); var label = el('div', '', item.name);
      label.appendChild(el('small', '', '群号 ' + item.group_id)); row.appendChild(label);
      var buttons = el('div', 'actions'); row.appendChild(buttons);
      var edit = el('button', 'btn ghost', '改名'); edit.type = 'button';
      edit.onclick = function () { if (busy) return; name.value = item.name; groupId.value = item.group_id; groupId.readOnly = true; save.textContent = '保存名称'; cancel.hidden = false; name.focus(); };
      buttons.appendChild(edit);
      var remove = el('button', 'btn ghost', '停止监控'); remove.type = 'button';
      remove.onclick = function () {
        if (busy || !window.confirm('停止监控“' + item.name + '”？此前的消息和待办会保留，不会退出 QQ 群。')) return;
        change({action:'remove', group_id:item.group_id}, '已停止收集该群的新消息，原有记录保留。');
      }; buttons.appendChild(remove); list.appendChild(row);
    });
  }
  function change(payload, message) {
    if (isOffline()) { status.textContent = '当前离线，连接恢复后再保存。'; return; }
    busy = true; save.disabled = true; status.textContent = '正在保存……';
    fetch('/api/groups', {method:'POST', headers:{'X-Token':token, 'Content-Type':'application/json'}, body:JSON.stringify(payload)})
      .then(function (response) { return response.json().then(function (data) { if (!response.ok) throw new Error(data.error || '保存失败'); return data; }); })
      .then(function (data) { draw(data); reset(); status.textContent = message; })
      .catch(function (error) { status.textContent = '未保存：' + error.message + '。请检查后重试。'; })
      .finally(function () { busy = false; save.disabled = false; });
  }
  form.onsubmit = function (event) {
    event.preventDefault(); if (busy) return;
    change({action:'upsert', group_id:groupId.value.trim(), name:name.value.trim()}, '已保存。QQ 在线时会收集该群的新消息，无需重启或重新登录。');
  };
  api('/api/groups').then(function (data) { draw(data); save.disabled = !data.editable; status.textContent = '已保存 ' + data.groups.length + ' 个群。同一群号再次提交只会更新名称。'; })
    .catch(function () { status.textContent = '群列表读取失败，请重新打开设置页。'; });
}

function openMobileSetup(){
 var dialog=el('dialog','manual-dialog');dialog.appendChild(el('h2','','Redmi K70 手机连接'));
 dialog.appendChild(el('p','manual-hint','电脑和手机的 Tailscale 请登录同一个账号，手机端保持连接。'));
 var status=el('p','group-hint','正在检查连接状态…');status.setAttribute('role','status');dialog.appendChild(status);
 var setup=el('button','btn primary','继续连接设置');setup.onclick=function(){api('/api/mobile/setup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'continue'})}).then(function(){status.textContent=loaded?'已请求在电脑置顶显示扫码窗口。':'连接助手已启动，请完成官方登录与手机连接。';}).catch(function(error){status.textContent=error.message;});};dialog.appendChild(setup);
 var login=el('button','btn','打开电脑官方登录页');login.onclick=function(){api('/api/mobile/setup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'login'})}).then(function(){status.textContent='已请求打开电脑官方登录页，请在电脑上完成登录。';}).catch(function(error){status.textContent=error.message;});};dialog.appendChild(login);
 var install=el('details');var summary=el('summary','','手机尚未安装？展开扫码下载官方应用');install.appendChild(summary);
 var installImage=el('img');installImage.src='/api/mobile/qr.svg?kind=install';installImage.alt='官方 Tailscale Android 安装包';installImage.style.cssText='display:block;width:220px;max-width:100%;background:white;margin:14px auto';install.appendChild(installImage);dialog.appendChild(install);
 var pairing=el('div');pairing.hidden=true;dialog.appendChild(pairing);
 pairing.appendChild(el('p','','用手机浏览器扫码打开同一个工作台；随后可在浏览器菜单中添加到主屏幕。'));
 var image=el('img');image.alt='自己的手机访问工作台二维码';image.style.cssText='display:block;width:260px;max-width:100%;background:white;margin:14px auto';pairing.appendChild(image);
 var hint=el('p','manual-hint','此二维码用于自己的手机。电脑开机且助手运行时可更新；电脑关机后只能查看已缓存的待办。');pairing.appendChild(hint);
 var close=el('button','btn','关闭');close.onclick=function(){dialog.close();};dialog.appendChild(close);
 var timer=null,loaded=false;
 function poll(){api('/api/mobile').then(function(data){if(!dialog.open)return;
   status.textContent='电脑：'+(data.connected?'已连接':data.installed?'待登录或连接':'待完成安装')+' · 手机：'+(data.android_online?'已检测到同账号 Android 在线':'等待手机连接')+' · 工作台：'+(data.private_ready?'私人访问已就绪':'等待电脑完成连接设置');
   setup.textContent=data.private_ready?'在电脑置顶显示二维码':'继续连接设置';
   if(data.private_ready){pairing.hidden=false;if(!loaded){image.src='/api/mobile/qr.svg?kind=workbench';loaded=true;}}else pairing.hidden=true;
   install.open=!data.android_online;
 }).catch(function(error){status.textContent=error.message;}).finally(function(){if(dialog.open)timer=setTimeout(poll,4000);});}
 dialog.addEventListener('close',function(){if(timer)clearTimeout(timer);dialog.remove();});document.body.appendChild(dialog);dialog.showModal();poll();
}
function openCalendarPreview(){
 var dialog=el('dialog','manual-dialog');dialog.appendChild(el('h2','','导出手机日历'));
 dialog.appendChild(el('p','manual-hint','只列出今天及以后、有明确截止时间的未完成任务。请勾选后下载；软件不会直接写入你的手机日历。'));
 var list=el('div');dialog.appendChild(list);list.textContent='正在读取可导出的任务…';
 var footer=el('div','manual-actions');dialog.appendChild(footer);
 var cancel=el('button','btn','取消');cancel.onclick=function(){dialog.close();};footer.appendChild(cancel);
 var download=el('button','btn primary','下载所选日历');download.disabled=true;footer.appendChild(download);
 dialog.addEventListener('close',function(){dialog.remove();});document.body.appendChild(dialog);dialog.showModal();
 api('/api/calendar/preview').then(function(data){list.textContent='';(data.tasks||[]).forEach(function(task){
   var row=el('label');row.style.cssText='display:flex;align-items:flex-start;gap:10px;margin:14px 0';var check=el('input');check.type='checkbox';check.checked=true;check.value=task.id;check.style.cssText='width:18px;flex:none;margin-top:4px';row.appendChild(check);row.appendChild(el('span','',task.summary+' · '+task.deadline.replace('T',' ')));list.appendChild(row);
 });if(!data.tasks.length)list.textContent='目前没有符合条件的日程。';download.disabled=!data.tasks.length;}).catch(function(error){list.textContent=error.message;});
 download.onclick=function(){var ids=Array.from(list.querySelectorAll('input:checked')).map(function(check){return check.value;});if(!ids.length){download.textContent='请先勾选日程';return;}download.disabled=true;
  fetch('/api/calendar.ics?ids='+encodeURIComponent(ids.join(',')),{headers:{'X-Token':token}}).then(function(response){if(!response.ok)throw Error('日历导出未完成');return response.blob();}).then(function(blob){var url=URL.createObjectURL(blob);var a=el('a');a.href=url;a.download='群消息待办.ics';a.click();setTimeout(function(){URL.revokeObjectURL(url);},5000);download.disabled=false;download.textContent='已下载，可导入手机日历';}).catch(function(error){download.disabled=false;download.textContent=error.message;});
 };
}
var historyJob = null, historyTimer = null;
function historyPost(path, body) { return api(path, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}); }
function historyError(error) { document.getElementById('history-status').textContent = error.message || '操作未完成，请稍后重试。'; document.getElementById('history-read').disabled=false; }
function drawHistory(job) {
  historyJob=job.id;
  var busy=job.status==='reading'||job.status==='reviewing';
  var labels={reading:'正在读取历史',ready:'历史已读取',reviewing:'AI 正在审查',reviewed:'审查完成',failed:'操作未完成',cancelled:'已取消',interrupted:'上次操作已中断'};
  document.getElementById('history-status').textContent=(labels[job.status]||job.status)+' · '+job.group_name+' · 已取得 '+job.count+' 条文字记录 · '+job.pages+' 页'+(job.reviewed!==undefined?' · AI 批次 '+job.reviewed+'/'+(job.batches||'?'):'')+' · 新增待办 '+(job.added||0)+' 件。'+(job.coverage||'')+(job.error||'');
  var review=document.getElementById('history-review');review.hidden=busy||!job.count||!['ready','reviewed','failed'].includes(job.status);review.textContent='将这 '+job.count+' 条记录交给 DeepSeek 审查并补入任务';
  document.getElementById('history-cancel').hidden=!busy;
  document.getElementById('history-read').disabled=busy;
  var results=document.getElementById('history-results');results.textContent='';
  (job.items||[]).forEach(function(item){
    var card=el('article','card');card.appendChild(el('h3','',item.summary));
    card.appendChild(el('p','group-hint',(item.deadline_date?'截止日期 '+item.deadline_date+' · ':'')+item.reason+' · '+item.result));
    var evidence=el('details');evidence.appendChild(el('summary','','查看原文依据 · '+item.source_time.replace('T',' ')));evidence.appendChild(el('p','',item.quote));
    if(item.deadline_quote)evidence.appendChild(el('p','','截止原文：'+item.deadline_quote));card.appendChild(evidence);
    if(!item.task_id){var add=el('button','btn','补充信息并加入待办');add.onclick=function(){manualForm.reset();manualRequestId='';manualLastBody='';document.getElementById('manual-add').click();manualForm.elements.summary.value=item.summary;manualForm.elements.group.value=job.group_name;var nl=String.fromCharCode(10);manualForm.elements.note.value=item.quote+nl+'原消息时间：'+item.source_time+(item.deadline_quote?nl+'截止原文：'+item.deadline_quote:'');manualForm.elements.deadline_date.value=item.deadline_date||'';manualForm.elements.deadline_time.value=item.deadline_time||'';};card.appendChild(add);}
    results.appendChild(card);
  });
  if(historyTimer)clearTimeout(historyTimer);
  if(busy)historyTimer=setTimeout(function(){api('/api/history/'+job.id).then(drawHistory).catch(historyError);},1600);
  else if(job.status==='reviewed')loadTasks();
}
function loadHistory() {
  api('/api/groups').then(function(data){var select=document.getElementById('history-group');var old=select.value;select.textContent='';(data.groups||[]).forEach(function(g){var option=el('option','',g.name+' · '+g.group_id);option.value=g.group_id;select.appendChild(option);});if(old)select.value=old;}).catch(historyError);
  if(historyJob)api('/api/history/'+historyJob).then(drawHistory).catch(historyError);
  else api('/api/history').then(function(data){if(data.jobs&&data.jobs.length)drawHistory(data.jobs[0]);}).catch(historyError);
}
document.getElementById('history-range').onchange=function(){document.getElementById('history-dates').hidden=this.value!=='custom';};
document.getElementById('history-form').onsubmit=function(event){event.preventDefault();var range=document.getElementById('history-range').value;var body={group_id:document.getElementById('history-group').value,days:range==='custom'?7:Number(range)};if(range==='custom'){body.start_date=document.getElementById('history-start').value;body.end_date=document.getElementById('history-end').value;}document.getElementById('history-read').disabled=true;historyPost('/api/history',body).then(drawHistory).catch(historyError);};
document.getElementById('history-review').onclick=function(){this.hidden=true;historyPost('/api/history/'+historyJob,{action:'review'}).then(drawHistory).catch(historyError);};
document.getElementById('history-cancel').onclick=function(){this.disabled=true;var button=this;historyPost('/api/history/'+historyJob,{action:'cancel'}).then(function(){button.disabled=false;document.getElementById('history-status').textContent='已请求取消，当前读取或 AI 批次返回后结束。';}).catch(function(error){button.disabled=false;historyError(error);});};

document.querySelectorAll('.tabs button').forEach(function (button) {
  button.onclick = function () {
    document.querySelectorAll('.tabs button').forEach(function (other) { other.classList.remove('active'); });
    button.classList.add('active');
    var tab = button.getAttribute('data-tab');
    history.replaceState(null, '', location.pathname + location.search + '#' + tab);
    ['tasks', 'notices', 'settings', 'history','calendar','pet'].forEach(function (name) {
      document.getElementById('tab-' + name).hidden = name !== tab;
    });
    if (tab === 'notices') loadNotices();
    if (tab === 'settings') loadSettings();
    if (tab === 'history') loadHistory();
    if (tab === 'calendar') loadCalendar();
    if (tab === 'pet') loadPet();
  };
});

__FEATURE_JS__
__QQ_LOGIN_JS__
__PET_JS__
loadTasks();
if (location.hash === '#settings') document.querySelector('[data-tab="settings"]').click();
if (location.hash === '#history') document.querySelector('[data-tab="history"]').click();
if (location.hash === '#calendar') document.querySelector('[data-tab="calendar"]').click();
if (location.hash === '#pet') document.querySelector('[data-tab="pet"]').click();
if (location.hash === '#mobile') {document.querySelector('[data-tab="settings"]').click();openMobileSetup();}
setInterval(function(){if(document.visibilityState!=='hidden')loadTasks();},15000);
</script>
</body>
</html>
"""
# 两套配色的唯一来源是 theme.py：这里只负责把它拼进 CSS 变量块，避免两边各写一份而漂移
PAGE_HTML = PAGE_HTML.replace("__THEME_DARK__", theme.css_block("dark")).replace(
    "__THEME_LIGHT__", theme.css_block("light")
).replace('__APP_VERSION__',APP_VERSION).replace('__FEATURE_JS__',(Path(__file__).resolve().parents[2]/'assets/workbench-features.js').read_text(encoding='utf8')).replace('__QQ_LOGIN_JS__',(Path(__file__).resolve().parents[2]/'assets/qq-login.js').read_text(encoding='utf8')).replace('__FEATURE_CSS__',(Path(__file__).resolve().parents[2]/'assets/workbench-features.css').read_text(encoding='utf8')+'\n'+(Path(__file__).resolve().parents[2]/'assets/qq-login.css').read_text(encoding='utf8'))
_pet_assets = Path(__file__).resolve().parents[2]/'assets'
PAGE_HTML = PAGE_HTML.replace('__PET_HTML__',(_pet_assets/'pet.html').read_text(encoding='utf8')).replace('__PET_JS__',(_pet_assets/'pet.js').read_text(encoding='utf8')).replace('</style>','\n'+(_pet_assets/'pet.css').read_text(encoding='utf8')+'\n</style>',1)


def _deadline_label(value: dt.datetime | None, now: dt.datetime) -> tuple[str, bool]:
    if not isinstance(value, dt.datetime):
        return "", False
    overdue = value < now
    if value.date() == now.date():
        return (f"今天 {value:%H:%M} 已过" if overdue else f"今天 {value:%H:%M} 截止"), overdue
    if value.date() == now.date() + dt.timedelta(days=1):
        return f"明天 {value:%H:%M} 截止", False
    # 跨天的旧截止时间要写成「已过」，否则过期任务会显示成「XX 截止」，读起来像还没到点
    return f"{value:%m-%d %H:%M} {'已过' if overdue else '截止'}", overdue and value.date() < now.date()


def snooze_default_until(settings: Settings, now: dt.datetime) -> str:
    """稍后提醒的默认时间：次日早晨的截止提醒时刻，否则次日同一时间。"""
    target = now + dt.timedelta(days=1)
    if settings.deadline_reminders_enabled:
        try:
            hour, minute = (int(part) for part in str(settings.deadline_morning).split(":", 1))
        except (TypeError, ValueError):
            hour, minute = 7, 30
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            target = target.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return iso(target)


def group_tasks(tasks: list[dict[str, Any]], now: dt.datetime) -> dict[str, Any]:
    candidate: list[dict[str, Any]] = []
    today: list[dict[str, Any]] = []
    week: list[dict[str, Any]] = []
    later: list[dict[str, Any]] = []
    done: list[dict[str, Any]] = []
    horizon = now + dt.timedelta(days=7)
    for task in tasks:
        status = str(task.get("status") or "open")
        if status in {"dismissed", "expired"}:
            continue
        deadline = parse_iso(task.get("deadline"))
        payload = dict(task)
        payload["deadline_text"], payload["overdue"] = _deadline_label(deadline, now)
        payload['brief']=card_brief(payload)
        payload['countdown']=countdown(deadline,now,payload.get('presentation',{}).get('time_kind','deadline')) if status!='done' else ''
        if deadline and status!='done' and not source_clock(payload.get('presentation',{}).get('quote') or payload.get('evidence') or '') and deadline.strftime('%H:%M')=='23:59':
            days=(deadline.date()-now.date()).days
            payload['countdown']=('今天截止（时刻未注明）' if days==0 else f'还剩 {days} 天' if days>0 else f'已逾期 {abs(days)} 天')
            payload['deadline_text']=('今天' if days==0 else f'{deadline.month}月{deadline.day}日')+'（时刻未注明）'
            payload['brief']=card_brief(payload)
        plan=payload.get('presentation') or {}
        if plan.get('time_kind')=='event':payload['deadline_text']=payload['deadline_text'].replace('截止','开始')
        active=any(not p.get('done') and (p.get('start_date') or p.get('end_date',''))<=now.date().isoformat()<=p.get('end_date','') for p in plan.get('milestones',[]))
        if payload.get('presentation',{}).get('milestones'):
            payload['presentation']={**payload['presentation'],'milestones':[{**p,'when':phase_when(p)} for p in payload['presentation']['milestones']]}
        snooze_until = parse_iso(payload.get("snooze_until"))
        payload["snoozed"] = bool(isinstance(snooze_until, dt.datetime) and snooze_until > now)
        payload["snooze_text"] = (
            snooze_until.strftime("%m-%d %H:%M") if payload["snoozed"] and snooze_until else ""
        )
        payload["done"] = status == "done"
        if status == "candidate":
            payload["confidence_text"] = confidence.confidence_text(
                float(payload.get("confidence") or 0.0)
            )
            detail = str(payload.get("candidate_detail") or "")
            try:
                parsed = json.loads(detail) if detail else {}
            except json.JSONDecodeError:
                parsed = {}
            payload["confidence_reason"] = str(parsed.get("reason") or "需要你确认后再进入正式待办。")
            payload["confidence_why"] = confidence.describe_triggers(
                parsed.get("triggers") or [], limit=3
            )
            candidate.append(payload)
            continue
        if payload["done"]:
            done.append(payload)
            continue
        if deadline is None:
            later.append(payload)
        elif deadline.date() <= now.date() or active:
            today.append(payload)
        elif deadline <= horizon:
            week.append(payload)
        else:
            later.append(payload)
    done.sort(key=lambda item: str(item.get("done_at") or ""), reverse=True)
    return {
        "candidates": candidate[:40],
        "today": today[:40],
        "week": week[:40],
        "later": later[:40],
        "done": done[:20],
    }


def overview(tasks: list[dict[str, Any]], grouped: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
    open_tasks = [task for task in tasks if str(task.get("status") or "open") == "open"]
    candidates = grouped.get("candidates") or []
    done_count = sum(1 for task in tasks if str(task.get("status") or "") == "done")
    today = grouped["today"]
    if today:
        nearest = min(
            (parse_iso(task.get("deadline")) for task in today if parse_iso(task.get("deadline"))),
            default=None,
        )
        overdue = sum(1 for task in today if task.get("overdue"))
        if overdue and overdue == len(today):
            headline = f"今天 {len(today)} 件，都已过期"
        elif overdue:
            headline = f"今天 {len(today)} 件，{overdue} 件已过期"
        elif nearest:
            headline = f"今天 {len(today)} 件，最近一件 {nearest:%H:%M} 截止"
        else:
            headline = f"今天 {len(today)} 件"
    elif candidates:
        headline = f"{len(candidates)} 条通知待确认"
    elif open_tasks:
        headline = "今天没有到期的事"
    else:
        headline = "所有待办都清空了"
    if any(task.get("overdue") for task in today):
        subline = "先处理已逾期事项，再处理今天到期的任务"
    elif today:
        subline = "按截止时间从上到下处理"
    elif candidates:
        subline = "确认后才会进入正式待办"
    elif open_tasks:
        subline = "当前没有到期事项，可按自己的节奏推进"
    else:
        subline = "可以休息一下，新的通知会自动汇总"
    actionable = len(open_tasks) + len(candidates) + done_count
    progress = round(done_count * 100 / actionable) if actionable else 0
    return {"headline": f'{now.month}月{now.day}日', "subline": subline, "progress": progress}


class _Handler(BaseHTTPRequestHandler):
    server_version = "qq-tasks/1.0"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - 与基类签名一致
        LOGGER.debug("web %s %s", self.command, urllib.parse.urlparse(self.path).path)

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command!='HEAD':self.wfile.write(body)

    def _html(self, body: str) -> None:
        raw = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        signed = self._browser_session()
        tailnet_tls = self.headers.get('Tailscale-User-Login') and self.headers.get('Host','').split(':')[0].endswith('.ts.net')
        secure = '; Secure' if self.client_address[0] in ('127.0.0.1','::1') and (self.headers.get('X-Forwarded-Proto')=='https' or tailnet_tls) else ''
        self.send_header('Set-Cookie', 'qq_digest_session='+signed+'; Path=/; HttpOnly; SameSite=Strict; Max-Age=604800'+secure)
        self.send_header('Cache-Control','no-store')
        self.send_header('Referrer-Policy','no-referrer')
        mobile=getattr(self.server,'mobile_access',None)
        if mobile and 'Android' in self.headers.get('User-Agent','') and self.client_address[0] in ('127.0.0.1','::1') and self.headers.get('Tailscale-User-Login'):
            mobile.note_phone_visit()
        self.end_headers()
        self.wfile.write(raw)

    def _asset(self, body: str, content_type: str, *, cache: str = "no-store") -> None:
        raw = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", cache)
        if content_type.startswith("text/javascript"):
            self.send_header("Service-Worker-Allowed", "/")
        self.end_headers()
        self.wfile.write(raw)

    def _browser_session(self) -> str:
        key = str(getattr(self.server,'token','')).encode('utf8')
        return hmac.new(key,b'qq-digest-browser-session-v1',hashlib.sha256).hexdigest()

    def _authorized(self) -> bool:
        token = str(getattr(self.server, "token", "") or "")
        if not token:
            return True
        expected = token.encode("utf-8")
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        supplied = list(params.get("token", [])) + [self.headers.get("X-Token", "")]
        # 恒定时间比较，避免按字符逐位泄露 token
        if any(hmac.compare_digest(str(item).encode("utf-8"), expected) for item in supplied):
            return True
        try:
            cookie=SimpleCookie(self.headers.get('Cookie',''))
            value=cookie.get('qq_digest_session')
            return bool(value and hmac.compare_digest(value.value.encode('utf8'),self._browser_session().encode('utf8')))
        except CookieError:
            return False

    def _cross_site(self) -> bool:
        """浏览器跨站 POST 会带 Origin；与 Host 不一致（含 `null`）即判为跨站，防 CSRF。"""
        origin = str(self.headers.get("Origin") or "").strip()
        if not origin:
            return False
        host = str(self.headers.get("Host") or "").strip().lower()
        return urllib.parse.urlparse(origin).netloc.lower() != host

    @property
    def store(self) -> Store:
        return self.server.store  # type: ignore[attr-defined]

    def _query(self) -> dict[str, list[str]]:
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

    def _query_int(self, name: str, default: int, low: int, high: int) -> int:
        raw = (self._query().get(name) or [str(default)])[0]
        return restapi.clamp_int(raw, default, low, high)

    def _query_flag(self, name: str) -> bool:
        value = str((self._query().get(name) or [""])[0]).strip().lower()
        return value in {"1", "true", "yes", "on"}

    def _apk_authorized(self) -> bool:
        ticket=(self._query().get('download_ticket') or [''])[0]
        return self._authorized() or android_install.valid_download_ticket(self.server.settings.data_dir.parent,self.server.token,ticket)

    def do_HEAD(self) -> None:
        path=urllib.parse.urlparse(self.path).path.rstrip('/') or '/'
        if path=='/api/mobile/android.apk':
            if self._apk_authorized():android_install.send_apk(self,self.server.settings.data_dir.parent,head_only=True)
            else:
                android_install.note_download(self,self.server.settings.data_dir.parent,401)
                self._json(401,{'error':'下载授权已失效，请回安装页重新点击。'})
            return
        self._json(405,{'error':'请使用 GET 请求。'})

    def do_GET(self) -> None:  # noqa: N802 - 基类命名
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if path in ("/health", "/api/health"):
            self._json(200, restapi.health_payload())
            return
        if path == "/manifest.webmanifest":
            self._asset(MANIFEST_JSON, "application/manifest+json; charset=utf-8")
            return
        if path == "/icon.svg":
            self._asset(ICON_SVG, "image/svg+xml; charset=utf-8", cache="public, max-age=86400")
            return
        if path == '/vendor/fullcalendar-6.1.20.min.js':
            asset=Path(__file__).resolve().parents[2]/'assets/vendor/fullcalendar-6.1.20.min.js'
            if asset.is_file():self._asset(asset.read_text(encoding='utf8'),'text/javascript; charset=utf-8',cache='public, max-age=31536000, immutable')
            else:self._json(404,{'error':'日历组件不可用'})
            return
        if path == '/vendor/canvas-confetti-1.9.4.js':
            asset=Path(__file__).resolve().parents[2]/'assets/vendor/canvas-confetti-1.9.4.js'
            self._asset(asset.read_text(encoding='utf8'),'text/javascript; charset=utf-8',cache='public, max-age=31536000, immutable')
            return
        if path in ('/icon-192.png','/icon-512.png'):
            asset=Path(__file__).resolve().parents[2]/'assets'/path[1:]
            try: body=asset.read_bytes()
            except OSError:
                self._json(404,{'error':'icon unavailable'})
                return
            self.send_response(200);self.send_header('Content-Type','image/png');self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','public, max-age=86400');self.end_headers();self.wfile.write(body)
            return
        if path == "/sw.js":
            self._asset(SERVICE_WORKER_JS, "text/javascript; charset=utf-8")
            return
        if path == '/api/mobile/android.apk':
            if self._apk_authorized():android_install.send_apk(self,self.server.settings.data_dir.parent)
            else:
                android_install.note_download(self,self.server.settings.data_dir.parent,401)
                self._json(401,{'error':'下载授权已失效，请回安装页重新点击。'})
            return
        if not self._authorized():
            self._json(401, {"ok": False, "error": "invalid token"})
            return
        if path == '/android':
            self._html(android_install.install_page(self.server.token))
            return
        if path == '/api/pet':
            self._json(200,pet.snapshot(self.store));return
        if path == '/api/pet/avatar':
            raw=pet.avatar(self.store)
            if not raw:self._json(404,{'error':'尚未上传头像。'});return
            self.send_response(200);self.send_header('Content-Type','image/png');self.send_header('Content-Length',str(len(raw)));self.send_header('Cache-Control','private, no-store');self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(raw);return
        if path == '/api/qq-session':
            self._json(200,self.server.qq_session.status())
            return
        if path == '/api/mobile/android':
            self._json(200,android_install.download_info(self.server.settings.data_dir.parent,self.server.token))
            return
        if path in ('/api/mobile','/api/mobile/qr.svg'):
            mobile=getattr(self.server,'mobile_access',None)
            if mobile is None:
                self._json(403,{'error':'手机访问尚未设置。'})
                return
            try:
                if path.endswith('.svg'):
                    kind=(self._query().get('kind') or ['workbench'])[0]
                    self._asset(mobile.qr_svg(kind),'image/svg+xml; charset=utf-8')
                else:self._json(200,mobile.status())
            except ValueError as error:self._json(400,{'error':str(error)})
            return
        if path == "/api":
            self._json(200, restapi.index_payload())
            return
        if path == "/api/groups":
            manager = getattr(self.server, "group_manager", None)
            result = manager.snapshot() if manager else {
                "editable": False,
                "groups": [{"group_id": gid, "name": self.server.settings.group_aliases.get(gid, gid)}
                           for gid in self.server.settings.group_whitelist],
            }
            self._json(200, result)
            return
        if path == '/api/history' or path.startswith('/api/history/'):
            manager = getattr(self.server, 'history_review', None)
            if manager is None:
                self._json(403, {'error': '当前运行方式未开启历史审查。'})
                return
            try:
                result = {'jobs': manager.recent()} if path == '/api/history' else manager.get(path.rsplit('/', 1)[-1])
                self._json(200, result)
            except ValueError as error:
                self._json(400, {'error': str(error)})
            return
        if path in ("/api/notifications", "/api/notices"):
            self._json(
                200,
                restapi.notifications(
                    self.store, limit=self._query_int("limit", 8, 1, 50)
                ),
            )
            return
        if path == "/api/deadlines":
            self._json(
                200,
                restapi.deadlines(
                    self.store,
                    now=now_local(),
                    limit=self._query_int("limit", 200, 1, 1000),
                    include_done=self._query_flag("include_done"),
                ),
            )
            return
        if path == "/api/calendar.ics":
            include_done = self._query_flag("include_done")
            tasks = self.store.list_tasks(statuses=taskstatus.statuses_for(include_done))
            selected = (self._query().get('ids') or [''])[0]
            if selected:
                try:
                    ids = {int(value) for value in selected.split(',') if value}
                    if len(ids)>200 or not ids: raise ValueError()
                except ValueError:
                    self._json(400, {'error':'所选日程无效。'})
                    return
                tasks = [task for task in tasks if task['id'] in ids]
            body = ics.build_calendar(
                tasks,
                now=now_local(),
                include_done=include_done,
            )
            raw = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/calendar; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Disposition", 'attachment; filename="qq-digest.ics"')
            self.end_headers()
            self.wfile.write(raw)
            return
        if path == '/api/calendar/preview':
            today=now_local().date()
            tasks=[]
            for task in self.store.list_tasks():
                deadline=parse_iso(task.get('deadline',''))
                if task.get('status')=='open' and deadline and deadline.date()>=today:
                    tasks.append({key:task.get(key) for key in ['id','summary','deadline','groups']})
            self._json(200, {'tasks':tasks[:200]})
            return
        if path == "/api/conflicts":
            include_done = self._query_flag("include_done")
            self._json(
                200,
                conflicts.payload(
                    self.store.list_tasks(statuses=taskstatus.statuses_for(include_done)),
                    now=now_local(),
                    window_minutes=self._query_int("window", conflicts.DEFAULT_WINDOW_MINUTES, 0, 1440),
                    include_done=include_done,
                ),
            )
            return
        if path in ("/", "/index.html"):
            self._html(PAGE_HTML)
            return
        if path == "/api/tasks":
            now = now_local()
            tasks = self.store.list_tasks()
            source_map={}
            with self.store._connect() as db:
                for row in db.execute('SELECT task_id,msg_id,quote FROM task_sources ORDER BY rowid DESC LIMIT 2000'):
                    source_map.setdefault(row['task_id'],[]).append({'msg_id':row['msg_id'],'quote':row['quote']})
            for task in tasks:
                origin=task.get('origin_msg_id') or task['task_key']
                task['extra_sources']=[s for s in source_map.get(task['id'],[]) if s['msg_id']!=origin][:5]
            reviews=self.server.task_review.latest()
            for task in tasks:
                if task['id'] in reviews:task['review']=reviews[task['id']]
            grouped = group_tasks(tasks, now)
            payload = overview(tasks, grouped, now)
            advice=self.server.daily_advice.get(tasks,now)
            payload['subline']=advice['text'];payload['advice_source']=advice['source']
            payload["stats"] = self.store.task_stats()
            payload['pet'] = pet.snapshot(self.store)
            payload['intake'] = self.store.intake_status(now.strftime('%Y-%m-%d'))
            payload['collector'] = self.server.qq_session.status()
            payload.update(grouped)
            payload['sync']={'version':APP_VERSION,'server_time':now.isoformat(),'revision':hashlib.sha256(json.dumps(tasks,sort_keys=True,default=str).encode()).hexdigest()[:12]}
            if 'Android' in self.headers.get('User-Agent','') and self.client_address[0] in ('127.0.0.1','::1') and self.headers.get('Tailscale-User-Login'):
                version=self.headers.get('X-Client-Version','')
                mode=self.headers.get('X-Display-Mode','')
                report={'at':int(time.time()*1000),'client_version':version if len(version)<40 and all(c.isdigit() or c=='.' for c in version) else '',
                        'server_version':APP_VERSION,'display_mode':mode if mode in ('standalone','browser','native') else 'unknown','authenticated':True}
                try:(self.server.settings.data_dir.parent/'runtime/mobile-sync.json').write_text(json.dumps(report),encoding='utf8')
                except OSError:pass
            self._json(200, payload)
            return
        if path == '/api/calendar/events':
            query=self._query()
            try:self._json(200,calendar_payload(self.store,(query.get('start') or [''])[0],(query.get('end') or [''])[0]))
            except ValueError as error:self._json(400,{'error':str(error)})
            return
        if path == '/api/courses':
            self._json(200,{'courses':courses(self.store)});return
        if path == '/api/task-reviews':
            self._json(200,{'items':self.server.task_review.history()});return
        if path == "/api/insights":
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                days = int((query.get("days") or ["30"])[0])
            except (TypeError, ValueError):
                days = 30
            days = max(1, min(365, days))
            self._json(
                200,
                {
                    "stats": self.store.correction_stats(days=days),
                    "insights": self.store.correction_insights(days=days),
                },
            )
            return
        if path == "/api/meta":
            settings = self.server.settings  # type: ignore[attr-defined]
            manager = getattr(self.server, "meta_provider", None)
            info: dict[str, Any] = {}
            if callable(manager):
                try:
                    info = manager() or {}
                except Exception:  # noqa: BLE001 - 元信息失败不影响页面
                    info = {}
            self._json(
                200,
                {
                    "groups": list(info.get("groups") or []),
                    "channels": list(info.get("channels") or []),
                    "reminders": info.get("reminders") or "",
                    "started_at": info.get("started_at") or "",
                    "insights": list(info.get("insights") or []),
                    "push_budget": info.get("push_budget") or "",
                    "quiet_hours": info.get("quiet_hours") or "",
                    "stats": self.store.task_stats(),
                },
            )
            return
        if path in ("/panel", "/panel.html"):
            self._html(observe.render_page())
            return
        if path == "/api/panel":
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                days = int((query.get("days") or [str(observe.DEFAULT_DAYS)])[0])
            except (TypeError, ValueError):
                days = observe.DEFAULT_DAYS
            self._json(200, observe.payload(observe.snapshot(self.store, days=days)))
            return
        self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - 基类命名
        if not self._authorized():
            self._json(401, {"ok": False, "error": "invalid token"})
            return
        if self._cross_site():
            self._json(403, {"ok": False, "error": "cross-site request blocked"})
            return
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if path in ('/api/pet','/api/pet/avatar'):
            try:
                length=int(self.headers.get('Content-Length') or 0)
                maximum=2*1024*1024 if path.endswith('/avatar') else 1024
                if not 0<length<=maximum:raise ValueError('图片或请求过大，请缩小后重试。')
                raw=self.rfile.read(length)
                if path.endswith('/avatar'):
                    self._json(200,pet.save_avatar(self.store,raw));return
                payload=json.loads(raw.decode('utf8'))
                if not isinstance(payload,dict):raise ValueError('宠物请求格式无效。')
                if payload.get('action')=='rename':result=pet.save_name(self.store,payload.get('name'))
                elif payload.get('action')=='remove_avatar':result=pet.remove_avatar(self.store)
                else:result=pet.interact(self.store,payload)
                self._json(200,result)
            except (ValueError,TypeError,UnicodeError) as error:self._json(400,{'error':str(error)})
            except OSError:self._json(500,{'error':'宠物数据保存未完成，请稍后重试。'})
            return
        if path == '/api/qq-session':
            try:
                length=int(self.headers.get('Content-Length') or 0)
                if not 0<length<=256:raise ValueError('登录操作请求过大或为空。')
                payload=json.loads(self.rfile.read(length).decode('utf8'))
                if not isinstance(payload,dict):raise ValueError('登录操作格式无效。')
                self._json(200,self.server.qq_session.begin(payload.get('action')))
            except (ValueError,TypeError,UnicodeError) as error:self._json(400,{'error':str(error)})
            except OSError:self._json(500,{'error':'登录操作未能启动，请稍后重试。'})
            return
        if path == '/api/courses':
            try:
                length=int(self.headers.get('Content-Length') or 0)
                if not 0<length<=4096:raise ValueError('课程请求过大或为空。')
                payload=json.loads(self.rfile.read(length).decode('utf8'))
                if not isinstance(payload,dict):raise ValueError('课程格式无效。')
                self._json(200,save_course(self.store,payload))
            except (ValueError,TypeError,UnicodeError) as error:self._json(400,{'error':str(error)})
            return
        if path == '/api/mobile/setup':
            mobile=getattr(self.server,'mobile_access',None)
            try:
                length=int(self.headers.get('Content-Length') or 0)
                if not mobile or not 0<length<=512:raise ValueError('手机连接操作不可用。')
                payload=json.loads(self.rfile.read(length).decode('utf8'))
                if not isinstance(payload,dict):raise ValueError('请求格式无效。')
                self._json(200,mobile.begin_setup(payload.get('action')))
            except (ValueError,UnicodeError) as error:self._json(400,{'error':str(error)})
            except Exception:self._json(500,{'error':'连接助手暂时无法启动，请稍后重试。'})
            return
        if path == '/api/app/stop':
            handler=getattr(self.server,'stop_handler',None)
            if handler is None:
                self._json(403, {'error':'当前运行方式不支持从界面停止。'})
                return
            self._json(200, {'ok':True})
            def stop_later():
                time.sleep(.4)
                try: handler()
                except Exception: pass
            threading.Thread(target=stop_later,daemon=True).start()
            return
        if path == '/api/history' or path.startswith('/api/history/'):
            manager = getattr(self.server, 'history_review', None)
            if manager is None:
                self._json(403, {'error': '当前运行方式未开启历史审查。'})
                return
            try:
                length = int(self.headers.get('Content-Length') or 0)
                if not 0 < length <= 4096:
                    raise ValueError('请求内容过大或为空。')
                body = json.loads(self.rfile.read(length).decode('utf8'))
                if not isinstance(body, dict):
                    raise ValueError('请求格式无效。')
                action = body.get('action', 'read')
                if path == '/api/history' and action == 'read':
                    result = manager.start(body)
                elif action == 'review':
                    result = manager.review(path.rsplit('/', 1)[-1])
                elif action == 'cancel':
                    result = manager.cancel(path.rsplit('/', 1)[-1])
                else:
                    raise ValueError('不支持的操作。')
                self._json(200, result)
            except (ValueError, TypeError, UnicodeError) as error:
                self._json(400, {'error': str(error)})
            except Exception:
                self._json(500, {'error': '历史审查暂时未能启动，请稍后重试。'})
            return
        if path == "/api/tasks/manual":
            self._create_manual_task()
            return
        if not path.startswith("/api/tasks/"):
            if path == "/api/groups":
                self._update_groups()
                return
            self._json(404, {"ok": False, "error": "not found"})
            return
        try:
            task_id = int(path.rsplit("/", 1)[-1])
        except ValueError:
            self._json(400, {"ok": False, "error": "bad task id"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            self._json(400, {"ok": False, "error": "bad content length"})
            return
        if length < 0 or length > MAX_BODY_BYTES:
            self._json(400, {"ok": False, "error": "bad body"})
            return
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace") or "{}")
        except (ValueError, json.JSONDecodeError):
            self._json(400, {"ok": False, "error": "bad json"})
            return
        if not isinstance(payload,dict):
            self._json(400,{'error':'请求格式应为对象。'});return
        action = str(payload.get("action") or "").strip().lower()
        if action=='review':
            try:self._json(202,self.server.task_review.submit(task_id,payload))
            except (ValueError,TypeError) as error:self._json(400,{'error':str(error)})
            return
        if action=='undo_correction':
            try:
                if payload.get('review_id') and self.server.task_review.latest().get(task_id,{}).get('id')!=payload['review_id']:raise ValueError('这不是这项待办最近一次修正，请重新查看记录。')
                self._json(200,{'ok':self.store.undo_task_edit(task_id)})
            except ValueError as error:self._json(400,{'error':str(error)})
            return
        if not action and "done" in payload:
            action = "done" if bool(payload.get("done")) else "reopen"
        reward_receipt={}
        if action == "correct":
            try:
                changed = self.store.apply_task_correction(
                    task_id,
                    str(payload.get("correction") or ""),
                    value=payload.get("value", ""),
                )
            except ValueError as error:
                self._json(400, {"ok": False, "error": str(error)})
                return
        else:
            if action not in {"confirm", "dismiss", "done", "reopen", "snooze", "stage_done", "stage_reopen"}:
                self._json(400, {"ok": False, "error": "unsupported action"})
                return
            detail: dict[str, Any] = {}
            if action == "snooze":
                settings = self.server.settings  # type: ignore[attr-defined]
                detail = {"until": snooze_default_until(settings, now_local())}
            elif action in ('stage_done','stage_reopen'):detail={'milestone_id':str(payload.get('milestone_id') or '')}
            try:changed = self.store.apply_task_action(task_id, action, detail=detail,reward_receipt=reward_receipt)
            except ValueError as error:
                self._json(400,{'error':str(error)})
                return
        task = self.store.get_task(task_id) if changed else None
        self._json(
            200 if changed else 404,
            {
                "ok": changed,
                "id": task_id,
                "action": action,
                "reward": reward_receipt,
                "done": bool(task and task.get("status") == "done"),
                "status": (task or {}).get("status", ""),
            },
        )

    def _create_manual_task(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= 48 * 1024:
                raise ValueError("请求内容过大或为空。")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            result = create_manual_task(self.store, payload)
        except (ValueError, UnicodeError) as error:
            self._json(400, {"error": str(error)})
            return
        except Exception:
            # Keep DB paths and user-entered content out of error responses/logs.
            self._json(500, {"error": "本机保存未成功，请稍后重试。"})
            return
        self._json(200, result)

    def _update_groups(self) -> None:
        manager = getattr(self.server, "group_manager", None)
        if manager is None:
            self._json(403, {"error": "当前运行方式未开启群管理。"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= 4096:
                raise ValueError("请求内容过大或为空。")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            result = manager.update(payload)
        except (ValueError, UnicodeError) as error:
            self._json(400, {"error": str(error)})
            return
        except OSError:
            self._json(500, {"error": "本机保存未成功，请检查磁盘空间或文件权限。"})
            return
        self._json(200, result)


class TaskWebServer:
    """待办台的 HTTP 服务，绑定在独立端口上。"""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        *,
        logger: logging.Logger | None = None,
        meta_provider: Any = None,
        group_manager: Any = None,
        history_review: Any = None,
        stop_handler: Any = None,
        mobile_access: Any = None,
        qq_session: Any = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.logger = logger or LOGGER
        self.meta_provider = meta_provider
        self.group_manager = group_manager
        self.history_review = history_review
        self.stop_handler = stop_handler
        self.mobile_access = mobile_access
        self.qq_session = qq_session or QQSession(settings)
        self.task_review=TaskReview(settings,store)
        self.daily_advice=DailyAdvice(settings,store)
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.token = settings.web_token or store.meta_get("web_token", "")
        if not self.token:
            # 无论监听回环还是对外，都自动生成 token 并落库：绑定回环反而免鉴权是安全边界反转。
            self.token = secrets.token_urlsafe(12)
            store.meta_set("web_token", self.token)

    @property
    def is_alive(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def start(self) -> bool:
        if self.is_alive:
            return True
        try:
            server = ThreadingHTTPServer((self.settings.web_host, self.settings.web_port), _Handler)
        except OSError as error:
            self.logger.error("待办台启动失败（%s:%d）：%s", self.settings.web_host, self.settings.web_port, error)
            return False
        server.store = self.store  # type: ignore[attr-defined]
        server.settings = self.settings  # type: ignore[attr-defined]
        server.token = self.token  # type: ignore[attr-defined]
        server.meta_provider = self.meta_provider  # type: ignore[attr-defined]
        server.group_manager = self.group_manager  # type: ignore[attr-defined]
        server.history_review = self.history_review
        server.stop_handler = self.stop_handler
        server.mobile_access = self.mobile_access
        server.qq_session = self.qq_session
        server.task_review=self.task_review
        server.daily_advice=self.daily_advice
        self.server = server
        self.thread = threading.Thread(target=server.serve_forever, name="tasks-web", daemon=True)
        self.thread.start()
        self.logger.info(
            "待办台已启动：http://%s:%d/ （%s）",
            self.settings.web_host,
            self.settings.web_port,
            "需要 token" if self.token else "未设 token",
        )
        return True

    def stop(self) -> None:
        if self.server is not None:
            try:
                self.server.shutdown()
                if self.thread is not None:
                    self.thread.join(timeout=5)
                self.server.server_close()
            except Exception:  # noqa: BLE001 - 关闭失败不应阻塞退出
                self.logger.exception("待办台关闭失败")
        self.server = None
        self.thread = None

    def describe(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.settings.web_enabled),
            "alive": self.is_alive,
            "host": self.settings.web_host,
            "port": self.settings.web_port,
            "auth": bool(self.token),
        }
