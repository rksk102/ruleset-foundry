"""对账 `artifacts` 分支 tree 与本轮产物文件。

发布流水线在决定跳过产物推送前，必须证明分支的实际内容与本轮产物一致；
`.product-sha256` 标记只是一份声明，不能当作内容证明。本模块给出可测试的
最小对账：递归 tree 的路径集合、普通 blob 类型与逐文件原始字节。

公开分支只承载规则产物：TXT 根目录的 `products.manifest` 仅用于发布校验，
既不要求出现在分支中，也不允许继续留在分支里（残留即视为多余文件重推）。

退出码：0 = 完全一致；10 = 内容不一致（调用方应重推）；2 = 无法完成对账
（读取/解析/git 调用失败，调用方必须 fail-closed，不能当作一致或跳过）。
"""

import argparse
import hashlib
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

MISMATCH_EXIT = 10
ERROR_EXIT = 2
REGULAR_BLOB_MODES = frozenset({"100644", "100755"})
DEFAULT_MARKER = ".product-sha256"
# 分支根目录允许出现的控制文件：忽略规则与产品哈希标记（标记名可由 --marker 指定）。
GITIGNORE = ".gitignore"
# 与 scripts/manifest.MANIFEST_NAME 保持一致（tests 校验）；仅 TXT 根目录下的清单除外，
# 嵌套同名文件仍必须逐字节对账。
MANIFEST_NAME = "products.manifest"


class TreeError(Exception):
    """无法完成对账；不代表内容一致，调用方必须 fail-closed。"""


@dataclass(frozen=True)
class TreeEntry:
    mode: str
    otype: str
    oid: str
    size: int | None
    path: str


