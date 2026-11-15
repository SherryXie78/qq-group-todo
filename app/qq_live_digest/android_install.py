"""Authenticated distribution of the personal Android integration, never keys."""
from pathlib import Path
import hashlib,hmac,json,re,time
import threading
from urllib.parse import urlencode

APK_NAME='group-todo-2026.11.01.1.apk'
DOWNLOAD_TTL=1800
_EVENT_LOCK=threading.Lock()


def download_info(root,secret):
    info=release_info(root)
    if not info['ready']:return info
    expiry=int(time.time())+DOWNLOAD_TTL
    signature=_download_signature(secret,expiry,info['sha256'])
    return dict(info,download_url='/api/mobile/android.apk?'+urlencode({'download_ticket':str(expiry)+'.'+signature}),download_expires_at=expiry)


def _download_signature(secret,expiry,digest):
    message=f'qq-workbench-apk-v1\n{APK_NAME}\n{digest}\n{expiry}'.encode()
    return hmac.new(str(secret).encode(),message,hashlib.sha256).hexdigest()


def valid_download_ticket(root,secret,value):
    if not secret or not isinstance(value,str) or not re.fullmatch(r'\d{10}\.[0-9a-f]{64}',value):return False
    stamp,signature=value.split('.');expiry=int(stamp);now=int(time.time())
    if not now<=expiry<=now+DOWNLOAD_TTL:return False
    info=release_info(root)
    return info['ready'] and hmac.compare_digest(signature,_download_signature(secret,expiry,info['sha256']))


def note_download(handler,root,status,*,sent=0,start=0,finished=False):
    """Keep only a small anonymous troubleshooting history; never log URLs/cookies."""
    agent=handler.headers.get('User-Agent','')
    client='native' if 'GroupTodoNative' in agent else 'android-download-manager' if 'DownloadManager' in agent else 'android-browser' if 'Android' in agent else 'other'
    event={'at':int(time.time()),'method':handler.command,'status':status,'client':client,'bytes_sent':sent,'range_start':start,'finished':finished}
    file=Path(root)/'runtime/android-download-events.json'
    try:
        with _EVENT_LOCK:
            file.parent.mkdir(parents=True,exist_ok=True)
            try:events=json.loads(file.read_text(encoding='utf8'))
            except (OSError,ValueError):events=[]
            events=events[-19:] if isinstance(events,list) else []
            events.append(event);temporary=file.with_suffix('.tmp');temporary.write_text(json.dumps(events),encoding='utf8');temporary.replace(file)
    except OSError:pass


def release_info(root):
    directory=Path(root)/'android-release'
    try:
        info=json.loads((directory/'release.json').read_text(encoding='utf-8'))
        file=directory/APK_NAME
        if info.get('filename')!=APK_NAME or file.stat().st_size!=info['bytes'] or not re.fullmatch(r'[0-9a-f]{64}',info.get('sha256','')):raise ValueError()
        return dict(info,ready=True)
    except (OSError,ValueError,KeyError,TypeError):
        return {'ready':False}


def send_apk(handler,root,*,head_only=False):
    info=release_info(root)
    if not info['ready']:
        handler._json(404,{'error':'安卓整合包尚未就绪。'});note_download(handler,root,404);return
    file=Path(root)/'android-release'/APK_NAME
    size=file.stat().st_size;start=0;end=size-1;status=200
    requested=handler.headers.get('Range')
    if requested:
        match=re.fullmatch(r'bytes=(\d+)-(\d*)',requested.strip())
        if match:
            start=int(match[1]);end=min(int(match[2]) if match[2] else size-1,size-1)
        if not match or start>end or start>=size:
            handler.send_response(416);handler.send_header('Content-Range',f'bytes */{size}');handler.send_header('Content-Length','0');handler.end_headers();note_download(handler,root,416,start=start);return
        status=206
    if head_only:note_download(handler,root,status,start=start,finished=True)
    handler.send_response(status)
    handler.send_header('Content-Type','application/vnd.android.package-archive')
    handler.send_header('Content-Length',str(end-start+1))
    handler.send_header('Content-Disposition','attachment; filename="'+APK_NAME+'"')
    handler.send_header('Accept-Ranges','bytes')
    if status==206:handler.send_header('Content-Range',f'bytes {start}-{end}/{size}')
    handler.send_header('Cache-Control','private, no-store')
    handler.send_header('X-Content-Type-Options','nosniff')
    handler.send_header('Referrer-Policy','no-referrer')
    handler.end_headers()
    if head_only:return
    sent=0;finished=False
    try:
        with file.open('rb') as source:
            source.seek(start);remaining=end-start+1
            while remaining:
                block=source.read(min(65536,remaining))
                if not block:break
                handler.wfile.write(block);remaining-=len(block);sent+=len(block)
            finished=remaining==0
    except (BrokenPipeError,ConnectionResetError,TimeoutError):pass
    finally:note_download(handler,root,status,sent=sent,start=start,finished=finished)


