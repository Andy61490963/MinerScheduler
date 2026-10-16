"""依保存的時段功耗開始核對及挖礦，保留風扇、感測與停止保護。"""
import ctypes as C
import copy
from collections import deque
from datetime import datetime, timezone
import hashlib
import json
import msvcrt
from pathlib import Path
import re
import queue
import sys
import threading
import time
import subprocess

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
import TEST_啟動核對 as startup
import TEST_執行工具 as gpu
from TEST_顯卡讀值 import ReadOnlyGpu
from TEST_排程規則 import FAN_LIMIT, stable_temperatures, mining_allowed, period_profile
from TEST_即時通道 import ChannelWriter, BackgroundFileWriter

STATE=ROOT/'TEST_Status.json'
STOP=ROOT/'TEST_停止.flag'
LOG=ROOT/'TEST.log'
SCHEDULE_END='已進入離峰時段，依排程停止挖礦。'
sys.stdout.reconfigure(encoding='utf-8')


def utc():
    return datetime.now(timezone.utc).isoformat()


def save(data):
    data['更新時間UTC']=utc()
    temporary=STATE.with_suffix('.tmp')
    temporary.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    # 背景紀錄容許Windows短暫鎖檔；獨立風扇及溫度監控仍持續運作。
    deadline=time.monotonic()+1
    while True:
        try:
            temporary.replace(STATE)
            return
        except PermissionError:
            if time.monotonic()>=deadline:
                raise
            time.sleep(.01)


def preflight():
    if C.windll.shell32.IsUserAnAdmin():
        raise RuntimeError('礦工必須使用一般帳號權限，不接受管理員執行。')
    config=json.loads((ROOT/'TEST_設定.json').read_text(encoding='utf-8-sig'))
    if not mining_allowed(config['功耗排程']):
        raise RuntimeError(SCHEDULE_END)
    if config['收款網路']!='TRON (TRC20)' or config['收款幣種']!='USDT' or config['最大風扇百分比']!=FAN_LIMIT or config['礦池']!='stratum+ssl://pearlpow.unmineable.com:4444':
        raise RuntimeError('收款、礦池或100%風扇上限條件與已核准設定不符。')
    certificate=json.loads((ROOT/'TEST_啟動核對結果.json').read_text(encoding='utf-8-sig'))
    age=(datetime.now(timezone.utc)-datetime.fromisoformat(certificate['結束UTC'])).total_seconds()
    checked_powers={period_profile(config['功耗排程'],peak)[0] for peak in (True,False)}
    if not certificate['核對通過'] or certificate['功耗上限W'] not in checked_powers or age<0 or age>1800:
        raise RuntimeError('沒有最近三十分鐘內依保存設定通過的啟動核對。')
    if certificate['保護程式SHA256']!=startup.policy_hashes():
        raise RuntimeError('保護設定或程式已變更，必須重新核對啟動。')
    if hashlib.sha256(startup.MINER.read_bytes()).hexdigest()!=startup.helper.EXPECTED:
        raise RuntimeError('礦工內容與核對過的官方檔案不同。')
    guard=startup.read_guard(certificate['保護識別碼'])
    watts=startup.configured_power(guard,config)
    initial=gpu.read_gpu()
    if initial['識別碼']!='GPU-__LOCAL_UUID__' or abs(initial['功耗上限W']-watts)>.6 or initial['核心溫度C']>=50 or initial['顯卡功耗W']>50:
        raise RuntimeError('保存的時段功耗或啟動前的閒置條件不符。')
    return config,guard


def monitor_log(offset,tail,accepted):
    if not LOG.exists():
        return offset,tail,accepted
    if LOG.stat().st_size<offset:
        offset,tail,accepted=0,'',0
    with LOG.open('rb') as stream:
        stream.seek(offset)
        chunk=stream.read(262144)
        offset=stream.tell()
    content=chunk.decode('utf-8',errors='replace')
    carry=tail.rsplit('\n',1)[-1] if tail and not tail.endswith('\n') else ''
    for line in (carry+content).splitlines(keepends=True):
        # 僅計算個別算力證明的接受訊息，避免把彙總欄位當成接受次數。
        if line.endswith('\n') and line.rstrip().endswith('[0]') and re.search(r'(?i)\b(?:share|result)\b.*\baccepted\b|\baccepted\b.*\b(?:share|result)\b',line) and not re.search(r'(?i)(total|accepted\s*[:=]\s*0)',line):
            accepted+=1
    tail=(tail+content)[-65536:]
    if 'GPU sensors thread started' in tail or 'GPU temperature protection monitor thread started' in tail:
        raise RuntimeError('礦工啟用了禁止的GPU控制流程，停止。')
    return offset,tail,accepted


