"""Status and explicit desktop actions for the approved shared QQ session."""
from __future__ import annotations
import datetime as dt,json,math,os,re,subprocess,threading,time,uuid
from pathlib import Path
from .temporal import BEIJING


def read_json(path):
    try:
        if path.stat().st_size>16384:return {}
        value=json.loads(path.read_text(encoding='utf-8-sig'))
        return value if isinstance(value,dict) else {}
    except (OSError,ValueError):return {}


def write_job(root,value):
    path=Path(root)/'runtime/qq-login-action.json';path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        temporary.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8');temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def action_environment():
    return {key:value for key,value in os.environ.items() if key not in ('QQ_ASSISTANT_DEEPSEEK_KEY','DASHSCOPE_API_KEY','DEEPSEEK_API_KEY')}


class QQSession:
    def __init__(self,settings,*,spawn=None,clock=None):
        self.root=Path(settings.data_dir).parent;self.spawn=spawn or subprocess.Popen
        self.clock=clock or time.time;self.lock=threading.Lock()

    def approved_account(self):
        consent=read_json(self.root/'consent.json')
        profile=(self.root/'runtime/snowluma-coexistence.json').is_file()
        account=str(read_json(self.root/'SnowLuma/config/approved-account.json').get('uin',''))
        return account if consent.get('qq_main_account_approved') is True and profile and re.fullmatch(r'\d{5,10}',account) else ''

    def status(self):
        now=self.clock();account=self.approved_account();health=read_json(self.root/'runtime/collector-health.json')
        helper=self.root/'QQ-Login-Control.py';python=self.root/'app/.venv/Scripts/pythonw.exe'
        result={'supported':bool(account and helper.is_file() and python.is_file()),'online':False,
                'account_label':'原账号（尾号 '+account[-3:]+'）' if account else '尚未授权',
                'checked_at':dt.datetime.fromtimestamp(now,BEIJING).isoformat(timespec='seconds')}
        def count(key):
            try:return min(10000,max(0,int(health.get(key) or 0)))
            except (ValueError,TypeError,OverflowError):return 0
        try:stamp=float(health.get('at') or 0)/1000
        except (ValueError,TypeError,OverflowError):stamp=0
        fresh=math.isfinite(stamp) and 0<=now-stamp<35
        result['last_checked']=dt.datetime.fromtimestamp(stamp,BEIJING).isoformat(timespec='seconds') if fresh else ''
        if not account:
            state='not_configured';title='QQ 接入尚未配置';message='当前环境未配置已授权的原账号共存接入。'
        elif health.get('state')=='stopped':
            state='stopped';title='采集服务已停止';message='已保存的待办仍可查看，点击“恢复采集”重新接入原账号。'
        elif not fresh:
            state='unresponsive';title='采集服务未响应';message='状态已过期，不能据此判断 QQ 是否掉线。请先尝试“恢复采集”，无需退出 QQ。'
        elif health.get('mode')!='shared-desktop-qq':
            state='not_configured';title='采集模式待检查';message='当前不是已批准的普通 QQ 共存接入，不会启动另一套 QQ 登录。'
        elif health.get('online') is True:
            state='online';title='QQ 采集在线';message='正在共享普通电脑版 QQ 的原账号，仅整理选定群消息。'
            result['online']=True
        elif health.get('state')=='error':
            state='error';title='采集接入异常';message='如果普通 QQ 已正常登录，请点击“恢复采集”；这不会退出或重登 QQ。'
        elif count('matched_process_count'):
            state='connecting';title='正在接入原账号';message='已识别原账号，正在恢复采集。稍后点击“重新检测”。'
        elif count('account_confirmed_process_count'):
            state='account_unconfirmed';title='采集账号待确认';message='尚未识别到已授权的原账号。请确认电脑版 QQ 登录的是原账号；不会自动接入其他账号。'
        else:
            state='login_required';title='等待 QQ 登录';message='请在电脑打开普通 QQ，用原账号扫码或确认登录；无需注册账号。'
        result.update(state=state,title=title,message=message)
        job=read_json(self.root/'runtime/qq-login-action.json')
        if job:
            try:age=now-float(job.get('requested_at') or 0)
            except (TypeError,ValueError,OverflowError):age=999999
            active=job.get('status')=='running' and 0<=age<100
            result['action']={key:job.get(key,'') for key in ('id','action','status','message')}
            result['action']['active']=active
            result['action']['visible']=active or 0<=age<180
            if job.get('status')=='running' and not active:
                result['action'].update(status='interrupted',message='上次操作未确认完成，请重新检测当前状态。')
        return result

    def begin(self,action):
        if action not in ('open_login','reconnect'):raise ValueError('不支持的 QQ 登录操作。')
        with self.lock:
            current=self.status()
            if not current['supported']:raise ValueError('当前环境未配置可用的原账号登录入口。')
            if (current.get('action') or {}).get('active'):return dict(current,ok=True,already_running=True)
            previous=read_json(self.root/'runtime/qq-login-action.json')
            try:recent=0<=self.clock()-float(previous.get('requested_at') or 0)<5
            except (TypeError,ValueError,OverflowError):recent=False
            if recent:return dict(current,ok=True,already_running=True)
            job={'id':uuid.uuid4().hex,'action':action,'status':'running','requested_at':self.clock(),
                 'message':'正在电脑打开普通 QQ…' if action=='open_login' else '正在恢复只读采集，普通 QQ 将保持运行…'}
            write_job(self.root,job)
            try:
                self.spawn([str(self.root/'app/.venv/Scripts/pythonw.exe'),str(self.root/'QQ-Login-Control.py'),action,job['id']],
                    cwd=self.root,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                    env=action_environment(),
                    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            except OSError:
                job.update(status='error',message='操作未能启动，请在电脑打开 QQ 后重新检测。');write_job(self.root,job)
            return dict(self.status(),ok=job['status']!='error')


def run_action(root,action,job_id,*,run=None,spawn=None,focus=None):
    """Fixed local operations only. Never calls a QQ message or login API."""
    root=Path(root);run=run or subprocess.run;spawn=spawn or subprocess.Popen
    if focus is None:
        from .qq_window import focus_existing_qq
        focus=focus_existing_qq
    manager=QQSession(type('Local',(),{'data_dir':root/'data'})())
    if not manager.approved_account():raise ValueError('Original QQ consent/profile is missing.')
    if action not in ('open_login','reconnect') or not re.fullmatch(r'[0-9a-f]{32}',job_id):raise ValueError('Invalid operation.')
    job=read_json(root/'runtime/qq-login-action.json')
    if job.get('id')!=job_id or job.get('action')!=action:return
    ps=Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
    try:
        if action=='open_login':
            client=Path(str(read_json(root/'runtime/qq-client.json').get('executable') or ''))
            if client.name.lower()!='qq.exe' or not client.is_file() or any(word in str(client).lower() for word in ('napcat','snowluma')):
                raise ValueError('Normal QQ executable is not configured.')
            if not focus(client):spawn([str(client)],cwd=client.parent,env=action_environment())
        scripts=['Stop-SnowLuma.ps1','Start-SnowLuma.ps1'] if action=='reconnect' else ['Start-SnowLuma.ps1']
        for script in scripts:
            args=[str(ps),'-NoProfile','-ExecutionPolicy','Bypass','-File',str(root/script)]
            if script.startswith('Start-'):args.append('-NoBrowser')
            run(args,cwd=root,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                env=action_environment(),
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),timeout=55,check=True)
        job.update(status='done',message='已在电脑打开普通 QQ。请用原账号完成登录，状态会自动检测。' if action=='open_login' else '采集服务已重新启动，正在重新识别原账号；无需退出普通 QQ。')
    except Exception as error:
        job.update(status='error',error_type=type(error).__name__,message='操作未完成。请先确认普通 QQ 可正常使用，再重新检测或恢复采集。')
    if read_json(root/'runtime/qq-login-action.json').get('id')==job_id:
        job['finished_at']=time.time();write_job(root,job)
