"""Compare planning-off behavior against frozen product_foundation_v1 (mocked HTTP)."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
HARNESS = r'''
import json, io, sys
from pathlib import Path
from unittest.mock import patch
from cortex.product import execute_request
from cortex.registry import Registry
from cortex.domain import Task
from cortex.policies import Budget
root=Path(sys.argv[1])
registry=Registry.load(root/'config/cortex.example.json')
outputs=[]
for filename,fault in [('supplier_task.json','none'),('supplier_task.json','extract_timeout'),
    ('supplier_task.json','extract_timeout_persistent'),('supplier_task.json','bad_arithmetic'),
    ('missing_shipping_control.json','none'),('shipping_alias_control.json','none'),('csv_task.json','none')]:
    task=Task.from_dict(json.loads((root/'examples'/filename).read_text()))
    result=execute_request('Compare suppliers',registry,task,Budget(max_model_calls=0),fault=fault,offline=True)
    execution=result['supplier_execution']
    outputs.append({'case':[filename,fault], 'status':result['status'],'result':result['result'],
       'budget':result['budget'],'tokens':result['tokens'],'classification':result['classification'],
       'roles':result['roles'],'trace':result['trace'], 'actions': [row['action'] for row in execution['trace']]})
requests=[]
responses=[{'task_type':'mixed','required_capabilities':['code_generation','document_draft'],
    'missing_essential_information':[],'needs_decomposition':True,'uncertain':False},
    {'task_type':'coding','required_capabilities':['code_generation'],'missing_essential_information':[],
    'needs_decomposition':False,'uncertain':False}, 'def add(a,b): return a+b']
def reply(request,**kwargs):
    requests.append(json.loads(request.data))
    content=responses.pop(0)
    payload={'model':'mock-model','done':True,'done_reason':'stop','message':{'content':content if type(content) is str else json.dumps(content)},
        'prompt_eval_count':20,'eval_count':10}
    return io.BytesIO(json.dumps(payload).encode())
with patch('urllib.request.urlopen',side_effect=reply):
    for goal in ['Draft code and a report','Draft an add function']:
        result=execute_request(goal,registry)
        trace=result['trace']
        # Provider timings are real local mock execution durations, not baseline behavior.
        usage=[{key:value for key,value in row.items() if key!='latency_ms'} for row in result['model_usage']]
        outputs.append({'status':result['status'],'result':result['result'],'budget':result['budget'],
            'tokens':result['tokens'],'roles':result['roles'],'trace':trace,'usage':usage})
print(json.dumps({'outputs':outputs,'requests':requests}))
'''


def execute(source_root):
    result = subprocess.run([sys.executable, "-c", HARNESS, str(ROOT)], cwd=source_root,
                            check=True, capture_output=True, text=True, timeout=30)
    return json.loads(result.stdout)


def main():
    with tempfile.TemporaryDirectory(prefix="cortex_product_baseline_") as folder:
        frozen = Path(folder)
        (frozen / "cortex").mkdir()
        with zipfile.ZipFile(ROOT / "snapshots/product_foundation_v1_checkpoint.zip") as archive:
            for name in archive.namelist():
                if name.startswith("cortex/") and name.endswith(".py") and len(Path(name).parts) == 2:
                    (frozen / name).write_bytes(archive.read(name))
        old = execute(frozen)
        current = execute(ROOT)
    assert old == current, "Planning-off path differs from frozen product foundation"
    print("Frozen product foundation matches planning-off on seven supplier cases and two mocked classification/draft cases, including exact model request payloads.")


if __name__ == "__main__":
    main()
