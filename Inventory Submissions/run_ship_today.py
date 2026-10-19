"""Ship today: US CommerceHub files, the Canada PDF, then FedEx if Lowe's saved.

US CommerceHub saves the PDF and CSV for Depot, Depot Special Order, and Lowe's.
Canada CommerceHub saves the packing-slip PDF. WorldShip and SPS are not part
of this run. FedEx starts only after the Lowe's PDF and CSV are both on disk.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ORDER_PULLS = _HERE.parent / "Order Pulls"
_CH_MANIFEST = _ORDER_PULLS / "commercehub_pull_manifest.json"
_CA_MANIFEST = _ORDER_PULLS / "commercehub_canada_pull_manifest.json"

_US_NAMES = ("Depot", "Depot Special Order", "Lowe's")
_US_KINDS = ("pdf", "csv")


def _log(msg: str) -> None:
    print(f"[ship-today] {msg}", flush=True)


def _run(name: str, cmd: list[str], cwd: Path) -> int:
    _log(f"=== {name} ===")
    completed = subprocess.run(cmd, cwd=str(cwd))
    code = int(completed.returncode or 0)
    if code != 0:
        _log(f"{name} failed (exit {code}).")
    return code


def _today_label() -> str:
    today = date.today()
    return f"{today.month}-{today.day}-{today.year}"


def _load_manifest(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _log(f"Cannot read {path.name}: {exc}")
        return None
    if not isinstance(data, dict):
        return None
    if data.get("date") != _today_label():
        _log(f"{path.name} is not from today ({data.get('date') or 'no date'}).")
        return None
    return data


def _saved_paths(manifest: dict, name: str, kind: str) -> list[str]:
    paths: list[str] = []
    for item in manifest.get("saved") or []:
        if item.get("name") == name and item.get("kind") == kind:
            path = Path(item.get("path") or "")
            if path.is_file():
                paths.append(str(path))
    return paths


def _saved_path(manifest: dict, name: str, kind: str) -> str | None:
    paths = _saved_paths(manifest, name, kind)
    return paths[0] if paths else None


def _was_seen(manifest: dict, name: str, kind: str | None = None) -> bool:
    for item in manifest.get("seen") or []:
        if item.get("name") != name:
            continue
        if kind is None or item.get("kind") == kind:
            return True
    return False


def _report_us(manifest: dict) -> None:
    seen = sorted({item.get("name") for item in manifest.get("seen") or [] if item.get("name")})
    _log(
        "CHECK US CommerceHub saw: "
        + (", ".join(seen) if seen else "(no Depot, Depot Special Order, or Lowe's files)")
    )
    for name in _US_NAMES:
        if name == "Lowe's":
            continue
        if not _was_seen(manifest, name):
            _log(f"CHECK {name}: not on US CommerceHub.")
            continue
        for kind in _US_KINDS:
            if not _was_seen(manifest, name, kind):
                _log(f"CHECK {name} {kind.upper()}: not on US CommerceHub.")
                continue
            paths = _saved_paths(manifest, name, kind)
            seen_count = sum(
                1
                for item in manifest.get("seen") or []
                if item.get("name") == name and item.get("kind") == kind
            )
            for path in paths:
                _log(f"CHECK saved {name} {kind.upper()} -> {path}")
            if len(paths) < seen_count:
                _log(
                    f"WARNING: {name} {kind.upper()} saved {len(paths)} of {seen_count} file(s)."
                )


def _lowes_saved(manifest: dict) -> bool | None:
    """True when both Lowe's files are saved, False when a seen file is missing, None when Lowe's was not there."""
    if not _was_seen(manifest, "Lowe's"):
        _log("Lowe's was not on US CommerceHub. FedEx will not start.")
        return None
    missing: list[str] = []
    for kind in _US_KINDS:
        path = _saved_path(manifest, "Lowe's", kind)
        if path:
            _log(f"CHECK saved Lowe's {kind.upper()} -> {path}")
        else:
            missing.append(kind.upper())
    if missing:
        _log(
            "Lowe's "
            + " and ".join(missing)
            + " was not saved. FedEx will not start."
        )
        return False
    _log("Lowe's PDF and CSV are saved.")
    return True


def _report_canada(manifest: dict | None, pull_code: int) -> None:
    if manifest is None:
        if pull_code != 0:
            _log(
                "WARNING: CommerceHub Canada pull failed and no PDF record was written. "
                "FedEx still runs if Lowe's was saved."
            )
        else:
            _log("CHECK CommerceHub Canada: no PDF record (nothing waiting, or Canada is not set up).")
        return
    seen = [item.get("name") for item in manifest.get("seen") or [] if item.get("name")]
    _log("CHECK CommerceHub Canada saw: " + (", ".join(seen) if seen else "(none)"))
    if not seen:
        if pull_code != 0:
            _log("WARNING: CommerceHub Canada pull failed. FedEx still runs if Lowe's was saved.")
        return
    for item in manifest.get("seen") or []:
        name = item.get("name") or ""
        kind = item.get("kind") or "pdf"
        path = _saved_path(manifest, name, kind)
        if path:
            _log(f"CHECK saved {name} {kind.upper()} -> {path}")
        else:
            _log(
                f"WARNING: {name} {kind.upper()} was seen on CommerceHub Canada but was not saved. "
                "FedEx still runs if Lowe's was saved."
            )


def main() -> int:
    os.chdir(_HERE)
    if str(_HERE) not in sys.path:
        sys.path.insert(0, str(_HERE))
    py = sys.executable
    us_script = _ORDER_PULLS / "commercehub_pull.py"
    if not us_script.is_file():
        _log(f"Missing {us_script}")
        return 1

    us_code = _run(
        "US CommerceHub",
        [py, str(us_script), "--only", *_US_NAMES],
        _ORDER_PULLS,
    )
    us_manifest = _load_manifest(_CH_MANIFEST)
    if us_manifest is None:
        _log("Cannot confirm today's US CommerceHub download.")
    else:
        _report_us(us_manifest)

    ca_code = _run(
        "CommerceHub Canada",
        [py, str(us_script), "--account", "canada"],
        _ORDER_PULLS,
    )
    _report_canada(_load_manifest(_CA_MANIFEST), ca_code)

    if us_manifest is None:
        _log("FedEx will not start.")
        return us_code or 1

    lowes_ready = _lowes_saved(us_manifest)
    if lowes_ready is None:
        if ca_code != 0:
            _log("REMINDER: the CommerceHub Canada PDF pull failed earlier.")
        _log("Ship today finished.")
        return 0
    if not lowes_ready:
        return 1

    code = _run("FedEx batch", [py, str(_HERE / "run_fedex_batch.py")], _HERE)
    if code != 0:
        return code
    if ca_code != 0:
        _log("REMINDER: the CommerceHub Canada PDF pull failed earlier. Pull Canada again from menu 0.")
    _log("Ship today finished.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
