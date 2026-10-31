# Contributing to runspec

Thank you for your interest in contributing. This document covers how the
project is structured and how to get started.

---

## Repository Structure

This is a mono-repo. All official language packs live here:

```
packages/python/    ← reference implementation, built first
packages/node/      ← Node/TypeScript pack (planned)
packages/go/        ← Go pack (planned)
tests/integration/  ← compliance suite all packs must pass
spec/               ← canonical format specification
```

## The Spec is the Source of Truth

Before changing any behaviour, check `spec/SPEC.md`. If your change affects
the format or inference rules, the spec must be updated first. All language
packs must then be updated to match, and all compliance fixtures must pass.

## Getting Started — Python

```bash
git clone https://github.com/runspec-app/runspec
cd runspec/packages/python

# Create a virtual environment
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

# Install in editable mode with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Run linting and type checks
ruff check .
mypy runspec/
```

## Getting Started — Node (when active)

```bash
cd packages/node
npm install
npm test
```

## Getting Started — Go (when active)

```bash
cd packages/go
go test ./...
```

## Compliance Tests

Every language pack must pass the integration compliance suite:

```bash
cd tests/integration
pytest compliance/
```

The fixtures in `tests/integration/fixtures/` are the canonical test inputs.
If you add a new inference rule or feature, add a fixture for it first.

## Pull Request Guidelines

- One PR per feature or fix
- Update `spec/SPEC.md` if the format changes
- All tests must pass including compliance
- Add a fixture for new behaviour
- Keep commits focused — one logical change per commit

## Branching

- `main` — stable, released
- `dev` — active development, PRs target this
- `feature/your-feature` — feature branches off `dev`

## Reporting Issues

Use the GitHub issue templates:
- **Bug report** — something doesn't work as documented
- **Feature request** — something you'd like to see added

## Code Style

### Python
- Formatted with `ruff format`
- Linted with `ruff check`
- Type-checked with `mypy`
- Docstrings for all public functions

### Node (when active)
- TypeScript strict mode
- ESLint + Prettier

### Go (when active)
- `gofmt` formatted
- `golangci-lint` clean
