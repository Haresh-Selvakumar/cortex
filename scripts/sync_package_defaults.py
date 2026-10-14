"""Copy only tracked example configurations into the installed package."""
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
directory=ROOT/'cortex/default_config'
directory.mkdir(exist_ok=True)
offline=json.loads((ROOT/'config/cortex.public.example.json').read_text(encoding='utf-8'))
for row in offline['models']:row['enabled']=False
(ROOT/'config/cortex.public.offline.json').write_text(json.dumps(offline,indent=2)+'\n',encoding='utf-8')
for name in ('cortex.example.json','cortex.public.example.json','cortex.public.offline.json','tools.example.json'):
    (directory/name).write_bytes((ROOT/'config'/name).read_bytes())
(directory/'supplier_task.json').write_bytes((ROOT/'examples/supplier_task.json').read_bytes())
