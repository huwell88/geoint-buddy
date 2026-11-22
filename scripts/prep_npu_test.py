import sys
sys.path.insert(0, '/home/orangepi/geoint-buddy/backend')
from app.tools.vlm import stitch_bbox

img, meta = stitch_bbox([98.69, 31.07, 98.71, 31.09])
img.save('/tmp/aoi_npu_test.png')
print('saved', img.size, meta)
