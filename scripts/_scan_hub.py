import re, sys
for path in [r'C:\Users\49402\AppData\Local\Temp\trae\toolcall-output\f3a7b9d9-3abc-4ae7-980f-36dbc3d07739.txt',
             r'C:\Users\49402\AppData\Local\Temp\trae\toolcall-output\90a8c43c-f866-44e5-8a45-4dfdc4656092.txt']:
    text = open(path, encoding='utf-8', errors='ignore').read()
    paths = re.findall(r'"Path":"([^"]+)"', text)
    print('===', len(paths), 'entries')
    # 只打印叶子文件（非目录）与关键名
    for p in paths:
        low = p.lower()
        if any(k in low for k in ['qwen3', '.cix', 'cfg/', 'readme', 'export', '.py', '.json', '.onnx']):
            print(p)
