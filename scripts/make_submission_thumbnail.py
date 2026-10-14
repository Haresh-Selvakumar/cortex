"""Original code-drawn Cortex branding; not a screenshot or generated evidence."""
from pathlib import Path
import hashlib
import json
from PIL import Image,ImageDraw,ImageFont

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'submission/public_submission_v1'
OUT.mkdir(parents=True,exist_ok=True)
image=Image.new('RGB',(1200,800),'#f4f6f2');draw=ImageDraw.Draw(image)
def font(size,bold=False):
    path=Path('C:/Windows/Fonts')/('seguisb.ttf' if bold else 'segoeui.ttf')
    return ImageFont.truetype(str(path),size)
draw.rounded_rectangle((48,46,114,112),radius=17,fill='#244f45')
draw.text((65,47),'C',font=font(47,True),fill='white')
draw.text((137,47),'FRIDAY / CORTEX',font=font(26,True),fill='#244f45')
draw.text((137,83),'BUDGET-AWARE EXECUTION',font=font(16),fill='#6a7d77')
draw.text((60,160),'Turn a request',font=font(79,True),fill='#203533')
draw.text((60,250),'into evidence.',font=font(79,True),fill='#203533')
draw.text((64,373),'Plan the work. Choose a worker. Check the result.',font=font(29),fill='#6a7d77')
for index,(title,note) in enumerate([('PLAN','Validated steps'),('ROUTE','Visible decisions'),('VERIFY','Declared checks')]):
    x=60+index*366
    draw.rounded_rectangle((x,467,x+330,640),radius=20,fill='#ffffff',outline='#dce4dc',width=2)
    draw.rounded_rectangle((x+22,491,x+60,529),radius=10,fill='#e7eee6')
    draw.text((x+35,494),str(index+1),font=font(21,True),fill='#285644')
    draw.text((x+22,548),title,font=font(30,True),fill='#244f45')
    draw.text((x+22,593),note,font=font(21),fill='#6a7d77')
draw.text((64,701),'Durable jobs  /  Shared budgets  /  Completion traces',font=font(22),fill='#244f45')
path=OUT/'cortex-thumbnail-1200x800.png'
with path.open('xb') as output:image.save(output,format='PNG',optimize=True)
assert image.size==(1200,800) and path.stat().st_size<5_000_000
(OUT/'thumbnail-provenance.json').write_text(json.dumps({'type':'original code-drawn brand graphic; not product evidence',
    'dimensions':[1200,800],'bytes':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
    'generator':'scripts/make_submission_thumbnail.py','imagegen_used':False},indent=2)+'\n')
print(json.dumps({'thumbnail':str(path),'size':path.stat().st_size}))
