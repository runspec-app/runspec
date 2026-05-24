---
description: Add a new runspec runnable to the current project — TOML section, code stub, entry point, and validation
---

Add a runnable to this project: **$ARGUMENTS**

## Current project state

Existing runspec.toml files:
!`find . -name "runspec.toml" -not -path "*/.venv/*" -not -path "*/node_modules/*" -not -path "*/site-packages/*" 2>/dev/null`

Contents of the nearest runspec.toml (if any):
!`find . -name "runspec.toml" -not -path "*/.venv/*" -not -path "*/node_modules/*" -not -path "*/site-packages/*" 2>/dev/null | head -1 | xargs cat 2>/dev/null || echo "(none — will create)"`

---

## Hard rules — do not deviate

1. **runspec.toml lives inside the package directory** (mypkg/runspec.toml), never at the project root. The package dir contains __init__.py (Python) or index.ts/index.js (Node).

2. **Each runnable is a top-level TOML section**: [name]. Never [runnables.name] or [scripts.name].

3. **[config] is reserved** — never use it as a runnable name.

4. **Entry point name must exactly match the runnable name** — runspec local discovers entry points by matching installed script name to TOML section name.

5. **Reinstall after editing pyproject.toml** — pip install -e . (Python) or npm install (Node).

6. Default autonomy = "confirm" unless explicitly asked otherwise.

---

## Arg types

| Type      | Notes                                          |
|-----------|------------------------------------------------|
| "str"     | Text                                           |
| "int"     | Integer                                        |
| "float"   | Float                                          |
| "flag"    | Boolean switch; default = false                |
| "path"    | File/dir path coerced to pathlib.Path (Python) |
| "choice"  | Requires options = [...]                       |

Inference: no default → required = true. default = false → type inferred as "flag". options = [...] → type inferred as "choice".

Inline table style for args:
```toml
[myrule.args]
directory = {type = "path",   description = "Directory to process", default = "."}
dry_run   = {type = "flag",   description = "Preview only",         default = false}
format    = {type = "choice", description = "Output format",        options = ["text", "json"], default = "text"}
```

---

## Python stub

```python
from runspec import parse

def main():
    args = parse()
    print("done")
```

pyproject.toml entry:
```toml
[project.scripts]
myrule = "mypkg.myrule:main"
```

## Node/TypeScript stub

```typescript
import { parse } from "runspec-node";
const args = parse();
console.log("done");
```

package.json entry:
```json
"bin": { "myrule": "dist/myrule.js" }
```

---

## Steps

1. Find the package directory — locate runspec.toml or the source dir with __init__.py / index.ts.
2. Add the runnable section to runspec.toml. If creating fresh, add the schema comment first:
   #:schema https://raw.githubusercontent.com/JasonFinestone/runspec/main/schema/runspec.schema.json
3. Create the code stub in the package directory.
4. Wire the entry point in pyproject.toml or package.json.
5. Reinstall — pip install -e . or npm install.
6. Validate — run runspec local and confirm the runnable appears.
