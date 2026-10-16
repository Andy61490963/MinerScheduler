"""依本次授權每檔五秒加三十瓦；只由既有保護程式調整功耗。"""
from datetime import datetime,timedelta,timezone
from pathlib import Path
import csv
import ctypes
import hashlib
import json
import os
import re
import statistics
import subprocess
import sys
import time
import uuid

LIVE=Path(r'C:\test\CORE')
sys.path[:0]=[str(LIVE),str(LIVE/'TEST_效率監控')]
from TEST_啟動核對 import read_shared_text
from TEST_即時通道 import read_channel
from TEST_顯卡讀值 import ReadOnlyGpu
from actions import stop_fingerprint,launcher_environment,mining_jobs
from server import verified_miner
import settings

CONFIG=LIVE/'TEST_設定.json'
STOP=LIVE/'TEST_停止.flag'
SMI=Path(os.environ['SystemRoot'])/'System32/nvidia-smi.exe'
PS=Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
RESULTS=Path(r'C:\test\PROJECTS\TESTS\results')/datetime.now().strftime('%Y%m%d_%H%M%S_重塗後三十瓦短測')
REPORT={'狀態':'尚未啟動','步幅W':30,'每檔秒數':5,'原廠上限W':450,'結果':[],
        '測試界線':{'核心C':80,'熱點C':105,'顯存C':90},'限制':'每檔5秒僅代表短測，不能當作長時間穩定性證明。'}

def atomic(path,data):
    temporary=path.with_suffix('.實機短測.tmp')
    temporary.write_bytes(data if isinstance(data,bytes) else (json.dumps(data,ensure_ascii=False,indent=2)+'\n').encode('utf-8'))
    temporary.replace(path)

def save():
    REPORT['更新台灣時間']=datetime.now().astimezone().isoformat()
    atomic(RESULTS/'結果.json',REPORT)

def emit(message):
    print(message,flush=True)

def thermal():
    fields='clocks_event_reasons.sw_thermal_slowdown,clocks_event_reasons.hw_thermal_slowdown,clocks_event_reasons_counters.sw_thermal_slowdown,clocks_event_reasons_counters.hw_thermal_slowdown'
    result=subprocess.run([str(SMI),'--query-gpu='+fields,'--format=csv,noheader,nounits'],
        capture_output=True,text=True,check=True,timeout=5,creationflags=0x08000000)
    values=next(csv.reader([result.stdout.strip()]))
    active=any(value.strip()=='Active' for value in values[:2])
    counters=[float(value.strip()) for value in values[2:] if value.strip() not in ('N/A','[N/A]','Not Supported')]
    return active,sum(counters)

def health(identity=None):
    if STOP.exists():raise RuntimeError('收到停止標記，停止測試，不自行重新啟動。')
    status,guard=read_channel('miner'),read_channel('guard')
    if not status or not guard:raise RuntimeError('即時心跳缺少，不依賴磁碟舊資料繼續測試。')
    now=datetime.now(timezone.utc)
    for name,data in (('礦工',status),('保護',guard)):
        if (now-datetime.fromisoformat(data['更新時間UTC'])).total_seconds()>4:
            raise RuntimeError(name+'即時心跳逾時。')
    if guard.get('狀態')!='排程保護已就緒' or not status.get('狀態','').startswith('挖礦中'):
        raise RuntimeError('礦工或保護尚未就緒。')
    if identity and guard['執行識別碼']!=identity:raise RuntimeError('保護程序更換，不繼續短測。')
    if status.get('保護識別碼')!=guard['執行識別碼']:raise RuntimeError('礦工與保護識別碼不一致。')
    if not verified_miner(status.get('礦工PID')):raise RuntimeError('官方礦工程序已結束。')
    sample=status['目前讀值']
    if any(item['實際百分比']!=80 for item in sample['風扇']) or any(item['設定百分比']!=80 for item in sample['控制']):
        raise RuntimeError('顯卡風扇未維持本次80%設定。')
    return status,guard,sample

def rates(start,end):
    values=[]
    for line in read_shared_text(LIVE/'TEST.log').splitlines():
        match=re.search(r'\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\] GPU0 .*?: ([\d.]+) TH/s',line)
        if match:
            timestamp=datetime.fromisoformat(match[1]).replace(tzinfo=timezone(timedelta(hours=8))).timestamp()
            if start<=timestamp<=end:values.append(float(match[2]))
    return values

