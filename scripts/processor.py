import base64
import binascii
import ipaddress
import re
import sys
from urllib.parse import urlsplit

import utils

_SUFFIX_TYPES = {"DOMAIN-SUFFIX", "HOST-SUFFIX"}
_EXACT_TYPES = {"DOMAIN", "HOST", "FULL"}
_CIDR_TYPES = {"IP-CIDR", "IP-CIDR6"}
_UNEXPRESSIBLE_IP_TYPES = {"SRC-IP-CIDR", "IP-SUFFIX", "SRC-IP-SUFFIX", "IP-ASN", "SRC-IP-ASN"}
_UNSUPPORTED_TYPES = {
    "DST-IP-CIDR",
    "DST-IP-ASN",
    "DST-GEOIP",
    "SCRIPT",
    "SRC-PORT-RANGE",
    "DST-PORT-RANGE",
}
_OPAQUE_TYPES = {
    "DOMAIN-KEYWORD",
    "DOMAIN-REGEX",
    "DOMAIN-WILDCARD",
    "PROCESS-NAME",
    "PROCESS-PATH",
    "PROCESS-NAME-REGEX",
    "PROCESS-PATH-REGEX",
    "PROCESS-NAME-WILDCARD",
    "PROCESS-PATH-WILDCARD",
    "RULE-SET",
    "SUB-RULE",
    "MATCH",
    "NETWORK",
    "DST-PORT",
    "SRC-PORT",
    "IN-TYPE",
    "IN-USER",
    "IN-NAME",
    "IN-PORT",
    "REMATCH-NAME",
    "UID",
    "DSCP",
    "AND",
    "OR",
    "NOT",
    "GEOIP",
    "GEOSITE",
    "SRC-GEOIP",
}

_MIHOMO_RULE_TYPES = _SUFFIX_TYPES | _EXACT_TYPES | _CIDR_TYPES | _UNEXPRESSIBLE_IP_TYPES | _OPAQUE_TYPES
_ALL_RULE_TYPE_NAMES = _MIHOMO_RULE_TYPES | _UNSUPPORTED_TYPES

_RULE_TYPE_RE = re.compile(
    r"(" + "|".join(sorted(_ALL_RULE_TYPE_NAMES, key=len, reverse=True)) + r")"
    r"\s*,\s*(.*)$",
    re.IGNORECASE,
)


def classify_rule_line(line):
    m = _RULE_TYPE_RE.match(line)
    if not m:
        return None, None, None
    type_name = m.group(1).upper()
    payload = m.group(2).strip()
    if type_name in _CIDR_TYPES:
        parts = [part.strip() for part in payload.split(",")]
        if any(part.lower() == "src" for part in parts[1:]):
            return "opaque", parts[0], "SRC-IP-CIDR"
        payload = parts[0]
    elif type_name in _SUFFIX_TYPES or type_name in _EXACT_TYPES:
        payload = payload.split(",", 1)[0].strip()
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


def _idna_labels(labels):
    try:
        encoded = [label.encode("idna") if not label.isascii() else label.encode("ascii") for label in labels]
    except (UnicodeError, UnicodeDecodeError):
        return None
    if any(len(label) > 63 for label in encoded) or sum(len(label) + 1 for label in encoded) + 1 > 255:
        return None
    return encoded


def _valid_wildcard(value):
    if value.endswith(".") or value.startswith("*.") is False or any(c.isspace() for c in value):
        return False
    if "/" in value:
        return False
    labels = value.split(".")
    if len(labels) < 2 or labels[0] != "*":
        return False
    seen_entity = False
    for label in labels[1:]:
        if label == "*":
            if seen_entity:
                return False
            continue
        seen_entity = True
        if not label or label[0] == "-" or label[-1] == "-" or not all(c.isalnum() or c in "-_" for c in label):
            return False
    if not seen_entity:
        return False
    return not labels[-1].isdigit() and _idna_labels([label for label in labels if label != "*"]) is not None


