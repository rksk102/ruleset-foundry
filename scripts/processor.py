import base64
import binascii
import re
import sys

import utils

_RULE_TYPE_RE = re.compile(
    r'(DOMAIN-SUFFIX|HOST-SUFFIX|DOMAIN|HOST|FULL|DOMAIN-WILDCARD|'
    r'IP-CIDR6|IP-CIDR|SRC-IP-CIDR|DST-IP-CIDR|IP-SUFFIX|IP-ASN|SRC-IP-ASN|DST-IP-ASN|'
    r'GEOIP|GEOSITE|SRC-GEOIP|DST-GEOIP|'
    r'DOMAIN-KEYWORD|DOMAIN-REGEX|PROCESS-NAME|PROCESS-PATH|PROCESS-NAME-REGEX|'
    r'PROCESS-PATH-REGEX|RULE-SET|SUB-RULE|MATCH|NETWORK|DST-PORT|SRC-PORT|IN-TYPE|'
    r'IN-USER|IN-NAME|IN-PORT|SRC-PORT-RANGE|DST-PORT-RANGE|SCRIPT)'
    r'\s*,\s*(.*)$',
    re.IGNORECASE,
)

_SUFFIX_TYPES = {"DOMAIN-SUFFIX", "HOST-SUFFIX"}
_EXACT_TYPES = {"DOMAIN", "HOST", "FULL"}
_IP_TYPES = {
    "IP-CIDR", "IP-CIDR6", "SRC-IP-CIDR", "DST-IP-CIDR", "IP-SUFFIX",
    "IP-ASN", "SRC-IP-ASN", "DST-IP-ASN",
}
_OPAQUE_TYPES = {
    "DOMAIN-KEYWORD", "DOMAIN-REGEX", "PROCESS-NAME", "PROCESS-PATH",
    "PROCESS-NAME-REGEX", "PROCESS-PATH-REGEX", "RULE-SET", "SUB-RULE", "MATCH",
    "NETWORK", "DST-PORT", "SRC-PORT", "IN-TYPE", "IN-USER", "IN-NAME", "IN-PORT",
    "SRC-PORT-RANGE", "DST-PORT-RANGE", "SCRIPT", "GEOIP", "GEOSITE",
    "SRC-GEOIP", "DST-GEOIP", "DOMAIN-WILDCARD",
}


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
    if type_name in _IP_TYPES:
        return "ip", payload, type_name
    if type_name in _OPAQUE_TYPES:
        return "opaque", payload, type_name
    return None, None, None


def new_stats():
    return {
        "suffix": 0,
        "exact": 0,
        "relaxed_exact": 0,
        "wildcard": 0,
        "bare_single_label": 0,
        "ip_in_domain": 0,
        "dropped_exception": 0,
        "dropped_keyword": 0,
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
    if '\0' in text: return False
    non_printable = sum(1 for c in text if not c.isprintable() and c not in '\r\n\t')
    if len(text) > 0 and (non_printable / len(text)) > 0.3:
        return False
    return True

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

def parse_lines(raw_content):
    content = explicit_base64_decode(raw_content)
    lines = []

    in_payload = False
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


def _analyze_and_process_domain(lines):
    valid_domains = set()
    stats = new_stats()
    ip_check = re.compile(r'^\d{1,3}(\.\d{1,3}){3}$')

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
        if s.startswith('*.'):
            s = re.sub(r'\s+', '', s)
            if not s.endswith('.'):
                stats["wildcard"] += 1
                valid_domains.add(s)
                continue
        if s.startswith('+.'):
            semantic = 'suffix'
            s = s[2:]
        elif s.startswith('.'):
            semantic = semantic or 'suffix'
            s = s.lstrip('.')

        parts = s.split()
        if len(parts) >= 2 and parts[0] in ('127.0.0.1', '0.0.0.0', '::1'):
            s = parts[1]

        if s.startswith('||'): s = s[2:]
        if '$' in s: s = s.split('$')[0]
        if '^' in s: s = s.replace('^', '')
        s = re.sub(r'^(\*\.|\+\.|\.)', '', s)
        if '/' in s: s = s.split('/')[0]
        if ':' in s: s = s.split(':')[0]

        s = s.strip().lower()
        if not s:
            stats["unrecognized"] += 1
            continue
        if '.' not in s:
            valid = all(c.isalnum() or c in '-_' for c in s) and s[0] != '-' and s[-1] != '-'
            if valid and semantic == 'suffix':
                stats["suffix"] += 1
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
        if ip_check.match(s):
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
        else:
            stats["relaxed_exact"] += 1
            valid_domains.add(s)

    return sorted(valid_domains), stats


def process_domain_detailed(lines):
    return _analyze_and_process_domain(lines)


def analyze_domain(lines):
    return _analyze_and_process_domain(lines)[1]


def process_domain(lines):
    return _analyze_and_process_domain(lines)[0]


def process_ip(lines):
    result, errors = utils.flatten_ip_cidr(lines, extract=True)
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
        result, errors = process_ip(lines)
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
