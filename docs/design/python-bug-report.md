## Bug: hyphenated arg names silently fail required validation

**Package:** `runspec`
**Version:** `0.20.1`
**Python:** `3.13`

### Description

Any argument whose TOML key contains a hyphen (e.g. `output-file`) is never
recognised as provided during validation, even when correctly supplied on the
CLI. Required hyphenated args always raise "Missing required argument", and
optional ones always fall back to their defaults.

### Root cause

`_parse_argv` (in `parser.py`) stores parsed values using normalised
(underscore) keys:

```python
result: dict[str, Any] = {name.replace("-", "_"): None for name in arg_specs}
```

But `validate_args` (in `validator.py`) looks values up using the **raw**
spec name (with hyphens):

```python
for name, spec in arg_specs.items():
    value = parsed_values.get(name)   # "output-file" never found
```

So `parsed_values.get("output-file")` always returns `None` because the
key was stored as `"output_file"`. The same issue exists in
`validate_groups` for all four lookup sites.

### Minimal reproduction

`runspec.toml`:

```toml
[my-tool.args.output-file]
type     = "path"
required = true
description = "Destination file"
```

```bash
my-tool --output-file /tmp/out.txt
# ✗  Missing required argument: --output-file
```

Works only if you rename the arg key to `output_file` (underscore) in the
TOML — but then `--help` shows `--output_file` which is unconventional.

### Fix

In `validator.py`, normalise the name before the lookup in both functions:

```python
# validate_args
value = parsed_values.get(name.replace("-", "_"))

# validate_groups — all four get() calls
provided = [a for a in group_args if parsed_values.get(a.replace("-", "_")) is not None]
# ...
if parsed_values.get(condition_arg.replace("-", "_")) is not None:
    missing = [a for a in required_args if parsed_values.get(a.replace("-", "_")) is None]
```

---

## Resolution (0.20.2)

Confirmed and fixed exactly as proposed. `validate_args` and `validate_groups`
in `validator.py` now normalise the name (`name.replace("-", "_")`) before
looking the value up in `parsed_values`, matching how `_parse_argv`,
`_apply_env`, `_apply_defaults`, and `_coerce_values` already key it. The raw
(hyphenated) name is kept for display in error messages.

Regression tests:
- `tests/test_validator.py::TestHyphenatedNames` — unit coverage for
  `validate_args` and `validate_groups` (exclusive + conditional).
- `tests/test_parser.py::TestHyphenatedArgValidation` — full `parse()` pipeline,
  required hyphenated arg provided vs missing.