def ipcidr_drop_reason(type_name):
    if type_name == "SRC-IP-CIDR":
        return "SRC-IP-CIDR 的 src 语义由引用侧 RULE-SET,...,src 决定，规则集文件本身无法表达"
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
        "dropped_modifier": 0,
        "dropped_path": 0,
        "dropped_keyword": 0,
        "dropped_default_route": 0,
        "extracted_url": 0,
        "dropped_rule_type": {},
        "unrecognized": 0,
    }


def safe_decode(binary_data):
    for codec in ["utf-8", "gb18030", "latin1"]:
        try:
            return binary_data.decode(codec).strip()
        except Exception:
            continue
    return ""


def is_text_data(text):
    if "\0" in text:
        return False
    non_printable = sum(1 for c in text if not c.isprintable() and c not in "\r\n\t")
    return not (text and non_printable / len(text) > 0.3)


def explicit_base64_decode(text):
    if len(text) < 20 or re.search(r"[^A-Za-z0-9+/=\s]", text[:256]):
        return text
    s = text.replace("\n", "").replace("\r", "").strip()
    if " " in s or len(s) < 20:
        return text

    try:
        decoded_bytes = base64.b64decode(s, validate=True)
        decoded_str = safe_decode(decoded_bytes)
        has_yaml_head = any(_YAML_HEAD_RE.match(line) for line in decoded_str.splitlines())
        if is_text_data(decoded_str) and (
            "\n" in decoded_str
            or has_yaml_head
            or _split_semantic_prefix(decoded_str)[0] is not None
            or classify_rule_line(decoded_str)[0] in ("suffix", "exact")
        ):
            return decoded_str
    except (binascii.Error, ValueError):
        pass
    return text


class YamlPayloadError(ValueError):
    pass


class ParsedLines(list):
    def __init__(self, values=(), yaml_errors=0):
        super().__init__(values)
        self.yaml_errors = yaml_errors


def _yaml_payload_lines(content):
    try:
        import yaml
    except Exception as exc:
        raise YamlPayloadError("YAML 输入需要 PyYAML 安全解析") from exc
    try:
        data = yaml.load(content, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))
    except Exception as exc:
        raise YamlPayloadError(f"YAML 结构无效: {exc}") from exc
    if not isinstance(data, dict) or any(key not in ("payload", "rules", "behavior") for key in data):
        raise YamlPayloadError("YAML 顶层必须仅含 payload 或 rules 列表")
    for key in ("payload", "rules"):
        if key not in data:
            continue
        value = data[key]
        if not isinstance(value, list):
            raise YamlPayloadError(f"YAML {key} 必须是列表")
        result = ParsedLines()
        for item in value:
            if not isinstance(item, str):
                result.yaml_errors += 1
                continue
            cleaned = item.split(" #", 1)[0].strip()
            if cleaned:
                result.append(cleaned)
            else:
                result.yaml_errors += 1
        return result
    raise YamlPayloadError("YAML 缺少 payload 或 rules 列表")


_YAML_HEAD_RE = re.compile(r"^\s*(?:payload|rules):", re.IGNORECASE)

_HOSTS_LOCAL_PREFIXES = ("127.0.0.1", "0.0.0.0", "::1")

_SEMANTIC_PREFIXES = (
    ("domain-suffix:", "suffix"),
    ("domain:", "suffix"),
    ("full:", "exact"),
    ("host:", "exact"),
)

_HOSTS_COMMENT_RE = re.compile(r"\s#")


class _HostsField(str):
    """hosts 行展开出的单个候选。

    字符串值保留原前缀以维持 parse_lines 契约；语义和原始字段单独保存，
    防止字段里的 full:/domain: 等文本被二次解释为规则语法。
    """

    def __new__(cls, value, semantic, field):
        entry = super().__new__(cls, value)
        entry.semantic = semantic
        entry.field = field
        return entry


