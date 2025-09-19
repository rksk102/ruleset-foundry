#!/usr/bin/env bash
set -euo pipefail

# 可选严格模式：任一源失败就让 Job 失败；默认 false（与工作流一致）
STRICT="${STRICT:-false}"

SOURCE_DIR="rulesets"
TMP_DIR="${RUNNER_TEMP:-/tmp}/sync-tmp"
mkdir -p "$TMP_DIR"

# 退出/中断时清理所有下载残留与空目录
cleanup() {
  if [ -d "$SOURCE_DIR" ]; then
    find "$SOURCE_DIR" -type f \( -name "*.download" -o -name "*.source" \) -delete 2>/dev/null || true
    find "$SOURCE_DIR" -type d -empty -delete 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

# 扩展名映射：哪些输入扩展需要强制保存为 .txt
FORCE_TXT_EXTS="list"
force_txt_ext() {
  local ext="${1,,}"
  for t in $FORCE_TXT_EXTS; do
    if [[ "$ext" == "$t" ]]; then return 0; fi
  done
  return 1
}

# 归一化：规则策略（policy）和类型（type）
normalize_policy() {
  local p="${1,,}"
  case "$p" in
    reject|block|deny|ad|ads|adblock|拦截|拒绝|屏蔽|广告) echo "block" ;;
    direct|bypass|no-proxy|直连|直连规则)               echo "direct" ;;
    proxy|proxied|forward|代理|代理规则)               echo "proxy" ;;
    *) echo "" ;;
  esac
}
normalize_type() {
  local t="${1,,}"
  case "$t" in
    domain|domains|domainset) echo "domain" ;;
    ip|ipcidr|ip-cidr|cidr)   echo "ipcidr" ;;
    classical|classic|mix|mixed|general|all) echo "classical" ;;
    *) echo "" ;;
  esac
}
is_policy_token() { [[ -n "$(normalize_policy "$1")" ]]; }
is_type_token()   { [[ -n "$(normalize_type   "$1")" ]]; }

# 输出相对路径：<policy>/<type>/<owner>/<文件名[映射ext]>
map_out_relpath() {
  local policy="$1"; local type="$2"; local owner="$3"; local fn="$4"
  local ext="${fn##*.}"
  local base="${fn%.*}"
  local mapped="$fn"
  if force_txt_ext "$ext"; then
    mapped="${base}.txt"
  fi
  echo "${policy}/${type}/${owner}/${mapped}"
}

# 1) 预清洗 sources.urls：去 BOM/CR、行尾内联注释、首尾空白（保留 [policy:] 和 [type:] 段落头）
if [ ! -f sources.urls ]; then
  echo "sources.urls not found, skip."
  exit 0
fi

CLEAN="${TMP_DIR}/sources.cleaned"
awk 'NR==1{ sub(/^\xEF\xBB\xBF/,"") } { print }' sources.urls \
| sed 's/\r$//' \
| sed -E 's/[[:space:]]+#.*$//' \
| sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//' \
> "$CLEAN"

# 2) 解析：生成 triplets.tsv（policy \t type \t url）
TRIPLETS="${TMP_DIR}/triplets.tsv"
: > "$TRIPLETS"

current_policy="proxy"
current_type="domain"

