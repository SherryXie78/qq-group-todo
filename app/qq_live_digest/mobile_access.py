"""Private Tailscale status and locally generated phone pairing QR codes."""
from __future__ import annotations
import json,re,subprocess,threading,time,os
from pathlib import Path
from urllib.parse import urlencode

class MobileAccess:
    def __init__(self,settings,*,runner=None):
        self.settings=settings;self.root=settings.data_dir.parent
        self.runner=runner or self._run
        self.lock=threading.Lock();self.cached=None;self.checked=0

    def _run(self,args):
        value=json.loads((self.root/'runtime/tailscale-client.json').read_text(encoding='utf-8-sig'))
        executable=Path(value['executable'])
        if not executable.is_file(): raise FileNotFoundError()
        result=subprocess.run([str(executable),*args],capture_output=True,timeout=5,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if result.returncode: raise RuntimeError('Tailscale unavailable')
        return json.loads(result.stdout.decode('utf8'))

    def status(self,*,refresh=False):
        with self.lock:
            if self.cached is not None and not refresh and time.monotonic()-self.checked<4:return dict(self.cached)
            result={'approved':False,'installed':False,'connected':False,'private_ready':False,'android_online':0}
            try:
                consent=json.loads((self.root/'consent.json').read_text(encoding='utf-8-sig'))
                if not consent.get('mobile_access_approved'):return result
                result['approved']=True
                status=self.runner(['status','--json']);result['installed']=True
                own_node=status.get('Self') or {}
                result['connected']=status.get('BackendState')=='Running' and bool(own_node.get('Online'))
                result['state']=status.get('BackendState','unknown')
                own=own_node.get('UserID')
                result['android_online']=sum(1 for peer in (status.get('Peer') or {}).values() if own is not None and peer.get('OS')=='android' and peer.get('Online') and peer.get('UserID')==own)
                domain=str(own_node.get('DNSName','')).rstrip('.')
                if result['connected'] and re.fullmatch(r'[a-z0-9][a-z0-9.-]*\.ts\.net',domain):
                    config=self.runner(['serve','status','--json'])
                    proxy=config.get('Web',{}).get(domain+':443',{}).get('Handlers',{}).get('/',{}).get('Proxy')
                    funnel=any(config.get('AllowFunnel',{}).values())
                    if proxy=='http://127.0.0.1:8766' and not funnel:
                        result.update(private_ready=True,url='https://'+domain+'/')
            except (OSError,ValueError,KeyError,RuntimeError,subprocess.SubprocessError):
                result.setdefault('state','setup_pending')
            try:
                visit=json.loads((self.root/'runtime/mobile-visit.json').read_text(encoding='utf8'))
                result['last_phone_visit']=visit.get('at',0)
            except (OSError,ValueError):pass
            self.cached=result;self.checked=time.monotonic()
            return dict(result)

    def qr_svg(self,kind):
        import qrcode
        import qrcode.image.svg
        if kind=='install':
            value='https://pkgs.tailscale.com/stable/tailscale-android-universal-1.104.1.apk'
        elif kind=='workbench':
            status=self.status()
            if not status.get('private_ready'):raise ValueError('请先完成两端 Tailscale 登录与私人连接设置。')
            if not self.settings.web_token:raise ValueError('工作台授权未就绪。')
            value=status['url']+'?'+urlencode({'token':self.settings.web_token})
        else:raise ValueError('二维码类型无效。')
        return qrcode.make(value,image_factory=qrcode.image.svg.SvgPathImage,box_size=8,border=4).to_string().decode('utf8')

    def note_phone_visit(self):
        path=self.root/'runtime/mobile-visit.json'
        path.write_text(json.dumps({'at':int(time.time()*1000),'authenticated':True,'device':'Android'}),encoding='utf8')

    def begin_setup(self,action):
        consent=json.loads((self.root/'consent.json').read_text(encoding='utf-8-sig'))
        if not consent.get('mobile_access_approved'):raise ValueError('私人手机访问尚未授权。')
        if action=='login':
            args=[str(self.root/'app/.venv/Scripts/pythonw.exe'),str(self.root/'Login-Tailscale.py')]
        elif action=='continue':
            if self.status(refresh=True).get('private_ready'):
                args=[str(self.root/'app/.venv/Scripts/pythonw.exe'),str(self.root/'Show-Mobile-Pairing.py')]
            else:
                shell=Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
                args=[str(shell),'-NoProfile','-ExecutionPolicy','Bypass','-File',str(self.root/'Start-Mobile-Setup.ps1')]
        else:raise ValueError('不支持的连接操作。')
        subprocess.Popen(args,cwd=self.root,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        return {'ok':True}
