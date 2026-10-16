"""確認分層原始碼可組裝成相容版本，且測試入口拒絕改動運行目錄。"""
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import context
from run_tests import audit


class ProjectStructureTests(unittest.TestCase):
    def test_build_contains_only_declared_production_files(self):
        project = context.PROJECT
        manifest = project.load_manifest()
        expected = {item['target'] for item in manifest['files']}
        actual = {str(path.relative_to(context.BUILD)).replace('\\', '/') for path in context.BUILD.rglob('*') if path.is_file()}
        self.assertEqual(actual, expected | {'建置資訊.json'})
        self.assertFalse(any('測試' in name or name.endswith(('.log', '.jsonl', '.dat')) for name in expected))
        for item in manifest['files']:
            source = project.contained(context.FORMAL, item['source'])
            output = project.contained(context.BUILD, item['target'])
            self.assertEqual(source.read_bytes(), output.read_bytes())

    def test_guard_certificate_hashes_include_all_existing_protections(self):
        import TEST_啟動核對 as startup
        hashes = startup.policy_hashes()
        self.assertEqual(len(hashes), 16)
        for name, value in hashes.items():
            self.assertEqual(hashlib.sha256((context.BUILD / name).read_bytes()).hexdigest(), value)

    def test_relative_manifest_paths_cannot_escape(self):
        for value in ('../CORE/TEST_設定.json', 'C:/test/CORE/TEST_設定.json', '/Windows/test', '..\\test', ''):
            with self.subTest(path=value), self.assertRaises(ValueError):
                context.PROJECT.contained(context.FORMAL, value)

    def test_build_refuses_live_source_or_existing_output(self):
        for path in (Path(r'C:\test\CORE'), Path(r'C:\test\CORE\整理輸出'),
                     context.FORMAL / 'dist', context.FORMAL.parent, context.BUILD):
            with self.subTest(path=path), self.assertRaises(ValueError):
                context.PROJECT.build(path)

    def test_test_runner_blocks_live_writes_and_process_creation(self):
        import os
        cases = [('open', (r'C:\test\CORE\TEST_停止.flag', 'w', os.O_WRONLY)),
                 ('os.rename', (r'C:\test\CORE\TEST_設定.json', '其他位置', -1, -1)),
                 ('subprocess.Popen', ('powershell.exe', ['任意命令'], None, None)),
                 ('socket.connect', (None, ('127.0.0.1', 18776))),
                 ('socket.bind', (None, ('127.0.0.1', 18776)))]
        for event, args in cases:
            with self.subTest(event=event), self.assertRaises(PermissionError):
                audit(event, args)

    def test_captured_config_is_used_without_changing_limits(self):
        config = json.loads((context.BUILD / 'TEST_設定.json').read_text(encoding='utf-8'))
        self.assertEqual(config['功耗排程']['核心降瓦溫度C'], 80)
        self.assertEqual(config['功耗排程']['核心停止溫度C'], 82)
        self.assertEqual(config['功耗排程']['主機板風扇百分比'], 50)
        self.assertEqual(config['功耗排程']['尖峰風扇百分比'], 80)
        self.assertEqual(config['功耗排程']['離峰風扇百分比'], 80)


if __name__ == '__main__':
    unittest.main()
