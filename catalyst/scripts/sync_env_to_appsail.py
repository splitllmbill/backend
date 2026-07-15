#!/usr/bin/env python3
"""Copy ../../.env values into appsail/app-config.json env_variables.

Convenience for a *manual* local `catalyst deploy` when you want the deployed
app to run with the same config as your local backend. HOST/PORT are skipped
(AppSail supplies the port).

SECURITY: app-config.json is tracked in git with placeholder values. Running
this fills it with your real secrets, so DO NOT commit the result --
`git checkout catalyst/appsail/app-config.json` (or `git stash`) after
deploying. CI does not use this script; production secrets belong in the
Catalyst console env_variables.
"""

import json
from pathlib import Path

CATALYST_DIR = Path(__file__).resolve().parent.parent  # backend/catalyst
REPO_DIR = CATALYST_DIR.parent                          # backend
ENV_FILE = REPO_DIR / ".env"
APP_CONFIG = CATALYST_DIR / "appsail" / "app-config.json"

SKIP_KEYS = {"HOST", "PORT"}


def parse_env(path: Path) -> dict:
    env = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in SKIP_KEYS:
            env[key] = value
    return env


def main():
    if not ENV_FILE.exists():
        raise SystemExit(f"{ENV_FILE} not found")
    config = json.loads(APP_CONFIG.read_text())
    env = parse_env(ENV_FILE)
    config["env_variables"] = {**config.get("env_variables", {}), **env}
    APP_CONFIG.write_text(json.dumps(config, indent=4) + "\n")
    print(f"Synced {len(env)} env vars from {ENV_FILE} -> {APP_CONFIG}")
    print("Reminder: do NOT commit app-config.json with real secrets.")


if __name__ == "__main__":
    main()
