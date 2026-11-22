import json, urllib.request

BASE = 'http://127.0.0.1:8000'

def post(path, body):
    req = urllib.request.Request(BASE+path, data=json.dumps(body).encode(),
                                 headers={'Content-Type':'application/json'})
    return urllib.request.urlopen(req, timeout=900)

# 白格滑坡核心 AOI
aoi_geom = {'type':'Polygon','coordinates':[[[98.685,31.065],[98.715,31.065],[98.715,31.095],[98.685,31.095],[98.685,31.065]]]}
r = json.load(post('/api/tasks', {'title':'白格滑坡端侧VLM判读','aoi_geojson':aoi_geom}))
tid = r['id']
print('TASK:', tid)

resp = post('/api/chat', {'task_id':tid,
    'message':'请调用 vlm_inspect 工具对圈选区域做遥感影像判读，重点识别滑坡体迹象，给出研判结论并说明置信度。'})
buf = b''
while True:
    chunk = resp.read(4096)
    if not chunk:
        break
    buf += chunk
for block in buf.decode().split('\n\n'):
    if not block.startswith('data: '):
        continue
    ev = json.loads(block[6:])
    k = ev.get('kind')
    if k == 'tool_call':
        print('[TOOL]', ev.get('tool'), str(ev.get('args'))[:120])
    elif k == 'tool_result':
        print('[RESULT]', ev.get('tool'), ev.get('elapsed_s'), 's ::', str(ev.get('preview'))[:400])
    elif k == 'tool_error':
        print('[TOOL_ERR]', ev.get('tool'), ev.get('message'))
    elif k == 'final':
        print('[FINAL]', ev['content'][:600])
    elif k == 'error':
        print('[ERROR]', ev['message'][:200])
    elif k == 'round':
        u = ev['usage']
        print('[round %s]' % ev['round'], ev['side'],
              'prompt=%s completion=%s %ss' % (u['prompt_tokens'], u['completion_tokens'], u['elapsed_s']))

# 落图检查
layers = json.load(urllib.request.urlopen(BASE + '/api/layers?task_id=' + tid, timeout=30))
print('LAYERS:', [(l['name'], l['kind']) for l in layers])