def _split_semantic_prefix(text):
    """拆出 full:/host:/domain:/domain-suffix: 前缀，返回 (语义, 剩余文本)。"""
    lowered = text.lower()
    for prefix, kind in _SEMANTIC_PREFIXES:
        if lowered.startswith(prefix):
            return kind, text[len(prefix) :]
    return None, text


def _split_hosts_line(line):
    """把 hosts 行拆成 (语义前缀原文, 语义, hostname 列表)；不是 hosts 行时返回 None。

    行内注释以空白（含 tab）后的 # 起始，注释内容不进入候选。只有“本地地址 + 至少一个
    hostname”才是 hosts 行，裸本地地址行保持原样交给后续清洗。
    """
    text = line.strip()
    comment = _HOSTS_COMMENT_RE.search(text)
    if comment:
        text = text[: comment.start()].rstrip()
    prefix = ""
    semantic = None
    for candidate, kind in _SEMANTIC_PREFIXES:
        if text[: len(candidate)].lower() == candidate:
            prefix = text[: len(candidate)]
            semantic = kind
            text = text[len(candidate) :]
            break
    parts = text.split()
    if len(parts) < 2 or parts[0] not in _HOSTS_LOCAL_PREFIXES:
        return None
    return prefix, semantic, parts[1:]


def _expand_hosts_entries(lines):
    """hosts 行（[语义前缀] 本地地址 + hostname...）展开为逐 hostname 的 _HostsField。

    每个 hostname 保留原语义前缀，因此 exact/suffix 语义逐个生效，分母按 hostname 计；
    每个字段恰好进入输出或计入一次丢弃，保持“输入 hostname 守恒”。非 hosts 行原样保留，
    没有 hosts 行时不复制列表。
    """
    expanded = None
    for index, line in enumerate(lines):
        entry = _split_hosts_line(line)
        if entry is None:
            if expanded is not None:
                expanded.append(line)
            continue
        prefix, semantic, hostnames = entry
        if expanded is None:
            expanded = list(lines[:index])
        expanded.extend(_HostsField(prefix + name, semantic, name) for name in hostnames)
    if expanded is None:
        return lines
    result = ParsedLines(expanded, yaml_errors=getattr(lines, "yaml_errors", 0))
    return result


def parse_lines(raw_content):
    content = explicit_base64_decode(raw_content)
    content_lines = content.splitlines()
    probe = []
    for raw in content_lines:
        s = raw.strip()
        if not s or s.startswith("#") or s.startswith("!"):
            continue
        probe.append(s)
        if len(probe) >= 50:
            break

    if any(_YAML_HEAD_RE.match(l) for l in probe):
        yaml_lines = _yaml_payload_lines(content)
        return _expand_hosts_entries(yaml_lines)

    lines = ParsedLines()
    for line in content_lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("#") or line.startswith("!"):
            continue
        if " #" in line:
            line = line.split(" #")[0].strip()

        if line.startswith("- "):
            lines.append(line[2:].strip("'\" "))
        else:
            lines.append(line.strip("'\" "))

    return _expand_hosts_entries(lines)