def read_runtime_guard(identity):
    """狀態檔短暫延遲最多重試兩秒；必須取得新且通過全部條件的資料才返回。"""
    deadline=time.monotonic()+2
    while True:
        try:
            return startup.read_guard(identity,realtime=True)
        except RuntimeError as exc:
            if str(exc)!='功耗保護尚未就緒、已停止或資料過期。':
                raise
            info=startup.read_channel('guard')
            if info is None:
                if STOP.exists() or time.monotonic()>=deadline:
                    raise
                time.sleep(.02)
                continue
            age=(datetime.now(timezone.utc)-datetime.fromisoformat(info['更新時間UTC'])).total_seconds()
            if (STOP.exists() or info.get('執行識別碼')!=identity
                    or info.get('狀態') not in ('150W保護已就緒','排程保護已就緒')
                    or not 4<age<8 or time.monotonic()>=deadline):
                raise
            time.sleep(.05)


class BackgroundReading:
    """讀值在獨立執行緒進行；主監控不用等Windows檔案或驅動呼叫返回。"""
    def __init__(self,read,validate,interval,on_error=None,gap_limit=None):
        self.lock=threading.Lock()
        self.stop=threading.Event()
        self.state={'資料':None,'時間':time.monotonic(),'序號':0,'錯誤':'','最長讀取秒數':0}
        def loop():
            previous=time.monotonic()
            while not self.stop.is_set():
                begin=time.monotonic()
                data=None
                try:
                    if gap_limit is not None and begin-previous>gap_limit:
                        raise RuntimeError('實際安全監控中斷超過允許時間，停止負載。')
                    if STOP.exists():
                        raise RuntimeError('收到停止指令。')
                    data=read()
                    if validate:
                        validate(data)
                    if gap_limit is not None and time.monotonic()-begin>gap_limit:
                        raise RuntimeError('實際安全監控讀取逾時，停止負載。')
                    error=''
                except Exception as exc:
                    error=str(exc)
                    if on_error:
                        on_error(error)
                with self.lock:
                    self.state.update({'資料':data,'時間':time.monotonic(),'序號':self.state['序號']+1,'錯誤':error,'最長讀取秒數':max(self.state['最長讀取秒數'],time.monotonic()-begin)})
                if error:
                    break
                previous=begin
                self.stop.wait(max(0,interval-(time.monotonic()-begin)))
        self.thread=threading.Thread(target=loop,daemon=True)
        self.thread.start()

    def snapshot(self):
        with self.lock:
            return self.state.copy()


class BackgroundLog:
    """紀錄檔讀寫不阻塞風扇與溫度的停止保護。"""
    def __init__(self):
        self.queue=queue.Queue(maxsize=1)
        self.stop=threading.Event()
        self.lock=threading.Lock()
        self.state={'接受算力證明數':0,'礦工控制已關閉':False,'錯誤':'','最長記錄秒數':0,'更新時間':time.monotonic()}
        initialized=False
        def record(data):
            nonlocal initialized
            trace_path=ROOT/'TEST_風扇溫度.jsonl'
            with trace_path.open('a' if initialized else 'w',encoding='utf-8') as trace:
                trace.write(json.dumps({'時間UTC':data.get('更新時間UTC',utc()),**data['目前讀值'],'經過秒數':data['經過秒數']},ensure_ascii=False)+'\n')
                initialized=True
            if trace_path.stat().st_size>10*1024*1024:
                trace_path.replace(ROOT/'TEST_風扇溫度.前段.jsonl')
                initialized=False
            save(data)
        self.recording=BackgroundFileWriter(record)
        def loop():
            offset,tail,accepted=0,'',0
            mode=False
            try:
                while not self.stop.is_set():
                    try:
                        data=self.queue.get(timeout=.2)
                    except queue.Empty:
                        continue
                    begin=time.monotonic()
                    try:
                        offset,tail,accepted=monitor_log(offset,tail,accepted)
                    except OSError as exc:
                        with self.lock:
                            self.state['讀取提示']='礦工紀錄暫時無法讀取：'+str(exc)
                        self.stop.wait(.2)
                        continue
                    mode=mode or ('GPU sensors: disabled' in tail and 'Cpu mining: disabled' in tail)
                    if data['經過秒數']>20 and not mode:
                        raise RuntimeError('未確認關閉礦工GPU控制及CPU挖礦。')
                    with self.lock:
                        self.state.update({'接受算力證明數':accepted,'礦工控制已關閉':mode,'讀取提示':'','最長記錄秒數':max(self.state['最長記錄秒數'],time.monotonic()-begin),'更新時間':time.monotonic()})
                    data.update({'接受算力證明數':accepted,'礦工控制已關閉':mode})
                    self.recording.submit(data)
            except Exception as exc:
                with self.lock:
                    self.state['錯誤']=str(exc)
        self.thread=threading.Thread(target=loop,daemon=True)
        self.thread.start()

    def snapshot(self):
        with self.lock:
            result=self.state.copy()
        recording=self.recording.snapshot()
        result.update({'寫入提示':recording['錯誤'],'寫入更新時間':recording['更新時間'],
                       '最長記錄秒數':max(result['最長記錄秒數'],recording['最長記錄秒數'])})
        return result

    def submit(self,data):
        try:
            self.queue.get_nowait()
        except queue.Empty:
            pass
        self.queue.put_nowait(copy.deepcopy(data))