def _git(*args):
    try:
        result = subprocess.run(["git", *args], capture_output=True, check=False)
    except OSError as exc:
        raise TreeError(f"无法执行 git {' '.join(args)}: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise TreeError(f"git {' '.join(args)} 失败（退出码 {result.returncode}）: {detail}")
    return result.stdout.decode("utf-8", "surrogateescape")


def repository_root():
    root = _git("rev-parse", "--show-toplevel").strip()
    if not root:
        raise TreeError("无法定位 git 工作区根目录")
    return Path(root).resolve()


def object_format():
    """仓库对象格式（sha1/sha256），用于按字节复算 blob 对象 ID。"""
    name = _git("rev-parse", "--show-object-format").strip()
    if name not in ("sha1", "sha256"):
        raise TreeError(f"不支持的对象格式: {name!r}")
    return name


def list_tree(ref):
    """递归列出 ref 的全部 tree 条目（路径相对工作区根目录）。"""
    output = _git("ls-tree", "-r", "-l", "-z", "--full-tree", ref)
    entries = []
    for record in output.split("\0"):
        if not record:
            continue
        meta, separator, path = record.partition("\t")
        if not separator or not path:
            raise TreeError(f"无法解析 tree 记录: {record!r}")
        parts = meta.split()
        if len(parts) != 4:
            raise TreeError(f"无法解析 tree 元数据: {meta!r}")
        mode, otype, oid, size = parts
        if size == "-":
            size_bytes = None
        else:
            try:
                size_bytes = int(size)
            except ValueError as exc:
                raise TreeError(f"无法解析 tree 条目大小: {meta!r}") from exc
        entries.append(TreeEntry(mode, otype, oid, size_bytes, path))
    return entries


def _root_key(path, root):
    """把路径换算为工作区根目录相对路径，与 tree 条目路径同形。"""
    try:
        return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError as exc:
        raise TreeError(f"本地产物不在工作区内: {path}") from exc


def collect_local_files(dirs, root):
    """收集本地产物的路径 -> 文件映射，键与 tree 路径同形（相对工作区根目录）。"""
    files = {}
    root = Path(root).resolve()
    for name in dirs:
        directory = Path(name)
        if directory.is_symlink():
            raise TreeError(f"本地产物目录是符号链接: {name}")
        if not directory.is_dir():
            raise TreeError(f"本地产物目录不存在: {name}")
        for path in sorted(directory.rglob("*")):
            if path.is_symlink():
                raise TreeError(f"本地产物是符号链接: {path}")
            if not path.is_file():
                continue
            files[_root_key(path, root)] = path
    return files


def published_local_files(txt_dir, mrs_dir, root):
    """本地产物中应当出现在公开分支的文件；TXT 根目录的产品清单不发布。"""
    files = collect_local_files((txt_dir, mrs_dir), root)
    files.pop(_root_key(Path(txt_dir) / MANIFEST_NAME, root), None)
    return files


def _blob_oid(path, algo):
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise TreeError(f"无法读取本地产物 {path}: {exc}") from exc
    digest = hashlib.new(algo)
    digest.update(b"blob %d\0" % len(data))
    digest.update(data)
    return digest.hexdigest(), len(data)


def compare_tree(entries, local, algo, controls):
    """返回不一致说明列表；空列表表示分支内容与本地产物逐字节一致。"""
    if not local:
        return ["本地没有任何产物文件，拒绝据此跳过发布"]
    problems = []
    seen = set()
    for entry in entries:
        is_blob = entry.otype == "blob" and entry.mode in REGULAR_BLOB_MODES
        if entry.path in controls:
            if not is_blob:
                problems.append(f"控制文件不是普通 blob: {entry.path}（mode={entry.mode} type={entry.otype}）")
            continue
        if not is_blob:
            problems.append(f"分支条目不是普通 blob: {entry.path}（mode={entry.mode} type={entry.otype}）")
            if entry.path in local:
                seen.add(entry.path)
            continue
        path = local.get(entry.path)
        if path is None:
            problems.append(f"分支多出本地产物不存在的文件: {entry.path}")
            continue
        seen.add(entry.path)
        oid, size = _blob_oid(path, algo)
        if entry.size is not None and entry.size != size:
            problems.append(f"内容大小不一致（分支 {entry.size} != 本地 {size}）: {entry.path}")
        elif oid != entry.oid:
            problems.append(f"内容字节不一致: {entry.path}")
    for path in sorted(set(local) - seen):
        problems.append(f"分支缺少本地产物: {path}")
    return problems


def _allowed_controls(marker):
    if not marker or "/" in marker or "\\" in marker or marker in (".", ".."):
        raise TreeError(f"非法控制文件路径: {marker!r}")
    return {GITIGNORE, marker}


def main(argv=None):
    parser = argparse.ArgumentParser(description="对账 artifacts 分支 tree 与本轮产物")
    parser.add_argument("--ref", required=True, help="要比较的 git 引用，例如 FETCH_HEAD")
    parser.add_argument("--txt-dir", required=True, help="merged TXT 产物目录（相对工作区根目录）")
    parser.add_argument("--mrs-dir", required=True, help="MRS 产物目录（相对工作区根目录）")
    parser.add_argument("--marker", default=DEFAULT_MARKER, help="分支根目录的产品哈希标记文件")
    args = parser.parse_args(argv)

    try:
        root = repository_root()
        local = published_local_files(args.txt_dir, args.mrs_dir, root)
        entries = list_tree(args.ref)
        problems = compare_tree(entries, local, object_format(), _allowed_controls(args.marker))
    except TreeError as exc:
        print(f"::error::artifacts tree 对账失败: {exc}", file=sys.stderr)
        return ERROR_EXIT

    if problems:
        print("artifacts 分支内容与本轮产物不一致:")
        for problem in problems[:20]:
            print(f"  - {problem}")
        if len(problems) > 20:
            print(f"  - ……共 {len(problems)} 项")
        return MISMATCH_EXIT
    print(f"artifacts 分支内容与本轮产物一致（{len(local)} 个文件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
