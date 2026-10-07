import base64
import binascii
import ipaddress
import re
import sys

import utils

_SUFFIX_TYPES = {"DOMAIN-SUFFIX", "HOST-SUFFIX"}
_EXACT_TYPES = {"DOMAIN", "HOST", "FULL"}
_CIDR_TYPES = {"IP-CIDR", "IP-CIDR6"}
_UNEXPRESSIBLE_IP_TYPES = {"SRC-IP-CIDR", "IP-SUFFIX", "SRC-IP-SUFFIX", "IP-ASN", "SRC-IP-ASN"}
_UNSUPPORTED_TYPES = {
    "DST-IP-CIDR", "DST-IP-ASN", "DST-GEOIP", "SCRIPT",
    "SRC-PORT-RANGE", "DST-PORT-RANGE",
}
_OPAQUE_TYPES = {
    "DOMAIN-KEYWORD", "DOMAIN-REGEX", "DOMAIN-WILDCARD",
    "PROCESS-NAME", "PROCESS-PATH", "PROCESS-NAME-REGEX", "PROCESS-PATH-REGEX",
    "PROCESS-NAME-WILDCARD", "PROCESS-PATH-WILDCARD",
    "RULE-SET", "SUB-RULE", "MATCH", "NETWORK", "DST-PORT", "SRC-PORT",
    "IN-TYPE", "IN-USER", "IN-NAME", "IN-PORT", "REMATCH-NAME",
    "UID", "DSCP", "AND", "OR", "NOT", "GEOIP", "GEOSITE", "SRC-GEOIP",
}

_MIHOMO_RULE_TYPES = (
    _SUFFIX_TYPES | _EXACT_TYPES | _CIDR_TYPES
    | _UNEXPRESSIBLE_IP_TYPES | _OPAQUE_TYPES
)

_RULE_TYPE_RE = re.compile(
    r'(' + '|'.join(sorted(_MIHOMO_RULE_TYPES | _UNSUPPORTED_TYPES, key=len, reverse=True)) + r')'
    r'\s*,\s*(.*)$',
    re.IGNORECASE,
)


def classify_rule_line(line):
    m = _RULE_TYPE_RE.match(line)
    if not m:
        return None, None, None
    type_name = m.group(1).upper()
    payload = m.group(2).strip()
    for mod in ("no-resolve", "src"):
        if payload.lower().endswith("," + mod):
            payload = payload[: -(len(mod) + 1)].strip()
    if type_name in _SUFFIX_TYPES:
        return "suffix", payload, type_name
    if type_name in _EXACT_TYPES:
        return "exact", payload, type_name
    if type_name in _CIDR_TYPES:
        return "ip", payload, type_name
    return "opaque", payload, type_name


def _is_ip_literal(value):
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def _valid_wildcard(value):
    if value.endswith('.') or value.startswith('*.') is False or any(c.isspace() for c in value):
        return False
    if '/' in value:
        return False
    labels = value.split('.')
    if len(labels) < 2 or labels[0] != '*':
        return False
    for label in labels[1:]:
        if label == '*':
            continue
        if not label or label[0] == '-' or label[-1] == '-' or not all(
                c.isalnum() or c in '-_' for c in label):
            return False
    return True


def ipcidr_drop_reason(type_name):
    if type_name == "SRC-IP-CIDR":
        return ("SRC-IP-CIDR 的 src 语义由引用侧 RULE-SET,...,src 决定，"
                "规则集文件本身无法表达")
    if type_name in _UNEXPRESSIBLE_IP_TYPES:
        return f"mihomo 的 {type_name} 无法用 ipcidr 规则集表达（载荷不是 CIDR）"
    if type_name in _UNSUPPORTED_TYPES:
        return f"mihomo 不支持 {type_name}，无法用 ipcidr 规则集表达"
    return f"{type_name} 不是 IP 规则，无法用 ipcidr 规则集表达"


def new_stats():
    return {
        "suffix": 0,
        "subdomain": 0,
        "relaxed_exact": 0,
        "wildcard": 0,
        "bare_single_label": 0,
        "ip_in_domain": 0,
        "dropped_exception": 0,
        "dropped_keyword": 0,
        "dropped_default_route": 0,
        "dropped_rule_type": {},
        "unrecognized": 0,
    }


def safe_decode(binary_data):
    for codec in ['utf-8', 'gb18030', 'latin1']:
        try:
            return binary_data.decode(codec).strip()
        except Exception:
            continue
    return ""

def is_text_data(text):
    if '\0' in text:
        return False
    non_printable = sum(1 for c in text if not c.isprintable() and c not in '\r\n\t')
    return not (text and non_printable / len(text) > 0.3)

