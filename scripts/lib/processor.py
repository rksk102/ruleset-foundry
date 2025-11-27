#!/usr/bin/env python3
import sys
import re
import ipaddress
import base64

def decode_if_base64(content):
    """尝试智能探测并解码 Base64 内容"""
    s = content.strip()
    if ' ' not in s and len(s) % 4 == 0 and len(s) > 20:
        try:
            decoded = base64.b64decode(s).decode('utf-8', errors='ignore')
            if '\n' in decoded or '\r' in decoded:
                return decoded
        except Exception:
            pass
    return content

def parse_content(text):
    """从各种格式（YAML, List, Text）中提取有效行"""
    lines = []
    text = decode_if_base64(text)
    
    # 简单的 YAML Payload 探测
    has_payload_keyword = re.search(r'^[\s]*payload:', text, re.MULTILINE | re.IGNORECASE)
    in_payload = False
    
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('!') or line.startswith('#'): continue
        
        # 去掉行内注释 ( # comment)
        if '#' in line: line = line.split('#', 1)[0].strip()
        
        # 处理 Clash YAML
        if has_payload_keyword:
            if re.match(r'^[\s]*payload:', line, re.IGNORECASE):
                in_payload = True
                # 处理行内数组 payload: [a, b]
                m = re.match(r'^[\s]*payload:\s*\[(.*)\]', line, re.IGNORECASE)
                if m:
                    for p in m.group(1).split(','):
                        p = p.strip().strip("'").strip('"')
                        if p: lines.append(p)
                continue
            if in_payload:
                if line.startswith('-'):
                    val = line[1:].strip().strip("'").strip('"')
                    if val: lines.append(val)
                elif ':' in line:
                    in_payload = False # 可能是下一个key，退出payload模式
            continue
        
        # 处理普通 List (- domain)
        if line.startswith('- '): line = line[2:].strip()
        
        # 去引号
        line = line.strip("'").strip('"')
        if line: lines.append(line)
            
    return lines

def process_domain(raw_list):
    """域名清洗：去重、转小写、去Adblock修饰符、去非法字符"""
    valid_domains = set()
    # 简单的 IP 识别正则，用于剔除混入域名列表的 IP
    re_ip = re.compile(r'^\d{1,3}(\.\d{1,3}){3}$')
    
    for item in raw_list:
        s = item.lower().strip()
        if not s: continue
        
        # 1. Adblock 语法清洗 (||example.com^$third-party)
        if s.startswith('||'): s = s[2:]
        if s.endswith('^'): s = s[:-1]
        if '$' in s: s = s.split('$')[0] # 暴力截断 $ 参数
        
        # 2. 去除通配符前缀 (+. *. .)
        s = re.sub(r'^(\*\.|\+\.|\.)', '', s)
        
        # 3. 去除路径和端口
        if '/' in s: s = s.split('/')[0]
        if ':' in s: s = s.split(':')[0]
            
        # 4. 严格校验
        if not s or '.' not in s: continue
        if re_ip.match(s): continue # 剔除纯IP
        
        # 允许 字母 数字 - _ . 
        if not all(c.isalnum() or c in '-._' for c in s): continue
        
        valid_domains.add(s)
        
    return sorted(list(valid_domains))

def process_ip(raw_list):
    """IP清洗：标准化格式、分离v4/v6、智能合并CIDR"""
    ipv4_nets = []
    ipv6_nets = []
    
    for item in raw_list:
        s = item.strip()
        # 提取 "IP-CIDR, 1.1.1.1/24" -> "1.1.1.1/24"
        m = re.match(r'^(?:ip(?:-)?cidr6?|ip6|ip)\s*[:,]?\s*([^,\s]+)', s, re.IGNORECASE)
        if m: s = m.group(1)
            
        try:
            # strict=False 允许 192.168.1.1/24 (自动修正为 .0/24)
            net = ipaddress.ip_network(s, strict=False)
            if net.version == 4:
                ipv4_nets.append(net)
            else:
                ipv6_nets.append(net)
        except ValueError:
            continue

    # 智能合并 (这是 Python 库的强项)
    merged_v4 = list(ipaddress.collapse_addresses(ipv4_nets))
    merged_v6 = list(ipaddress.collapse_addresses(ipv6_nets))
    
    return [str(n) for n in merged_v4 + merged_v6]

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "domain"
    
    try:
        raw_content = sys.stdin.read()
    except Exception:
        sys.exit(0)

    lines = parse_content(raw_content)
    
    if mode == 'ipcidr':
        result = process_ip(lines)
    else:
        result = process_domain(lines)
        
    for r in result:
        print(r)

if __name__ == '__main__':
    main()
