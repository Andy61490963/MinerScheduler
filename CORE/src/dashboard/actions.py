"""明確點擊後啟動原有流程；不自動重試，不直接控制硬體。"""
import ctypes
import hashlib
import os
from pathlib import Path
import subprocess
import threading
import time
import uuid
import settings

ROOT=Path(__file__).resolve().parent
SOURCE=ROOT.parent
STOP=SOURCE/'TEST_停止.flag'
ACTION_LOG=ROOT/'操作.log'


def launcher_environment():
    environment=os.environ.copy()
    environment['PSModulePath']='C:/Windows/System32/WindowsPowerShell/v1.0/Modules;C:/Program Files/WindowsPowerShell/Modules'
    return environment


def manual_stop_requested():
    try:
        return not STOP.read_text(encoding='utf-8-sig',errors='replace').startswith('Emerald 本地模型暫停挖礦：')
    except FileNotFoundError:
        return False


def mining_jobs():
    """只查詢此工作目錄的礦工與啟動入口，不操作其他工作。"""
    import json
    source=r"""[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
    $jobs=@(Get-CimInstance Win32_Process | Where-Object {
      $_.ExecutablePath -eq 'C:\test\CORE\TEST_Core\TEST.exe' -or
      ($_.ExecutablePath -eq 'C:\test\CORE\TEST_環境\python.exe' -and $_.CommandLine -like '*C:\test\CORE\TEST_入口.py*')
    } | Select-Object ProcessId,Name)
    ConvertTo-Json -InputObject $jobs -Compress
    """
    reply=subprocess.run(['C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe','-NoLogo','-NoProfile','-NonInteractive','-Command',source],
                         capture_output=True,encoding='utf-8-sig',errors='replace',timeout=8,
                         creationflags=subprocess.CREATE_NO_WINDOW,env=launcher_environment(),check=True)
    result=json.loads(reply.stdout)
    if not isinstance(result,list):
        raise RuntimeError('無法確認礦工與入口程序。')
    return result


def terminate_verified_miner(pid):
    """人工停止未完成時，只結束同一個已驗證映像控制代碼所指的礦工。"""
    from ctypes import wintypes as W
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=[W.DWORD,W.BOOL,W.DWORD]
    kernel.OpenProcess.restype=W.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes=[W.HANDLE,W.DWORD,W.LPWSTR,ctypes.POINTER(W.DWORD)]
    kernel.TerminateProcess.argtypes=[W.HANDLE,W.UINT]
    kernel.CloseHandle.argtypes=[W.HANDLE]
    handle=kernel.OpenProcess(0x1001,False,int(pid))
    if not handle:
        return False
    try:
        name=ctypes.create_unicode_buffer(32768)
        length=W.DWORD(len(name))
        if (kernel.QueryFullProcessImageNameW(handle,0,name,ctypes.byref(length))
                and Path(name.value).resolve()==(SOURCE/'TEST_Core/TEST.exe').resolve()):
            return bool(kernel.TerminateProcess(handle,1))
        return False
    finally:
        kernel.CloseHandle(handle)


def healthy_snapshot(current):
    return (current.get('state')=='running' and current.get('guard_ok') is True
            and 0<=time.time()-current.get('server_time',0)<=6
            and current.get('status_age',999)<=5 and current.get('guard_age',999)<=5)


