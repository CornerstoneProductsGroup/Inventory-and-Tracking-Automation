"""Ship today: pull CommerceHub orders, confirm the files that were seen, then FedEx.

CommerceHub Home Depot, Depot Special Order, and Lowe's must be saved for
everything that was seen. FedEx batch runs when Lowe's was seen, after its
Order Splitter output CSV exists.

The CommerceHub Canada pull runs after the US pull. If Canada fails, it is
logged as a warning and FedEx still runs for the US orders.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ORDER_PULLS = _HERE.parent / "Order Pulls"
_CH_MANIFEST = _ORDER_PULLS / "commercehub_pull_manifest.json"

# Names CommerceHub is expected to show.
_CH_NAMES = ("Depot", "Depot Special Order", "Lowe's")


def _log(msg: str) -> None:
    print(f"[ship-today] {msg}", flush=True)


def _run(name: str, cmd: list[str], cwd: Path) -> int:
    _log(f"=== {name} ===")
    completed = subprocess.run(cmd, cwd=str(cwd))
    code = int(completed.returncode or 0)
    if code != 0:
        _log(f"{name} failed (exit {code}).")
    return code


def _load_manifest(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(f"Missing pull record: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _saved_ok(manifest: dict, name: str, kind: str) -> str | None:
    for item in manifest.get("saved") or []:
        if item.get("name") == name and item.get("kind") == kind:
            path = Path(item.get("path") or "")
            if path.is_file():
                return str(path)
            return None
    return None


def _confirm_saved(manifest: dict, names: tuple[str, ...]) -> tuple[list[str], list[str]]:
    """Return (retailer names that were seen, problem messages)."""
    seen_names: list[str] = []
    problems: list[str] = []
    for item in manifest.get("seen") or []:
        name = item.get("name") or ""
        kind = item.get("kind") or ""
        if name not in names:
            continue
        if name not in seen_names:
            seen_names.append(name)
        path = _saved_ok(manifest, name, kind)
        if path:
            _log(f"CHECK saved {name} {kind.upper()} -> {path}")
        else:
            file_name = item.get("file") or kind
            problems.append(
                f"{name} {kind.upper()} was seen on CommerceHub ({file_name}) but was not saved."
            )
    return seen_names, problems


def _wait_for_lowes_output() -> int:
    from automation.fedex_batch_config import lowes_output_path

    path = lowes_output_path()
    wait_min = float((os.environ.get("SHIP_TODAY_CSV_WAIT_MINUTES") or "45").strip() or "45")
    deadline = time.monotonic() + max(1.0, wait_min) * 60.0
    while True:
        if path.is_file():
            _log(f"CHECK output CSV Lowe's -> {path}")
            return 0
        if time.monotonic() >= deadline:
            _log(f"Stopping. Lowe's output CSV did not appear before the wait ended: {path}")
            return 1
        _log("Still waiting for: Lowe's output CSV")
        time.sleep(15)


def main() -> int:
    os.chdir(_HERE)
    if str(_HERE) not in sys.path:
        sys.path.insert(0, str(_HERE))

    py = sys.executable
    pull_code = 0
    canada_failed = False
    # (title, script, extra args, required). Canada is not required: a failure
    # there is reported but does not stop FedEx.
    pulls = (
        ("commercehub_pull.py", "commercehub_pull.py", [], True),
        ("commercehub_pull.py --account canada", "commercehub_pull.py", ["--account", "canada"], False),
    )
    for title, script, extra, required in pulls:
        path = _ORDER_PULLS / script
        if not path.is_file():
            _log(f"Missing {path}")
            return 1
        code = _run(title, [py, str(path), *extra], _ORDER_PULLS)
        if code != 0:
            if required:
                pull_code = code
            else:
                canada_failed = True
                _log(f"WARNING: CommerceHub Canada pull failed (exit {code}). "
                     "Check Order Pulls/commercehub_canada_pull.log. Continuing with US.")
    if pull_code != 0:
        _log("Order pull did not finish cleanly. FedEx will not start.")
        return pull_code

    try:
        commercehub = _load_manifest(_CH_MANIFEST)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        _log(f"Cannot confirm what was seen: {exc}")
        return 1

    ch_seen = sorted({item.get("name") for item in commercehub.get("seen") or [] if item.get("name")})
    _log(
        "CHECK CommerceHub saw: "
        + (", ".join(ch_seen) if ch_seen else "(no Home Depot, Depot Special Order, or Lowe's files)")
    )

    ch_names, problems = _confirm_saved(commercehub, _CH_NAMES)
    if problems:
        _log("Downloaded files do not match what was seen:")
        for problem in problems:
            _log(f"  {problem}")
        _log("FedEx will not start.")
        return 1

    if "Lowe's" in ch_names:
        if _wait_for_lowes_output() != 0:
            _log("FedEx will not start.")
            return 1
        code = _run("FedEx batch", [py, str(_HERE / "run_fedex_batch.py")], _HERE)
        if code != 0:
            return code
    else:
        _log("Lowe's was not on CommerceHub. Skipping FedEx.")

    if canada_failed:
        _log("REMINDER: the CommerceHub Canada pull failed earlier. Pull Canada again (menu 0).")
    _log("Ship today finished.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
