"""Compare current baseline behavior and model inputs with frozen selective-v1."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
HARNESS = r'''
import io, json, sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
from cortex.domain import Task
from cortex.engine import run
from cortex.policies import Budget, OllamaPolicy
root = Path(sys.argv[2])
cases = [
    ('supplier_task.json', 'none', False),
    ('supplier_task.json', 'extract_timeout', False),
    ('supplier_task.json', 'bad_arithmetic', False),
    ('missing_shipping_control.json', 'none', False),
    ('shipping_alias_control.json', 'none', False),
    ('supplier_task.json', 'extract_timeout', True),
]
outputs = []
for filename, fault, force_plan in cases:
    requests = []
    def response(request, **kwargs):
        payload = json.loads(request.data)
        requests.append(payload)
        state = json.loads(payload['messages'][1]['content'])
        action = 'plan' if force_plan and len(requests) == 1 else state['admissible_actions'][0]
        raw = {'message': {'content': json.dumps({'action': action, 'reason': 'Mock decision'})},
               'prompt_eval_count': 10, 'eval_count': 2}
        return io.BytesIO(json.dumps(raw).encode())
    task = Task.from_dict(json.loads((root/'examples'/filename).read_text()))
    with patch('urllib.request.urlopen', side_effect=response):
        result = run(task, OllamaPolicy('test', selective=True), Budget(max_model_calls=4), fault=fault)
    outputs.append({'case':[filename,fault,force_plan], 'requests':requests,
                    'status':result['status'], 'result':result['result'], 'budget':result['budget'],
                    'tokens':result['tokens'], 'actions':[e['action'] for e in result['trace']]})
print(json.dumps(outputs))
'''


def execute(source_root):
    result = subprocess.run([sys.executable, "-c", HARNESS, str(source_root), str(ROOT)],
                            cwd=source_root, check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def main():
    snapshot = ROOT / "snapshots" / "selective_v1_checkpoint.zip"
    with tempfile.TemporaryDirectory(prefix="cortex_baseline_") as folder:
        frozen = Path(folder)
        (frozen / "cortex").mkdir()
        with zipfile.ZipFile(snapshot) as archive:
            for name in archive.namelist():
                parts = Path(name).parts
                if len(parts) == 2 and parts[0] == "cortex" and name.endswith(".py"):
                    (frozen / name).write_bytes(archive.read(name))
        old = execute(frozen)
        current = execute(ROOT)
    assert current == old, "Current selective baseline differs in requests or outcomes"
    print("Frozen selective-v1 matches current baseline on five paired cases plus a planner case: actions, outcomes, budgets, tokens and exact provider payloads.")


if __name__ == "__main__":
    main()
