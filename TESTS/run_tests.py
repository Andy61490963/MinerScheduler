"""單一離線測試入口：組裝臨時版本、阻擋外部程序及正式目錄寫入。"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
import uuid

ROOT = Path(__file__).resolve().parent
FORMAL = ROOT.parent / 'CORE'
LIVE = Path(r'C:\test\CORE').resolve()


def audit(event, args):
    """測試不可碰正式設定或啟停程序；連線僅供臨時 HTTP 測試。"""
    if event == 'open':
        path, mode, flags = args
        if isinstance(path, (str, bytes, os.PathLike)):
            target = Path(os.fsdecode(path)).resolve()
            writing = flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
            if writing and target.is_relative_to(LIVE):
                raise PermissionError('離線測試禁止寫入正式運行目錄。')
    elif event in ('os.remove', 'os.rmdir', 'os.mkdir', 'os.rename', 'os.chmod', 'os.utime', 'os.link', 'os.symlink'):
        count = 2 if event in ('os.rename', 'os.link', 'os.symlink') else 1
        for raw in args[:count]:
            if isinstance(raw, (str, bytes, os.PathLike)) and Path(os.fsdecode(raw)).resolve().is_relative_to(LIVE):
                raise PermissionError('離線測試禁止變更正式運行目錄。')
    elif event in ('subprocess.Popen', 'os.system', 'os.startfile', 'os.startfile/2'):
        raise PermissionError('離線測試禁止啟動外部程序；請使用模擬。')
    elif event == 'ctypes.dlopen' and any(name in str(args[0]).lower() for name in ('nvml', 'nvapi')):
        raise PermissionError('離線測試禁止載入顯卡控制函式庫。')
    elif event == 'socket.connect':
        address = args[1]
        if not isinstance(address, tuple) or address[0] not in ('127.0.0.1', '::1') or address[1] in (18776, 8082):
            raise PermissionError('離線測試不可連線正式監控、模型或外部服務。')
    elif event == 'socket.bind':
        address = args[1]
        if not isinstance(address, tuple) or address[0] != '127.0.0.1' or address[1] != 0:
            raise PermissionError('離線測試僅可監聽臨時的本機埠。')


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    sys.dont_write_bytecode = True
    sys.addaudithook(audit)
    spec = importlib.util.spec_from_file_location('formal_project', FORMAL / 'project.py')
    project = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = project
    spec.loader.exec_module(project)
    with TemporaryDirectory(prefix='挖礦離線測試_') as temporary:
        isolated = Path(temporary) / 'CORE'
        report = project.build(isolated)
        sys.path[:0] = [str(isolated / 'TEST_效率監控'), str(isolated), str(ROOT)]
        import context
        context.BUILD = isolated
        context.FORMAL = FORMAL
        context.PROJECT = project
        context.REPORT = report
        import TEST_即時通道 as channels
        # 即使未來測試誤用預設通道，也不得接觸正在運行的 Global 心跳。
        channels.NAMES = {key: 'Local\\TESTS_' + uuid.uuid4().hex for key in channels.NAMES}
        def forbidden_global(*args, **kwargs):
            raise PermissionError('離線測試禁止建立正式的共享記憶體通道。')
        channels.create_global = forbidden_global
        suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'), pattern='test_*.py')
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        (ROOT / '測試結果.json').write_text(json.dumps({
            '測試數': result.testsRun, '失敗數': len(result.failures), '錯誤數': len(result.errors),
            '通過': result.wasSuccessful(), '執行方式': '臨時版本、模擬硬體、禁止正式目錄寫入及外部程序',
        }, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
