import sys, json
sys.path.insert(0, '/home/orangepi/geoint-buddy/backend')
from app.tools import call

# 小 AOI 直接测 vlm_inspect 工具（端侧 VLM）
r = call('vlm_inspect', {
    'bbox': [98.69, 31.07, 98.71, 31.09],
    'focus': 'general',
    'task_id': None,
})
print(json.dumps(r, ensure_ascii=False)[:1500])
