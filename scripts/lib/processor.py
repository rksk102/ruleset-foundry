#!/usr/bin/env python3
import sys
import re
import ipaddress
import base64
import binascii

# ==========================================
# 核心工具函数
# ==========================================

def safe_decode(binary_data):
    """智能解码：尝试 UTF-8，失败则回退"""
    for codec in ['utf-8', 'gb18030', 'latin1']:
        try:
            return binary_data.decode(codec).strip()
        except Exception:
            continue
    return ""

def is_text_data(text):
    """判断是否为有效文本（防止Base64解出二进制乱码）"""
    if '\0' in text: return False
    non_printable = sum(1 for c in text if not c.isprintable() and c not in '\r\n\t')
    if len(text) > 0 and (non_printable / len(text)) > 0.3:
        return False
    return True

def explicit_base64_decode(text):
    """深度 Base64 清洗"""
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
    """全能解析器：处理 YAML, Hosts, List, Base64"""
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

# ==========================================
# 类型处理器
# ==========================================

def process_domain(lines):
    """
    智能域名清洗 (已修复 full: 等前缀问题)
    """
    valid_domains = set()
    ip_check = re.compile(r'^\d{1,3}(\.\d{1,3}){3}$')
    
    # 定义需要移除的前缀列表 (注意顺序，长前缀在前)
    prefixes = [
        'full:', 'domain:', 'host:', 'keyword:', 'regexp:', 
        'domain-suffix:', 'domain-keyword:', '+.'
    ]
    
    for item in lines:
        s = item.lower().strip()
        if not s: continue
        
        # 1. 剔除白名单规则
        if s.startswith('@@'): continue
        
        # 2. 【关键修复】去除规则前缀
        # 必须在 split(':') 之前执行
        for prefix in prefixes:
            if s.startswith(prefix):
                s = s[len(prefix):]
                break # 匹配到一个就停止

        # 3. Hosts 格式清洗
        parts = s.split()
        if len(parts) >= 2:
            if parts[0] in ['127.0.0.1', '0.0.0.0', '::1']:
                s = parts[1]
        
        # 4. Adblock 语法清洗
        if s.startswith('||'): s = s[2:]
        if s.endswith('^'): s = s[:-1]
        if '$' in s: s = s.split('$')[0]
        s = re.sub(r'^(\*\.|\+\.|\.)', '', s)
        
        # 5. 去除路径和端口
        if '/' in s: s = s.split('/')[0]
        
        # 此时再处理冒号，剩下的通常就是端口号了
        if ':' in s: s = s.split(':')[0]
        
        # 6. 最终校验
        if not s or '.' not in s: continue
        if ' ' in s: continue
        if ip_check.match(s): continue
        
        # 允许字母、数字、点、横杠、下划线
        if not all(c.isalnum() or c in '-._' for c in s): continue
        
        valid_domains.add(s)
        
    return sorted(list(valid_domains))

def process_ip(lines):
    """智能 IP 清洗"""
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

# ==========================================
# 主入口
# ==========================================

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
