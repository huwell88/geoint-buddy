import json, urllib.request
req = urllib.request.Request('http://127.0.0.1:8000/api/network',
                             data=json.dumps({'state': 'OFFLINE'}).encode(),
                             headers={'Content-Type': 'application/json'})
print(urllib.request.urlopen(req, timeout=30).read().decode())
