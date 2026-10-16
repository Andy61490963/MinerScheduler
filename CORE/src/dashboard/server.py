"""本機效率與紀錄監控；明確點擊時透過既有一般使用者流程恢復挖礦。"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from collections import deque
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import shutil
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit,parse_qs

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT.parent
sys.path.insert(0,str(SOURCE))
sys.path.insert(0,str(ROOT))
from TEST_即時通道 import read_channel
from actions import MiningActions, manual_stop_requested
import settings
IDENTITY = "test-mining-efficiency-monitor-v1"
ACTION_TOKEN=secrets.token_urlsafe(24)
TW = timezone(timedelta(hours=8))
LOG_LINE = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\]")
HASH_LINE = re.compile(r"\]\s+GPU0\s+.+?:\s*([\d.]+)\s*([kMGTPE]?H/s).*?\bA:(\d+)\s+R:(\d+)\s+HW:(\d+)")
AVERAGE_LINE = re.compile(r"Average hashrate: 1m ([\d.]+) ([kMGTPE]?H/s) \| 15m ([\d.]+) ([kMGTPE]?H/s)")


def number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def stamp(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=TW).timestamp() if parsed.tzinfo is None else parsed.timestamp()
    except (ValueError, TypeError, OverflowError):
        return None


def age(timestamp, now):
    return max(0, now - timestamp) if timestamp is not None else None


def ths(value, unit):
    multiplier = {"H/S": 1e-12, "KH/S": 1e-9, "MH/S": 1e-6, "GH/S": .001,
                  "TH/S": 1, "PH/S": 1000, "EH/S": 1e6}.get(unit.upper())
    parsed = number(value)
    return parsed * multiplier if parsed is not None and multiplier is not None else None


if os.name == "nt":
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetFileSizeEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_longlong)]
    kernel.SetFilePointerEx.argtypes = [wintypes.HANDLE, ctypes.c_longlong, ctypes.c_void_p, wintypes.DWORD]
    kernel.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                               ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.argtypes=[wintypes.HANDLE,wintypes.DWORD,wintypes.LPWSTR,ctypes.POINTER(wintypes.DWORD)]


def shared_read(path: Path, limit=262144):
    """允許原程式同時讀、寫、替換檔案；最多讀取檔尾指定長度。"""
    if os.name != "nt":
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - limit))
            return stream.read(limit).decode("utf-8-sig", errors="replace")
    handle = kernel.CreateFileW(str(path), 0x80000000, 0x7, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise OSError(ctypes.get_last_error(), "紀錄檔暫時無法讀取")
    try:
        length = ctypes.c_longlong()
        if not kernel.GetFileSizeEx(handle, ctypes.byref(length)):
            raise OSError(ctypes.get_last_error(), "無法取得紀錄檔長度")
        offset = max(0, length.value - limit)
        if not kernel.SetFilePointerEx(handle, offset, None, 0):
            raise OSError(ctypes.get_last_error(), "無法讀取紀錄檔尾端")
        buffer = ctypes.create_string_buffer(min(limit, length.value) + 1)
        count = wintypes.DWORD()
        if not kernel.ReadFile(handle, buffer, min(limit, length.value), ctypes.byref(count), None):
            raise OSError(ctypes.get_last_error(), "讀取紀錄檔失敗")
        return buffer.raw[:count.value].decode("utf-8-sig", errors="replace")
    finally:
        kernel.CloseHandle(handle)


def pid_alive(pid):
    if not pid or os.name != "nt":
        return None
    handle = kernel.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return False if ctypes.get_last_error() == 87 else None
    try:
        result = wintypes.DWORD()
        return result.value == 259 if kernel.GetExitCodeProcess(handle, ctypes.byref(result)) else None
    finally:
        kernel.CloseHandle(handle)


def parse_log(text, start=0):
    reports = {}
    averages = {}
    for line in text.splitlines():
        match = LOG_LINE.match(line)
        if not match:
            continue
        timestamp = datetime.strptime(match[1], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TW).timestamp()
        if timestamp < start:
            continue
        hashed = HASH_LINE.search(line)
        if hashed:
            reports[timestamp] = {"t": timestamp, "hashrate": ths(hashed[1], hashed[2]),
                                  "accepted": int(hashed[3]), "rejected": int(hashed[4]),
                                  "errors": int(hashed[5])}
        average = AVERAGE_LINE.search(line)
        if average:
            averages[timestamp] = {"one_minute": ths(average[1], average[2]),
                                   "fifteen_minutes": ths(average[3], average[4])}
    for timestamp, report in reports.items():
        report.update(averages.get(timestamp, {}))
    return sorted(reports.values(), key=lambda item: item["t"])


def verified_miner(pid):
    if not pid or os.name!='nt':
        return False
    handle=kernel.OpenProcess(0x1000,False,int(pid))
    if not handle:
        return False
    try:
        name=ctypes.create_unicode_buffer(32768)
        size=wintypes.DWORD(len(name))
        return bool(kernel.QueryFullProcessImageNameW(handle,0,name,ctypes.byref(size))
                    and Path(name.value).resolve()==(SOURCE/'TEST_Core/TEST.exe').resolve()
                    and pid_alive(pid))
    finally:
        kernel.CloseHandle(handle)


def telemetry(values, timestamp):
    return {"t": timestamp, "power": number(values.get("功耗W")),
            "core": number(values.get("核心C")), "hotspot": number(values.get("熱點C")),
            "memory": number(values.get("顯存C"))}


def aligned_efficiency(report, samples):
    """以算力回報前約 30 秒的 GPU 平均耗電估算，資料不足時不產生數字。"""
    window = [p for p in samples if report["t"] - 30 <= p["t"] <= report["t"]
              and p.get("power") is not None and p["power"] > 0]
    if (len(window) < 10 or window[-1]["t"] - window[0]["t"] < 20
            or report["t"] - window[-1]["t"] > 5
            or any(b["t"] - a["t"] > 5 for a, b in zip(window, window[1:]))):
        return None, None
    mean = sum(p["power"] for p in window) / len(window)
    return report["hashrate"] / mean if report.get("hashrate") is not None else None, mean


class Monitor:
    def __init__(self):
        self.lock = threading.Lock()
        self.snapshot = {"identity": IDENTITY, "state": "waiting", "message": "正在讀取既有紀錄", "history": [], "hash_history": []}
        self.samples = deque(maxlen=1500)
        self.session = None
        self.sources = {}
        self.boards = {"fans": [], "timestamp": None, "error": None}
        self.stop = threading.Event()

    def read_json(self, name):
        channel_name={'TEST_Status.json':'miner','TEST_功耗狀態.json':'guard'}.get(name)
        live=read_channel(channel_name) if channel_name else None
        if live and stamp(live.get('更新時間UTC')) is not None and abs(time.time()-stamp(live['更新時間UTC']))<=15:
            self.sources[name]=live
            return live,None
        try:
            result = json.loads(shared_read(SOURCE / name, 65536))
            if not isinstance(result, dict):
                raise ValueError("格式錯誤")
            self.sources[name] = result
            return result, None
        except (OSError, ValueError):
            return self.sources.get(name, {}), "暫時無法讀取最新資料"

    def seed(self, start, now):
        self.samples.clear()
        try:
            for line in shared_read(SOURCE / "TEST_風扇溫度.jsonl", 2097152).splitlines():
                try:
                    entry = json.loads(line)
                    timestamp = stamp(entry.get("時間UTC"))
                    if timestamp is not None and max(start, now - 900) <= timestamp <= now + 2:
                        self.samples.append(telemetry(entry, timestamp))
                except (ValueError, TypeError, AttributeError):
                    continue
        except OSError:
            pass

    def collect(self):
        now = time.time()
        status, status_error = self.read_json("TEST_Status.json")
        guard, guard_error = self.read_json("TEST_功耗狀態.json")
        start = stamp(status.get("開始UTC")) or now
        session = (status.get("開始UTC"), status.get("礦工PID"))
        if session != self.session:
            self.session = session
            self.seed(start, now)
        timestamp = stamp(status.get("更新時間UTC"))
        status_age = age(timestamp, now)
        values = status.get("目前讀值") or {}
        live = pid_alive(status.get("礦工PID"))
        fresh = status_age is not None and status_age <= 15
        running = fresh and str(status.get("狀態", "")).startswith("挖礦中") and live is not False
        try:
            manual_stop = manual_stop_requested()
            stop_error = None
        except OSError:
            manual_stop = None
            stop_error = "暫時無法確認人工停止標記"
        state = "running" if running else "unknown" if not fresh else "stopped"
        if manual_stop and live is False:
            state = "stopped"
        sensor_time = timestamp
        if not running:
            # 停挖後礦工不再更新；風扇排程仍可提供即時降溫讀值。
            fan_status, _ = self.read_json("TEST_風扇排程狀態.json")
            fan_time = stamp(fan_status.get("更新時間UTC"))
            if fan_time is not None and 0 <= now - fan_time <= 15 and fan_time >= (timestamp or 0):
                values = {**(fan_status.get("顯卡") or {}),
                          "風扇": fan_status.get("兩組風扇", []),
                          "控制": fan_status.get("兩組風扇控制", [])}
                sensor_time = fan_time
        sensor_age = age(sensor_time, now)
        sensor_fresh = sensor_age is not None and sensor_age <= 15
        if sensor_time is not None and (not self.samples or sensor_time > self.samples[-1]["t"]):
            self.samples.append(telemetry(values, sensor_time))
        while self.samples and self.samples[0]["t"] < now - 900:
            self.samples.popleft()
        reports = []
        log_error = None
        try:
            reports = parse_log(shared_read(SOURCE / "TEST.log"), start - 1)
        except (OSError, ValueError):
            log_error = "暫時無法讀取算力回報"
        history = [dict(item) for item in self.samples]
        for report in reports:
            report["efficiency"], report["aligned_power"] = aligned_efficiency(report, history)
        latest = dict(reports[-1]) if reports else {}
        hash_age = age(latest.get("t"), now)
        hash_valid = running and hash_age is not None and hash_age <= 180
        if not hash_valid:
            latest["hashrate"] = None
            latest["efficiency"] = None
        guard_timestamp = stamp(guard.get("更新時間UTC"))
        guard_age = age(guard_timestamp, now)
        guard_ok = (guard_age is not None and guard_age <= 15
                    and guard.get("執行識別碼") == status.get("保護識別碼")
                    and guard.get("狀態") == "排程保護已就緒")
        fan_controls = {item.get("編號"): number(item.get("設定百分比"))
                        for item in values.get("控制", []) if isinstance(item, dict)}
        fans = [{"name": f"顯卡風扇 {item.get('編號')}", "rpm": number(item.get("RPM")),
                 "percent": number(item.get("實際百分比")), "target": fan_controls.get(item.get("編號"))}
                for item in values.get("風扇", []) if isinstance(item, dict)]
        warnings = []
        if status_error:
            warnings.append(status_error)
        if stop_error:
            warnings.append(stop_error)
        if sensor_age is None or sensor_age > 5:
            warnings.append("感測紀錄更新延遲；請以資料時間判讀")
        if running and not guard_ok:
            warnings.append("保護心跳延遲或尚未確認")
        hotspot = number(values.get("熱點C"))
        if sensor_fresh and hotspot is not None and hotspot >= 105:
            warnings.append(f"熱點溫度偏高：{hotspot:.1f}°C，請留意散熱狀況")
        if running and (hash_age is None or hash_age > 120):
            warnings.append("算力回報尚未更新；超過 180 秒時暫不計算效率")
        if log_error:
            warnings.append(log_error)
        if (status.get('紀錄寫入提示') or guard.get('紀錄寫入提示')
                or (number(status.get('紀錄寫入延遲秒數')) or 0)>10
                or (number(guard.get('紀錄寫入延遲秒數')) or 0)>10):
            warnings.append('磁碟紀錄暫時延遲，正在背景重試；即時保護心跳仍獨立運作')
        with self.lock:
            boards = dict(self.boards)
        result = {
            "identity": IDENTITY, "server_time": now, "state": state,
            "action":ACTIONS.snapshot(),"action_token":ACTION_TOKEN,"reason":status.get('原因',''),
            "manual_stop": manual_stop,
            "message": "人工停挖中" if manual_stop and state == "stopped" else status.get("狀態") or "尚無挖礦狀態紀錄", "warnings": warnings,
            "session_start": start, "elapsed": max(0, now - start) if running else number(status.get("經過秒數")), "miner_pid": status.get("礦工PID"),
            "process_alive": live, "status_time": timestamp, "status_age": status_age,
            "sensor_fresh": sensor_fresh, "sensor_time": sensor_time, "sensor_age": sensor_age,
            "hash_age": hash_age, "hash_time": latest.get("t"),
            "hash_valid": hash_valid, "hashrate": latest.get("hashrate"),
            "one_minute": latest.get("one_minute") if hash_valid else None,
            "fifteen_minutes": latest.get("fifteen_minutes") if hash_valid else None,
            "efficiency": latest.get("efficiency"), "aligned_power": latest.get("aligned_power"),
            "accepted": status.get("接受算力證明數"), "rejected": latest.get("rejected"),
            "hardware_errors": latest.get("errors"), "guard_ok": guard_ok,
            "guard_time": guard_timestamp, "guard_age": guard_age,
            "guard_read_error": guard_error, "power": number(values.get("功耗W")),
            "limit": number(values.get("功耗上限W")), "utilization": number(values.get("使用率")),
            "core_clock": number(values.get("核心時脈MHz")), "memory_clock": number(values.get("顯存時脈MHz")),
            "temperatures": {"core": number(values.get("核心C")), "hotspot": hotspot, "memory": number(values.get("顯存C"))},
            "peaks": {"core": number(status.get("最高核心C")), "hotspot": number(status.get("最高熱點C")), "memory": number(status.get("最高顯存C"))},
            "gpu_fans": fans, "board": boards,
            "history": history, "hash_history": [report for report in reports if report["t"] >= now - 900],
        }
        with self.lock:
            self.snapshot = result

    def run(self):
        while not self.stop.is_set():
            began = time.monotonic()
            try:
                self.collect()
            except Exception:
                # 維持網頁可用，但不可將收集失敗偽裝成即時資料。
                with self.lock:
                    self.snapshot = {**self.snapshot, "state": "unknown", "sensor_fresh": False,
                                     "hash_valid": False, "hashrate": None, "efficiency": None,
                                     "one_minute": None, "fifteen_minutes": None, "guard_ok": False,
                                     "warnings": ["資料收集暫時失敗，等待下一次更新"]}
            self.stop.wait(max(.1, 2 - (time.monotonic() - began)))

    def board_loop(self):
        executable = shutil.which("pwsh") or str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe")
        while not self.stop.is_set():
            try:
                completed = subprocess.run([executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                            "-File", str(ROOT / "board_fans.ps1")],
                                           capture_output=True, encoding="utf-8-sig", timeout=6,
                                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if completed.returncode != 0:
                    raise ValueError("風扇快取暫時無法讀取")
                data = json.loads(completed.stdout.strip())
                if not isinstance(data.get("fans"), list):
                    raise ValueError("資料格式錯誤")
                data.update(timestamp=time.time(), error=None)
                with self.lock:
                    self.boards = data
            except (OSError, ValueError, subprocess.TimeoutExpired):
                with self.lock:
                    self.boards = {**self.boards, "error": "FanControl 快取暫時無法讀取"}
            self.stop.wait(15)


MONITOR = Monitor()
def current_snapshot():
    with MONITOR.lock:
        return MONITOR.snapshot.copy()
ACTIONS=MiningActions(current_snapshot,verified_miner)
LOG_SOURCES={'miner':('礦工',SOURCE/'TEST.log'),'startup':('啟動',SOURCE/'TEST_入口輸出.log'),
             'guard':('功耗與保護',SOURCE/'TEST_排程紀錄.jsonl'),'actions':('監控操作',ROOT/'操作.log')}


def log_tail(source):
    if source not in LOG_SOURCES:
        raise ValueError('不支援的紀錄來源')
    name,path=LOG_SOURCES[source]
    try:
        content=shared_read(path,98304)
        text='\n'.join(content.splitlines()[-180:])
        updated=path.stat().st_mtime
    except FileNotFoundError:
        text='尚無紀錄。'
        updated=None
    if source=='guard':
        live=read_channel('guard')
        if live:
            summary={key:live.get(key) for key in ('狀態','原因','目標功耗上限W','風扇目標百分比','紀錄寫入提示','更新時間UTC')}
            text+='\n\n── 最新即時保護心跳 ──\n'+json.dumps(summary,ensure_ascii=False,indent=2)
    return {'source':source,'name':name,'text':text,'updated':updated,'read_time':time.time()}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def json_reply(self,data,code=200):
        body=json.dumps(data,ensure_ascii=False,allow_nan=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError,ConnectionResetError):
            pass

    def do_POST(self):
        host=self.headers.get('Host','')
        origin=self.headers.get('Origin','')
        allowed={f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}'}
        if (host not in allowed or origin!='http://'+host or self.headers.get('X-Monitor-Token')!=ACTION_TOKEN
                or self.headers.get('Content-Type','').split(';')[0]!='application/json'):
            self.json_reply({'ok':False,'message':'僅允許本機監控畫面的操作。'},403)
            return
        if self.path not in ('/api/start','/api/stop','/api/settings'):
            self.json_reply({'ok':False,'message':'不支援的操作。'},404)
            return
        if (SOURCE/'.搬移中.flag').exists() and self.path!='/api/stop':
            self.json_reply({'ok':False,'message':'正式程式正在搬移，完成前保留停挖；停止按鈕仍可使用。'},409)
            return
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<=length<=1024:
                raise ValueError()
            payload=json.loads(self.rfile.read(length) or b'{}')
            if self.path!='/api/settings' and payload!={}:
                raise ValueError()
        except (ValueError,TypeError):
            self.json_reply({'ok':False,'message':'操作內容格式不符。'},400)
            return
        try:
            result=(ACTIONS.apply_settings(payload) if self.path=='/api/settings' else
                    ACTIONS.request_stop() if self.path=='/api/stop' else ACTIONS.begin())
            self.json_reply(result,202)
        except ValueError as exc:
            self.json_reply({'ok':False,'message':str(exc)},400)
        except (OSError,KeyError,TypeError):
            self.json_reply({'ok':False,'message':'設定暫時無法讀取；未套用，請稍後重試。'},503)

    def do_GET(self):
        host = self.headers.get("Host", "").split(":")[0].lower()
        if host not in ("127.0.0.1", "localhost"):
            self.send_error(403)
            return
        route = urlsplit(self.path).path
        if route=='/api/settings':
            try:
                self.json_reply(settings.public_settings())
            except (OSError,ValueError,KeyError,TypeError):
                self.json_reply({'message':'設定暫時無法讀取。'},503)
            return
        if route=='/api/logs':
            try:
                source=parse_qs(urlsplit(self.path).query).get('source',['miner'])[0]
                self.json_reply(log_tail(source))
            except ValueError:
                self.json_reply({'message':'不支援的紀錄來源。'},400)
            except OSError:
                self.json_reply({'message':'紀錄暫時無法讀取，稍後會重試。'},503)
            return
        if route == "/api/health":
            body = json.dumps({"identity": IDENTITY}, ensure_ascii=False).encode("utf-8")
            content_type = "application/json; charset=utf-8"
        elif route == "/api/snapshot":
            with MONITOR.lock:
                snapshot=MONITOR.snapshot.copy()
            snapshot.update(action=ACTIONS.snapshot(),action_token=ACTION_TOKEN)
            body = json.dumps(snapshot, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
            content_type = "application/json; charset=utf-8"
        elif route in ("/", "/index.html", "/app.js", "/style.css"):
            path = ROOT / ("index.html" if route == "/" else route[1:])
            body = path.read_bytes()
            content_type = {".html": "text/html", ".js": "text/javascript", ".css": "text/css"}[path.suffix] + "; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


def main():
    parser = argparse.ArgumentParser(description="本機挖礦效率監控與開始／停止控制")
    parser.add_argument("--port", type=int, default=18776)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    for worker in (MONITOR.run, MONITOR.board_loop):
        threading.Thread(target=worker, daemon=True).start()
    try:
        server.serve_forever(poll_interval=.5)
    except KeyboardInterrupt:
        pass
    finally:
        MONITOR.stop.set()
        server.server_close()


if __name__ == "__main__":
    main()
