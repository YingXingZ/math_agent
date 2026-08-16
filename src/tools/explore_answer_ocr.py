import json, re, sys
F = sys.argv[1]
raw = open(F, encoding='utf-8', errors='replace').read()
try:
    obj = json.loads(raw)
    text = obj.get('content') or raw
except Exception:
    text = raw
print('TEXT LEN', len(text))

# 找章节/习题块头：常见 "习题 1.1" 或 "1.1 习题" 或 "习题解答 1.1"
pat = re.compile(r'习题\s*(\d+\.\d+)')
hits = list(pat.finditer(text))
print('习题X.Y 标记数:', len(hits))
print('前10个标记:', [(h.group(1), h.start()) for h in hits[:10]])

# 打印 1.1 块样本
for target in ['1.1', '1.2', '2.1']:
    for h in hits:
        if h.group(1) == target:
            s = h.start()
            e = text.find('习题', s + 5)
            e = e if e != -1 else s + 1200
            block = text[s:e]
            print(f'\n===== 习题 {target} 块 (长度 {len(block)}) =====')
            print(block[:900])
            break
