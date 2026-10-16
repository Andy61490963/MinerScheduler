"""正式專案的唯一建置入口；不啟停礦工、不部署、不呼叫硬體介面。"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent


def contained(root, name):
    """清單只允許專案內的相對路徑，拒絕穿越、磁碟代號與連結逃逸。"""
    raw = str(name)
    relative = PurePosixPath(raw)
    if not raw or '\\' in raw or ':' in raw or relative.is_absolute() or '..' in relative.parts:
        raise ValueError(f'清單路徑不合法：{raw}')
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or path == root.resolve():
        raise ValueError(f'清單路徑超出專案：{raw}')
    return path


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_manifest(root=None):
    root = (root or ROOT).resolve()
    manifest = json.loads((root / 'project.json').read_text(encoding='utf-8'))
    if manifest['schema'] != 1 or not manifest['files']:
        raise ValueError('專案清單格式不符。')
    targets, sources = set(), set()
    for item in manifest['files']:
        source = contained(root, item['source'])
        target = contained(root, item['target'])
        source_key, target_key = str(source).casefold(), str(target).casefold()
        if source_key in sources or target_key in targets:
            raise ValueError('專案清單有重複的來源或輸出路徑。')
        sources.add(source_key)
        targets.add(target_key)
        if not source.is_file():
            raise ValueError(f'缺少正式檔案：{item["source"]}')
    for item in manifest.get('dependencies', []):
        target = contained(root, item['target'])
        target_key = str(target).casefold()
        if target_key in targets:
            raise ValueError('第三方依賴與正式檔案的輸出路徑重複。')
        targets.add(target_key)
        contained(Path(manifest['runtime_root']), item['target'])
    return manifest


def check(root=None):
    root = (root or ROOT).resolve()
    manifest = load_manifest(root)
    for item in manifest['files']:
        path = contained(root, item['source'])
        if item['vendor'] and digest(path) != item['origin_sha256']:
            raise ValueError(f'第三方必要依賴內容已變更：{item["source"]}')
        if path.suffix == '.py':
            compile(path.read_bytes(), str(path), 'exec')
        elif path.suffix == '.json':
            json.loads(path.read_text(encoding='utf-8-sig'))
    live = Path(manifest['runtime_root'])
    for item in manifest.get('dependencies', []):
        if digest(contained(live, item['target'])) != item['sha256']:
            raise ValueError(f'第三方必要依賴與留存雜湊不符：{item["target"]}')
    return {'結果': '離線檢查通過', '正式檔案數': len(manifest['files']), '運行版本': '未修改'}


def build(output, root=None, include_vendor=False):
    """組裝相容的執行版本至全新目錄；不複製狀態、紀錄、測試與備份。"""
    root = (root or ROOT).resolve()
    manifest = load_manifest(root)
    output = Path(output).resolve()
    live = Path(manifest['runtime_root']).resolve()
    # 同時拒絕正式執行區、專案來源區，以及它們的上層目錄。
    projects = root.parent
    for protected in (live, projects):
        if output.is_relative_to(protected) or protected.is_relative_to(output):
            raise ValueError('輸出不可位於運行目錄、專案目錄或其上層；請使用獨立建置目錄。')
    if output.exists():
        raise ValueError('輸出目錄已存在；不覆蓋任何既有版本。')
    check(root)
    output.mkdir(parents=True)
    built = []
    for item in manifest['files']:
        if item['vendor'] and not include_vendor:
            continue
        source, target = contained(root, item['source']), contained(output, item['target'])
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        actual = digest(target)
        if actual != digest(source):
            raise ValueError(f'建置輸出核對失敗：{item["target"]}')
        built.append({'path': item['target'], 'sha256': actual})
    if include_vendor:
        for item in manifest.get('dependencies', []):
            source, target = contained(live, item['target']), contained(output, item['target'])
            if digest(source) != item['sha256']:
                raise ValueError(f'第三方依賴與留存雜湊不符：{item["target"]}')
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            if digest(target) != item['sha256']:
                raise ValueError(f'第三方依賴複製失敗：{item["target"]}')
            built.append({'path': item['target'], 'sha256': item['sha256']})
    report = {'專案': manifest['name'], '建置UTC': datetime.now(timezone.utc).isoformat(),
              '正式運行目錄': str(live), '部署狀態': '未部署',
              '用途': '離線測試' if not include_vendor else '待人工安排的相容版本建置',
              'files': built}
    (output / '建置資訊.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description='正式挖礦專案：檢查與建置；不影響運行中的挖礦。')
    commands = parser.add_subparsers(dest='command')
    commands.add_parser('check', help='離線檢查程式語法、設定與第三方依賴完整性')
    package = commands.add_parser('build', help='只輸出至全新目錄，不部署或啟動')
    package.add_argument('--output', required=True, help='獨立且尚未存在的建置目錄')
    package.add_argument('--with-dependencies', action='store_true', help='另複製既有且雜湊吻合的第三方依賴；預設只建置原始碼與設定')
    args = parser.parse_args()
    if args.command == 'check':
        result = check()
    elif args.command == 'build':
        report = build(args.output, include_vendor=args.with_dependencies)
        result = {'結果': '建置完成', '輸出': str(Path(args.output).resolve()), '檔案數': len(report['files']),
                  '部署狀態': report['部署狀態'], '運行版本': '未修改'}
    else:
        result = {'正式原始碼': str(ROOT), '離線檢查': 'python project.py check',
                  '建立版本': 'python project.py build --output <全新目錄>',
                  '目前監控': 'http://127.0.0.1:18776/', '說明': '操作挖礦仍使用既有監控頁；這裡只管理原始碼。'}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
