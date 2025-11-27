#!/usr/bin/env python3
import os
import sys
import re
import ipaddress
import base64

# =========================
# 配置与全局变量
# =========================
SOURCE_DIR = "rulesets"

STATS = {
    "files_processed": 0,
    "base64_decoded": 0,
    "original_lines": 0,
    "valid_lines": 0,
    "errors": []
}

# =========================
# GitHub Actions 辅助函数
# =========================
def gh_group_start(title):
    print(f"::group::🛠️ {title}")
    sys.stdout.flush()

def gh_group_end():
    print("::endgroup::")
    sys.stdout.flush()

def print_step(msg):
    print(f"\033[1;34m[PROC]\033[0m {msg}")

def print_success(msg):
    print(f"\033[1;32m[OK]\033[0m   {msg}")

def gh_error(msg, file=None):
    msg_str = f"::error::{msg}" if not file else f"::error file={file}::{msg}"
    print(msg_str)
    STATS["errors"].append(msg)

# =========================
# 核心逻辑 (来源于你上传的文件)
# =========================

def decode_if_base64(content):
    """尝试探测并解码 Base64 内容"""
    s = content.strip()
    if ' ' not in s and len(s) % 4 == 0 and len(s) > 20:
        try:
            decoded = base64.b64decode(s).decode('utf-8', errors='ignore')
            if '\n' in decoded or '\r' in decoded:
                STATS["base64_decoded"] += 1
                return decoded
        except Exception:
            pass
    return content

def parse_content_to_list(text):
    """
    提取文本中的有效行，支持 Yaml Payload 提取
    """
    lines = []
    text = decode_if_base64(text)
    
    # 简单的 YAML payload 探测
    has_payload_keyword = re.search(r'^[\s]*payload:', text, re.MULTILINE | re.IGNORECASE)
    in_payload = False
    
    raw_lines = text.splitlines()
    STATS["original_lines"] += len(raw_lines)
    
    for line in raw_lines:
        line = line.strip()
        if not line: continue
        if line.startswith('#') or line.startswith('!') or line.startswith('//'): continue
        
        # 去除行尾注释
        if ' #' in line: line = line.split(' #', 1)[0].strip()
        if '#' in line and not has_payload_keyword: # 简单防止误伤 url anchor
             line = line.split('#', 1)[0].strip()

        # 处理 Clash YAML 结构 (payload:)
        if has_payload_keyword:
            if re.match(r'^[\s]*payload:', line, re.IGNORECASE):
                in_payload = True
                # 检查内联 [a, b]
                m_inline = re.match(r'^[\s]*payload:\s*\[(.*)\]', line, re.IGNORECASE)
                if m_inline:
                    parts = m_inline.group(1).split(',')
                    for p in parts:
                        p = p.strip().strip("'").strip('"')
                        if p: lines.append(p)
                continue
            
            if in_payload:
                if line.startswith('-'):
                    val = line[1:].strip().strip("'").strip('"')
                    if val: lines.append(val)
                elif ':' in line:
                    in_payload = False # 遇到下一个 key
                continue

        # 普通列表处理 (- domain)
        if line.startswith('- '):
            line = line[2:].strip()
        
        line = line.strip("'").strip('"')
        if line:
            lines.append(line)
            
    return lines

def process_domain_list(raw_list):
    """
    清洗域名：转小写、去前缀、去重、排序
    """
    valid_domains = set()
    re_ip = re.compile(r'^\d{1,3}(\.\d{1,3}){3}$') # 简单过滤纯IP
    
    for item in raw_list:
        s = item.lower().strip()
        
        # Adblock 转换 ||example.com^ -> example.com
        if s.startswith('||'): s = s[2:]
        if s.endswith('^'): s = s[:-1]
        
        # 去除通配符
        s = re.sub(r'^(\*\.|\+\.|\.)', '', s)
        
        # 丢弃路径和端口
        if '/' in s: s = s.split('/')[0]
        if ':' in s: s = s.split(':')[0]
            
        if not s or '.' not in s: continue
        if re_ip.match(s): continue 
        
        # 合法性检查
        if not all(c.isalnum() or c in '-._' for c in s): continue
            
        valid_domains.add(s)
        
    return sorted(list(valid_domains))

