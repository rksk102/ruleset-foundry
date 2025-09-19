#!/usr/bin/env bash
# 合并 rulesets 下的文件到 merge-outputs/，并输出 merge-used.list 与 merge-map.tsv
set -euo pipefail

CONFIG_FILE="${CONFIG_FILE:-merge-config.yaml}"

echo "[merge] Using config: ${CONFIG_FILE}" >&2

# 用 Python 解析 YAML（优先 PyYAML；若无则用简易解析器），并执行合并逻辑
python3 - <<'PY'
import os, sys, re, glob, io
from collections import OrderedDict

CONFIG_FILE = os.environ.get("CONFIG_FILE", "merge-config.yaml")
OUT_DIR = "merge-outputs"
USED_LIST = "merge-used.list"
MAP_TSV = "merge-map.tsv"

# 读取文件
with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
    text = f.read()

# 尝试 PyYAML 解析
merges = None
try:
    import yaml
    data = yaml.safe_load(text) or {}
    merges = data.get('merges', []) or []
except Exception:
    # 简易解析器（适配示例格式）
    merges = []
    in_merges = False
    cur = None
    in_inputs = False
    for raw in text.splitlines():
        line = raw.rstrip('\n')
        if re.match(r'^\s*#', line) or line.strip() == '':
            continue
        if re.match(r'^\s*merges\s*:\s*$', line):
            in_merges = True
            continue
        if not in_merges:
            continue
        # 新条目
        m = re.match(r'^\s*-\s+name:\s*(.+?)\s*$', line)
        if m:
            name = m.group(1).strip().strip('"').strip("'")
            cur = {"name": name, "inputs": [], "description": ""}
            merges.append(cur)
            in_inputs = False
            continue
        # 描述
        m = re.match(r'^\s*description:\s*(.+?)\s*$', line)
        if m and cur is not None:
            desc = m.group(1).strip().strip('"').strip("'")
            cur["description"] = desc
            continue
        # inputs 块开始
        if re.match(r'^\s*inputs\s*:\s*$', line):
            in_inputs = True
            continue
        # inputs 列表项
        m = re.match(r'^\s*-\s+(.+?)\s*$', line)
        if m and in_inputs and cur is not None:
            pat = m.group(1).strip().strip('"').strip("'")
            cur["inputs"].append(pat)
            continue
        # 其他情况忽略（或下一条目开始时覆盖）

# 归一化策略（从路径首段或 name 键推断）
def normalize_policy(s: str) -> str:
    s = (s or "").lower()
    if re.search(r'(reject|block|deny|ads?|adblock|拦截|拒绝|屏蔽|广告)', s): return "block"
    if re.search(r'(direct|bypass|no-?proxy|直连)', s): return "direct"
    if re.search(r'(proxy|proxied|forward|代理)', s): return "proxy"
    return ""

os.makedirs(OUT_DIR, exist_ok=True)
used_set = set()
merge_map = []  # (name, policy)

def rel_rulesets(path: str) -> str:
    if path.startswith("rulesets/"):
        return path[len("rulesets/"):]
    return path

def list_inputs(inputs):
    files = []
    for pat in inputs:
        patt = os.path.join("rulesets", pat)
        matches = glob.glob(patt, recursive=True)
        if not matches:
            print(f"[merge] warn: no matches for pattern: {pat}", file=sys.stderr)
        for p in matches:
            if os.path.isfile(p):
                files.append(p)
    # 去重、稳定顺序
    seen = set()
    res = []
    for p in files:
        if p not in seen:
            seen.add(p)
            res.append(p)
    return res

def merge_one(name: str, inputs: list, description: str):
    # 推断策略：优先用第一个输入的首段 policy，否则看 name；再没有则默认 proxy
    policy = "proxy"
    if inputs:
        first_rel = rel_rulesets(inputs[0])
        head = first_rel.split("/", 1)[0]
        pol = normalize_policy(head)
        if pol: policy = pol
    if not inputs:
        # 用 name 启发（all-proxy 等）
        pol2 = normalize_policy(name)
        if pol2: policy = pol2

    # 输出文件
    out_path = os.path.join(OUT_DIR, name)
    # 确保扩展名
    if not re.search(r'\.txt$', out_path, re.IGNORECASE):
        out_path += ".txt"
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    # 去重合并（跳过空行/注释），保持行序
    uniq = OrderedDict()
    for p in inputs:
        try:
            with open(p, 'r', encoding='utf-8', errors='ignore') as fin:
                for line in fin:
                    s = line.strip()
                    if not s: continue
                    if s.startswith('#') or s.startswith('!'): continue
                    if s not in uniq:
                        uniq[s] = True
        except Exception as e:
            print(f"[merge] warn: read fail {p}: {e}", file=sys.stderr)

    with open(out_path, 'w', encoding='utf-8') as fout:
        fout.write(f"# merged by merge-rules.sh\n")
        if description:
            fout.write(f"# {description}\n")
        fout.write(f"# inputs: {len(inputs)} files\n")
        for s in uniq.keys():
            fout.write(s + "\n")

    # 记录 used
    for p in inputs:
        used_set.add(rel_rulesets(p))

    # 记录策略映射
    base = os.path.basename(out_path)
    merge_map.append((base, policy))
    print(f"[merge] built {out_path} (lines={len(uniq)})", file=sys.stderr)

# 遍历配置执行合并
n = 0
for m in merges:
    name = (m.get("name") or "").strip()
    inputs = list_inputs(m.get("inputs") or [])
    desc = (m.get("description") or "").strip()
    if not name:
        print("[merge] skip item without name", file=sys.stderr)
        continue
    merge_one(name, inputs, desc)
    n += 1

# 输出 used list
with open(USED_LIST, 'w', encoding='utf-8') as f:
    for rel in sorted(used_set):
        f.write(rel + "\n")

# 输出 merge-map.tsv：name<TAB>policy
with open(MAP_TSV, 'w', encoding='utf-8') as f:
    for name, policy in merge_map:
        f.write(f"{name}\t{policy}\n")

print(f"[merge] outputs: {n}, used files: {len(used_set)}", file=sys.stderr)
PY

echo "[merge] Done. Files: $(ls -1 merge-outputs 2>/dev/null | wc -l | tr -d ' ') ; used=$(wc -l < merge-used.list 2>/dev/null || echo 0) ; map=$(wc -l < merge-map.tsv 2>/dev/null || echo 0)" >&2
