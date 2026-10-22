"""保守的两级重复清理。默认预览；只隔离，不删除。Python 3.10+。

python 两级重复文件安全清理.py 路径 --output 报告目录
显式添加 --apply 才隔离。运行时应停止其他程序修改扫描目录。
同级完整目录优先；保护其全部目录树；其余叶子目录仅清理内部重复。
"""
import argparse
import hashlib
import json
import os
import stat
import uuid
from collections import defaultdict
from pathlib import Path

RESERVED = '_重复文件隔离区'
CHUNK = 1024 * 1024


class SafetyError(RuntimeError):
    pass


def linked(st):
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, 'st_file_attributes', 0) & 0x400)


def fingerprint(st):
    # Windows 的 stat/fstat 对 ctime 的语义可能不同，不能互相比较。
    return (st.st_size, st.st_mtime_ns, st.st_dev, st.st_ino,
            st.st_ctime_ns if os.name != 'nt' else None)


def read_file(path):
    before = path.lstat()
    if linked(before) or not stat.S_ISREG(before.st_mode):
        raise SafetyError(f'拒绝链接或特殊文件：{path}')
    h = hashlib.sha256()
    with path.open('rb') as stream:
        if fingerprint(os.fstat(stream.fileno())) != fingerprint(before):
            raise SafetyError(f'打开时文件变化：{path}')
        for block in iter(lambda: stream.read(CHUNK), b''):
            h.update(block)
        after = os.fstat(stream.fileno())
    if fingerprint(before) != fingerprint(after) or fingerprint(path.lstat()) != fingerprint(before):
        raise SafetyError(f'读取期间文件变化：{path}')
    return (before.st_size, h.hexdigest(), fingerprint(before))


def scan(root, exclude_reserved=False):
    """任何不可读项或链接均使扫描失败，不把缺失项当作空目录。"""
    root = Path(root)
    files, dirs = {}, set()

    def visit(path):
        st = path.lstat()
        if linked(st) or not stat.S_ISDIR(st.st_mode):
            raise SafetyError(f'拒绝链接或非目录：{path}')
        rel = path.relative_to(root).as_posix()
        dirs.add(rel)
        before = sorted(path.iterdir(), key=lambda p: p.name)
        for child in before:
            if exclude_reserved and path == root and child.name == RESERVED:
                continue
            cs = child.lstat()
            if linked(cs):
                raise SafetyError(f'拒绝链接或重解析点：{child}')
            if stat.S_ISDIR(cs.st_mode):
                visit(child)
            else:
                files[child.relative_to(root).as_posix()] = read_file(child)
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


def plan(root):
    root = Path(root).absolute()
    # 不允许通过符号链接、Windows junction 或其他重解析点进入根目录。
    for p in (root, *root.parents):
        if linked(p.lstat()):
            raise SafetyError(f'根路径含链接：{p}')
    tree = scan(root, True)
    groups = defaultdict(list)
    for name in sorted(tree['dirs'] - {'.'}, key=lambda s: (s.count('/'), order(s))):
        sub = subtree(tree, name)
        if sub['files']:  # 纯空目录不清理，但空目录结构参与完整目录比较。
            groups[(str(Path(name).parent), content(sub))].append(name)
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
    leaves = tree['dirs'] - {str(Path(d).parent).replace('\\', '/') for d in tree['dirs'] if d != '.'}
    fg, cross = defaultdict(list), defaultdict(list)
    for name, value in tree['files'].items():
        cross[value[:2]].append(name)
        parent = Path(name).parent.as_posix()
        if parent in leaves and not any(under(name, p) for p in protected):
            fg[(parent, value[:2])].append(name)
    for group in fg.values():
        group.sort(key=order)
        for source in group[1:]:
            jobs.append({'kind': 'file', 'source': source, 'keeper': group[0],
                         'expected': tree['files'][source], 'keep_expected': tree['files'][group[0]]})
    return {'root': str(root), 'jobs': jobs, 'protected': sorted(protected),
            'cross_directory_groups': [sorted(v) for v in cross.values()
                                       if len({str(Path(n).parent) for n in v}) > 1]}


def append_log(path, record):
    with path.open('a', encoding='utf-8') as f:
        f.write(json.dumps(record, ensure_ascii=False) + '\n')
        f.flush()
        os.fsync(f.fileno())


def audit(journal):
    """只读核对中断日志；不自动回滚，也不据此继续移动。"""
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


def execute(p, output, apply=False, hook=None):
    root, output = Path(p['root']), Path(output).absolute()
    if output == root or root in output.parents:
        raise SafetyError('报告目录必须在扫描根目录之外')
    output.mkdir(parents=True, exist_ok=True)
    run = uuid.uuid4().hex
    journal = output / f'清理日志_{run}.jsonl'
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
            source, keeper = root / job['source'], root / job['keeper']
            row = {'id': i, 'kind': job['kind'], 'source': str(source), 'keeper': str(keeper)}
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
                               if not (source.parent == root and x.name == RESERVED))
                    unchanged = (leaf and read_file(source) == job['expected'] and read_file(keeper) == job['keep_expected'])
            except (OSError, SafetyError) as exc:
                unchanged = False
                row['reason'] = str(exc)
            if not unchanged:
                row.update(status='skipped', reason=row.get('reason', '源或保留副本发生变化'))
                append_log(journal, row)
                rows.append(row)
                continue
            # 每项独享新建目录，不复用同名目标；os.rename 不做跨盘复制回退。
            slot = target / str(i)
            slot.mkdir()
            dest = slot / source.name
            row.update(destination=str(dest), status='pending')
            append_log(journal, row)
            if hook:
                hook('before_rename', i, job)
            os.rename(source, dest)
            if hook:
                hook('after_rename', i, job)
            row = {**row, 'status': 'done'}
            append_log(journal, row)
            rows.append(row)
        if locked:
            (lock / '日志路径.txt').unlink()
            lock.rmdir()
            locked = False
    except BaseException as exc:
        append_log(journal, {'status': 'stopped', 'error': f'{type(exc).__name__}: {exc}'})
        raise
    finally:
        report = output / f'清理报告_{run}.json'
        report.write_text(json.dumps({'root': str(root), 'apply': apply, 'rows': rows,
                          'protected': p['protected'], 'cross_directory_groups': p['cross_directory_groups'],
                          'journal': str(journal) if apply else None,
                          'manual_audit_required': locked}, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', nargs='?', type=Path)
    parser.add_argument('--output', type=Path, default=Path.cwd() / '清理报告')
    parser.add_argument('--apply', action='store_true', help='实际隔离（不删除）')
    parser.add_argument('--audit', type=Path, help='只读检查已有日志')
    args = parser.parse_args()
    if args.audit:
        print(json.dumps(audit(args.audit), ensure_ascii=False, indent=2))
        return
    if args.root is None:
        parser.error('必须显式指定扫描根目录')
    try:
        p = plan(args.root)
        report = execute(p, args.output, args.apply)
        print('模式：', '隔离' if args.apply else '预览')
        print('计划项目：', len(p['jobs']), '\n报告：', report)
    except (OSError, SafetyError, KeyboardInterrupt) as exc:
        parser.exit(1, f'已停止：{exc}\n如有运行锁，请核查日志及隔离区后人工处理。\n')


if __name__ == '__main__':
    main()