def stop_fingerprint():
    try:
        stat=STOP.stat()
        return hashlib.sha256(STOP.read_bytes()).hexdigest()+':'+str(stat.st_mtime_ns//100+621355968000000000)
    except FileNotFoundError:
        return '無'


class MiningActions:
    def __init__(self,current,verified_alive):
        self.current=current
        self.verified_alive=verified_alive
        self.lock=threading.Lock()
        self.command_lock=threading.Lock()
        self.operation=0
        self.cancel=threading.Event()
        self.start_done=threading.Event()
        self.start_done.set()
        self.state={'busy':False,'phase':'idle','message':'','updated':time.time()}

    def snapshot(self):
        with self.lock:
            return self.state.copy()

    def update(self,phase,message,busy=True,operation=None):
        with self.lock:
            if operation is not None and operation!=self.operation:
                return
            self.state={'busy':busy,'phase':phase,'message':message,'updated':time.time()}
        try:
            with ACTION_LOG.open('a',encoding='utf-8') as stream:
                stream.write(time.strftime('[%Y-%m-%d %H:%M:%S] ')+message+'\n')
        except OSError:
            pass

    def begin(self):
        with self.lock:
            if self.state['busy']:
                return {'ok':True,'message':'正在處理開始或停止要求，請稍候。'}
            current=self.current()
            if healthy_snapshot(current) and self.verified_alive(current.get('miner_pid')):
                return {'ok':True,'message':'挖礦已在運行，沒有重複啟動。'}
            self.operation+=1
            operation=self.operation
            self.cancel=threading.Event()
            cancel=self.cancel
            self.start_done.clear()
            self.state={'busy':True,'phase':'starting','message':'準備原有啟動核對。','updated':time.time()}
        fingerprint=stop_fingerprint()
        threading.Thread(target=self.run,args=(fingerprint,operation,cancel),daemon=True).start()
        return {'ok':True,'message':'已開始啟動流程，進度會顯示在畫面及啟動 LOG。'}

    def request_stop(self):
        with self.lock:
            if self.state.get('phase')=='stopping' and self.state['busy']:
                return {'ok':True,'message':'停止要求已送出，正在確認礦工結束。'}
            self.cancel.set()
            self.operation+=1
            operation=self.operation
            self.state={'busy':True,'phase':'stopping','message':'正在停止挖礦，並保留人工停挖狀態。','updated':time.time()}
        marker='監控畫面人工停止：'+str(uuid.uuid4())
        try:
            with self.command_lock:
                STOP.write_text(marker,encoding='utf-8')
        except OSError as exc:
            self.update('error','停止要求寫入失敗：'+str(exc),False,operation)
            return {'ok':False,'message':'停止要求未能寫入，請查看操作 LOG。'}
        self.update('stopping','已送出人工停止；正在確認礦工與啟動工作結束。',True,operation)
        threading.Thread(target=self.wait_stopped,args=(marker,operation),daemon=True).start()
        return {'ok':True,'message':'已送出停止要求；停止後不會自行恢復，直到再次按開始。'}

    def apply_settings(self,payload):
        plan=settings.prepare(payload)
        with self.lock:
            if self.state['busy']:
                raise ValueError('正在處理其他操作，請等進度完成；停止按鈕仍可使用。')
            if plan['unchanged']:
                return {'ok':True,'message':'設定相同，沒有變更或中斷挖礦。'}
            if ctypes.windll.shell32.IsUserAnAdmin():
                raise ValueError('監控必須以一般使用者運行。')
            fingerprint=stop_fingerprint()
            if STOP.exists() and not manual_stop_requested():
                raise ValueError('本地模型正在協調暫停；請等恢復後調整，或先按停止挖礦再設定。')
            current=self.current()
            resume=current.get('state')=='running'
            if resume and (fingerprint!='無' or not healthy_snapshot(current) or not self.verified_alive(current.get('miner_pid'))):
                raise ValueError('挖礦或保護狀態尚未確認，請先查看 LOG 或恢復挖礦。')
            self.operation+=1
            operation=self.operation
            self.cancel=threading.Event()
            cancel=self.cancel
            self.start_done.clear()
            self.state={'busy':True,'phase':'settings','message':'準備停止負載並保存設定。','updated':time.time()}
        threading.Thread(target=self.run_settings,args=(plan,fingerprint,resume,operation,cancel),daemon=True).start()
        return {'ok':True,'message':'已收到設定；先停止負載，再保存並重新核對。'+('原本挖礦正常，通過後恢復挖礦。' if resume else '原本未挖礦，完成後保持停止。')}

    def run_settings(self,plan,initial_fingerprint,resume,operation,cancel):
        expected=initial_fingerprint
        touched=False
        fans_stopped=False
        fans_started=False
        outcome=None
        def checkpoint():
            if cancel.is_set():
                raise RuntimeError('收到較新的人工停止，取消設定後的恢復。')
            if expected is not None and stop_fingerprint()!=expected:
                raise RuntimeError('人工停止狀態已變更，取消套用或恢復。')
            if expected is None and STOP.exists():
                raise RuntimeError('收到人工或保護停止，不再恢復。')
        def update(message):
            self.update('settings',message,True,operation)
        try:
            with self.command_lock:
                checkpoint()
                if not STOP.exists():
                    STOP.write_text('監控畫面套用設定暫停：'+str(uuid.uuid4()),encoding='utf-8')
                expected=stop_fingerprint()
                touched=True
            values=plan['values']
            update(f"收到設定：平日{values['day_watts']}W、離峰{values['night_watts']}W、顯卡{values['gpu_fan']}%、主機板{values['board_fan']}%；正在停止負載並等待低功耗基準。")
            settings.wait_quiet(checkpoint)
            checkpoint()
            settings.workers('停止風扇')
            fans_stopped=True
            checkpoint()
            backup=settings.write_plan(plan)
            update('設定已保存，備份：'+str(backup)+'；正在載入並核對五組風扇。')
            checkpoint()
            settings.workers('啟動風扇')
            fans_started=True
            settings.verify_fans(plan['values'],checkpoint)
            checkpoint()
            if not resume:
                outcome=('success','設定已套用，五組風扇已確認；保留原本停挖狀態，按開始才挖礦。')
                return
            self.run(expected,operation,cancel,complete=False)
            with self.lock:
                if operation!=self.operation:
                    return
                if self.state['phase']!='success':
                    raise RuntimeError(self.state['message'])
            expected=None
            update('已重新開始挖礦，等待一分鐘負載核對及功耗目標確認。')
            power=settings.verify_guard(plan['values'],checkpoint,plan['documents'][settings.FILES[0]]['功耗排程'])
            outcome=('success',f'設定已套用，五組風扇與保護已確認；目前保護目標{power}W，溫控優先。')
        except Exception as exc:
            if touched:
                try:
                    with self.command_lock:
                        if not STOP.exists():
                            STOP.write_text('監控設定核對未通過，保留停挖：'+str(exc),encoding='utf-8')
                except OSError as stop_error:
                    exc=RuntimeError(str(exc)+'；停止標記寫入失敗：'+str(stop_error))
            outcome=('error','套用未完成：'+str(exc))
        finally:
            if fans_stopped and not fans_started:
                try:
                    settings.workers('啟動風扇')
                except Exception as exc:
                    outcome=('error','風扇背景未恢復，保留停挖：'+str(exc))
            self.start_done.set()
            if outcome:
                self.update(*outcome,False,operation)

    def wait_stopped(self,marker,operation):
        began=time.monotonic()
        try:
            while time.monotonic()-began<60:
                with self.command_lock:
                    # 啟動腳本可能剛解除舊停止標記；取消啟動後持續保留較新的人工停止。
                    if not STOP.exists():
                        STOP.write_text(marker,encoding='utf-8')
                jobs=mining_jobs()
                if not jobs and self.start_done.is_set():
                    self.update('stopped','挖礦已停止。人工停挖已保留，按「開始／恢復挖礦」才會再開始。',False,operation)
                    return
                if time.monotonic()-began>=5:
                    for job in jobs:
                        if job.get('Name','').lower()=='test.exe':
                            terminate_verified_miner(job['ProcessId'])
                time.sleep(.5)
            raise RuntimeError('停止要求已保留，但舊工作尚未完全結束；請查看操作 LOG。')
        except Exception as exc:
            self.update('error',str(exc),False,operation)

    def run(self,initial_fingerprint,operation=None,cancel=None,complete=True):
        def checkpoint():
            if cancel is not None and cancel.is_set():
                raise RuntimeError('已取消啟動，保留較新的人工停止。')
        def update(phase,message,busy=True):
            self.update(phase,message,busy or not complete,operation)
        try:
            checkpoint()
            if ctypes.windll.shell32.IsUserAnAdmin():
                raise RuntimeError('監控必須以一般使用者啟動，礦工不能使用管理員權限。')
            marker='監控畫面恢復挖礦：'+str(uuid.uuid4())
            with self.command_lock:
                checkpoint()
                if stop_fingerprint()!=initial_fingerprint:
                    raise RuntimeError('收到較新的人工停止，已取消本次恢復。')
                STOP.write_text(marker,encoding='utf-8')
                expected=stop_fingerprint()
            update('checking','正在收妥前次工作並重新核對保護，不強制結束不明程序。')
            arguments=['C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe',
                       '-NoLogo','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass',
                       '-File',str(SOURCE/'TEST_啟動.ps1'),'-監控停止驗證',expected]
            began=time.time()
            environment=launcher_environment()
            checkpoint()
            with ACTION_LOG.open('ab') as output:
                child=subprocess.Popen(arguments,cwd=SOURCE,stdout=output,stderr=subprocess.STDOUT,
                                       creationflags=subprocess.CREATE_NO_WINDOW,env=environment)
                try:
                    code=child.wait(timeout=45)
                except subprocess.TimeoutExpired:
                    child.terminate()
                    raise RuntimeError('既有啟動流程逾時，已保留停止狀態；詳見操作 LOG。')
            checkpoint()
            if code:
                raise RuntimeError('啟動流程未通過，請查看操作／啟動 LOG；沒有強制重啟。')
            update('checking','啟動核對進行中，通過後自動開始挖礦。')
            deadline=time.monotonic()+180
            while time.monotonic()<deadline:
                checkpoint()
                if STOP.exists():
                    raise RuntimeError('收到新的人工停止或保護停止，已取消本次恢復。')
                current=self.current()
                if current.get('state')=='running' and self.verified_alive(current.get('miner_pid')):
                    update('success','挖礦已開始；風扇、溫度及功耗保護持續運作。',False)
                    return
                if (current.get('state')=='stopped' and (current.get('status_time') or 0)>began
                        and (current.get('session_start') or 0)>=began):
                    raise RuntimeError(current.get('reason') or current.get('message') or '啟動核對未通過，請查看啟動 LOG。')
                time.sleep(1)
            raise RuntimeError('三分鐘內尚未開始挖礦，請查看啟動 LOG；排除原因後可再次按開始。')
        except Exception as exc:
            update('error',str(exc),False)
        finally:
            if complete:
                self.start_done.set()
