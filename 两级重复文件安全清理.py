"""保守的两级增量重复清理 (V2 数据库断点续扫版)。默认预览；只隔离，不删除。Python 3.10+。

支持 SQLite 索引缓存哈希、断点续扫、完整文件夹分批安全隔离与实时容量统计。
极度保守：选项 A（优先完整文件夹隔离，绝不拆散工程，零散文件最后处理）。

python 两级重复文件安全清理.py 路径 --output 报告目录
双击默认预览脚本所在文件夹，结束后按回车退出。
显式添加 --apply 才隔离；双击模式也可将 MOVE_DUPLICATES 改为 True。
"""
import argparse
import csv
import hashlib
import json
import os
import sqlite3
import stat
import sys
import threading
import time
import uuid
from collections import defaultdict
from pathlib import Path

RESERVED = '_重复文件隔离区'
REPORTS = '_重复文件报告'
MOVE_DUPLICATES = False  # 默认先预览；确认后改为 True 才隔离。
EXTENSIONS = None  # 全部类型；例如 ('.jpg', '.png')，大小写不敏感。
CHUNK = 1024 * 1024
PROGRESS = None


def readable_size(n):
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if n < 1024 or unit == 'TiB':
            return f'{n:.2f} {unit}'
        n /= 1024


class Progress:
    """独立心跳只输出状态，不访问磁盘；磁盘读取阻塞时也能显示等待。"""
    def __init__(self):
        self.started = self.changed = time.monotonic()
        self.stage = '准备扫描'
        self.path = ''
        self.files = self.dirs = self.bytes = self.offset = self.size = 0
        self.scanned = self.scanned_bytes = 0
        self.cached_hashes = 0
        self.stop = threading.Event()
        self.lock = threading.Lock()

    def phase(self, stage):
        with self.lock:
            self.stage = stage
            self.path = ''
            self.offset = self.size = 0
        self.render()

    def current(self, path, size=0):
        with self.lock:
            self.path = str(path)
            self.offset, self.size = 0, size
            self.changed = time.monotonic()

    def read(self, amount):
        with self.lock:
            self.offset += amount
            self.bytes += amount
            self.changed = time.monotonic()

    def render(self):
        with self.lock:
            elapsed = time.monotonic() - self.started
            idle = time.monotonic() - self.changed
            message = (f'[{self.stage}] 已扫描 {self.scanned:,} 个文件 / {readable_size(self.scanned_bytes)} | '
                       f'目录 {self.dirs} | 哈希命中缓存 {self.cached_hashes:,} | '
                       f'已校验文件 {self.files} | 累计读取 {readable_size(self.bytes)} | 用时 {elapsed:.0f} 秒')
            if self.path:
                message += f'\n  当前：{self.path}'
                if self.size:
                    message += f'（{readable_size(self.offset)} / {readable_size(self.size)}）'
            if idle >= 5:
                message += f'\n  本步骤 {idle:.0f} 秒未更新，可能正在等待磁盘或处理目录；可按 Ctrl+C 停止。'
        print(message, flush=True)

    def heartbeat(self):
        while not self.stop.wait(1):
            self.render()

    def __enter__(self):
        global PROGRESS
        PROGRESS = self
        self.thread = threading.Thread(target=self.heartbeat, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        global PROGRESS
        self.stop.set()
        self.thread.join(timeout=2)
        self.render()
        PROGRESS = None


class SafetyError(RuntimeError):
    pass


def linked(st):
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, 'st_file_attributes', 0) & 0x400)


def fingerprint(st):
    # Windows 的 stat/fstat 对 ctime 的语义可能不同，不能互相比较。
    return (st.st_size, st.st_mtime_ns, st.st_dev, st.st_ino,
            st.st_ctime_ns if os.name != 'nt' else None)