def _record_domain(s, semantic, domain_kind, stats, valid_domains):
    """把已确定语义的候选记入统计与输出集合；不能作为域名识别的计入一次未识别。"""
    s = s.strip().lower()
    if not s:
        stats["unrecognized"] += 1
        return
    if _is_ip_literal(s):
        stats["unrecognized"] += 1
        return
    if "." not in s:
        if s.upper() in _ALL_RULE_TYPE_NAMES:
            stats["unrecognized"] += 1
            return
        valid = all(c.isalnum() or c in "-_" for c in s) and s[0] != "-" and s[-1] != "-"
        if not valid or _idna_labels([s]) is None:
            stats["unrecognized"] += 1
        elif semantic == "suffix":
            stats["suffix"] += 1
            valid_domains.add("+." + s)
        elif semantic == "subdomain":
            stats["subdomain"] += 1
            valid_domains.add("." + s)
        elif semantic is None and domain_kind == "suffix":
            stats["suffix_promoted"] += 1
            valid_domains.add("+." + s)
        else:
            stats["bare_single_label"] += 1
            valid_domains.add(s)
        return
    if " " in s or "*" in s:
        stats["unrecognized"] += 1
        return
    if s.startswith(".") or s.endswith(".") or ".." in s:
        stats["unrecognized"] += 1
        return

    labels = s.split(".")
    if not all(
        label and label[0] != "-" and label[-1] != "-" and all(c.isalnum() or c in "-_" for c in label)
        for label in labels
    ) or _idna_labels(labels) is None:
        stats["unrecognized"] += 1
        return

    if semantic == "suffix":
        stats["suffix"] += 1
        valid_domains.add("+." + s)
    elif semantic == "subdomain":
        stats["subdomain"] += 1
        valid_domains.add("." + s)
    elif semantic is None and domain_kind == "suffix":
        stats["suffix_promoted"] += 1
        valid_domains.add("+." + s)
    else:
        stats["relaxed_exact"] += 1
        valid_domains.add(s)


def _extract_url_hostname(value):
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in ("http", "https") or parsed.username is not None or parsed.password is not None:
            return None
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            return None
        return parsed.hostname
    except ValueError:
        return None


def _analyze_and_process_domain(lines, domain_kind="exact"):
    valid_domains = set()
    stats = new_stats()
    stats["suffix_promoted"] = 0
    stats["unrecognized"] = getattr(lines, "yaml_errors", 0)

    for item in _expand_hosts_entries(lines):
        s = item.strip()
        if not s:
            continue

        if isinstance(item, _HostsField):
            # 只消费原行语义和原始字段，字段里的前缀文本不再是语法
            _record_domain(item.field, item.semantic, domain_kind, stats, valid_domains)
            continue

        lowered = s.lower()

        if lowered.startswith("@@"):
            stats["dropped_exception"] += 1
            continue

        if lowered.startswith(("keyword:", "domain-keyword:", "regexp:", "include:", "ext:")):
            stats["dropped_keyword"] += 1
            continue

        semantic, s = _split_semantic_prefix(s)
        explicit_semantic = semantic is not None

        if semantic is None:
            if lowered.startswith("+."):
                semantic = "suffix"
            elif s.startswith("*."):
                semantic = "wildcard"

        kind, payload, type_name = classify_rule_line(s)
        if kind in ("suffix", "exact"):
            if semantic is not None and semantic != kind:
                stats["unrecognized"] += 1
                continue
            semantic, s = kind, payload
        elif kind == "ip":
            stats["ip_in_domain"] += 1
            continue
        elif kind == "opaque":
            stats["dropped_rule_type"][type_name] = stats["dropped_rule_type"].get(type_name, 0) + 1
            continue

        s = s.strip()
        if semantic is None and kind is None and re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", s):
            hostname = _extract_url_hostname(s)
            if hostname is None:
                stats["unrecognized"] += 1
                continue
            stats["extracted_url"] += 1
            _record_domain(hostname, None, domain_kind, stats, valid_domains)
            continue
        if s.startswith("//"):
            stats["dropped_path"] += 1
            continue
        if re.match(r"^\*\.", s):
            if semantic in ("exact", "suffix") or not _valid_wildcard(s):
                stats["unrecognized"] += 1
                continue
            semantic = "wildcard"
            stats["wildcard"] += 1
            valid_domains.add(s.lower())
            continue
        if s.startswith("+."):
            if semantic == "exact":
                stats["unrecognized"] += 1
                continue
            semantic = "suffix"
            s = s[2:]
        elif s.startswith("."):
            if semantic == "exact":
                stats["unrecognized"] += 1
                continue
            if semantic == "suffix":
                s = s[1:]
            else:
                semantic = "subdomain"
                s = s[1:]

        adblock_anchor = s.startswith("||")
        if adblock_anchor:
            if semantic is not None and semantic != "suffix":
                stats["unrecognized"] += 1
                continue
            s = s[2:]
            # ABP 的 || 锚点匹配根域及其子域，对应 mihomo 的 +.
            semantic = "suffix"
        if "$" in s:
            modifiers = s.split("$", 1)[1]
            names = {mod.split("=", 1)[0].strip().lower() for mod in modifiers.split(",")}
            if "badfilter" in names:
                stats["dropped_exception"] += 1
            else:
                stats["dropped_modifier"] += 1
            continue
        if adblock_anchor and "/" in s:
            stats["dropped_path"] += 1
            continue
        if adblock_anchor and s.endswith("^"):
            s = s[:-1]
        if "^" in s:
            stats["dropped_path"] += 1
            continue
        if s.startswith("+."):
            if semantic == "exact":
                stats["unrecognized"] += 1
                continue
            semantic = "suffix"
            s = s[2:]
        if "/" in s:
            ip_candidate = s.strip("[]").split("/", 1)[0]
            if _is_ip_literal(s) or _is_ip_literal(ip_candidate) or explicit_semantic or kind in ("suffix", "exact"):
                stats["unrecognized"] += 1
            else:
                stats["dropped_path"] += 1
            continue
        s = s.strip("[]")
        if ":" in s:
            if adblock_anchor:
                stats["dropped_path"] += 1
                continue
            head, _, port = s.rpartition(":")
            if port.isdigit() and "." in head:
                s = head

        _record_domain(s, semantic, domain_kind, stats, valid_domains)

    return sorted(valid_domains), stats