def explicit_base64_decode(text):
    s = text.replace('\n', '').replace('\r', '').strip()
    if ' ' in s or len(s) < 20: return text

    try:
        decoded_bytes = base64.b64decode(s, validate=True)
        decoded_str = safe_decode(decoded_bytes)
        if is_text_data(decoded_str):
            return decoded_str
    except (binascii.Error, ValueError):
        pass
    return text

def _yaml_payload_lines(content):
    try:
        import yaml
    except Exception:
        return None
    try:
        data = yaml.safe_load(content)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    for key in ("payload", "rules"):
        value = data.get(key)
        if isinstance(value, list) and all(isinstance(x, str) for x in value):
            return [x.strip() for x in value if x.strip()]
    return None


_YAML_HEAD_RE = re.compile(r'^\s*(?:payload|rules):', re.IGNORECASE)


def parse_lines(raw_content):
    content = explicit_base64_decode(raw_content)
    yaml_payload_pattern = re.compile(r'^\s*payload:', re.IGNORECASE)
    content_lines = content.splitlines()
    probe = []
    for raw in content_lines:
        s = raw.strip()
        if not s or s.startswith('#') or s.startswith('!'):
            continue
        probe.append(s)
        if len(probe) >= 50:
            break

    if any(_YAML_HEAD_RE.match(l) for l in probe):
        yaml_lines = _yaml_payload_lines(content)
        if yaml_lines is not None:
            return yaml_lines

    lines = []
    in_payload = False
    has_payload = any(yaml_payload_pattern.match(l) for l in probe)

    for line in content_lines:
        line = line.strip()
        if not line: continue
        if line.startswith('#') or line.startswith('!'): continue
        if ' #' in line: line = line.split(' #')[0].strip()

        if has_payload:
            if yaml_payload_pattern.match(line):
                in_payload = True
                m = re.search(r'\[(.*)\]', line)
                if m:
                    for x in m.group(1).split(','):
                        lines.append(x.strip("'\" "))
                continue

            if in_payload:
                if re.match(r'^[a-zA-Z0-9_-]+:', line):
                    in_payload = False
                    continue
                if line.startswith('- '):
                    lines.append(line[2:].strip("'\" "))
                elif line.startswith('-'):
                    lines.append(line[1:].strip("'\" "))
            continue

        if line.startswith('- '):
            lines.append(line[2:].strip("'\" "))
        else:
            lines.append(line.strip("'\" "))

    return lines