while IFS= read -r line; do
  # 跳过空行/纯注释
  [[ -z "$line" ]] && continue
  [[ "$line" =~ ^# ]] && continue

  # 段落头：[policy: ...]
  if [[ "$line" =~ ^\[policy:[[:space:]]*([^\]]+)[[:space:]]*\]$ ]]; then
    pol_guess="${BASH_REMATCH[1]}"
    pol_norm="$(normalize_policy "$pol_guess")"
    current_policy="${pol_norm:-proxy}"
    continue
  fi
  # 段落头：[type: ...]
  if [[ "$line" =~ ^\[type:[[:space:]]*([^\]]+)[[:space:]]*\]$ ]]; then
    type_guess="${BASH_REMATCH[1]}"
    type_norm="$(normalize_type "$type_guess")"
    current_type="${type_norm:-domain}"
    continue
  fi

  # 含 URL 的行，解析前缀 token（policy/type 或键值对）
  if [[ "$line" =~ https?:// ]]; then
    url_word="$(awk '{ for (i=1;i<=NF;i++) if ($i ~ /^https?:\/\//) { print $i; exit } }' <<< "$line")"
    prefix="${line%%$url_word*}"

    pol="$current_policy"
    typ="$current_type"

    IFS=' ' read -r -a toks <<< "$prefix"
    for tk in "${toks[@]}"; do
      [[ -z "$tk" ]] && continue
      if [[ "$tk" =~ ^policy[:=](.+)$ ]]; then
        v="${BASH_REMATCH[1]}"
        v_norm="$(normalize_policy "$v")"
        [[ -n "$v_norm" ]] && pol="$v_norm"
        continue
      fi
      if [[ "$tk" =~ ^type[:=](.+)$ ]]; then
        v="${BASH_REMATCH[1]}"
        v_norm="$(normalize_type "$v")"
        [[ -n "$v_norm" ]] && typ="$v_norm"
        continue
      fi
      v_pol="$(normalize_policy "$tk")"
      if [[ -n "$v_pol" ]]; then pol="$v_pol"; continue; fi
      v_typ="$(normalize_type "$tk")"
      if [[ -n "$v_typ" ]]; then typ="$v_typ"; continue; fi
    done

    pol="${pol:-$current_policy}"
    typ="${typ:-$current_type}"

    echo -e "${pol}\t${typ}\t${url_word}" >> "$TRIPLETS"
    continue
  fi
done < "$CLEAN"

if [ ! -s "$TRIPLETS" ]; then
  echo "No usable URLs after parsing. Skip."
  exit 0
fi

# 3) 通用净化器（去注释、去 YAML payload: 与行首 -、去引号等）
SAN_AWK="${TMP_DIR}/sanitize.awk"
cat > "$SAN_AWK" <<'AWK'
BEGIN { first=1 }
{
  if (first) {
    sub(/^\xEF\xBB\xBF/, "")
    sub(/\r$/, "")
    if ($0 ~ /^[[:space:]]*payload:[[:space:]]*$/) { first=0; next }
    first=0
  }
  sub(/\r$/, "")

  line = $0
  tmp = line
  sub(/^[[:space:]]+/, "", tmp)
  if (tmp ~ /^#/ || tmp ~ /^!/) next

  sub(/[[:space:]]+#.*$/, "", line)
  sub(/^[[:space:]]*-[[:space:]]*/, "", line)

  if (line ~ /^'.*'$/) { line = substr(line, 2, length(line)-2) }
  if (line ~ /^".*"$/) { line = substr(line, 2, length(line)-2) }

  sub(/，.*$/, "", line)

  sub(/^[[:space:]]+/, "", line)
  sub(/[[:space:]]+$/, "", line)

  gsub(/[[:space:]]*,[[:space:]]*/, ",", line)

  if (line == "") next
  print line
}
AWK

# 3b) domain 专用净化器：仅保留纯 FQDN（ASCII / punycode），剔除正则/通配/URL/端口等
SAN_DOMAIN_AWK="${TMP_DIR}/sanitize_domain.awk"
cat > "$SAN_DOMAIN_AWK" <<'AWK'
BEGIN { IGNORECASE=1 }
function valid_domain(s,   n,parts,i,tld,p) {
  if (length(s) < 1 || length(s) > 253) return 0
  # 仅允许 a-z 0-9 . -
  if (s ~ /[^a-z0-9\.-]/) return 0
  # 不允许首尾是 . 或 -
  if (s ~ /^[\.-]/ || s ~ /[\.-]$/) return 0
  # 折叠重复点
  while (s ~ /\.\./) gsub(/\.\./,".",s)
  # 必须至少一个点
  if (s !~ /\./) return 0
  n = split(s, parts, ".")
  tld = parts[n]
  # TLD 需为字母 2-63 或 punycode
  if (!(tld ~ /^[a-z]{2,63}$/ || tld ~ /^xn--[a-z0-9-]{2,59}$/)) return 0
  for (i=1;i<=n;i++) {
    p = parts[i]
    if (length(p) < 1 || length(p) > 63) return 0
    if (p ~ /^-/ || p ~ /-$/) return 0
    if (p ~ /[^a-z0-9-]/) return 0
  }
  return 1
}
{
  s = $0

  # 丢弃明显是正则/关键字
  if (s ~ /^[[:space:]]*(regexp|keyword)[[:space:]]*[:=]/) next
  if (s ~ /[\^\$\|\(\)\[\]\{\}\\\?\*\+]/) next

  # 标准化
  sub(/^[[:space:]]+/, "", s); sub(/[[:space:]]+$/, "", s)
  gsub(/^['"]|['"]$/, "", s)
  gsub(/，.*$/, "", s)
  s = tolower(s)

  # 去掉 scheme、认证信息、路径/查询
  sub(/^[a-z0-9+.-]+:\/\//, "", s)
  sub(/^[^@]+@/, "", s)
  sub(/[\/\?].*$/, "", s)

  # 去掉已知前缀 full:/domain:/host:/suffix:
  s = gensub(/^(full|domain|host|suffix)[[:space:]]*[:=][[:space:]]*/, "", 1, s)

  # Adblock 风格
  sub(/^\|\|/, "", s); sub(/^\|/, "", s); sub(/\^$/, "", s)

  # 去通配与前导点、端口
  sub(/^(\*\.|\.|\+\.)/, "", s)
  sub(/:[0-9]+$/, "", s)

  if (valid_domain(s)) {
    if (!seen[s]++) print s
  }
}
AWK

# 3c) ipcidr 专用净化器：借助 Python 严格校验 IPv4/IPv6（含 CIDR）
SAN_IP_PY="${TMP_DIR}/sanitize_ipcidr.py"
cat > "$SAN_IP_PY" <<'PY'
import sys, re, ipaddress
seen = set()
for raw in sys.stdin:
    s = raw.strip()
    if not s or s.startswith('#') or s.startswith('!'):
        continue
    s = s.strip('\'"')
    # 从经典格式提取：IP-CIDR[,|-]value[,flags]
    m = re.match(r'(?i)^\s*(ip(?:-)?cidr6?|ip6(?:-)?cidr|ip6|ip)\s*[:,]\s*([^,\s#;]+)', s)
    if m:
        s = m.group(2)
    # 去 flags/尾注
    s = re.split(r'[#\s,;]', s)[0].strip()
    # 去中括号
    s = s.strip('[]')
    out = None
    try:
        if '/' in s:
            n = ipaddress.ip_network(s, strict=False)  # 接受主机地址形式的网段
            out = str(n)
        else:
            a = ipaddress.ip_address(s)
            out = str(a)
    except Exception:
        continue
    if out not in seen:
        print(out)
        seen.add(out)
PY

# 4) 来源目录名解析
get_owner_dir() {
  local url="$1"
  local host
  host=$(echo "$url" | awk -F/ '{print $3}')
  if [ "$host" = "raw.githubusercontent.com" ]; then
    echo "$url" | awk -F/ '{print $4}'
  elif [ "$host" = "cdn.jsdelivr.net" ]; then
    # https://cdn.jsdelivr.net/gh/<owner>/<repo>@<ref>/...
    local p4
    p4=$(echo "$url" | awk -F/ '{print $4}')
    if [ "$p4" = "gh" ]; then
      echo "$url" | awk -F/ '{print $5}'
    else
      echo "$host"
    fi
  else
    echo "$host"
  fi
}

# 5) 下载（含 Loyalsoldier 路径纠错）
try_download() {
  local url="$1"; local out="$2"
  local code
  code=$(curl -sL --create-dirs -o "${out}.download" -w "%{http_code}" "$url" || true)
  if [ "$code" -ge 200 ] && [ "$code" -lt 300 ] && [ -s "${out}.download" ]; then
    echo "OK  ($code): $url"
    return 0
  fi
  echo "Warn ($code): $url"

  if [[ "$url" == https://raw.githubusercontent.com/Loyalsoldier/clash-rules/release/ruleset/* ]]; then
    local alt="${url/\/release\/ruleset\//\/release\/}"
    echo "Retry with corrected URL: $alt"
    code=$(curl -sL -o "${out}.download" -w "%{http_code}" "$alt" || true)
    if [ "$code" -ge 200 ] && [ "$code" -lt 300 ] && [ -s "${out}.download" ]; then
      echo "OK  ($code): $alt"
      return 0
    else
      echo "Fail($code): $alt"
    fi
  fi

  rm -f "${out}.download"
  return 1
}

# 6) 构建期望文件列表并清理“孤儿”
EXP="${TMP_DIR}/expected_files.list"
ACT="${TMP_DIR}/actual_files.list"
: > "$EXP"; : > "$ACT"

while IFS=$'\t' read -r policy type url; do
  owner="$(get_owner_dir "$url")"
  fn="$(basename "$url")"
  pol_norm="$(normalize_policy "$policy")"; typ_norm="$(normalize_type "$type")"
  pol="${pol_norm:-proxy}"; typ="${typ_norm:-domain}"
  rel_out="$(map_out_relpath "$pol" "$typ" "$owner" "$fn")"
  echo "${SOURCE_DIR}/${rel_out}" >> "$EXP"
done < "$TRIPLETS"

if [ -d "$SOURCE_DIR" ]; then
  find "$SOURCE_DIR" -type f > "$ACT"
fi

sort -u "$ACT" -o "$ACT" || true
sort -u "$EXP" -o "$EXP"

comm -23 "$ACT" "$EXP" | while read -r f; do
  [ -n "$f" ] && echo "Prune: $f" && rm -f "$f" || true
done

# 7) 拉取并净化（写入 <policy>/<type>/<owner>/<文件>）
mkdir -p "$SOURCE_DIR"
fail_count=0

while IFS=$'\t' read -r policy type url; do
  owner="$(get_owner_dir "$url")"
  fn="$(basename "$url")"
  pol_norm="$(normalize_policy "$policy")"; typ_norm="$(normalize_type "$type")"
  pol="${pol_norm:-proxy}"; typ="${typ_norm:-domain}"
  rel_out="$(map_out_relpath "$pol" "$typ" "$owner" "$fn")"
  out="${SOURCE_DIR}/${rel_out}"

  echo "Fetch [${pol}/${typ}] -> ${url}"
  mkdir -p "$(dirname "$out")"
  if ! try_download "$url" "$out"; then
    echo "::warning::Download failed for $url"
    fail_count=$((fail_count+1))
    continue
  fi

  # 通用净化 -> 类型化净化 -> 去重
  tmp1="${out}.stage1"
  tmp2="${out}.stage2"

  awk -f "$SAN_AWK" "${out}.download" > "$tmp1"

  if [ "$typ" = "domain" ]; then
    awk -f "$SAN_DOMAIN_AWK" "$tmp1" > "$tmp2"
  elif [ "$typ" = "ipcidr" ]; then
    python3 "$SAN_IP_PY" < "$tmp1" > "$tmp2"
  else
    # classical 等其他类型保持通用净化即可
    cp "$tmp1" "$tmp2"
  fi

  # 去重保持顺序
  awk '!seen[$0]++' "$tmp2" > "$out"

  rm -f "${out}.download" "$tmp1" "$tmp2"
  echo "Saved: $out"
done < "$TRIPLETS"

# 8) 清空空目录 + 兜底清理一切残留
cleanup

# 9) 失败汇总 + 严格模式
if [ "$fail_count" -gt 0 ]; then
  echo "::warning::Total failed sources: $fail_count"
  if [ "$STRICT" = "true" ]; then
    echo "STRICT mode enabled. Failing the job."
    exit 1
  fi
fi

# 10) 提交变更（仅在有变更时）
if [[ -z $(git status -s) ]]; then
  echo "No changes."
  exit 0
fi

git config user.name 'GitHub Actions Bot'
git config user.email 'actions@github.com'
git add -A
git commit -m "chore(daily-sync): Update rule sets (policy/type/source) for $(date +'%Y-%m-%d')"
git push