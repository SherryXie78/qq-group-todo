"""Synthetic local workbench; no QQ, AI, push, or host configuration."""
from pathlib import Path
import argparse, dataclasses, datetime as dt, json, sys, uuid, webbrowser

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/'app'))
from qq_live_digest.config import Settings
from qq_live_digest.service import DigestService
from qq_live_digest.manual_tasks import create_manual_task
from qq_live_digest.temporal import BEIJING
from qq_live_digest.weburl import build_web_url

def build_settings(port=8876):
    return dataclasses.replace(Settings(), data_dir=ROOT/'demo-data',log_dir=ROOT/'demo-logs',
        official_bot_enabled=False,appid='',secret='',onebot_enabled=False,
        group_whitelist=(),catchup_enabled=False,attachments_enabled=False,vision_enabled=False,
        napcat_api_url='',napcat_api_token='',llm_enabled=False,dashscope_api_key='',
        web_enabled=True,web_host='127.0.0.1',web_port=port,
        deadline_reminders_enabled=False,candidate_push_enabled=False,weekly_review_enabled=False)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8876)
    parser.add_argument('--no-browser',action='store_true')
    args=parser.parse_args()
    settings=build_settings(args.port)
    service=DigestService(settings,pushers=[])
    if not service.store.meta_get('public_demo_seeded',''):
        today=dt.datetime.now(BEIJING).date()
        examples=[('示例：提交课程练习','线上提交',0),('示例：准备活动材料','示例教室',1),('示例：每周项目复盘','本机工作台',3)]
        for title,place,offset in examples:
            create_manual_task(service.store,{'client_id':str(uuid.uuid4()),'summary':title,'group':'虚构演示',
                'note':'全部为程序生成的示例。地点：'+place,'deadline_date':str(today+dt.timedelta(days=offset)),'deadline_time':'20:00'})
        service.store.meta_set('public_demo_seeded','1')
    service.start(start_bot=False)
    try:
        token=service.store.meta_get('web_token','')
        url=build_web_url(settings,token=token)
        if not args.no_browser:webbrowser.open(url)
        print('本地虚构数据演示已启动；按 Ctrl+C 停止。')
        service.run_forever()
    except KeyboardInterrupt:pass
    finally:service.stop()

if __name__=='__main__':main()
