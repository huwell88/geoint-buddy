import sys, json
sys.path.insert(0, '/home/orangepi/geoint-buddy/backend')
from app.tools import call

r = call('dem_stats', {'bbox': [98.685, 31.065, 98.715, 31.095]})
print('STATS:', json.dumps(r, ensure_ascii=False))
r2 = call('dem_elevation', {'points': [[98.7028, 31.0805], [98.70, 31.08]]})
print('ELEV:', json.dumps(r2, ensure_ascii=False))
