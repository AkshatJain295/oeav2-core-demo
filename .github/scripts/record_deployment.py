#!/usr/bin/env python3
"""
Appends one deployment to the deployment log and re-renders the human-readable view.

Called by the deploy workflow's "record" job, inside a checkout of the `logs` branch.
Writes three files into --dir:

  deployments.jsonl   full history, one JSON object per line (append-only, machine-readable)
  current_state.json  what each tenant is running right now (machine-readable)
  README.md           the same, as tables. GitHub renders it when you open the `logs` branch.

Usage:
  python record_deployment.py --dir <logs-checkout> --entry '<json>'
  python record_deployment.py --dir <logs-checkout> --render-only

Standard library only.
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REQUIRED = ("timestamp", "tenant", "core_release_tag", "status", "deployed_by", "dry_run")
STATUSES = ("success", "failed", "cancelled")
RECENT_LIMIT = 50
CELL_LIMIT = 160


def load_entries(path: Path):
    """Reads the history. A damaged line is skipped, never fatal: a log must not stop deploys."""
    entries, skipped = [], 0
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                skipped += 1
    return entries, skipped


def validate_entry(entry: dict) -> None:
    missing = [k for k in REQUIRED if k not in entry]
    if missing:
        raise ValueError(f"entry is missing required field(s): {missing}")
    if entry["status"] not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}, got {entry['status']!r}")
    if not isinstance(entry["dry_run"], bool):
        raise ValueError("dry_run must be true or false")


def _time(ts: str) -> str:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")
    except (ValueError, AttributeError):
        return str(ts or "")


def _cell(value, limit: int = CELL_LIMIT) -> str:
    text = " ".join(str(value if value is not None else "").split())
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text.replace("|", "\\|")


def _plural(n: int, noun: str) -> str:
    return noun if n == 1 else noun + "s"


def _people(names) -> str:
    return ", ".join(names) if names else "—"


def _run_link(entry: dict) -> str:
    url = entry.get("run_url")
    return f"[run]({url})" if url else "—"


def build_state(entries):
    """Per tenant: the latest REAL successful deploy, and whether a later real attempt failed."""
    real = sorted((e for e in entries if not e.get("dry_run")), key=lambda e: e["timestamp"])
    state = {}
    for tenant in sorted({e["tenant"] for e in real}):
        attempts = [e for e in real if e["tenant"] == tenant]
        last_ok = next((e for e in reversed(attempts) if e["status"] == "success"), None)
        latest = attempts[-1]
        state[tenant] = {
            "release": last_ok["core_release_tag"] if last_ok else None,
            "deployed_by": last_ok["deployed_by"] if last_ok else None,
            "approved_by": last_ok.get("approved_by", []) if last_ok else [],
            "deployed_at": last_ok["timestamp"] if last_ok else None,
            "run_url": last_ok.get("run_url") if last_ok else None,
            "latest_attempt": {"status": latest["status"], "at": latest["timestamp"], "run_url": latest.get("run_url")},
        }
    return state


def render_readme(entries, skipped: int = 0) -> str:
    state = build_state(entries)
    real_count = sum(1 for e in entries if not e.get("dry_run"))
    out = [
        "# Deployment log",
        "",
        "Written automatically by the deploy workflows. Do not edit by hand. Times are UTC.",
        "",
        f"{len(entries)} {_plural(len(entries), 'deployment')} recorded "
        f"({real_count} real, {len(entries) - real_count} {_plural(len(entries) - real_count, 'dry run')}).",
        "",
        "## Running now",
        "",
        "Latest successful real deployment per tenant. Dry runs never count.",
        "",
    ]
    if state:
        out += ["| Tenant | Release | Deployed by | Approved by | Since (UTC) | Run | Latest attempt |",
                "|---|---|---|---|---|---|---|"]
        for tenant, s in state.items():
            la = s["latest_attempt"]
            ok_row = s["deployed_at"] is not None
            same = ok_row and la["at"] == s["deployed_at"] and la["status"] == "success"
            attempt = "ok" if same else f"**{la['status'].upper()}** {_time(la['at'])}" + (f" ([run]({la['run_url']}))" if la.get("run_url") else "")
            out.append("| " + " | ".join([
                _cell(tenant),
                _cell(s["release"]) if s["release"] else "none yet",
                _cell(s["deployed_by"]) if s["deployed_by"] else "—",
                _cell(_people(s["approved_by"])),
                _time(s["deployed_at"]) if ok_row else "—",
                f"[run]({s['run_url']})" if s["run_url"] else "—",
                attempt,
            ]) + " |")
    else:
        out.append("_Nothing has been deployed yet._")

    out += ["", f"## Recent deployments (newest first, last {RECENT_LIMIT})", ""]
    recent = sorted(entries, key=lambda e: e["timestamp"], reverse=True)[:RECENT_LIMIT]
    if recent:
        out += ["| When (UTC) | Tenant | Release | Result | Dry run | Deployed by | Approved by | Modules | Run |",
                "|---|---|---|---|---|---|---|---|---|"]
        for e in recent:
            result = e["status"] if e["status"] == "success" else f"**{e['status'].upper()}**"
            if e["status"] != "success" and e.get("error"):
                result += f": {_cell(e['error'], 80)}"
            out.append("| " + " | ".join([
                _time(e["timestamp"]),
                _cell(e["tenant"]),
                _cell(e["core_release_tag"]),
                result,
                "yes" if e.get("dry_run") else "no",
                _cell(e["deployed_by"]),
                _cell(_people(e.get("approved_by"))),
                _cell(", ".join(e.get("modules_enabled") or []) or "—"),
                _run_link(e),
            ]) + " |")
    else:
        out.append("_Nothing recorded yet._")

    if skipped:
        out += ["", f"_{skipped} damaged line(s) in deployments.jsonl were skipped when building this page._"]
    return "\n".join(out) + "\n"


def record(directory: Path, entry: dict | None) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    history = directory / "deployments.jsonl"
    if entry is not None:
        validate_entry(entry)
        # A hand edit (or the web editor) can leave the file without a final newline.
        # Appending straight after it would glue two entries onto one line and corrupt both.
        needs_newline = history.exists() and history.stat().st_size > 0 and not history.read_bytes().endswith(b"\n")
        with history.open("a", encoding="utf-8") as fh:
            if needs_newline:
                fh.write("\n")
            fh.write(json.dumps(entry, separators=(",", ":"), sort_keys=True) + "\n")
    entries, skipped = load_entries(history)
    (directory / "README.md").write_text(render_readme(entries, skipped), encoding="utf-8")
    (directory / "current_state.json").write_text(json.dumps(build_state(entries), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", required=True, type=Path)
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--entry", help="the deployment, as a JSON object")
    group.add_argument("--render-only", action="store_true")
    args = ap.parse_args(argv)
    try:
        record(args.dir, None if args.render_only else json.loads(args.entry))
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
