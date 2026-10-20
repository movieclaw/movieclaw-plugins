"""Release gate: structural success must never conceal known semantic gaps."""
from pathlib import Path
import json
import sys
baseline=json.loads((Path(__file__).resolve().parents[1]/'tests/contract_baseline.json').read_text())
if not baseline['semantic_compliance']:
    print('BLOCKED: full DriveBackend compliance has not been achieved:')
    for item in baseline['blockers']:print('- '+item)
    sys.exit(2)
print('No recorded semantic blockers; review evidence before publishing.')
