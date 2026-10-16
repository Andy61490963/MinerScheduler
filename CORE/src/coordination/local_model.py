"""本地模型忙碌時暫停挖礦；閒置後交回原啟動流程，不更改硬體保護。"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parent
MODEL_SLOTS = "http://127.0.0.1:8082/slots"
PREFIX = "Emerald 本地模型暫停挖礦："


def model_busy(opener=urlopen):
    """服務停止可交還挖礦；載入中、逾時及無法確認時先暫停。"""
    try:
        with opener(MODEL_SLOTS, timeout=2.5) as response:
            slots = json.load(response)
        if not isinstance(slots, list) or not slots:
            return True
        if any(not isinstance(slot, dict) or type(slot.get("is_processing")) is not bool
               for slot in slots):
            return True
        return any(slot["is_processing"] for slot in slots)
    except HTTPError:
        return True
    except URLError as error:
        return not isinstance(error.reason, ConnectionRefusedError)
    except (OSError, ValueError, TypeError):
        return True


def powershell(source, capture=True):
    shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    runtime_env = os.environ.copy()
    runtime_env["PSModulePath"] = (
        str(shell.parent / "Modules") + ";"
        + str(Path(os.environ["ProgramFiles"]) / "WindowsPowerShell/Modules")
    )
    source = "$OutputEncoding=[Console]::OutputEncoding=[Text.UTF8Encoding]::new();\n" + source
    command = [str(shell), "-NoProfile", "-Command", source]
    options = dict(text=True, encoding="utf-8", errors="replace", timeout=25,
                   creationflags=0x08000000, check=True, env=runtime_env)
    if capture:
        return subprocess.run(command, capture_output=True, **options).stdout.strip()
    # 啟動程式會留下背景子程序；使用檔案，避免後代繼承 pipe 使父程序永遠等候。
    with (ROOT / "TEST_AI啟動輸出.log").open("ab") as output:
        subprocess.run(command, stdout=output, stderr=subprocess.STDOUT, **options)
    return ""


def mining_active():
    source = r"""
    $OutputEncoding=[Console]::OutputEncoding=[Text.UTF8Encoding]::new()
    $jobs=@(Get-CimInstance Win32_Process | Where-Object {
        $_.ExecutablePath -eq 'C:\test\CORE\TEST_Core\TEST.exe' -or
        ($_.ExecutablePath -eq 'C:\test\CORE\TEST_環境\python.exe' -and
         $_.CommandLine -like '*C:\test\CORE\TEST_入口.py*')
    })
    ($jobs.Count -gt 0) | ConvertTo-Json -Compress
    """
    result = json.loads(powershell(source))
    if type(result) is not bool:
        raise RuntimeError("無法確認既有挖礦程序，未啟動負載。")
    return result


def resume_mining(marker):
    # 原腳本仍負責時段、人工停止、官方檔案、功耗、風扇及溫度核對。
    powershell(
        "& ([scriptblock]::Create([IO.File]::ReadAllText('C:\\test\\CORE\\TEST_啟動.ps1'))) "
        "-AI恢復 -AI暫停標記 '" + marker.replace("'", "''") + "'", capture=False
    )


def stop_verified_miner():
    powershell(r"""
    Get-CimInstance Win32_Process | Where-Object {
        $_.ExecutablePath -eq 'C:\test\CORE\TEST_Core\TEST.exe'
    } | ForEach-Object { Stop-Process -Id $_.ProcessId -ErrorAction Stop }
    """)


class Coordinator:
    def __init__(self, root, active, resume, grace=60, claim=False):
        self.root, self.active, self.resume, self.grace = root, active, resume, grace
        self.stop = root / "TEST_停止.flag"
        self.lease = root / "TEST_AI暫停所有權.json"
        self.token = None
        self.idle_since = None
        try:
            saved = json.loads(self.lease.read_text(encoding="utf-8"))
            marker = saved.get("暫停標記")
            if isinstance(marker, str) and marker.startswith(PREFIX):
                if self.stop.read_text(encoding="utf-8") == marker:
                    self.token = marker
        except (OSError, ValueError):
            pass
        if claim and self.token is None:
            # 只由本次明確的「閒置後恢復」授權使用；一般登入不接管人工停止。
            self._claim(existing=True)

    def _claim(self, existing=False):
        marker = PREFIX + uuid4().hex
        try:
            with self.stop.open("w" if existing else "x", encoding="utf-8") as stream:
                stream.write(marker)
        except FileExistsError:
            return
        self.token = marker
        self.lease.write_text(json.dumps({"暫停標記": marker}, ensure_ascii=False), encoding="utf-8")

    def owns_stop(self):
        try:
            return self.token is not None and self.stop.read_text(encoding="utf-8") == self.token
        except OSError:
            return False

    def tick(self, busy, now):
        if self.token is not None and not self.owns_stop():
            self.token = None
        if busy:
            self.idle_since = None
            if self.token is None and not self.stop.exists() and self.active():
                self._claim()
            return "模型忙碌，暫停挖礦" if self.owns_stop() else "模型忙碌，未啟動挖礦"
        if self.idle_since is None:
            self.idle_since = now
        if self.stop.exists() and not self.owns_stop():
            return "保留人工停止，不自動恢復"
        if self.owns_stop():
            if now - self.idle_since < self.grace:
                return "模型閒置，等待恢復緩衝"
            self.resume(self.token)
            if self.stop.exists():
                raise RuntimeError("原啟動流程未解除 AI 暫停，保留停止狀態。")
            self.token = None
            return "已交回原挖礦時段排程"
        return "模型閒置，由原挖礦排程管理"


def main():
    import msvcrt

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claim-existing", action="store_true", help="接管本次 AI 工作暫停，閒置後恢復")
    args = parser.parse_args()
    with (ROOT / "TEST_AI協調鎖.dat").open("a+b") as lock:
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return
        coordinator = Coordinator(ROOT, mining_active, resume_mining, claim=args.claim_existing)
        previous = None
        last_save = 0
        pause_since = None
        while True:
            now = time.monotonic()
            busy = model_busy()
            state = coordinator.tick(busy, now)
            if coordinator.owns_stop() and busy:
                pause_since = now if pause_since is None else pause_since
                if now - pause_since >= 5:
                    # 正常先走原停止標記；程序未退出時，只停止已核對路徑的礦工。
                    stop_verified_miner()
                    pause_since = now
            else:
                pause_since = None
            if state != previous or now - last_save >= 10:
                (ROOT / "TEST_AI協調狀態.json").write_text(json.dumps({
                    "狀態": state, "模型忙碌": busy, "模型監測網址": MODEL_SLOTS,
                    "閒置緩衝秒數": 60, "協調程序PID": os.getpid(), "更新時間": time.time(),
                }, ensure_ascii=False, indent=2), encoding="utf-8")
                if state != previous:
                    print(state, flush=True)
                previous, last_save = state, now
            time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        (ROOT / "TEST_AI協調狀態.json").write_text(json.dumps({
            "狀態": "自動協調停止，請查看協調錯誤紀錄",
            "錯誤類型": type(error).__name__, "更新時間": time.time(),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        raise
