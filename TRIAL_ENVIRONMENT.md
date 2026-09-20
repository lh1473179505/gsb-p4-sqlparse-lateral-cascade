# Offline development environment

Python ≥3.10 (verified on 3.12). Upstream sqlparse source without functional patches.

Recreate the environment:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -U pip setuptools wheel hatchling editables pathspec packaging
.venv/bin/python -m pip install -r requirements-trial.txt
.venv/bin/python -m pip install --no-build-isolation --no-deps -e .
```

Run the offline regression suite from the repository root:

```sh
.venv/bin/python -m pytest -q
```

External HTTP is not required.
