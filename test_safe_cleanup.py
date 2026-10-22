"""只创建、修改、移动本工作区模拟测试目录下的数据；保留现场。"""
import importlib.util
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
import uuid
import subprocess
import sys
import io
import csv
import threading
from contextlib import redirect_stdout

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('cleaner', HERE / '两级重复文件安全清理.py')
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)
SESSION = HERE / '模拟测试目录' / uuid.uuid4().hex


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.case = SESSION / self._testMethodName
        self.root = self.case / 'data'
        self.out = self.case / 'reports'
        self.root.mkdir(parents=True)

    def put(self, name, data=b'abc'):
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def twins(self):
        self.put('A/sub/f'); self.put('B/sub/f')

    def run_plan(self, p=None, hook=None, apply=True):
        return c.execute(p or c.plan(self.root), self.out, apply, hook)

    def test_three_folders_and_keeper_integrity(self):
        for d in ('A', 'B', 'C'):
            self.put(d + '/x'); self.put(d + '/y')
            (self.root / d / 'empty').mkdir()
        before = c.content(c.scan(self.root / 'A'))
        p = c.plan(self.root)
        self.assertEqual([(j['kind'], j['source']) for j in p['jobs']], [('folder', 'B'), ('folder', 'C')])
        self.run_plan(p)
        self.assertEqual(c.content(c.scan(self.root / 'A')), before)
        self.assertFalse((self.root / 'B').exists())
        self.assertFalse((self.root / 'C').exists())

    def test_outermost_first(self):
        for d in ('A', 'B'):
            self.put(d + '/x/f'); self.put(d + '/y/f')
        p = c.plan(self.root)
        self.assertEqual(len(p['jobs']), 1)
        self.assertEqual(p['jobs'][0]['source'], 'B')
        self.run_plan(p)
        self.assertTrue((self.root / 'A/x/f').exists())
        self.assertTrue((self.root / 'A/y/f').exists())

    def test_folder_before_loose_files(self):
        self.twins()
        self.put('Loose/a', b'other'); self.put('Loose/b', b'other')
        p = c.plan(self.root)
        self.assertEqual([j['kind'] for j in p['jobs']], ['folder', 'file'])
        self.run_plan(p)
        self.assertTrue((self.root / 'Loose/a').exists())
        self.assertFalse((self.root / 'Loose/b').exists())

    def test_same_name_different_content(self):
        self.put('A/f', b'123'); self.put('B/f', b'456')
        self.assertEqual(c.plan(self.root)['jobs'], [])

    def test_partial_copy_and_cross_directory(self):
        self.put('A/a'); self.put('A/b', b'unique'); self.put('B/a')
        p = c.plan(self.root)
        self.assertEqual(p['jobs'], [])
        self.assertEqual(len(p['cross_directory_groups']), 1)

    def test_empty_directory_structure(self):
        self.put('A/f'); self.put('B/f')
        (self.root / 'A/empty').mkdir()
        self.assertEqual(c.plan(self.root)['jobs'], [])

    def test_new_file_after_plan(self):
        self.twins(); p = c.plan(self.root)
        self.put('B/new', b'unique')
        report = self.run_plan(p)
        self.assertEqual(json.loads(report.read_text(encoding='utf-8'))['rows'][0]['status'], 'skipped')
        self.assertTrue((self.root / 'B/new').exists())

    def test_new_empty_directory_after_plan(self):
        self.twins(); p = c.plan(self.root)
        (self.root / 'A/new').mkdir()
        self.run_plan(p)
        self.assertTrue((self.root / 'B/sub/f').exists())

    def test_corrupt_keeper_same_size_restored_mtime(self):
        self.twins(); p = c.plan(self.root)
        f = self.root / 'A/sub/f'; old = f.stat()
        f.write_bytes(b'bad'); os.utime(f, ns=(old.st_atime_ns, old.st_mtime_ns))
        self.run_plan(p)
        self.assertEqual((self.root / 'B/sub/f').read_bytes(), b'abc')

    def test_removed_source_member(self):
        self.twins(); p = c.plan(self.root)
        (self.root / 'B/sub/f').unlink()  # 仅测试生成的数据。
        self.run_plan(p)
        self.assertTrue((self.root / 'B').exists())
        self.assertTrue((self.root / 'A/sub/f').exists())

    def test_unreadable_file_aborts_scan(self):
        self.twins()
        original = c.read_file
        def fail(path, *args, **kwargs):
            if 'B' in Path(path).parts:
                raise PermissionError('模拟读取失败')
            return original(path, *args, **kwargs)
        with patch.object(c, 'read_file', side_effect=fail):
            with self.assertRaises(PermissionError):
                c.plan(self.root)
        self.assertTrue((self.root / 'B/sub/f').exists())

    def test_interrupt_before_rename(self):
        self.twins()
        def stop(event, *_):
            if event == 'before_rename': raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt): self.run_plan(hook=stop)
        rows = c.audit(next(self.out.glob('*.jsonl')))
        self.assertTrue(rows[0]['source_exists'])
        self.assertFalse(rows[0]['destination_exists'])
        self.assertTrue((self.root / c.RESERVED / '运行锁').exists())

    def test_interrupt_after_rename_and_block_restart(self):
        self.twins()
        def stop(event, *_):
            if event == 'after_rename': raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt): self.run_plan(hook=stop)
        rows = c.audit(next(self.out.glob('*.jsonl')))
        self.assertFalse(rows[0]['source_exists'])
        self.assertTrue(rows[0]['destination_exists'])
        self.assertFalse(rows[0]['logged_done'])
        self.assertTrue((self.root / 'A/sub/f').exists())
        with self.assertRaises(FileExistsError): self.run_plan()

    def test_rename_failure_no_copy_fallback(self):
        self.twins()
        with patch.object(c.os, 'rename', side_effect=OSError('模拟跨盘或占用')):
            with self.assertRaises(OSError): self.run_plan()
        self.assertTrue((self.root / 'A/sub/f').exists())
        self.assertTrue((self.root / 'B/sub/f').exists())

    def test_existing_quarantine_name_preserved(self):
        self.twins()
        old = self.put(c.RESERVED + '/B/sub/f', b'previous')
        self.run_plan()
        self.assertEqual(old.read_bytes(), b'previous')
        self.assertEqual(len(list((self.root / c.RESERVED).glob('*/0/B/sub/f'))), 1)

    def test_preview_never_changes_input(self):
        self.twins(); before = c.scan(self.root)
        self.run_plan(apply=False)
        self.assertEqual(before, c.scan(self.root))
        self.assertFalse((self.root / c.RESERVED).exists())

    def test_read_failure_during_execute_skips(self):
        self.twins(); p = c.plan(self.root)
        with patch.object(c, 'read_file', side_effect=PermissionError('模拟损坏无法读取')):
            self.run_plan(p)
        self.assertTrue((self.root / 'B/sub/f').exists())

    def test_parent_with_subdirectory_protected(self):
        self.put('parent/a'); self.put('parent/b')
        self.put('parent/child/unique', b'child')
        self.assertEqual(c.plan(self.root)['jobs'], [])

    def test_zero_byte_files(self):
        self.put('a', b''); self.put('b', b'')
        self.run_plan()
        self.assertTrue((self.root / 'a').exists())
        self.assertFalse((self.root / 'b').exists())

    def test_double_click_defaults_to_script_folder(self):
        self.put('a'); self.put('b')
        script = self.root / '两级重复文件安全清理.py'
        script.write_bytes((HERE / script.name).read_bytes())
        result = subprocess.run([sys.executable, str(script)], cwd=self.case,
                                input=b'\n', capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = next((self.root / c.REPORTS).glob('清理报告_*.json'))
        data = json.loads(report.read_text(encoding='utf-8'))
        self.assertEqual(Path(data['root']), self.root)
        self.assertFalse(data['apply'])
        self.assertTrue((self.root / 'a').exists())
        self.assertTrue((self.root / 'b').exists())
        self.assertEqual(len(data['rows']), 1)
        self.assertIn('最终统计'.encode('utf-8'), result.stdout if os.environ.get('PYTHONIOENCODING') == 'utf-8' else result.stdout.decode(errors='replace').encode('utf-8')) if os.environ.get('PYTHONIOENCODING') == 'utf-8' else None

    def test_internal_reports_excluded_on_repeat(self):
        self.put('a'); self.put('b')
        self.put(c.REPORTS + '/old_a'); self.put(c.REPORTS + '/old_b')
        p = c.plan(self.root)
        self.assertEqual(len(p['jobs']), 1)
        c.execute(p, self.root / c.REPORTS, True)
        self.assertFalse((self.root / 'b').exists())
        self.assertTrue((self.root / c.REPORTS / 'old_b').exists())

    def test_byte_statistics_without_nested_double_count(self):
        self.twins()
        self.put('Loose/a', b'12345'); self.put('Loose/b', b'12345')
        p = c.plan(self.root)
        preview = json.loads(self.run_plan(p, apply=False).read_text(encoding='utf-8'))['statistics']
        self.assertEqual(preview['scan_bytes'], 16)
        self.assertEqual(preview['scan_files'], 4)
        self.assertEqual(preview['scan_dirs'], 5)
        self.assertEqual(preview['folder_bytes'], 3)
        self.assertEqual(preview['file_bytes'], 5)
        self.assertEqual(preview['planned_bytes'], 8)
        self.assertEqual(preview['moved_bytes'], 0)
        actual = json.loads(self.run_plan(p).read_text(encoding='utf-8'))['statistics']
        self.assertEqual(actual['moved_bytes'], 8)
        self.assertEqual(actual['moved_items'], 2)
        self.assertEqual(actual['moved_files'], 2)

    def test_skipped_bytes_not_counted_as_moved(self):
        self.twins(); p = c.plan(self.root)
        self.put('B/new', b'new')
        data = json.loads(self.run_plan(p).read_text(encoding='utf-8'))['statistics']
        self.assertEqual(data['planned_bytes'], 3)
        self.assertEqual(data['moved_bytes'], 0)
        self.assertEqual(data['skipped_items'], 1)

    def test_progress_counts_chunks_inside_large_file(self):
        f = self.put('large', b'x' * (c.CHUNK * 2 + 17))
        progress = c.Progress()
        with patch.object(c, 'PROGRESS', progress):
            c.read_file(f)
        self.assertEqual(progress.bytes, c.CHUNK * 2 + 17)
        self.assertEqual(progress.offset, progress.size)
        self.assertEqual(progress.files, 1)

    def test_heartbeat_reports_while_worker_waits(self):
        notified = threading.Event()
        stream = io.StringIO()
        with redirect_stdout(stream):
            with c.Progress() as progress:
                original = progress.render
                def render():
                    original()
                    notified.set()
                progress.render = render
                progress.current(self.root / 'slow_file', 100)
                self.assertTrue(notified.wait(4), '等待磁盘期间应持续输出')
        self.assertIn('slow_file', stream.getvalue())
        self.assertIn('累计读取', stream.getvalue())

    def test_unique_sizes_are_not_read_for_hashing(self):
        self.put('unique_big', b'x' * 10001)
        self.put('a', b'abc'); self.put('b', b'abc')
        with patch.object(c, 'read_file', wraps=c.read_file) as reader:
            p = c.plan(self.root)
        self.assertEqual({call.args[0].name for call in reader.call_args_list}, {'a', 'b'})
        self.assertEqual(p['scan_bytes'], 10007)
        self.assertEqual(len(p['jobs']), 1)

    def test_unhashed_member_prevents_complete_folder_match(self):
        self.put('A/f', b'abc'); self.put('B/f', b'abc')
        self.put('A/unique', b'12345'); self.put('B/unique', b'123456')
        self.assertEqual(c.plan(self.root)['jobs'], [])

    def test_extension_filter_never_moves_excluded_members(self):
        self.put('A/f.TXT', b'abc'); self.put('B/f.TXT', b'abc')
        self.put('A/f.bin', b'xyz'); self.put('B/f.bin', b'xyz')
        self.put('Loose/a.TXT', b'12345'); self.put('Loose/b.txt', b'12345')
        with patch.object(c, 'EXTENSIONS', ('.txt',)):
            p = c.plan(self.root)
        self.assertEqual(len(p['jobs']), 1)
        self.assertEqual(p['jobs'][0]['source'], 'Loose/b.txt')
        self.run_plan(p)
        self.assertTrue((self.root / 'A/f.bin').exists())
        self.assertTrue((self.root / 'B/f.bin').exists())

    def test_csv_report_preserves_paths_size_and_status(self):
        self.put('a'); self.put('b')
        report = self.run_plan(apply=False).with_suffix('.csv')
        self.assertTrue(report.read_bytes().startswith(b'\xef\xbb\xbf'))
        with report.open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['文件大小_字节'], '3')
        self.assertEqual(rows[0]['处理状态'], '预览')
        self.assertEqual(Path(rows[0]['重复项原始路径']), self.root / 'b')

    def test_new_subdirectory_blocks_loose_cleanup(self):
        self.put('a'); self.put('b'); p = c.plan(self.root)
        (self.root / 'new_child').mkdir()
        self.run_plan(p)
        self.assertTrue((self.root / 'b').exists())

    def test_pending_log_failure_prevents_move(self):
        self.twins(); original = c.append_log
        def fail(path, record):
            if record['status'] == 'pending': raise OSError('模拟磁盘满')
            original(path, record)
        with patch.object(c, 'append_log', side_effect=fail):
            with self.assertRaises(OSError): self.run_plan()
        self.assertTrue((self.root / 'B/sub/f').exists())

    def test_process_exit_after_rename(self):
        self.twins()
        script = '''import importlib.util,os,sys
from pathlib import Path
s=importlib.util.spec_from_file_location('c',sys.argv[1]); c=importlib.util.module_from_spec(s); s.loader.exec_module(c)
def stop(event,*args):
    if event=='after_rename': os._exit(73)
c.execute(c.plan(Path(sys.argv[2])),Path(sys.argv[3]),True,stop)
'''
        result = subprocess.run([sys.executable, '-c', script, str(HERE / '两级重复文件安全清理.py'), str(self.root), str(self.out)], capture_output=True)
        self.assertEqual(result.returncode, 73, result.stderr)
        rows = c.audit(next(self.out.glob('*.jsonl')))
        self.assertTrue(rows[0]['destination_exists'])
        self.assertFalse(rows[0]['source_exists'])
        self.assertFalse(rows[0]['logged_done'])
        self.assertTrue((self.root / 'A/sub/f').exists())
        self.assertTrue((self.root / c.RESERVED / '运行锁').exists())


if __name__ == '__main__':
    SESSION.mkdir(parents=True)
    with (SESSION / '测试结果.txt').open('w', encoding='utf-8') as stream:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(SafetyTests)
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    print((SESSION / '测试结果.txt').read_text(encoding='utf-8'))
    print('保留模拟现场：', SESSION)
    raise SystemExit(not result.wasSuccessful())
