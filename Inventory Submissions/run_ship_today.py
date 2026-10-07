"""Ship today: pull orders, confirm the files that were seen, then WorldShip and FedEx.

CommerceHub Home Depot, Depot Special Order, and Lowe's must be saved and have
an Order Splitter output CSV. Depot, Depot Special Order, and Tractor Supply
must also be in the WorldShip CornerstoneMaster file before either ship step.
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
_SPS_MANIFEST = _ORDER_PULLS / "sps_pull_manifest.json"

# Names CommerceHub is expected to show, and the output CSV each one must produce.
_CH_OUTPUTS = ("Depot", "Depot Special Order", "Lowe's")
# These three land in CornerstoneMaster. Lowe's does not.
_MASTER_KEYS = {
    "Depot": "depot",
    "Depot Special Order": "thdso",
    "Tractor Supply": "tractor",
}


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


def _output_path(name: str) -> Path:
    from automation.fedex_batch_config import lowes_output_path
    from automation.ups_batch_config import lane_output_path

    if name == "Depot":
        return lane_output_path("depot")
    if name == "Depot Special Order":
        return lane_output_path("thdso")
    if name == "Lowe's":
        return lowes_output_path()
    if name == "Tractor Supply":
        return lane_output_path("tractor")
    raise KeyError(name)


def _saved_ok(manifest: dict, name: str, kind: str) -> str | None:
    for item in manifest.get("saved") or []:
        if item.get("name") == name and item.get("kind") == kind:
            path = Path(item.get("path") or "")
            if path.is_file():
                return str(path)
            return None
    return None


def _confirm_saved(manifest: dict, names: tuple[str, ...], *, require_both_kinds: bool) -> tuple[list[str], list[str]]:
    """Return (retailer names that were seen, problem messages)."""
    seen_names: list[str] = []
    problems: list[str] = []
    seen = manifest.get("seen") or []
    if require_both_kinds:
        for item in seen:
            name = item.get("name") or ""
            if name not in names and name not in _MASTER_KEYS:
                continue
            if name not in seen_names:
                seen_names.append(name)
            for kind in ("pdf", "csv"):
                path = _saved_ok(manifest, name, kind)
                if path:
                    _log(f"CHECK saved {name} {kind.upper()} -> {path}")
                else:
                    problems.append(f"{name} was seen on SPS but the {kind.upper()} was not saved.")
        return seen_names, problems

    for item in seen:
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


def _master_counts(needed: set[str]) -> dict[str, int]:
    from automation.worldship_cornerstone_master import load_cornerstone_orders

    counts: dict[str, int] = {key: 0 for key in needed}
    try:
        rows = load_cornerstone_orders()
    except Exception as exc:
        _log(f"CornerstoneMaster not ready yet: {exc}")
        return counts
    for row in rows:
        if row.retailer_key in counts:
            counts[row.retailer_key] += 1
    return counts


def _wait_for_outputs_and_master(output_names: list[str], master_names: list[str]) -> int:
    outputs = [(name, _output_path(name)) for name in output_names]
    master_needed = {_MASTER_KEYS[name] for name in master_names}
    wait_min = float((os.environ.get("SHIP_TODAY_CSV_WAIT_MINUTES") or "45").strip() or "45")
    deadline = time.monotonic() + max(1.0, wait_min) * 60.0
    while True:
        missing_files = [(name, path) for name, path in outputs if not path.is_file()]
        counts = _master_counts(master_needed) if master_needed else {}
        missing_master = [name for name in master_names if counts.get(_MASTER_KEYS[name], 0) == 0]
        if not missing_files and not missing_master:
            for name, path in outputs:
                _log(f"CHECK output CSV {name} -> {path}")
            for name in master_names:
                key = _MASTER_KEYS[name]
                _log(f"CHECK WorldShip master has {name}: {counts[key]} row(s)")
            return 0
        if time.monotonic() >= deadline:
            _log("Stopping. What was seen did not all become output before the wait ended.")
            for name, path in missing_files:
                _log(f"  missing output CSV for {name}: {path}")
            for name in missing_master:
                _log(f"  missing from WorldShip master CSV: {name}")
            return 1
        waiting = [name for name, _path in missing_files] + [
            f"{name} in master" for name in missing_master
        ]
        _log("Still waiting for: " + ", ".join(waiting))
        time.sleep(15)


def main() -> int:
    os.chdir(_HERE)
    if str(_HERE) not in sys.path:
        sys.path.insert(0, str(_HERE))

    py = sys.executable
    pull_code = 0
    for script in ("commercehub_pull.py", "sps_pull.py"):
        path = _ORDER_PULLS / script
        if not path.is_file():
            _log(f"Missing {path}")
            return 1
        code = _run(script, [py, str(path)], _ORDER_PULLS)
        if code != 0:
            pull_code = code
    if pull_code != 0:
        _log("Order pull did not finish cleanly. WorldShip and FedEx will not start.")
        return pull_code

    try:
        commercehub = _load_manifest(_CH_MANIFEST)
        sps = _load_manifest(_SPS_MANIFEST)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        _log(f"Cannot confirm what was seen: {exc}")
        return 1

    ch_seen = sorted({item.get("name") for item in commercehub.get("seen") or [] if item.get("name")})
    _log(
        "CHECK CommerceHub saw: "
        + (", ".join(ch_seen) if ch_seen else "(no Home Depot, Depot Special Order, or Lowe's files)")
    )
    sps_seen = [item.get("name") for item in sps.get("seen") or []]
    _log("CHECK SPS saw: " + (", ".join(sps_seen) if sps_seen else "(none)"))

    ch_names, ch_problems = _confirm_saved(commercehub, _CH_OUTPUTS, require_both_kinds=False)
    sps_names, sps_problems = _confirm_saved(sps, ("Tractor Supply",), require_both_kinds=True)
    problems = ch_problems + sps_problems
    if problems:
        _log("Downloaded files do not match what was seen:")
        for problem in problems:
            _log(f"  {problem}")
        _log("WorldShip and FedEx will not start.")
        return 1

    output_names = [name for name in _CH_OUTPUTS if name in ch_names]
    if "Tractor Supply" in sps_names:
        output_names.append("Tractor Supply")
    master_names = [name for name in ("Depot", "Depot Special Order", "Tractor Supply") if name in ch_names or name in sps_names]

    if not output_names:
        _log("No Home Depot, Depot Special Order, Lowe's, or Tractor Supply files were seen. WorldShip and FedEx will not start.")
        return 0

    if _wait_for_outputs_and_master(output_names, master_names) != 0:
        _log("WorldShip and FedEx will not start.")
        return 1

    if master_names:
        code = _run(
            "WorldShip save rows",
            [py, str(_HERE / "run_worldship_import.py"), "--stop-before-print"],
            _HERE,
        )
        if code != 0:
            _log("WorldShip did not finish the save rows. FedEx batch will not start.")
            return code
        _log("WorldShip is still open for warehouse printing.")
    else:
        _log("No Depot, Depot Special Order, or Tractor Supply files were seen. Skipping WorldShip.")

    if "Lowe's" in ch_names:
        code = _run("FedEx batch", [py, str(_HERE / "run_fedex_batch.py")], _HERE)
        if code != 0:
            return code
    else:
        _log("Lowe's was not on CommerceHub. Skipping FedEx.")

    _log("Ship today finished.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