def install_page(token):
    script=(Path(__file__).resolve().parents[2]/'assets/android-download.js').read_text(encoding='utf8')
    return PAGE.replace('__TOKEN_JSON__',json.dumps(token).replace('<','\\u003c')).replace('__DOWNLOAD_JS__',script)


PAGE='''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="referrer" content="no-referrer"><title>群待办 · 安卓独立版</title>
<style>[hidden]{display:none!important}body{font-family:system-ui,sans-serif;background:#f2f6fc;color:#17213b;max-width:640px;margin:auto;padding:24px;line-height:1.7}h1{font-size:26px}section{background:#fff;padding:20px;border-radius:18px;margin:18px 0}a,button{display:block;border:0;border-radius:12px;background:#2866d9;color:#fff;text-decoration:none;padding:13px 16px;font-size:16px;margin:12px 0;cursor:pointer}button:disabled{opacity:.5}small{color:#52617c}#result{overflow-wrap:anywhere}li{margin:12px 0}</style>
<h1>一个群待办，内置私人连接</h1>
<p>安装后直接进入待办，连接设置也在同一个应用里。电脑仍需开机，QQ与助手仍需运行。</p>
<section><h2>已有群待办：直接覆盖更新</h2><p>本次新增宠物头像的手机选照片入口。请下载后选择“更新”，保留现有应用；不要先卸载。沿用相同包名与签名，原工作台地址和内置连接配置会保留。更新后无需重新创建账号或添加 VPN 设备。</p><p>更新完成后，进入底栏“宠物 → 换个形象”，选择照片并保存即可。</p></section>
<section><h2>手机下载修复入口</h2><p>如果手机浏览器提示“下载失败”，请点下面的网页内下载。此方式显示进度，传完并校验后再保存安装包。</p><button id="browser-download" disabled>网页内下载（显示进度）</button><progress id="download-progress" max="100" value="0" style="width:100%;height:18px" hidden></progress><p id="download-status" role="status" aria-live="polite">正在检查下载入口…</p><a id="save-apk" hidden>保存安装包到手机</a><p><small>请保持电脑和内置私人连接运行，下载时暂时停留本页；中断后可继续，关闭页面会重新开始。保存后在手机“下载”里打开 APK，选择“更新”。</small></p></section>
<section><h2>1. 下载并安装</h2><p>个人整合测试版，沿用 Tailscale 1.104.2 连接核心，单独签名；不是官方 Tailscale 发布版。包内不含密码、API Key或工作台授权。</p>
<a id="download" hidden>备用：系统下载</a><small id="package">正在检查安装包…</small>
<p>先保留旧 Tailscale。下载完成后在手机的下载通知里打开 APK，由你确认安装；若提示权限，仅允许本次下载所用应用安装此 APK。</p></section>
<section><h2>2. 在新应用登录内置连接</h2><ol><li>打开新安装的“群待办”，点“内置连接登录”，使用你现在的 Tailscale 账号登录。</li><li>登录完成后返回群待办。若系统提示建立连接，由你确认；手机同一时间只能由一个应用提供VPN，确认后会切换到新应用。</li><li>若旧 Tailscale 已设为“始终开启VPN”，先在系统 VPN 设置里关闭旧应用的这一项。</li></ol>
<p>这一步是 Tailscale 登录，不需要重登 QQ，不需要输入 DeepSeek Key。</p></section>
<section><h2>3. 连接自己的工作台</h2><button id="copy">复制私人工作台地址</button><p id="result">点复制，然后回到新应用，在“连接我的工作台”里点“从剪贴板粘贴 → 保存并连接”。不要把私人地址发给别人。</p></section>
<section><h2>4. 验收后再移除旧入口</h2><p>在新应用确认：日期、待办和日历正确；退出后重新打开能自动连接；锁屏一分钟后回来能同步；关闭 Wi-Fi 使用流量也能刷新。</p>
<p>全部正常后，才卸载旧 Tailscale 和旧网页应用入口。新应用建议允许后台自启动、将省电策略设为无限制。系统 VPN 标记会保留。</p>
<a href="/">返回现有工作台</a></section>
<script>const token=__TOKEN_JSON__;history.replaceState(null,'',location.pathname);
document.getElementById('copy').onclick=async()=>{try{await navigator.clipboard.writeText(location.origin+'/?token='+encodeURIComponent(token));document.getElementById('result').textContent='已复制。回到新群待办，粘贴并保存；不要发送到聊天。';}catch(_){document.getElementById('result').textContent='复制未成功，请在当前浏览器允许本次剪贴板操作后重试。';}};
__DOWNLOAD_JS__</script></html>'''
