#!/usr/bin/env python3
import sys
import re
import ipaddress
import base64

# 配置：是否跳过本地回环和保留地址（对于公共规则集通常设为 True）
SKIP_LOCAL = True

def decode_if_base64(content):
    """尝试探测并解码 Base64 内容 (适配某些订阅源)"""
    s = content.strip()
    # 简单的 heuristic: 没有任何空格，且长度是4的倍数，且只含 base64 字符
    if ' ' not in s and len(s) % 4 == 0 and len(s) > 20:
        try:
            # 尝试解码
            decoded = base64.b64decode(s).decode('utf-8', errors='ignore')
            # 如果解码结果看起来像文本列表（有换行），则采纳
            if '\n' in decoded or '\r' in decoded:
                return decoded
        except Exception:
            pass
    return content

def parse_content(text):
    """
    从文本中提取有效行。
    处理 YAML Payload, List item (- item), 行内注释等。
    """
    lines = []
    text = decode_if_base64(text)
    
    # 状态机提取 payload (替代复杂的 regex)
    in_payload = False
    # 简单的 YAML payload 探测
    has_payload_keyword = re.search(r'^[\s]*payload:', text, re.MULTILINE | re.IGNORECASE)
    
    raw_lines = text.splitlines()
    
    for line in raw_lines:
        line = line.strip()
        if not line: continue
        
        # 1. 处理注释
        if line.startswith('#') or line.startswith('!'): continue
        
        # 2. 去除行尾注释 ( # comment)
        if '#' in line:
            line = line.split('#', 1)[0].strip()
        
        # 3. 处理 Clash YAML 结构
        # 如果整篇看起来有 payload 关键字，我们启用严格抓取
        if has_payload_keyword:
            if re.match(r'^[\s]*payload:', line, re.IGNORECASE):
                in_payload = True
                # 检查是否有内联数组 payload: [a, b]
                m_inline = re.match(r'^[\s]*payload:\s*\[(.*)\]', line, re.IGNORECASE)
                if m_inline:
                    # 简单的逗号分割，注意 strip
                    parts = m_inline.group(1).split(',')
                    for p in parts:
                        p = p.strip().strip("'").strip('"')
                        if p: lines.append(p)
                continue
            
            if in_payload:
                # 遇到顶级 key (不带缩进或是缩进变小)，可能退出了 payload
                # 这里简单判定：如果是 - 开头，提取；否则如果不是 payload，可能是下一个 key
                if line.startswith('-'):
                    val = line[1:].strip().strip("'").strip('"')
                    if val: lines.append(val)
                elif ':' in line:
                    # 只是个粗略防卫：如果是 key: value 且没有缩进，说明 payload 结束
                    # Clash 缩进通常是 2 空格。这里假设非列表项即结束
                    in_payload = False
            continue
        
        # 4. 普通列表处理 (兼容 Adblock ||domain^ 写法 和 普通 yaml 列表 - domain)
        if line.startswith('- '):
            line = line[2:].strip()
        
        # 去除引号
        line = line.strip("'").strip('"')
        
        if line:
            lines.append(line)
            
    return lines

def process_domain(raw_list):
    """
    清洗域名：转小写、去前缀(+. *.)、去多余符号、去重
    """
    valid_domains = set()
    
    # 预编译正则
    # 允许 中文域名 (xn--),, 字母数字, 点, 连字符
    # 排除 IP 地址 (简单的排除，后续 verify 也可以)
    re_ip = re.compile(r'^\d{1,3}(\.\d{1,3}){3}$')
    
    for item in raw_list:
        s = item.lower()
        
        # 1. Adblock 转换 (||example.com^ -> example.com)
        if s.startswith('||'): s = s[2:]
        if s.endswith('^'): s = s[:-1]
        
        # 2. 去除通配符的前缀 (+. *. .)
        # 注意：保留 exact match 还是 subdomain match 取决于上游逻辑。
        # 为了通用性，我们通常只取 pure domain。
        s = re.sub(r'^(\*\.|\+\.|\.)', '', s)
        
        # 3. 丢弃 URL 路径，只留 domain
        if '/' in s:
            s = s.split('/')[0]
        if ':' in s: # 去端口
            s = s.split(':')[0]
            
        # 4. 校验
        if not s or '.' not in s: continue
        if re_ip.match(s): continue # 混入的 IP 丢掉，因为这是 domain 列表
        
        # 简单的字符合法性检查
        if not all(c.isalnum() or c in '-._' for c in s):
            continue
            
        valid_domains.add(s)
        
    return sorted(list(valid_domains))

def process_ip(raw_list):
    """
    清洗 IP：标准化格式、过滤无效 IP、**合并网段 (CIDR Merge)**
    """
    ipv4_nets = []
    ipv6_nets = []
    
    for item in raw_list:
        s = item.strip()
        # 提取经典写法 "IP-CIDR, 1.1.1.1/24" -> "1.1.1.1/24"
        m = re.match(r'^(?:ip(?:-)?cidr6?|ip6|ip)\s*[:,]?\s*([^,\s]+)', s, re.IGNORECASE)
        if m:
            s = m.group(1)
            
        try:
            # strict=False 允许 192.168.1.1/24 这种非网络号写法 (自动转为 192.168.1.0/24)
            net = ipaddress.ip_network(s, strict=False)
            
            # 分类存入 v4 或 v6 列表
            if net.version == 4:
                ipv4_nets.append(net)
            else:
                ipv6_nets.append(net)
                
        except ValueError:
            continue

    # **核心修复：分别合并 v4 和 v6**
    merged_v4 = list(ipaddress.collapse_addresses(ipv4_nets))
    merged_v6 = list(ipaddress.collapse_addresses(ipv6_nets))
    
    # 合并结果返回
    return [str(n) for n in merged_v4 + merged_v6]

def main():
    if len(sys.argv) < 2:
        # 默认为 domain 模式方便调试，或者报错
        mode = "domain"
    else:
        mode = sys.argv[1] # domain 或 ipcidr
    
    # 1. 读取 stdin
    try:
        raw_content = sys.stdin.read()
    except Exception:
        sys.exit(0) # 空输入

    # 2. 初步解析成行
    lines = parse_content(raw_content)
    
    # 3. 按类型智能处理
    if mode == 'ipcidr':
        result = process_ip(lines)
    else:
        result = process_domain(lines)
        
    # 4. 输出
    for r in result:
        print(r)

if __name__ == '__main__':
    main()