def main():
    config,guard=preflight()
    reader=ReadOnlyGpu()
    initial=reader.read()
    startup.check(initial)
    kernel,job=startup.helper.make_job()
    process=None
    output=(ROOT/'TEST_Output.log').open('wb')
    report={'狀態':'準備啟動','持續負載核對通過':False,'開始UTC':utc(),'保護識別碼':guard['執行識別碼'],'功耗上限W':guard['目標功耗上限W'],'風扇上限百分比':FAN_LIMIT,'CPU挖礦':False,'收款網路':config['收款網路'],'收款地址':config['收款地址'],'礦工SHA256':startup.helper.EXPECTED,'最高實際風扇百分比':0,'最高設定風扇百分比':0,'最高核心C':0,'最高熱點C':0,'最高顯存C':0,'樣本數':0,'接受算力證明數':0,'目標檢查間隔秒數':.05,'最長檢查間隔秒數':0,'原因':''}
    begin=time.monotonic()
    live_channel=ChannelWriter('miner')
    previous=begin
    next_save=begin
    accepted=0
    sensors=power=logger=None
    job_active=threading.Event()
    job_active.set()
    immediate_reason=['']
    def stop_unsafe(reason):
        if not immediate_reason[0]:
            immediate_reason[0]=reason
        if job_active.is_set():
            kernel.TerminateJobObject(job,1)
    temperatures=deque()
    save(report)
    try:
        if not mining_allowed(config['功耗排程']):
            raise RuntimeError(SCHEDULE_END)
        arguments=startup.args(config['礦池'],LOG)
        # 在任何背景讀值啟動前清空本輪紀錄，避免把上一輪接受次數算進來。
        LOG.write_bytes(b'')
        process=startup.suspended_launch(arguments,output,kernel,job)
        def validate_sample(sample):
            startup.check(sample)
            if not mining_allowed(config['功耗排程']):
                raise RuntimeError(SCHEDULE_END)
        sensors=BackgroundReading(reader.read,validate_sample,.05,stop_unsafe,1)
        power=BackgroundReading(lambda:read_runtime_guard(guard['執行識別碼']),None,.2,stop_unsafe,4)
        logger=BackgroundLog()
        report['礦工PID']=process.pid
        report['監控PID']=__import__('os').getpid()
        report['狀態']='挖礦中，核對持續負載'
        print(f"啟動核對已通過；依保存設定開始{guard['目標功耗上限W']}W真實挖礦，持續讀取兩組風扇及溫度。",flush=True)
        # 程序建立與背景執行緒初始化完成後才開始計時，避免誤判啟動耗時。
        report['初始化秒數']=round(time.monotonic()-begin,3)
        begin=time.monotonic()
        previous=begin
        next_save=begin
        while True:
            current=time.monotonic()
            elapsed=current-begin
            gap=current-previous
            previous=current
            report['最長檢查間隔秒數']=max(report['最長檢查間隔秒數'],round(gap,4))
            if not mining_allowed(config['功耗排程']):
                raise RuntimeError(SCHEDULE_END)
            if process.poll() is not None:
                raise RuntimeError(immediate_reason[0] or f'礦工已退出，代碼{process.returncode}，不自動重啟。')
            sensor_state=sensors.snapshot()
            power_state=power.snapshot()
            log_state=logger.snapshot()
            if sensor_state['資料'] is None or power_state['資料'] is None:
                if current-sensor_state['時間']>1 or current-power_state['時間']>4:
                    raise RuntimeError('背景讀值未在期限內返回，停止。')
                if sensor_state['錯誤'] or power_state['錯誤']:
                    raise RuntimeError(sensor_state['錯誤'] or power_state['錯誤'])
                time.sleep(.01)
                continue
            current_guard=power_state['資料']
            sample=sensor_state['資料']
            report['樣本數']=sensor_state['序號']
            report['最高實際風扇百分比']=max(report['最高實際風扇百分比'],max(item['實際百分比'] for item in sample['風扇']))
            report['最高設定風扇百分比']=max(report['最高設定風扇百分比'],max(item['設定百分比'] for item in sample['控制']))
            for key in ('核心C','熱點C','顯存C'):
                report['最高'+key]=max(report['最高'+key],sample[key])
            report['經過秒數']=round(elapsed,1)
            report['目前讀值']={**sample,**current_guard['顯卡']}
            report['功耗上限W']=current_guard['顯卡']['功耗上限W']
            report['風扇目標百分比']=current_guard.get('風扇目標百分比')
            report['排程模式']=current_guard.get('模式','依保存的時段設定運行')
            report['平日提高功耗時段']=current_guard.get('平日提高功耗時段',False)
            report['各步最長秒數']={'顯卡讀取':round(sensor_state['最長讀取秒數'],4),'功耗狀態讀取':round(power_state['最長讀取秒數'],4),'紀錄檔讀寫':round(log_state['最長記錄秒數'],4)}
            startup.check(sample)
            if sensor_state['錯誤'] or power_state['錯誤'] or log_state['錯誤']:
                raise RuntimeError(sensor_state['錯誤'] or power_state['錯誤'] or log_state['錯誤'])
            if current-sensor_state['時間']>1:
                raise RuntimeError('顯卡即時感測超過一秒未更新，停止。')
            if current-power_state['時間']>4:
                raise RuntimeError('功耗即時保護超過四秒未更新，停止。')
            report['紀錄讀取提示']=log_state.get('讀取提示','')
            report['紀錄寫入提示']=log_state.get('寫入提示','')
            report['紀錄寫入延遲秒數']=round(max(0,current-log_state['寫入更新時間']),2)
            report['紀錄讀取延遲秒數']=round(max(0,current-log_state['更新時間']),2)
            if elapsed>20 and not log_state['礦工控制已關閉']:
                raise RuntimeError('未確認關閉礦工GPU控制及CPU挖礦。')
            if gap>1:
                # 安全讀值仍由獨立執行緒確認新鮮度及立即停機；紀錄迴圈延遲只記錄。
                report['紀錄迴圈超過一秒次數']=report.get('紀錄迴圈超過一秒次數',0)+1
            if current>=next_save:
                accepted=log_state['接受算力證明數']
                report['接受算力證明數']=accepted
                report['礦工控制已關閉']=log_state['礦工控制已關閉']
                temperatures.append((elapsed,sample['核心C'],sample['熱點C'],sample['顯存C']))
                while temperatures and elapsed-temperatures[0][0]>60:
                    temperatures.popleft()
                stable=stable_temperatures(temperatures,45)
                report['一分鐘GPU核心最高溫趨勢已穩定']=stable
                if elapsed>=60 and accepted>0 and stable and not report['持續負載核對通過']:
                    report['持續負載核對通過']=True
                    report['持續負載通過UTC']=utc()
                    report['狀態']='挖礦中，持續負載核對通過'
                    print('至少一分鐘持續負載與算力接受核對通過，繼續挖礦。',flush=True)
                if elapsed>900 and accepted==0 and current-log_state['更新時間']<=10:
                    raise RuntimeError('十五分鐘內沒有接受算力證明，停止以免空耗電。')
                if elapsed>1800 and not report['持續負載核對通過']:
                    raise RuntimeError('三十分鐘內未通過持續負載核對，停止。')
                report['更新時間UTC']=utc()
                live_channel.publish(report)
                logger.submit(report)
                next_save=current+1
            time.sleep(max(0,.05-(time.monotonic()-current)))
    except Exception as exc:
        report['原因']=str(exc)
        print(report['原因'],flush=True)
    finally:
        job_active.clear()
        kernel.TerminateJobObject(job,1)
        kernel.CloseHandle(job)
        if process and process.poll() is None:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # 保留原始停止原因；驅動未立即結束負載時不覆寫為等待逾時。
                report['礦工結束等待逾時']=True
        output.close()
        for worker in (sensors,power,logger):
            if worker:
                worker.stop.set()
        if logger:
            logger.recording.stop.set()
            logger.thread.join(timeout=5)
            report['接受算力證明數']=logger.snapshot()['接受算力證明數']
        # 內部錯誤停止礦工後，排程會因核對失效回到150W；使用者停止標記會結束功耗保護。
        report['狀態']='已停止'
        report['排程停止']=report.get('原因')==SCHEDULE_END
        report['結束UTC']=utc()
        report['更新時間UTC']=utc()
        live_channel.publish(report)
        save(report)
        live_channel.close()
        print('離峰停止，入口將等待下一個核准時段。' if report['排程停止'] else '礦工已停止，沒有自動重啟。',flush=True)
    return {'排程停止':report.get('排程停止',False),'原因':report.get('原因','')}


if __name__=='__main__':
    try:
        with (ROOT/'TEST_執行鎖.dat').open('a+b') as lock:
            if lock.tell()==0:
                lock.write(b'0')
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
            try:
                main()
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1)
    except Exception as exc:
        print('未開始或已停止：'+str(exc),flush=True)
        raise SystemExit(1)
