# runspec — Getting Started on a New Machine

Follow these steps in order. Takes about 15 minutes.

---

## 1. Prerequisites

Install these if you don't have them already:

| Tool | Version | Download |
|---|---|---|
| Python | 3.11 or later | https://python.org/downloads |
| Git | Any recent | https://git-scm.com |
| Node.js | 18 or later | https://nodejs.org (for Claude Code) |
| PyCharm Professional | Latest | https://jetbrains.com/pycharm |

To check what you have:
```bash
python --version
git --version
node --version
```

---

## 2. Install Claude Code

Claude Code is Anthropic's AI coding assistant that runs in your terminal.

```bash
npm install -g @anthropic/claude-code
```

Verify it installed:
```bash
claude --version
```

You'll need to authenticate with your Anthropic account the first time you run it.

---

## 3. Create the GitHub Repository

1. Go to https://github.com/new
2. Repository name: `runspec`
3. Owner: `runspec-app`
4. Visibility: Public
5. **Do not** initialise with README, .gitignore, or licence — the scaffold has these
6. Click **Create repository**

---

## 4. Set Up the Project

Unzip the scaffold and push it to GitHub:

```bash
# Unzip (adjust path to where you downloaded runspec.zip)
unzip runspec.zip
cd runspec

# Initialise git and push
git init
git add .
git commit -m "chore: initial mono-repo scaffold"
git branch -M main
git remote add origin https://github.com/runspec-app/runspec.git
git push -u origin main
```

Check GitHub — you should see all the files in the repository.

---

## 5. Set Up the Python Environment

```bash
# From inside the runspec folder
cd packages/python

# Create virtual environment
python -m venv .venv

# Activate it
# On Mac/Linux:
source .venv/bin/activate
# On Windows:
.venv\Scripts\activate

# Install the package in editable mode with dev tools
pip install -e ".[dev]"

# Verify the runspec command is available
runspec --help
```

---

## 6. Run the Tests

```bash
# Still inside packages/python with .venv active
pytest
```

You should see tests collected and running. Some may fail at this stage —
that is expected. The structure is in place and tests are written; the
implementations need completing. This is your starting point.

---

## 7. Open in PyCharm

1. Open PyCharm
2. Click **Open**
3. Navigate to and select the `packages/python/` folder (not the mono-repo root)
4. PyCharm detects `pyproject.toml` and configures the project automatically
5. When prompted, select the existing `.venv` virtual environment

**Why `packages/python/` and not the root?**
PyCharm works best when the folder you open contains the `pyproject.toml`.
You can still browse the full mono-repo from the file tree inside PyCharm.

---

## 8. Start Claude Code

Open a terminal at the **mono-repo root** (the `runspec/` folder, not `packages/python/`):

```bash
# Make sure you're at the root
cd /path/to/runspec

# Start Claude Code
claude
```

Claude Code reads `CLAUDE.md` automatically at the start of every session.
This file gives it the full project context — design decisions, structure,
rules, and current status — so you don't have to re-explain things each time.

---

## 9. Your First Claude Code Session

Once Claude Code starts, try this prompt to verify everything is working:

```
Read CLAUDE.md. Then read packages/python/runspec/runspec/inference.py and
packages/python/runspec/tests/test_inference.py. Tell me which inference rules
are tested and which are missing test coverage.
```

Claude Code should read both files and give you a gap analysis. That's a
good warm-up task that validates it can see the project correctly.

---

## 10. Recommended First Real Task

Once you're comfortable, this is a good first real coding task:

```
Read CLAUDE.md. Then read loader.py. Write a test file at
packages/python/runspec/tests/test_loader.py that tests the load_raw function
for the runspec.toml format. Use the fixtures
in tests/integration/fixtures/ as reference for what valid TOML looks like.
```

---

## Daily Workflow

```bash
# Start of day — activate environment
cd runspec/packages/python
source .venv/bin/activate       # Windows: .venv\Scripts\activate

# Run tests to see current state
pytest

# Open Claude Code (from mono-repo root)
cd ../..
claude

# End of day — commit your work
git add .
git commit -m "feat: describe what you built"
git push
```

---

## Useful Commands Reference

```bash
# Tests
pytest                          # run all tests
pytest tests/test_inference.py  # run one file
pytest -k "test_int"            # run tests matching a name
pytest -v                       # verbose output

# Code quality
ruff check .                    # lint
ruff format .                   # format
mypy runspec/                   # type check

# runspec CLI (after pip install -e ".[dev]")
runspec --help
runspec local
runspec local --format mcp

# Git
git status                      # what's changed
git diff                        # see changes
git add . && git commit -m ""   # commit everything
git push                        # push to GitHub
```

---

## If Something Goes Wrong

**`runspec` command not found**
```bash
# Make sure .venv is active — you should see (.venv) in your prompt
source .venv/bin/activate
pip install -e ".[dev]"
```

**Tests can't import runspec**
```bash
# Make sure you installed in editable mode
pip install -e ".[dev]"
```

**PyCharm doesn't find the interpreter**
- Go to Settings → Project → Python Interpreter
- Click Add → Existing environment
- Navigate to `packages/python/.venv/bin/python`

**Claude Code can't see project files**
- Make sure you started `claude` from the mono-repo root (`runspec/`)
- Not from `packages/python/`

---

## Project Links

- GitHub: https://github.com/runspec-app/runspec
- Design doc: `DESIGN.md` in the repo root
- Format spec: `spec/SPEC.md` in the repo root
- Claude context: `CLAUDE.md` in the repo root
