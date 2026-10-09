#!/bin/sh
# Refuse unsupported Python and failed setup steps; never overwrite existing files.
set -eu
store_root=
store_root_given=0
python=python3
while [ "$#" -gt 0 ]; do
    case "$1" in
        --store-root|--python)
            option=$1
            [ "$#" -ge 2 ] || { echo "setup: missing value for $option" >&2; exit 2; }
            case "$option" in
                --store-root) store_root=$2; store_root_given=1 ;;
                --python) python=$2 ;;
            esac
            shift 2 ;;
        *) echo "setup: unknown argument $1" >&2; exit 2 ;;
    esac
done
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
if ! python_version=$("$python" -c 'import sys; print("%d.%d" % sys.version_info[:2])'); then
    echo "setup: failed Python version step: $python must be >= 3.11" >&2
    exit 1
fi
major=${python_version%%.*}
minor=${python_version#*.}
case "$python_version" in
    *.*) ;;
    *)
        echo "setup: failed Python version step: $python must be >= 3.11; invalid version $python_version" >&2
        exit 1 ;;
esac
case "$major:$minor" in
    *[!0-9:]*|:*|*:)
        echo "setup: failed Python version step: $python must be >= 3.11; invalid version $python_version" >&2
        exit 1 ;;
esac
if [ "$major" -lt 3 ] || { [ "$major" -eq 3 ] && [ "$minor" -lt 11 ]; }; then
    echo "setup: failed Python version step: $python must be >= 3.11" >&2
    exit 1
fi
exec "$python" - "$script_dir" "$store_root" "$store_root_given" <<'PY'
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True
scripts = Path(sys.argv[1]).resolve()
skill = scripts.parent
repo = skill.parents[2]
step = "resolve store root"
try:
    config = skill / "config/store.json"
    if sys.argv[3] == "1":
        root = Path(sys.argv[2])
    elif config.exists() or config.is_symlink():
        step = "resolve configured store root"
        configured = json.loads(config.read_bytes())
        configured_root = configured.get("root") if isinstance(configured, dict) else None
        if not isinstance(configured_root, str) or not Path(configured_root).is_absolute():
            raise ValueError(f"{config} root {configured_root!r} must be an absolute string")
        root = Path(configured_root)
    elif sys.platform == "darwin":
        root = Path(os.environ["HOME"]) / "Library/Application Support/minsky/store"
    else:
        root = Path(os.environ.get("XDG_DATA_HOME") or str(Path(os.environ["HOME"]) / ".local/share")) / "minsky/store"
    if not root.is_absolute():
        raise ValueError(f"store root must be absolute: {root}")
    step = "render config/store.json"
    if config.exists() or config.is_symlink():
        print(f"skipped {step}: {config} already exists")
    else:
        value = json.loads((skill / "config/store.json.example").read_bytes())
        if value != {"store_id": "mars-minsky-cas-v1", "root": "__MINSKY_STORE_ROOT__", "enabled": True}:
            raise ValueError("config/store.json.example does not match the required format")
        value["root"] = str(root)
        with config.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(value) + "\n")
        print(f"done {step}: {config}")

    step = "resolve configured store root"
    configured = json.loads(config.read_bytes())
    configured_root = configured.get("root") if isinstance(configured, dict) else None
    if not isinstance(configured_root, str) or Path(configured_root) != root:
        raise ValueError(f"{config} root {configured_root!r} differs from requested root {root}; existing config preserved")
    step = "store.py init"
    if root.exists() or root.is_symlink():
        print(f"skipped {step}: {root} already exists")
    else:
        subprocess.run([sys.executable, str(scripts / "store.py"), "init", "--root", str(root)], check=True)
        print(f"done {step}: {root}")
    step = "store.py check"
    # Validate the persisted config, not an unrelated store inherited from the caller.
    checked = subprocess.run([sys.executable, str(scripts / "store.py"), "check", "--config", str(config)],
                             env={key: value for key, value in os.environ.items() if key != "MINSKY_STORE_ROOT"},
                             check=True, capture_output=True, text=True)
    validation = json.loads(checked.stdout)
    if validation.get("validation") != "ok":
        raise ValueError(f"store.py check refused {root}: {checked.stdout.strip()}")
    print(f"done {step}: {checked.stdout.strip()}")

    for source, target, label in (
        (repo / ".opencode/agents/minsky-reviewer.md.template", repo / ".opencode/agents/minsky-reviewer.md",
         "copy reviewer template"),
        (repo / ".minsky/binding.yaml.example", repo / ".minsky/binding.yaml", "copy binding example"),
    ):
        step = label
        if target.exists() or target.is_symlink():
            print(f"skipped {step}: {target} already exists")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open("rb") as original, target.open("xb") as destination:
                shutil.copyfileobj(original, destination)
            print(f"done {step}: {target}")
        if label == "copy reviewer template":
            print("EDIT: set your research workspace and audit's exact output paths; re-run the no-provider canary probe after every change")
    step = "audit-db.py initdb"
    subprocess.run([sys.executable, str(scripts / "audit-db.py"), "initdb"], cwd=repo, check=True)
    print(f"done {step}")
except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
    detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) and exc.stderr else str(exc)
    sys.stderr.write(f"setup: failed {step}: {detail}\n")
    sys.exit(1)
PY