def main():
    sys.stdout.reconfigure(encoding='utf-8')
    RESULTS.mkdir(parents=True)
    original=CONFIG.read_bytes()
    baseline=json.loads(original.decode('utf-8-sig'))
    initial_stop=stop_fingerprint()
    identity=uuid.uuid4().hex
    now=datetime.now(timezone.utc)
    trial={'識別碼':identity,'開始UTC':now.isoformat(),'結束UTC':(now+timedelta(minutes=20)).isoformat(),'步幅W':30,'功耗W':200}
    configured=False
    best=None
    REPORT.update({'開始台灣時間':datetime.now().astimezone().isoformat(),'測試識別碼':identity,'原設定已備份':str(RESULTS/'原設定.json')})
    (RESULTS/'原設定.json').write_bytes(original)
    def configure(watts):
        fresh=json.loads(read_shared_text(CONFIG))
        prior=fresh['功耗排程'].pop('臨時功耗短測',None)
        if fresh!=baseline:raise RuntimeError('正式設定被其他操作修改，取消短測，不覆蓋。')
        if prior is not None and prior['識別碼']!=identity:raise RuntimeError('已有其他短測，取消。')
        trial['功耗W']=watts
        fresh['功耗排程']['臨時功耗短測']=dict(trial)
        atomic(CONFIG,fresh)
    try:
        if ctypes.windll.shell32.IsUserAnAdmin():raise RuntimeError('實機短測與礦工必須使用一般使用者。')
        if mining_jobs():raise RuntimeError('已有礦工或啟動流程，未接管或重新啟動。')
        settings.verify_fans({'gpu_fan':80,'board_fan':50},lambda:None)
        if ReadOnlyGpu().read()['核心C']>=50:raise RuntimeError('起始核心未低於50°C，未啟動。')
        if stop_fingerprint()!=initial_stop:raise RuntimeError('收到較新的人工停止，取消啟動。')
        configure(200)
        configured=True
        REPORT['狀態']='一般使用者完整啟動核對中'
        save()
        with (RESULTS/'啟動.log').open('ab') as output:
            child=subprocess.Popen([str(PS),'-NoProfile','-ExecutionPolicy','Bypass','-File',str(LIVE/'TEST_啟動.ps1'),'-監控停止驗證',initial_stop],
                cwd=LIVE,env=launcher_environment(),stdout=output,stderr=subprocess.STDOUT,creationflags=0x08000000)
            child.wait(timeout=35)
        if child.returncode:raise RuntimeError('原啟動流程未通過，請看啟動.log；不重試。')
        deadline=time.monotonic()+360
        while True:
            if STOP.exists():raise RuntimeError('啟動期間收到停止標記，未覆蓋。')
            try:
                status,guard,sample=health()
                if guard['目標功耗上限W']==200 and guard.get('持續負載核對通過'):break
            except RuntimeError:
                pass
            if time.monotonic()>deadline:raise RuntimeError('完整負載核對等待逾時，不強制重啟。')
            time.sleep(.5)
        guard_id=guard['執行識別碼']
        REPORT.update({'狀態':'三十瓦逐檔短測中','礦工PID':status['礦工PID'],'保護識別碼':guard_id})
        save()
        emit('200W完整核對完成，開始每次加30W、每檔5秒。')
        stop_reason=''
        for watts in list(range(200,451,30))+[450]:
            if STOP.exists():raise RuntimeError('收到人工停止，不繼續加瓦。')
            configure(watts)
            deadline=time.monotonic()+8
            while True:
                status,guard,sample=health(guard_id)
                if guard['目標功耗上限W']==watts and abs(sample['功耗上限W']-watts)<=.6:break
                if time.monotonic()>deadline:raise RuntimeError('保護未套用指定功耗，停止加瓦。')
                time.sleep(.1)
            start=time.time()
            end=time.monotonic()+5
            active,counter=thermal()
            samples=[]
            next_thermal=time.monotonic()+2
            while time.monotonic()<end:
                status,guard,sample=health(guard_id)
                samples.append(sample)
                if guard['目標功耗上限W']<watts:stop_reason='功耗保護已降瓦。'
                if sample['核心C']>=80 or sample['熱點C']>=105 or sample['顯存C']>=90:
                    stop_reason='達到本次短測溫度界線。'
                if time.monotonic()>=next_thermal:
                    flag,total=thermal()
                    active=active or flag or total>counter
                    next_thermal=time.monotonic()+2
                if active:stop_reason='偵測到硬體或軟體熱降頻。'
                if stop_reason:break
                time.sleep(min(.25,max(0,end-time.monotonic())))
            elapsed=time.time()-start
            rate=rates(start,time.time())
            power=statistics.mean(row['功耗W'] for row in samples)
            row={'功耗上限W':watts,'停留秒數':round(elapsed,2),'平均實際功耗W':round(power,2),
                 '核心最高C':round(max(s['核心C'] for s in samples),2),'熱點最高C':round(max(s['熱點C'] for s in samples),2),
                 '顯存最高C':max(s['顯存C'] for s in samples),'平均算力THs':round(statistics.mean(rate),2) if rate else None,
                 '算力回報筆數':len(rate),'熱降頻':active,'接受份額':status.get('接受算力證明數'), '停止加瓦原因':stop_reason}
            if rate:row['每瓦THs']=round(statistics.mean(rate)/power,4)
            REPORT['結果'].append(row)
            if not stop_reason and elapsed>=4.9:best=watts
            REPORT['最高完成短測W']=best
            save()
            emit(json.dumps(row,ensure_ascii=False))
            if stop_reason:break
        REPORT.update({'狀態':'短測完成','結束原因':stop_reason or '已測至原廠450W上限'})
    except Exception as exc:
        REPORT.update({'狀態':'短測中止','原因':str(exc)})
        emit(str(exc))
    finally:
        if configured:
            fresh=json.loads(read_shared_text(CONFIG))
            current=fresh['功耗排程'].get('臨時功耗短測')
            if current and current.get('識別碼')==identity:
                fresh['功耗排程'].pop('臨時功耗短測')
                atomic(CONFIG,original if fresh==baseline else fresh)
                REPORT['原排程已恢復']=True
        REPORT['人工停止已保留']=STOP.exists()
        REPORT['結束台灣時間']=datetime.now().astimezone().isoformat()
        save()
        emit(json.dumps({key:value for key,value in REPORT.items() if key!='結果'},ensure_ascii=False))

if __name__=='__main__':main()
