# wiki/data — machine-readable references

Structured companions to the prose wiki. Some are **generated from code** and must be
regenerated rather than hand-edited.

| File | Format | Source of truth | Regenerate with |
|---|---|---|---|
| `enums.json` | JSON | `planning/models.py` | `python -c "import json,sys; sys.path.insert(0,'.'); from planning.models import *; ..."` (see below) |
| `tool-schemas.json` | JSON | `planning/schemas.py` | same pattern, dumps `TOOL_DEFINITIONS` |
| `state-machine.xml` | XML | `planning/state_machine.py` + `handlers.py` | hand-maintained; keep in step with the code |
| `config.json` | JSON | `planning/config.py` | hand-maintained |
| `versions.json` | JSON | git history + `08-changelog.md` | hand-maintained |

Regenerate the two code-derived files:

```bash
python -c "import json,sys; sys.path.insert(0,'.'); from planning.models import PlanStatus,TaskStatus,Decision,NextAction,ErrorCode; import io; d={'plan_status':[e.value for e in PlanStatus],'task_status':[e.value for e in TaskStatus],'decision':[e.value for e in Decision],'next_action':[e.value for e in NextAction],'error_code':[e.value for e in ErrorCode]}; open('wiki/data/enums.json','w',encoding='utf-8').write(json.dumps(d,ensure_ascii=False,indent=2))"

python -c "import json,sys; sys.path.insert(0,'.'); from planning.schemas import TOOL_DEFINITIONS; open('wiki/data/tool-schemas.json','w',encoding='utf-8').write(json.dumps({'tools':TOOL_DEFINITIONS},ensure_ascii=False,indent=2))"
```

(Add the `_comment` fields back if you regenerate; they are informational only.)