def process_ip_list(raw_list):
    """
    清洗 IP：标准化、合并网段 (Collapsing)
    """
    ipv4_nets = []
    ipv6_nets = []
    
    for item in raw_list:
        s = item.strip()
        # 提取 "IP-CIDR, 1.1.1.1/24"
        m = re.match(r'^(?:ip(?:-)?cidr6?|ip6|ip)\s*[:,]?\s*([^,\s]+)', s, re.IGNORECASE)
        if m: s = m.group(1)
            
        try:
            # strict=False 允许主机位不为0的写法
            net = ipaddress.ip_network(s, strict=False)
            if net.version == 4:
                ipv4_nets.append(net)
            else:
                ipv6_nets.append(net)
        except ValueError:
            continue

    # 核心功能：合并网段 (例如 1.1.1.1/32 + 1.1.1.0/32 -> 无需合并，或相邻合并)
    try:
        merged_v4 = list(ipaddress.collapse_addresses(ipv4_nets))
        merged_v6 = list(ipaddress.collapse_addresses(ipv6_nets))
        return [str(n) for n in merged_v4 + merged_v6]
    except Exception as e:
        # 万一合并出错，回退到包含重复的列表
        return sorted([str(n) for n in ipv4_nets + ipv6_nets])

# =========================
# 适配工作流的新 Main 函数
# =========================

def process_file(filepath):
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # 1. 初步解析 (通用)
        raw_list = parse_content_to_list(content)
        
        # 2. 判断处理模式 (根据路径判断 IP 还是 Domain)
        # 规则结构通常为: rulesets/block/domain/owner/file.txt
        # 或者 rulesets/direct/ipcidr/owner/file.txt
        # 我们检测路径中是否包含 'ipcidr' 或 'ip'，否则默认为 domain
        
        path_lower = filepath.lower()
        is_ip_mode = 'ipcidr' in path_lower or '/ip/' in path_lower
        
        if is_ip_mode:
            final_list = process_ip_list(raw_list)
        else:
            final_list = process_domain_list(raw_list)
            
        # 3. 写回
        with open(filepath, 'w', encoding='utf-8') as f:
            for line in final_list:
                f.write(line + "\n")
        
        STATS["files_processed"] += 1
        STATS["valid_lines"] += len(final_list)

    except UnicodeDecodeError:
        gh_error(f"Encoding error", file=filepath)
    except Exception as e:
        gh_error(f"Process error: {e}", file=filepath)

def main():
    print("::notice::Starting Smart Rule Processor (Base64/YAML/CIDR-Merge)...")
    
    if not os.path.exists(SOURCE_DIR):
        print(f"::warning::Directory '{SOURCE_DIR}' not found.")
        return

    gh_group_start(f"Processing {SOURCE_DIR}")
    
    # 扫描文件
    target_files = []
    for root, dirs, files in os.walk(SOURCE_DIR):
        for file in files:
            if file.endswith(('.txt', '.list', '.conf', '.yaml')):
                target_files.append(os.path.join(root, file))
    
    print_step(f"Found {len(target_files)} files.")
    
    # 执行处理
    for fp in target_files:
        process_file(fp)
        
    gh_group_end()

    # 输出报告
    removed_total = STATS["original_lines"] - STATS["valid_lines"]
    print_success("Sanitization & Optimization Complete.")
    print(f"  - Files: {STATS['files_processed']}")
    print(f"  - Base64 Decoded: {STATS['base64_decoded']}")
    print(f"  - Lines Kept: {STATS['valid_lines']}")
    print(f"  - Lines Reduced: {removed_total}")

    if os.getenv('GITHUB_STEP_SUMMARY'):
        with open(os.getenv('GITHUB_STEP_SUMMARY'), 'a', encoding='utf-8') as f:
            f.write("## 🧠 Intelligent Processor Report\n")
            f.write(f"- **Files Processed**: `{STATS['files_processed']}`\n")
            f.write(f"- **Base64 Sources Decoded**: `{STATS['base64_decoded']}`\n")
            f.write(f"- **Cleaned Rules**: `{STATS['valid_lines']}`\n")
            f.write(f"- **Reduction**: `{removed_total}` lines removed (duplicates, invalid, or aggregated CIDRs)\n")

if __name__ == '__main__':
    main()
