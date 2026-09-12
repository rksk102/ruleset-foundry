import base64
import binascii
import ipaddress
import re
import sys


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
    has_payload = any(yaml_payload_pattern.match(l) for l in content_lines[:50])

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
    """清洗域名并统计被丢弃/被放宽的特殊行。返回 (结果列表, stats)。"""
    valid_domains = set()
    stats = {
        "dropped_exception": 0,
        "dropped_keyword": 0,
        "widened_exact": 0,
        "bad_anchor": 0,
    }
    ip_check = re.compile(r'^\d{1,3}(\.\d{1,3}){3}$')

    prefixes = [
        'full:', 'domain:', 'host:',
        'domain-suffix:', '+.'
    ]

    for item in lines:
        s = item.lower().strip()
        if not s: continue

        if s.startswith('@@'):
            stats["dropped_exception"] += 1
            continue

        # 关键字/正则规则不当作域名处理
        if s.startswith(('keyword:', 'domain-keyword:', 'regexp:')):
            stats["dropped_keyword"] += 1
            continue

        # full:/host: 本为精确匹配，此处被放宽为 suffix，计入统计
        if s.startswith(('full:', 'host:')):
            stats["widened_exact"] += 1

        for prefix in prefixes:
            if s.startswith(prefix):
                s = s[len(prefix):]
                break

        parts = s.split()
        if len(parts) >= 2:
            if parts[0] in ['127.0.0.1', '0.0.0.0', '::1']:
                s = parts[1]

        if s.startswith('||'): s = s[2:]
        # 先剥 AdBlock 修饰符，再处理锚点，避免 "^$modifier" 中锚点落空被丢弃
        if '$' in s: s = s.split('$')[0]
        if '^' in s:
            stats["bad_anchor"] += 1
            s = s.replace('^', '')
        s = re.sub(r'^(\*\.|\+\.|\.)', '', s)
        if '/' in s: s = s.split('/')[0]
        if ':' in s: s = s.split(':')[0]
        if not s or '.' not in s: continue
        if ' ' in s: continue
        if ip_check.match(s): continue

        # 拒绝空标签、首尾点、连续点、首尾连字符
        if s.startswith('.') or s.endswith('.') or '..' in s: continue
        labels = s.split('.')
        if not all(
            label
            and label[0] != '-'
            and label[-1] != '-'
            and all(c.isalnum() or c in '-_' for c in label)
            for label in labels
        ):
            continue

        valid_domains.add(s)

    return sorted(valid_domains), stats


def analyze_domain(lines):
    """统计特殊行（用于可观测性告警），不改变清洗结果。"""
    return _analyze_and_process_domain(lines)[1]


def process_domain(lines):
    return _analyze_and_process_domain(lines)[0]

def process_ip(lines):
    v4_nets = []
    v6_nets = []
    regex_ip = re.compile(r'([0-9a-fA-F:.]+(?:/[0-9]+)?)')

    for item in lines:
        m = regex_ip.search(item)
        if not m: continue
        ip_str = m.group(1)
        try:
            net = ipaddress.ip_network(ip_str, strict=False)
            if net.prefixlen == 0: continue
            if net.version == 4:
                v4_nets.append(net)
            else:
                v6_nets.append(net)
        except ValueError:
            continue

    merged_v4 = ipaddress.collapse_addresses(v4_nets)
    merged_v6 = ipaddress.collapse_addresses(v6_nets)

    final_list = []
    final_list.extend(str(n) for n in merged_v4)
    final_list.extend(str(n) for n in merged_v6)

    return final_list

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
        result = process_ip(lines)
    else:
        result = process_domain(lines)

    for line in result:
        print(line)

if __name__ == '__main__':
    main()
