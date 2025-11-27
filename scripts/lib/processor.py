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
    # 如果包含大量空字符或不可打印字符，视为二进制
    if '\0' in text: return False
    # 简单的启发式：非打印字符超过 30% 可能是乱码
    non_printable = sum(1 for c in text if not c.isprintable() and c not in '\r\n\t')
    if len(text) > 0 and (non_printable / len(text)) > 0.3:
        return False
    return True

def explicit_base64_decode(text):
    """深度 Base64 清洗"""
    s = text.replace('\n', '').replace('\r', '').strip()
    if ' ' in s or len(s) < 20: return text # 包含空格通常不是 Base64 代码块
    
    try:
        # 尝试解码
        decoded_bytes = base64.b64decode(s, validate=True)
        decoded_str = safe_decode(decoded_bytes)
        
        if is_text_data(decoded_str):
            # 只有解码出来是像样的文本才返回
            return decoded_str
    except (binascii.Error, ValueError):
        pass
    
    return text

def parse_lines(raw_content):
    """
    全能解析器：处理 YAML, Hosts, List, Base64
    """
    content = explicit_base64_decode(raw_content)
    lines = []
    
    # --- 1. 检测 YAML Payload ---
    # 很多规则集是 Clash YAML 格式，即使扩展名是 .list
    in_payload = False
    yaml_payload_pattern = re.compile(r'^\s*payload:', re.IGNORECASE)
    
    content_lines = content.splitlines()
    
    # 快速扫描是否包含 payload 关键字
    has_payload = any(yaml_payload_pattern.match(l) for l in content_lines[:50])

    for line in content_lines:
        line = line.strip()
        if not line: continue
        # 跳过注释，但不要误伤 Adblock 的特殊语法 (@@)
        if line.startswith('#') or line.startswith('!'): continue
        
        # 去除尾部注释 ( # comment)
        if ' #' in line: line = line.split(' #')[0].strip()
        
        # YAML 逻辑
        if has_payload:
            if yaml_payload_pattern.match(line):
                in_payload = True
                # 处理 payload: [a, b]
                m = re.search(r'\[(.*)\]', line)
                if m:
                    for x in m.group(1).split(','):
                        lines.append(x.strip("'\" "))
                continue
            
            if in_payload:
                # 遇到缩进减少或新 Key，结束 payload
                if re.match(r'^[a-zA-Z0-9_-]+:', line):
                    in_payload = False
                    continue
                if line.startswith('- '):
                    lines.append(line[2:].strip("'\" "))
                elif line.startswith('-'): # 容错 -domain
                    lines.append(line[1:].strip("'\" "))
            continue # YAML 模式下不走后续逻辑
        
        # 普通列表 / Hosts 逻辑
        # 处理 "- domain" 格式
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
    智能域名清洗
    能力：Host解析、白名单剔除、Adblock转换、去重
    """
    valid_domains = set()
    
    # 域名验证正则 (允许 unicode 域名转码前格式，不允许空格)
    # 排除纯数字 IP
    ip_check = re.compile(r'^\d{1,3}(\.\d{1,3}){3}$')
    
    for item in lines:
        s = item.lower().strip()
        if not s: continue
        
        # 1. 【智能】剔除白名单规则 (@@)
        # 如果这是一个拦截列表，包含白名单规则会导致逻辑错误，必须丢弃
        if s.startswith('@@'): continue

    # 处理 full: domain: 等前缀，防止被后面的 split(':') 误伤
    for prefix in ['full:', 'domain:', 'host:', 'keyword:', 'regexp:']:
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
        
        # 2. 【智能】Hosts 格式清洗 (127.0.0.1 example.com)
        # 拆分空格，如果第一部分是 IP，取第二部分
        parts = s.split()
        if len(parts) >= 2:
            if parts[0] in ['127.0.0.1', '0.0.0.0', '::1']:
                s = parts[1]
        
        # 3. Adblock 语法标准清洗
        if s.startswith('||'): s = s[2:]
        if s.endswith('^'): s = s[:-1]
        
        # 4. 截断参数 ($image,domain=...)
        if '$' in s: s = s.split('$')[0]
        
        # 5. 去除通配符 (*.example.com -> example.com)
        s = re.sub(r'^(\*\.|\+\.|\.)', '', s)
        
        # 6. 去除路径和端口
        if '/' in s: s = s.split('/')[0]
        if ':' in s: s = s.split(':')[0]
        
        # 7. 最终校验
        if not s or '.' not in s: continue
        if ' ' in s: continue # 还有空格的一律不要
        if ip_check.match(s): continue # 混入的纯 IP 不要
        
        # 允许字母、数字、点、横杠、下划线
        if not all(c.isalnum() or c in '-._' for c in s): continue
        
        valid_domains.add(s)
        
    return sorted(list(valid_domains))

def process_ip(lines):
    """
    智能 IP 清洗
    能力：提取 IP/CIDR、标准化、自动合并相邻网段
    """
    v4_nets = []
    v6_nets = []
    
    # 宽松匹配：提取文本中看起来像 IP 或 CIDR 的部分
    # 可以匹配： "ip-cidr, 10.0.0.0/8" 或 "10.0.0.1" 或 "  192.168.1.1  "
    regex_ip = re.compile(r'([0-9a-fA-F:.]+(?:/[0-9]+)?)')
    
    for item in lines:
        # 只在行中查找第一个匹配项
        m = regex_ip.search(item)
        if not m: continue
        
        ip_str = m.group(1)
        
        try:
            # strict=False: 自动修正主机位 (192.168.1.5/24 -> 192.168.1.0/24)
            net = ipaddress.ip_network(ip_str, strict=False)
            
            # 过滤 0.0.0.0/0 这种危险的全局规则，防止误伤
            if net.prefixlen == 0: continue 
            
            if net.version == 4:
                v4_nets.append(net)
            else:
                v6_nets.append(net)
        except ValueError:
            continue
            
    # 【核心智能】基于数学逻辑合并网段
    # 例如：192.168.0.0/24 + 192.168.1.0/24 -> 192.168.0.0/23
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
    # 默认模式
    mode = "domain"
    if len(sys.argv) > 1:
        mode = sys.argv[1]
        
    # 读取 stdin 二进制流，为了更好地处理编码
    try:
        raw_bytes = sys.stdin.buffer.read()
    except Exception:
        return

    if not raw_bytes:
        return

    # 1. 解码
    content = safe_decode(raw_bytes)
    
    # 2. 提取行
    lines = parse_lines(content)
    
    # 3. 分类处理
    if mode == 'ipcidr':
        result = process_ip(lines)
    else:
        result = process_domain(lines)
        
    # 4. 输出
    for line in result:
        print(line)

if __name__ == '__main__':
    main()
