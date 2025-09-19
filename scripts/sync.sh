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
    find "$SOURCE_DIR" -type f \( -name "*.download" -o -name "*.source" -o -name "*.stage0" -o -name "*.stage1" -o -name "*.stage2" \) -delete 2>/dev/null || true
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

# 3) 通用净化器（去注释、去 YAML payload: 与行首 -、去引号等；涵盖常见 payload 列表）
SAN_AWK="${TMP_DIR}/sanitize.awk"
cat > "$SAN_AWK" <<'AWK'
BEGIN { first=1 }
{
  if (first) {
    sub(/^\xEF\xBB\xBF/, "")
    sub(/\r$/, "")
    first=0
  }
  sub(/\r$/, "")

  line = $0
  tmp = line
  sub(/^[[:space:]]+/, "", tmp)

  # 丢弃注释和 payload:（大小写不敏感）
  if (tmp ~ /^(#|!)/) next
  if (tmp ~ /^[Pp][Aa][Yy][Ll][Oo][Aa][Dd][[:space:]]*:/) next

  # 行内注释与 YAML 列表项
  sub(/[[:space:]]+#.*$/, "", line)
  sub(/^[[:space:]]*-[[:space:]]*/, "", line)

  # 去成对引号（仅当整行被同类引号包裹）
  if (line ~ /^'.*'$/) { line = substr(line, 2, length(line)-2) }
  if (line ~ /^".*"$/) { line = substr(line, 2, length(line)-2) }

  # 去中文逗号后面的注释/注解
  sub(/，.*$/, "", line)

  # 修剪两端空白与逗号空白
  sub(/^[[:space:]]+/, "", line)
  sub(/[[:space:]]+$/, "", line)
  gsub(/[[:space:]]*,[[:space:]]*/, ",", line)

  if (line == "") next
  print line
}
AWK

# 3b) domain 专用净化器：先去前缀再判定，避免误杀 '+.' '*.','.' 开头的域名
SAN_DOMAIN_AWK="${TMP_DIR}/sanitize_domain.awk"
cat > "$SAN_DOMAIN_AWK" <<'AWK'
function valid_domain(s,   n,parts,i,tld,p) {
  if (length(s) < 1 || length(s) > 253) return 0
  if (s ~ /[^a-z0-9\.-]/) return 0
  if (s ~ /^[\.-]/ || s ~ /[\.-]$/) return 0
  while (s ~ /\.\./) gsub(/\.\./,".",s)
  if (s !~ /\./) return 0
  n = split(s, parts, ".")
  tld = parts[n]
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

  # 标准化（顺序很重要）
  sub(/^[[:space:]]+/, "", s); sub(/[[:space:]]+$/, "", s)
  # 去成对引号
  if (s ~ /^'.*'$/) { s = substr(s, 2, length(s)-2) }
  if (s ~ /^".*"$/) { s = substr(s, 2, length(s)-2) }
  # 去中文逗号后注解
  sub(/，.*$/, "", s)
  s = tolower(s)

  # 去掉 scheme、认证信息、路径/查询
  sub(/^[a-z0-9+.-]+:\/\//, "", s)
  sub(/^[^@]+@/, "", s)
  sub(/[\/\?].*$/, "", s)

  # 去掉已知前缀 full:/domain:/host:/suffix:
  sub(/^(full|domain|host|suffix)[[:space:]]*[:=][[:space:]]*/, "", s)

  # Adblock 风格
  sub(/^\|\|/, "", s); sub(/^\|/, "", s); sub(/\^$/, "", s)

  # 去通配与前导点、'+.'、端口
  sub(/^(\+\.|\*\.|\.)/, "", s)
  sub(/:[0-9]+$/, "", s)

  # 去掉允许前缀后，再检查是否含有正则/通配符等字符；若仍存在则丢弃
  if (s ~ /[\^\$\|\(\)\[\]\{\}\\\?\*\+]/) next

  if (valid_domain(s)) {
    if (!seen[s]++) print s
  }
}
AWK

# 3c) ipcidr 专用净化器：严格校验 IPv4/IPv6（含 CIDR），从行内提取
SAN_IP_PY="${TMP_DIR}/sanitize_ipcidr.py"
cat > "$SAN_IP_PY" <<'PY'
import sys, re, ipaddress
seen = set()

# 粗略正则：提取行内第一个可能的 IPv4/IPv6（可带 CIDR）
re_v4 = re.compile(r'(?<![\d])(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?(?![\d])')
# 非贪婪 IPv6 + 可选 CIDR（允许 :: 缩写）
re_v6 = re.compile(r'([0-9A-Fa-f:]+:+[0-9A-Fa-f:]*)(?:/\d{1,3})?')

for raw in sys.stdin:
    s0 = raw.strip()
    if not s0 or s0.startswith('#') or s0.startswith('!'):
        continue

    s = s0.strip('\'"')

    # 1) 经典写法前缀（IP-CIDR、IP-CIDR6、IP、IP6）
    m = re.match(r'(?i)^\s*(ip(?:-)?cidr6?|ip6(?:-)?cidr|ip6|ip)\s*[:,]\s*([^,\s#;]+)', s)
    if m:
        s = m.group(2)
    else:
        # 2) 从整行中尽力抓第一个 IP/网段
        m4 = re_v4.search(s)
        m6 = re_v6.search(s)
        if m4 and (not m6 or m4.start() <= m6.start()):
            s = m4.group(0)
        elif m6:
            s = m6.group(0)
        else:
            continue

    # 去掉 flags/尾注/括号等
    s = re.split(r'[#\s,;]', s)[0].strip()
    s = s.strip('[]()')

    try:
        if '/' in s:
            n = ipaddress.ip_network(s, strict=False)
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

# 3d) YAML payload 提取器：支持 payload: - item 和 payload: [ ... ]，可多段出现
EXTRACT_YAML_PY="${TMP_DIR}/extract_yaml_payload.py"
cat > "$EXTRACT_YAML_PY" <<'PY'
import sys, re
from typing import List

def split_inline_array(s: str) -> List[str]:
    # 输入类似: [ "a", '+.b.com', 'c' ]
    # 返回: ["a", "+.b.com", "c"]
    out, cur = [], []
    in_s, in_d, esc, depth = False, False, False, 0
    for ch in s:
        if esc:
            cur.append(ch); esc = False; continue
        if ch == '\\':
            esc = True; continue
        if ch == "'" and not in_d:
            in_s = not in_s; continue
        if ch == '"' and not in_s:
            in_d = not in_d; continue
        if not in_s and not in_d:
            if ch == '[':
                depth += 1; continue
            if ch == ']':
                if cur:
                    token = ''.join(cur).strip()
                    if token:
                        out.append(token)
                    cur = []
                depth = max(0, depth-1)
                continue
            if ch == ',' and depth >= 1:
                token = ''.join(cur).strip()
                if token:
                    out.append(token)
                cur = []
                continue
        cur.append(ch)
    if cur:
        token = ''.join(cur).strip()
        if token:
            out.append(token)
    # 去掉外层可能残留的引号
    out = [t.strip().strip("'").strip('"') for t in out if t.strip()]
    return out

def extract(lines: List[str]) -> List[str]:
    res: List[str] = []
    i = 0
    # 去掉首行 BOM
    if lines:
        lines[0] = lines[0].lstrip('\ufeff')
    n = len(lines)
    while i < n:
        line = lines[i].rstrip('\r\n')
        m = re.match(r'^\s*payload\s*:\s*(.*)$', line, flags=re.IGNORECASE)
        if not m:
            i += 1
            continue
        rest = m.group(1).strip()
        base_indent = len(line) - len(line.lstrip(' '))
        # 1) 内联数组 payload: [ ... ]
        if rest.startswith('['):
            buf = [rest]
            j = i + 1
            # 收集直到配对的 ] 结束（支持跨行）
            open_count = rest.count('[') - rest.count(']')
            while j < n and open_count > 0:
                seg = lines[j].rstrip('\r\n')
                buf.append(seg)
                open_count += seg.count('[') - seg.count(']')
                j += 1
            inline = ' '.join(buf)
            res.extend(split_inline_array(inline))
            i = j
            continue
        # 2) 缩进列表：
        i += 1
        while i < n:
            l2 = lines[i].rstrip('\r\n')
            stripped = l2.lstrip(' ')
            indent = len(l2) - len(stripped)
            if not stripped:
                i += 1
                continue
            # 低于 payload 缩进，说明列表结束
            if indent <= base_indent and not stripped.startswith('-'):
                break
            # 仅接受 list item
            m2 = re.match(r'^\s*-\s*(.*)$', l2)
            if m2:
                val = m2.group(1).strip()
                # 去尾注
                val = re.split(r'\s+#', val, maxsplit=1)[0].strip()
                val = val.strip().strip("'").strip('"')
                if val:
                    res.append(val)
            i += 1
    return res

if __name__ == "__main__":
    data = sys.stdin.read().splitlines()
    items = extract(data)
    for x in items:
        if x:
            print(x)
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

# 自动判别是否为“域名列表”
is_domain_list() {
  local f="$1"
  awk '
    BEGIN{t=0; d=0}
    {
      s=$0
      gsub(/^[[:space:]]+|[[:space:]]+$/,"",s)
      if (s=="") next
      t++
      sub(/^(\+\.|\*\.|\.)/, "", s)
      s=tolower(s)
      if (s ~ /^[a-z0-9-]+(\.[a-z0-9-]+)+$/) d++
    }
    END{ if (t>0 && d*100/t >= 80) exit 0; else exit 1 }
  ' "$f"
}

# 自动判别是否为“IP/CIDR 列表”
is_ipcidr_list() {
  local f="$1"
  awk '
    BEGIN{t=0; p=0}
    {
      s=$0
      gsub(/^[[:space:]]+|[[:space:]]+$/,"",s)
      if (s=="") next
      t++
      # 经典前缀
      if (tolower(s) ~ /^(ip(-)?cidr6?|ip6(-)?cidr|ip6|ip)[ ,:]/) { p++; next }
      # 粗略 IPv4 / IPv4-CIDR
      if (s ~ /^[0-9]{1,3}(\.[0-9]{1,3}){3}(\/[0-9]{1,2})?([ ,].*)?$/) { p++; next }
      # 粗略 IPv6（含 ::）
      if (s ~ /:[0-9a-fA-F]/) { p++; next }
    }
    END{ if (t>0 && p*100/t >= 60) exit 0; else exit 1 }
  ' "$f"
}

# 粗判是否包含 YAML payload（触发提取器）
looks_like_yaml_payload() {
  local f="$1"
  if grep -qiE '^\s*payload\s*:' "$f"; then
    return 0
  fi
  case "${f##*.}" in
    yml|yaml) return 0 ;;
  esac
  return 1
}

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

  # 阶段化处理：先尽力从 YAML 中提取 payload，再做净化/解析
  tmp0="${out}.stage0"  # YAML 提取后的原始条目或原始内容
  tmp1="${out}.stage1"  # 通用净化结果（非 ipcidr 才生成）
  tmp2="${out}.stage2"  # 最终产物

  # 若像 YAML payload，先用 Python 提取器展开（支持内联数组）
  if looks_like_yaml_payload "${out}.download"; then
    python3 "$EXTRACT_YAML_PY" < "${out}.download" > "$tmp0" || true
  fi
  # 提取失败或不是 YAML payload：直接用原始内容
  if [ ! -s "$tmp0" ]; then
    cp "${out}.download" "$tmp0"
  fi

  # 类型化净化（注意：ipcidr 跳过通用净化，直接解析）
  if [ "$typ" = "ipcidr" ]; then
    python3 "$SAN_IP_PY" < "$tmp0" > "$tmp2"
  else
    # 其他类型先做通用净化
    awk -f "$SAN_AWK" "$tmp0" > "$tmp1"

    if [ "$typ" = "domain" ] || is_domain_list "$tmp1"; then
      if [ "$typ" != "domain" ]; then
        echo "Auto-detect: looks like domain list, override to domain for $fn"
      fi
      awk -f "$SAN_DOMAIN_AWK" "$tmp1" > "$tmp2"
    elif is_ipcidr_list "$tmp1"; then
      echo "Auto-detect: looks like IP/CIDR list, override to ipcidr for $fn"
      # 即便自动识别为 ipcidr，也用 tmp0 作为源喂给解析器，避免通用净化的副作用
      python3 "$SAN_IP_PY" < "$tmp0" > "$tmp2"
    else
      cp "$tmp1" "$tmp2"
    fi
  fi

  # 去重保持顺序
  awk '!seen[$0]++' "$tmp2" > "$out"

  rm -f "${out}.download" "$tmp0" "$tmp1" "$tmp2"
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