#!/usr/bin/env python3
"""
Validates every configs/*.json tenant config before it can be merged.

The deployment notebook validates a config at runtime, but by then a bad one
has already been merged and a deploy has already been requested. This runs on
the PR instead. It is deliberately stricter than the runtime check.

NOTE: KNOWN_MODULE_KEYS must be kept in sync with the same-named set in the
deployment notebook's "resolve tenant config" cell.

Usage:  python validate_tenant_configs.py [configs_dir]   (default: configs)
Exit code 1 if any config is invalid; all problems are reported, not just the first.
"""
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

KNOWN_MODULE_KEYS = {
    "fetch_releases", "create_folders", "create_environment",
    "create_lakehouses", "deploy_notebooks", "deploy_dashboards",
    "bootstrap_and_rebind",
}
ALLOWED_TOP_LEVEL = {"workspace", "modules", "credentials", "_comment"}
CREDENTIAL_FIELDS = {"vault_name", "tenant_id_secret", "client_id_secret", "client_secret_secret"}

GUID = re.compile(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")
# The file name doubles as the GitHub Environment name for Prod approvals.
NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")
TEMPLATE_SUFFIX = "-EXAMPLE"


def validate(path: Path):
    """Returns (errors, workspace_or_None)."""
    errors = []
    stem = path.stem
    is_template = stem.endswith(TEMPLATE_SUFFIX)
    base = stem[: -len(TEMPLATE_SUFFIX)] if is_template else stem

    if not NAME.match(base):
        errors.append(f"file name '{path.name}': use lowercase letters, digits and hyphens only")

    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return [f"not valid JSON: {e}"], None

    if not isinstance(cfg, dict):
        return ["top level must be a JSON object"], None

    unknown = set(cfg) - ALLOWED_TOP_LEVEL
    if unknown:
        errors.append(f"unknown top-level key(s): {sorted(unknown)} (allowed: {sorted(ALLOWED_TOP_LEVEL)})")

    workspace = cfg.get("workspace")
    if not isinstance(workspace, str) or not workspace.strip():
        errors.append("'workspace' is required and must be a non-empty string")
        workspace = None
    elif not is_template and not GUID.match(workspace):
        errors.append(f"'workspace' is not a valid GUID: '{workspace}' (placeholder left in?)")

    modules = cfg.get("modules")
    if not isinstance(modules, dict):
        errors.append("'modules' is required and must be an object")
    else:
        bad_keys = set(modules) - KNOWN_MODULE_KEYS
        if bad_keys:
            errors.append(f"unknown module key(s): {sorted(bad_keys)} (known: {sorted(KNOWN_MODULE_KEYS)})")
        for key, value in modules.items():
            if not isinstance(value, bool):
                errors.append(f"module '{key}' must be true or false, got {value!r}")

    creds = cfg.get("credentials")
    if creds is not None:
        if not isinstance(creds, dict):
            errors.append("'credentials' must be an object")
        else:
            missing = CREDENTIAL_FIELDS - set(creds)
            extra = set(creds) - CREDENTIAL_FIELDS
            if missing:
                errors.append(f"'credentials' is missing: {sorted(missing)}")
            if extra:
                # Guards against someone pasting a real secret value under a made-up key.
                errors.append(f"'credentials' has unexpected key(s): {sorted(extra)}. "
                              "It must only hold Key Vault secret NAMES, never secret values")
            for key in CREDENTIAL_FIELDS & set(creds):
                if not isinstance(creds[key], str) or not creds[key].strip():
                    errors.append(f"credentials.{key} must be a non-empty string")

    if not is_template:
        def walk(value, where):
            if isinstance(value, str) and value.startswith("REPLACE"):
                errors.append(f"{where} still holds a placeholder: '{value}'")
            elif isinstance(value, dict):
                for k, v in value.items():
                    walk(v, f"{where}.{k}")
        walk(cfg, "config")

    return errors, (None if is_template else workspace)


def main():
    configs_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "configs")
    if not configs_dir.is_dir():
        print(f"No '{configs_dir}' directory yet, nothing to validate.")
        return 0

    files = sorted(configs_dir.glob("*.json"))
    if not files:
        print(f"No config files in '{configs_dir}', nothing to validate.")
        return 0

    failed = False
    by_workspace = defaultdict(list)

    for path in files:
        errors, workspace = validate(path)
        if errors:
            failed = True
            for err in errors:
                print(f"::error file={path}::{path.name}: {err}")
        else:
            print(f"ok  {path.name}")
        if workspace:
            by_workspace[workspace.lower()].append(path.name)

    for workspace, names in by_workspace.items():
        if len(names) > 1:
            failed = True
            print(f"::error::Workspace {workspace} is used by more than one config: {sorted(names)}. "
                  "Two targets must never deploy into the same workspace.")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