def _analyze_and_process_domain(lines, domain_kind="exact"):
    valid_domains = set()
    stats = new_stats()
    stats["suffix_promoted"] = 0

    prefix_rules = (
        ('domain-suffix:', 'suffix'),
        ('domain:', 'suffix'),
        ('full:', 'exact'),
        ('host:', 'exact'),
    )

    for item in lines:
        s = item.strip()
        if not s: continue
        lowered = s.lower()

        if lowered.startswith('@@'):
            stats["dropped_exception"] += 1
            continue

        if lowered.startswith(('keyword:', 'domain-keyword:', 'regexp:', 'include:', 'ext:')):
            stats["dropped_keyword"] += 1
            continue

        semantic = None
        for prefix, kind in prefix_rules:
            if lowered.startswith(prefix):
                semantic = kind
                s = s[len(prefix):]
                break

        if semantic is None:
            if lowered.startswith('+.'):
                semantic = 'suffix'
            elif s.startswith('*.'):
                semantic = 'wildcard'

        kind, payload, type_name = classify_rule_line(s)
        if kind == 'suffix':
            semantic, s = 'suffix', payload
        elif kind == 'exact':
            semantic, s = 'exact', payload
        elif kind == 'ip':
            stats["ip_in_domain"] += 1
            continue
        elif kind == 'opaque':
            stats["dropped_rule_type"][type_name] = \
                stats["dropped_rule_type"].get(type_name, 0) + 1
            continue

        s = s.strip()
        if re.match(r'^\*\.', s):
            if not _valid_wildcard(s):
                stats["unrecognized"] += 1
                continue
            stats["wildcard"] += 1
            valid_domains.add(s.lower())
            continue
        if domain_kind == 'suffix' and semantic is None and s.startswith('+.'):
            semantic = 'suffix'
        if s.startswith('+.'):
            semantic = 'suffix'
            s = s[2:]
        elif s.startswith('.'):
            semantic = 'subdomain'
            s = s.lstrip('.')

        parts = s.split()
        if len(parts) >= 2 and parts[0] in ('127.0.0.1', '0.0.0.0', '::1'):
            s = parts[1]

        if s.startswith('||'): s = s[2:]
        if '$' in s: s = s.split('$')[0]
        if '^' in s: s = s.replace('^', '')
        s = re.sub(r'^(\+\.)', '', s)
        if '/' in s: s = s.split('/')[0]
        s = s.strip('[]')
        if ':' in s:
            head, _, port = s.rpartition(':')
            if port.isdigit() and '.' in head:
                s = head

        s = s.strip().lower()
        if not s:
            stats["unrecognized"] += 1
            continue
        if _is_ip_literal(s):
            stats["unrecognized"] += 1
            continue
        if '.' not in s:
            valid = all(c.isalnum() or c in '-_' for c in s) and s[0] != '-' and s[-1] != '-'
            if valid and semantic == 'suffix':
                stats["suffix"] += 1
                valid_domains.add('+.' + s)
                continue
            if valid and semantic == 'subdomain':
                stats["subdomain"] += 1
                valid_domains.add('.' + s)
                continue
            if valid and semantic is None and domain_kind == 'suffix':
                stats["suffix_promoted"] += 1
                valid_domains.add('+.' + s)
                continue
            if valid:
                stats["bare_single_label"] += 1
                valid_domains.add(s)
                continue
            stats["unrecognized"] += 1
            continue
        if ' ' in s:
            stats["unrecognized"] += 1
            continue
        if '*' in s:
            stats["unrecognized"] += 1
            continue
        if s.startswith('.') or s.endswith('.') or '..' in s:
            stats["unrecognized"] += 1
            continue

        labels = s.split('.')
        if not all(
            label
            and label[0] != '-'
            and label[-1] != '-'
            and all(c.isalnum() or c in '-_' for c in label)
            for label in labels
        ):
            stats["unrecognized"] += 1
            continue

        if semantic == 'suffix':
            stats["suffix"] += 1
            valid_domains.add('+.' + s)
        elif semantic == 'subdomain':
            stats["subdomain"] += 1
            valid_domains.add('.' + s)
        elif semantic is None and domain_kind == 'suffix':
            stats["suffix_promoted"] += 1
            valid_domains.add('+.' + s)
        else:
            stats["relaxed_exact"] += 1
            valid_domains.add(s)

    return sorted(valid_domains), stats


def process_domain_detailed(lines, domain_kind="exact"):
    return _analyze_and_process_domain(lines, domain_kind)


def process_ip_detailed(lines):
    candidates = []
    stats = new_stats()
    dropped_default_routes = []
    for line in lines:
        kind, payload, type_name = classify_rule_line(line)
        if kind is None:
            candidates.append(line)
            continue
        if kind == "ip":
            if payload:
                candidates.append(payload)
            else:
                stats["unrecognized"] += 1
            continue
        stats["dropped_rule_type"][type_name] = \
            stats["dropped_rule_type"].get(type_name, 0) + 1

    result, errors = utils.flatten_ip_cidr(
        candidates, extract=True, dropped_default_routes=dropped_default_routes)
    stats["dropped_default_route"] = len(dropped_default_routes)
    return result, errors, stats


def process_ip(lines):
    result, errors, _stats = process_ip_detailed(lines)
    return result, errors

def main():
    mode = "domain"
    if len(sys.argv) > 1:
        mode = sys.argv[1]

    try:
        raw_bytes = sys.stdin.buffer.read()
    except Exception:
        return

    if not raw_bytes:
        return

    content = safe_decode(raw_bytes)
    lines = parse_lines(content)

    if mode == 'ipcidr':
        result, errors, stats = process_ip_detailed(lines)
        if stats["dropped_default_route"]:
            print(f"# 丢弃默认路由(/0) 规则 {stats['dropped_default_route']} 行",
                  file=sys.stderr)
        for type_name, count in sorted(stats["dropped_rule_type"].items()):
            print(f"# 丢弃 {type_name} 规则 {count} 行: {ipcidr_drop_reason(type_name)}",
                  file=sys.stderr)
        if stats.get("unrecognized"):
            print(f"# 丢弃空载荷规则: {stats['unrecognized']} 行", file=sys.stderr)
        for bad, why in errors:
            print(f"# 丢弃无效 CIDR: {bad} -> {why}", file=sys.stderr)
    else:
        result, stats = process_domain_detailed(lines)
        if stats.get("unrecognized"):
            print(f"# 未识别行: {stats['unrecognized']}", file=sys.stderr)

    for line in result:
        print(line)

if __name__ == '__main__':
    main()