def process_domain_detailed(lines, domain_kind="exact"):
    return _analyze_and_process_domain(lines, domain_kind)


def process_ip_detailed(lines):
    candidates = []
    stats = new_stats()
    stats["unrecognized"] = getattr(lines, "yaml_errors", 0)
    dropped_default_routes = []
    for line in _expand_hosts_entries(lines):
        if isinstance(line, _HostsField):
            # hosts 字段不是规则语法：逐字段交给 CIDR 解析报错，而非重新分类
            candidates.append(line)
            continue
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
        stats["dropped_rule_type"][type_name] = stats["dropped_rule_type"].get(type_name, 0) + 1

    result, cidr_errors = utils.flatten_ip_cidr(
        candidates, extract=True, dropped_default_routes=dropped_default_routes
    )
    stats["dropped_default_route"] = len(dropped_default_routes)
    return result, cidr_errors, stats


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

    if mode == "ipcidr":
        result, errors, stats = process_ip_detailed(lines)
        if stats["dropped_default_route"]:
            print(f"# 丢弃默认路由(/0) 规则 {stats['dropped_default_route']} 行", file=sys.stderr)
        for type_name, count in sorted(stats["dropped_rule_type"].items()):
            print(f"# 丢弃 {type_name} 规则 {count} 行: {ipcidr_drop_reason(type_name)}", file=sys.stderr)
        if stats.get("unrecognized"):
            print(f"# 丢弃空载荷规则: {stats['unrecognized']} 行", file=sys.stderr)
        for bad, why in errors.samples:
            print(f"# 丢弃无效 CIDR: {bad} -> {why}", file=sys.stderr)
        if errors.count > len(errors.samples):
            print(f"# 另有 {errors.count - len(errors.samples)} 条无效 CIDR 未逐条列出", file=sys.stderr)
    else:
        result, stats = process_domain_detailed(lines)
        if stats.get("extracted_url"):
            print(f"# URL hostname 提取: {stats['extracted_url']} 行", file=sys.stderr)
        if stats.get("unrecognized"):
            print(f"# 未识别行: {stats['unrecognized']}", file=sys.stderr)

    for line in result:
        print(line)


if __name__ == "__main__":
    main()
