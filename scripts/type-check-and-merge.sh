#!/usr/bin/env bash
# 先类型核对，再合并，再按要求重整 merged-rules（只保留合并产物与未参与合并的源）
set -euo pipefail

INPUT_REF="${INPUT_REF:-main}"
MERGED_OWNER="${MERGED_OWNER:-rksk102}"

ts() { date +'%Y-%m-%d %H:%M:%S %Z'; }
echo "[flow] start at $(ts)" >&2

# 规范化策略
normalize_policy() {
  local p="$(echo "$1" | tr 'A-Z' 'a-z')"
  case "$p" in
    reject|block|deny|ad|ads|adblock|拦截|拒绝|屏蔽|广告) echo "block" ;;
    direct|bypass|no-proxy|直连|直连规则)               echo "direct" ;;
    proxy|proxied|forward|代理|代理规则)               echo "proxy" ;;
    *) echo "" ;;
  esac
}

# 从 rulesets 路径推断 policy/type/owner
# 输出：policy<TAB>type_hint<TAB>owner<TAB>rel
derive_from_rulesets() {
  local full="$1"
  local rel="${full#rulesets/}"
  IFS='/' read -r -a parts <<< "$rel"
  local pol="" typ="" own=""
  if [ "${#parts[@]}" -ge 4 ]; then
    pol="$(normalize_policy "${parts[0]}")"
    typ="${parts[1]}"
    own="${parts[2]}"
  fi
  echo -e "${pol}\t${typ}\t${own}\t${rel}"
}

# 检测类型（逐行扫描）
detect_type() {
  local txt="$1"
  [ -f "$txt" ] || { echo "domain"; return 0; }
  awk '
    function trim(s){ sub(/^[[:space:]]+/,"",s); sub(/[[:space:]]+$/,"",s); return s }
    BEGIN{ n_domain=0; n_ip=0; n_classic=0 }
    {
      line=$0
      sub(/#.*/,"",line); sub(/!.*/, "", line)
      line=trim(line)
      if (line=="") next
      if (line ~ /^[A-Z-]+,.+$/)                    { n_classic++; next }
      if (line ~ /^([A-Za-z0-9*-]+\.)+[A-Za-z0-9-]+$/ || line ~ /^\+\.[A-Za-z0-9.-]+$/ || line ~ /^\*[A-Za-z0-9.-]+$/) { n_domain++;  next }
      if (line ~ /^([0-9]{1,3}\.){3}[0-9]{1,3}(\/[0-9]{1,2})?$/ || line ~ /^[0-9A-Fa-f:]+(\/[0-9]{1,3})?$/)            { n_ip++;      next }
    }
    END{
      if (n_classic>0 && n_classic>=n_domain && n_classic>=n_ip)      print "classical";
      else if (n_domain>=n_ip)                                         print "domain";
      else                                                             print "ipcidr";
    }
  ' "$txt"
}

# 第 1 步：扫描 rulesets，写入 typedb.tsv
typedb="typedb.tsv"
: > "$typedb"
rules_count=0
if [ -d rulesets ]; then
  while IFS= read -r -d '' f; do
    rules_count=$((rules_count+1))
    IFS=$'\t' read -r pol typ_hint own rel < <(derive_from_rulesets "$f")
    [ -z "$pol" ] && pol="proxy"
    [ -z "$own" ] && own="unknown"
    det="$(detect_type "$f")"
    printf "%s\t%s\t%s\t%s\n" "$rel" "$pol" "$det" "$own" >> "$typedb"
  done < <(find rulesets -type f -name '*.txt' -print0)
fi
echo "[typescan] rulesets files: $rules_count ; typedb -> $typedb" >&2

# 第 2 步：执行合并（生成 merge-outputs、merge-used.list、merge-map.tsv）
echo "[merge] run scripts/merge-rules.sh ..." >&2
CONFIG_FILE="${CONFIG_FILE:-merge-config.yaml}" scripts/merge-rules.sh
echo "[merge] done." >&2

# 读取 used 清单
declare -A USED
if [ -f merge-used.list ]; then
  while IFS= read -r line; do
    line="$(echo "$line" | sed 's/^[[:space:]]\+//; s/[[:space:]]\+$//')"
    [ -z "$line" ] && continue
    USED["$line"]=1
  done < merge-used.list
fi
used_n="${#USED[@]}"

# 读取合并策略映射（name -> policy）
declare -A MERGE_POLICY
if [ -f merge-map.tsv ]; then
  while IFS=$'\t' read -r name policy; do
    [ -z "$name" ] && continue
    MERGE_POLICY["$name"]="$policy"
  done < merge-map.tsv
fi

# 第 3 步：重建 merged-rules 目录（只保留合并产物 + 未参与合并的源）
workdir="merged-rules.__new"
rm -rf "$workdir"
mkdir -p "$workdir"

# 3.1 合并产物 -> merged-rules/<policy>/<type>/<MERGED_OWNER>/<name>
if [ -d merge-outputs ]; then
  while IFS= read -r -d '' mf; do
    base="$(basename "$mf")"
    det="$(detect_type "$mf")"
    pol="${MERGE_POLICY[$base]:-}"
    if [ -z "$pol" ]; then
      # 兜底启发：从文件名推断
      low="$(echo "$base" | tr 'A-Z' 'a-z')"
      if   echo "$low" | grep -Eq 'reject|block|ad|ads|adblock'; then pol="block"
      elif echo "$low" | grep -Eq 'direct|bypass|no-?proxy';   then pol="direct"
      elif echo "$low" | grep -Eq 'proxy|proxied|forward';     then pol="proxy"
      else pol="proxy"
      fi
    fi
    out_dir="${workdir}/${pol}/${det}/${MERGED_OWNER}"
    mkdir -p "$out_dir"
    cp -f "$mf" "${out_dir}/${base}"
  done < <(find merge-outputs -type f -name '*.txt' -print0)
fi

# 3.2 未参与合并的源 -> merged-rules/<policy>/<type>/<owner>/<basename>
while IFS=$'\t' read -r rel pol det own; do
  # 跳过参与合并的源
  if [ -n "${USED[$rel]+x}" ]; then
    continue
  fi
  src="rulesets/${rel}"
  [ -f "$src" ] || continue
  base="$(basename "$rel")"
  [ -z "$own" ] && own="unknown"
  out_dir="${workdir}/${pol}/${det}/${own}"
  mkdir -p "$out_dir"
  cp -f "$src" "${out_dir}/${base}"
done < "$typedb"

# 原子替换
rm -rf "merged-rules"
mv "$workdir" "merged-rules"

# 摘要
total_out=$(find merged-rules -type f -name '*.txt' | wc -l | tr -d '[:space:]')
echo "[done] merged-rules files: ${total_out} ; used (merged inputs): ${used_n} ; time: $(ts)" >&2