class VaultDB:
    """SQLite 索引数据库：实现断点续扫、哈希缓存与事务安全的隔离日志。"""
    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode = WAL;")
        self._init_tables()

    def _init_tables(self):
        with self.conn:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS file_hashes (
                    rel_path TEXT PRIMARY KEY,
                    size INTEGER,
                    mtime_ns INTEGER,
                    sha256 TEXT,
                    updated_at REAL
                );
            """)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS isolation_journal (
                    job_id TEXT PRIMARY KEY,
                    run_id TEXT,
                    kind TEXT,
                    source TEXT,
                    keeper TEXT,
                    bytes INTEGER,
                    status TEXT,
                    reason TEXT,
                    destination TEXT,
                    updated_at REAL
                );
            """)

    def get_cached_hash(self, rel_path, size, mtime_ns):
        cursor = self.conn.cursor()
        cursor.execute("SELECT sha256 FROM file_hashes WHERE rel_path = ? AND size = ? AND mtime_ns = ?",
                       (rel_path, size, mtime_ns))
        row = cursor.fetchone()
        return row[0] if row else None

    def set_cached_hash(self, rel_path, size, mtime_ns, sha256):
        with self.conn:
            self.conn.execute("""
                INSERT OR REPLACE INTO file_hashes (rel_path, size, mtime_ns, sha256, updated_at)
                VALUES (?, ?, ?, ?, ?)
            """, (rel_path, size, mtime_ns, sha256, time.time()))

    def log_job(self, job_id, run_id, kind, source, keeper, bytes_count, status, destination="", reason=""):
        with self.conn:
            self.conn.execute("""
                INSERT OR REPLACE INTO isolation_journal (job_id, run_id, kind, source, keeper, bytes, status, destination, reason, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (str(job_id), run_id, kind, source, keeper, bytes_count, status, destination, reason, time.time()))

    def close(self):
        self.conn.close()


def read_file(path, root=None, db=None):
    if PROGRESS:
        PROGRESS.current(path)
    try:
        before = path.lstat()
    except (OSError, SafetyError) as exc:
        raise SafetyError(f'无法获取元数据或文件丢失：{path}') from exc

    if PROGRESS:
        PROGRESS.current(path, before.st_size)
    if linked(before) or not stat.S_ISREG(before.st_mode):
        raise SafetyError(f'拒绝链接或特殊文件：{path}')

    if db and root:
        try:
            rel_path = path.relative_to(root).as_posix()
            cached_sha = db.get_cached_hash(rel_path, before.st_size, before.st_mtime_ns)
            if cached_sha:
                if PROGRESS:
                    PROGRESS.cached_hashes += 1
                    PROGRESS.files += 1
                return (before.st_size, cached_sha, fingerprint(before))
        except (ValueError, OSError):
            pass

    h = hashlib.sha256()
    try:
        with path.open('rb') as stream:
            if fingerprint(os.fstat(stream.fileno())) != fingerprint(before):
                raise SafetyError(f'打开时文件变化：{path}')
            for block in iter(lambda: stream.read(CHUNK), b''):
                h.update(block)
                if PROGRESS:
                    PROGRESS.read(len(block))
            after = os.fstat(stream.fileno())
    except (OSError, SafetyError) as exc:
        raise SafetyError(f'读取或打开文件失败/文件并发变动：{path}') from exc

    if fingerprint(before) != fingerprint(after) or fingerprint(path.lstat()) != fingerprint(before):
        raise SafetyError(f'读取期间文件变化：{path}')
    if PROGRESS:
        PROGRESS.files += 1

    sha = h.hexdigest()
    if db and root:
        try:
            rel_path = path.relative_to(root).as_posix()
            db.set_cached_hash(rel_path, before.st_size, before.st_mtime_ns, sha)
        except (ValueError, OSError):
            pass

    return (before.st_size, sha, fingerprint(before))


def scan(root, exclude_reserved=False, hash_files=True, db=None):
    """任何不可读项或链接均使扫描失败，不把缺失项当作空目录。
    默认跳过 Windows 系统目录和回收站，避免扫描无用文件。"""
    root = Path(root)
    files, dirs = {}, set()

    def visit(path):
        if PROGRESS:
            PROGRESS.current(path)
        st = path.lstat()
        if linked(st) or not stat.S_ISDIR(st.st_mode):
            raise SafetyError(f'拒绝链接或非目录：{path}')
        rel = path.relative_to(root).as_posix()
        dirs.add(rel)
        if PROGRESS:
            PROGRESS.dirs += 1
        before = sorted(path.iterdir(), key=lambda p: p.name)
        for child in before:
            # 自动跳过保留区、报告目录、Windows 回收站与系统隐藏目录
            if exclude_reserved and path == root and child.name in (RESERVED, REPORTS):
                continue
            if child.name.upper() in ('$RECYCLE.BIN', 'SYSTEM VOLUME INFORMATION', 'RECYCLER'):
                continue
            cs = child.lstat()
            if linked(cs):
                raise SafetyError(f'拒绝链接或重解析点：{child}')
            if stat.S_ISDIR(cs.st_mode):
                visit(child)
            else:
                if not stat.S_ISREG(cs.st_mode):
                    raise SafetyError(f'拒绝特殊文件：{child}')
                rel_child = child.relative_to(root).as_posix()
                files[rel_child] = (read_file(child, root, db) if hash_files
                                   else (cs.st_size, None, fingerprint(cs)))
                if PROGRESS and not hash_files:
                    PROGRESS.scanned += 1
                    PROGRESS.scanned_bytes += cs.st_size
        if [p.name for p in before] != sorted(p.name for p in path.iterdir()):
            raise SafetyError(f'扫描期间目录成员变化：{path}')
    visit(root)
    return {'files': files, 'dirs': dirs}


def content(tree):
    return (tuple(sorted(tree['dirs'])),
            tuple(sorted((p, v[0], v[1]) for p, v in tree['files'].items())))


def subtree(tree, name):
    prefix = name + '/'
    return {'dirs': {'.'} | {d[len(prefix):] for d in tree['dirs'] if d.startswith(prefix)},
            'files': {f[len(prefix):]: v for f, v in tree['files'].items() if f.startswith(prefix)}}


def under(name, parent):
    return name == parent or name.startswith(parent + '/')


def order(name):
    return (name.casefold(), name)


def selected(name):
    return not EXTENSIONS or Path(name).suffix.lower() in {e.lower() for e in EXTENSIONS}


def plan(root, db=None):
    root = Path(root).absolute()
    # 不允许通过符号链接、Windows junction 或其他重解析点进入根目录。
    for p in (root, *root.parents):
        if linked(p.lstat()):
            raise SafetyError(f'根路径含链接：{p}')
    if PROGRESS:
        PROGRESS.phase('1/4 增量扫描文件元数据')
    scan_started = time.monotonic()
    tree = scan(root, True, hash_files=False, db=db)
    size_map = defaultdict(list)
    for name, value in tree['files'].items():
        if selected(name):
            size_map[value[0]].append(name)
    potential = [names for names in size_map.values() if len(names) > 1]
    if PROGRESS:
        print(f'\n文件元数据扫描完成：{len(tree["files"]):,} 个文件，'
              f'{readable_size(sum(v[0] for v in tree["files"].values()))}；'
              f'相同大小候选 {len(potential):,} 组 / {sum(map(len, potential)):,} 个文件', flush=True)
        PROGRESS.phase('2/4 比对哈希（复用数据库索引）')
    for names in potential:
        for name in names:
            old = tree['files'][name]
            try:
                value = read_file(root / name, root, db)
                if value[2] != old[2]:
                    # 文件在元数据扫描和哈希计算之间被修改，安全跳过此文件的哈希计算
                    continue
                tree['files'][name] = value
            except (FileNotFoundError, SafetyError) as exc:
                # 单个候选文件丢失或读取变动，跳过此文件比对，不崩溃退出
                continue
    if PROGRESS:
        PROGRESS.phase('3/4 优先查找完整重复文件夹（选项 A）')
    groups = defaultdict(list)
    for name in sorted(tree['dirs'] - {'.'}, key=lambda s: (s.count('/'), order(s))):
        if PROGRESS:
            PROGRESS.current(root / name)
        sub = subtree(tree, name)
        # 未哈希文件不参与完整目录判重；筛选时不连带移动未选类型。
        if sub['files'] and all(v[1] is not None for v in sub['files'].values()):
            groups[content(sub)].append(name)
    jobs, protected = [], set()
    for group in groups.values():
        if len(group) < 2 or any(under(n, p) for n in group for p in protected):
            continue
        group.sort(key=order)
        keeper = group[0]
        protected.update(group)
        for source in group[1:]:
            jobs.append({'kind': 'folder', 'source': source, 'keeper': keeper,
                         'expected': subtree(tree, source), 'keep_expected': subtree(tree, keeper)})

    if PROGRESS:
        PROGRESS.phase('4/4 检查叶子目录零散文件')
    leaves = tree['dirs'] - {str(Path(d).parent).replace('\\', '/') for d in tree['dirs'] if d != '.'}
    fg, cross = defaultdict(list), defaultdict(list)
    for name, value in tree['files'].items():
        if value[1] is not None:
            cross[value[:2]].append(name)
            parent = Path(name).parent.as_posix()
            if parent in leaves and not any(under(name, p) for p in protected):
                fg[(parent, value[:2])].append(name)
    for group in fg.values():
        group.sort(key=order)
        for source in group[1:]:
            jobs.append({'kind': 'file', 'source': source, 'keeper': group[0],
                         'expected': tree['files'][source], 'keep_expected': tree['files'][group[0]]})
    for job in jobs:
        job['bytes'] = (sum(v[0] for v in job['expected']['files'].values())
                        if job['kind'] == 'folder' else job['expected'][0])
        job['file_count'] = len(job['expected']['files']) if job['kind'] == 'folder' else 1
    return {'root': str(root), 'jobs': jobs, 'protected': sorted(protected),
            'scan_files': len(tree['files']), 'scan_dirs': len(tree['dirs']) - 1,
            'scan_bytes': sum(v[0] for v in tree['files'].values()),
            'cross_directory_groups': [sorted(v) for v in cross.values()
                                       if len({str(Path(n).parent) for n in v}) > 1]}


def append_log(path, record):
    with path.open('a', encoding='utf-8') as f:
        f.write(json.dumps(record, ensure_ascii=False) + '\n')
        f.flush()
        os.fsync(f.fileno())


def write_csv_report(path, rows, root, apply):
    fieldnames = ['处理状态', '类型', '文件大小_字节', '可读大小', '重复项原始路径', '保留副本路径', '目标隔离路径', '跳过或失败原因', '作业编号']
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow({
                '处理状态': '隔离' if r.get('status') == 'done' else ('预览' if r.get('status') == 'preview' else r.get('status', '')),
                '类型': '完整文件夹' if r.get('kind') == 'folder' else '零散文件',
                '文件大小_字节': r.get('bytes', 0),
                '可读大小': readable_size(r.get('bytes', 0)),
                '重复项原始路径': r.get('source', ''),
                '保留副本路径': r.get('keeper', ''),
                '目标隔离路径': r.get('destination', ''),
                '跳过或失败原因': r.get('reason', ''),
                '作业编号': r.get('id', '')
            })


def audit(journal):
    """只读核对中断日志；不自动回算，也不据此继续移动。"""
    pending, done = {}, set()
    for line in Path(journal).read_text(encoding='utf-8').splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            raise SafetyError('日志不完整，请人工核查隔离区')
        if row['status'] == 'pending':
            pending[row['id']] = row
        elif row['status'] == 'done':
            done.add(row['id'])
    result = []
    for key, row in pending.items():
        src, dst = Path(row['source']), Path(row['destination'])
        result.append({**row, 'logged_done': key in done,
                       'source_exists': os.path.lexists(src), 'destination_exists': os.path.lexists(dst)})
    return result


def execute(p, output, apply=False, hook=None, db=None):
    root, output = Path(p['root']), Path(output).absolute()
    if (output == root or root in output.parents) and output != root / REPORTS:
        raise SafetyError(f'报告目录应在扫描目录之外，或使用专用目录 {REPORTS}')
    for ancestor in (output, *output.parents):
        if os.path.lexists(ancestor) and linked(ancestor.lstat()):
            raise SafetyError('报告路径包含链接或重解析点')
    output.mkdir(parents=True, exist_ok=True)
    run = uuid.uuid4().hex
    journal = output / f'清理日志_{run}.jsonl'
    csv_report = output / f'清理报告_{run}.csv'
    json_report = output / f'清理报告_{run}.json'

    rows = []
    base = root / RESERVED
    lock = base / '运行锁'
    locked = False
    try:
        if apply:
            if os.path.lexists(base) and (linked(base.lstat()) or not base.is_dir()):
                raise SafetyError('隔离区不是普通目录')
            base.mkdir(exist_ok=True)
            # 中断后故意保留锁；必须核查日志后人工处理，禁止盲目重跑。
            lock.mkdir()
            locked = True
            append_log(journal, {'status': 'start', 'root': str(root), 'run': run})
            (lock / '日志路径.txt').write_text(str(journal), encoding='utf-8')
            target = base / run
            target.mkdir()
            if target.stat().st_dev != root.stat().st_dev:
                raise SafetyError('隔离区跨文件系统，禁止复制后删除')
        for i, job in enumerate(p['jobs']):
            if PROGRESS:
                PROGRESS.phase(f'{"分批复核与隔离" if apply else "生成预览清单"} {i + 1}/{len(p["jobs"])}')
            source, keeper = root / job['source'], root / job['keeper']
            row = {'id': i, 'kind': job['kind'], 'source': str(source), 'keeper': str(keeper),
                   'bytes': job['bytes'], 'file_count': job['file_count']}
            if not apply:
                rows.append({**row, 'status': 'preview'})
                continue
            if hook:
                hook('before_check', i, job)
            # 重新枚举目录：新增文件、空目录、删除、部分复制均可检测。
            try:
                for path in (source, keeper, *source.parents, *keeper.parents):
                    if linked(path.lstat()):
                        raise SafetyError(f'路径变为链接：{path}')
                if job['kind'] == 'folder':
                    unchanged = (scan(source) == job['expected'] and scan(keeper) == job['keep_expected'])
                else:
                    leaf = all(not linked(x.lstat()) and not x.is_dir() for x in source.parent.iterdir()
                               if not (source.parent == root and x.name in (RESERVED, REPORTS)))
                    unchanged = (leaf and read_file(source, root, db) == job['expected'] 
                                 and read_file(keeper, root, db) == job['keep_expected'])
            except (OSError, SafetyError) as exc:
                unchanged = False
                row['reason'] = str(exc)
            if not unchanged:
                row.update(status='skipped', reason=row.get('reason', '源或保留副本发生变化'))
                append_log(journal, row)
                if db:
                    db.log_job(i, run, job['kind'], str(source), str(keeper), job['bytes'], 'skipped', reason=row['reason'])
                rows.append(row)
                continue
            # 每项独享新建目录，不复用同名目标；os.rename 不做跨盘复制回退。
            slot = target / str(i)
            slot.mkdir()
            dest = slot / source.name
            row.update(destination=str(dest), status='pending')
            append_log(journal, row)
            if db:
                db.log_job(i, run, job['kind'], str(source), str(keeper), job['bytes'], 'pending', destination=str(dest))
            if hook:
                hook('before_rename', i, job)
            os.rename(source, dest)
            if hook:
                hook('after_rename', i, job)
            row = {**row, 'status': 'done'}
            append_log(journal, row)
            if db:
                db.log_job(i, run, job['kind'], str(source), str(keeper), job['bytes'], 'done', destination=str(dest))
            rows.append(row)
        if locked:
            (lock / '日志路径.txt').unlink()
            lock.rmdir()
            locked = False
    except BaseException as exc:
        append_log(journal, {'status': 'stopped', 'error': f'{type(exc).__name__}: {exc}'})
        raise
    finally:
        write_csv_report(csv_report, rows, root, apply)
        json_report.write_text(json.dumps({'root': str(root), 'apply': apply, 'rows': rows,
                          'statistics': statistics(p, rows),
                          'protected': p['protected'], 'cross_directory_groups': p['cross_directory_groups'],
                          'journal': str(journal) if apply else None,
                          'csv_report': str(csv_report),
                          'manual_audit_required': locked}, ensure_ascii=False, indent=2), encoding='utf-8')
    return json_report


def statistics(p, rows):
    return {'scan_files': p['scan_files'], 'scan_dirs': p['scan_dirs'], 'scan_bytes': p['scan_bytes'],
            'folder_jobs': sum(j['kind'] == 'folder' for j in p['jobs']),
            'folder_bytes': sum(j['bytes'] for j in p['jobs'] if j['kind'] == 'folder'),
            'file_jobs': sum(j['kind'] == 'file' for j in p['jobs']),
            'file_bytes': sum(j['bytes'] for j in p['jobs'] if j['kind'] == 'file'),
            'planned_bytes': sum(j['bytes'] for j in p['jobs']),
            'moved_items': sum(r['status'] == 'done' for r in rows),
            'moved_files': sum(r['file_count'] for r in rows if r['status'] == 'done'),
            'moved_bytes': sum(r['bytes'] for r in rows if r['status'] == 'done'),
            'skipped_items': sum(r['status'] == 'skipped' for r in rows)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', nargs='?', type=Path)
    parser.add_argument('--output', type=Path, help='默认写入扫描目录中的 _重复文件报告')
    parser.add_argument('--apply', action='store_true', help='实际隔离（不删除）')
    parser.add_argument('--audit', type=Path, help='只读检查已有日志')
    args = parser.parse_args()
    if args.audit:
        print(json.dumps(audit(args.audit), ensure_ascii=False, indent=2))
        return
    root = args.root if args.root is not None else Path(__file__).resolve().parent
    output = args.output if args.output is not None else root / REPORTS
    apply = args.apply or (len(sys.argv) == 1 and MOVE_DUPLICATES)
    
    db_path = output / 'cleaner_vault.db'
    db = VaultDB(db_path)

    try:
        print('扫描位置：', root.absolute(), flush=True)
        print('数据库索引：', db_path.absolute(), flush=True)
        print('模式：', '实际分批隔离（选项 A：完整工程优先，不删除）' if apply else '安全增量预览（不移动、不删除）', flush=True)
        print('每秒显示进展；总量未知时不显示虚假百分比。按 Ctrl+C 可停止。', flush=True)
        started = time.monotonic()
        with Progress():
            p = plan(root, db=db)
            report = execute(p, output, apply, db=db)
        stats = json.loads(report.read_text(encoding='utf-8'))['statistics']
        print('\n========== 最终统计 ==========')
        print(f'增量扫描：{stats["scan_dirs"]} 个文件夹，{stats["scan_files"]} 个文件，{readable_size(stats["scan_bytes"])}')
        print(f'完整重复文件夹：{stats["folder_jobs"]} 个，{readable_size(stats["folder_bytes"])}')
        print(f'零散重复文件：{stats["file_jobs"]} 个，{readable_size(stats["file_bytes"])}')
        print('计划隔离总容量：', readable_size(stats['planned_bytes']))
        print(f'实际隔离：{stats["moved_items"]} 项（含 {stats["moved_files"]} 个文件），{readable_size(stats["moved_bytes"])}')
        print('跳过项目：', stats['skipped_items'])
        print('跨目录重复内容组（仅报告）：', len(p['cross_directory_groups']))
        print(f'总耗时：{time.monotonic() - started:.1f} 秒')
        print('隔离不会释放磁盘空间。' if apply else '本次仅预览，没有移动或删除文件。')
        print('JSON 明细报告：', report)
        print('CSV 表格报告：', report.with_suffix('.csv'))
    except (OSError, SafetyError, KeyboardInterrupt) as exc:
        parser.exit(1, f'已停止：{exc}\n如有运行锁，请核查日志及隔离区后人工处理。\n')
    finally:
        db.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'程序未完成：{type(exc).__name__}: {exc}', file=sys.stderr)
        raise SystemExit(1)
    finally:
        if len(sys.argv) == 1:
            try:
                input('\n按 Enter 退出……')
            except (EOFError, KeyboardInterrupt):
                pass
