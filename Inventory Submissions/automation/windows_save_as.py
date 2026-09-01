"""Fill native Windows Save As / Save dialogs (Win32 — WorldShip Save Print Output)."""

from __future__ import annotations

import ctypes
import os
import time
from pathlib import Path


def _log(msg: str) -> None:
    print(f"[worldship/save] {msg}", flush=True)


def _set_clipboard(text: str) -> None:
    import win32clipboard
    import win32con

    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
    finally:
        win32clipboard.CloseClipboard()


def _send_vk(vk: int) -> None:
    import win32api
    import win32con

    win32api.keybd_event(vk, 0, 0, 0)
    win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)


def _send_alt_key(ch: str) -> None:
    import win32api
    import win32con

    vk = ord(ch.upper())
    win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
    win32api.keybd_event(vk, 0, 0, 0)
    win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)
    win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)


def _send_ctrl_v() -> None:
    import win32api
    import win32con

    win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)
    win32api.keybd_event(ord("V"), 0, 0, 0)
    win32api.keybd_event(ord("V"), 0, win32con.KEYEVENTF_KEYUP, 0)
    win32api.keybd_event(win32con.VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)


def _send_ctrl_a() -> None:
    import win32api
    import win32con

    win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)
    win32api.keybd_event(ord("A"), 0, 0, 0)
    win32api.keybd_event(ord("A"), 0, win32con.KEYEVENTF_KEYUP, 0)
    win32api.keybd_event(win32con.VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)


_last_nav_folder: Path | None = None


def _pause_s(env_key: str, default: float) -> float:
    raw = (os.environ.get(env_key) or str(default)).strip()
    try:
        return max(0.05, float(raw))
    except ValueError:
        return default


def _folder_nav_pause_s() -> float:
    return _pause_s("WORLDSHIP_SAVE_FOLDER_NAV_S", 1.2)


def _filename_settle_s() -> float:
    return _pause_s("WORLDSHIP_SAVE_FILENAME_SETTLE_S", 0.35)


def _after_folder_pause_s() -> float:
    """Pause after folder path is entered — lets the Save dialog finish loading."""
    return _pause_s("WORLDSHIP_SAVE_AFTER_FOLDER_S", 1.0)


def _filename_entry_attempts() -> int:
    raw = (os.environ.get("WORLDSHIP_SAVE_FILENAME_ATTEMPTS") or "3").strip()
    try:
        return max(1, min(5, int(raw)))
    except ValueError:
        return 3


def _reset_last_save_folder() -> None:
    global _last_nav_folder
    _last_nav_folder = None


def reset_last_save_folder() -> None:
    """Clear cached folder at start of a new label batch."""
    _reset_last_save_folder()


def _norm_path(p: str | Path) -> str:
    s = str(p).strip().strip('"').replace("/", "\\").rstrip("\\")
    if s.lower().startswith("address:"):
        s = s[8:].strip()
    if s.startswith("\\\\?\\UNC\\"):
        s = "\\\\" + s[8:]
    elif s.startswith("\\\\?\\"):
        s = s[4:]
    return os.path.normcase(s)


def _paths_equal(a: str | Path, b: str | Path) -> bool:
    na, nb = _norm_path(a), _norm_path(b)
    return bool(na) and na == nb


_USER_SHELL_FOLDER_NAMES = frozenset(
    {"documents", "desktop", "downloads", "pictures", "music", "videos", "onedrive"}
)


def _is_local_user_shell_folder(folder: str) -> bool:
    """True for Documents/Desktop/Downloads — never the label share."""
    n = _norm_path(folder)
    if not n or n.startswith("\\\\"):
        return False
    last = n.rsplit("\\", 1)[-1]
    if last in _USER_SHELL_FOLDER_NAMES and "\\" not in n:
        return True
    home = _norm_path(os.path.expanduser("~"))
    if home and (n == home or n.startswith(home + "\\")):
        return last in _USER_SHELL_FOLDER_NAMES or any(
            f"\\{name}" in n for name in _USER_SHELL_FOLDER_NAMES
        )
    return last in _USER_SHELL_FOLDER_NAMES and (
        "\\users\\" in n or n.startswith("c:\\")
    )


def _folder_looks_correct(current: str, want: Path) -> bool:
    if not current:
        return False
    if _paths_equal(current, want):
        return True
    c = _norm_path(current)
    if "\\" not in c:
        if c in _USER_SHELL_FOLDER_NAMES:
            return False
        return c == _norm_path(want.name)
    return False


def _gui_focus_hwnd() -> int:
    import win32gui
    import win32process

    class _RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    class _GUITHREADINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_uint),
            ("flags", ctypes.c_uint),
            ("hwndActive", ctypes.c_void_p),
            ("hwndFocus", ctypes.c_void_p),
            ("hwndCapture", ctypes.c_void_p),
            ("hwndMenuOwner", ctypes.c_void_p),
            ("hwndMoveSize", ctypes.c_void_p),
            ("hwndCaret", ctypes.c_void_p),
            ("rcCaret", _RECT),
        ]

    fg = win32gui.GetForegroundWindow()
    if not fg:
        return 0
    tid, _ = win32process.GetWindowThreadProcessId(fg)
    info = _GUITHREADINFO()
    info.cbSize = ctypes.sizeof(_GUITHREADINFO)
    if not ctypes.windll.user32.GetGUIThreadInfo(tid, ctypes.byref(info)):
        return 0
    return int(info.hwndFocus or 0)


def _hwnd_under_class(hwnd: int, class_names: set[str]) -> bool:
    import win32gui

    cur = hwnd
    for _ in range(16):
        if not cur:
            return False
        try:
            if win32gui.GetClassName(cur) in class_names:
                return True
            cur = win32gui.GetParent(cur)
        except Exception:
            return False
    return False


def _focus_is_nav_pane() -> bool:
    """Left folder tree (Documents / Desktop / …). Enter here opens that folder."""
    hwnd = _gui_focus_hwnd()
    return bool(hwnd) and _hwnd_under_class(hwnd, {"SysTreeView32"})


def _hwnd_class_chain(hwnd: int) -> str:
    import win32gui

    parts: list[str] = []
    cur = hwnd
    for _ in range(16):
        if not cur:
            break
        try:
            parts.append(win32gui.GetClassName(cur) or "")
            cur = win32gui.GetParent(cur)
        except Exception:
            break
    return " ".join(parts).lower()


def _hwnd_looks_like_search(hwnd: int) -> bool:
    return "search" in _hwnd_class_chain(hwnd)


def _hwnd_looks_like_address(hwnd: int) -> bool:
    chain = _hwnd_class_chain(hwnd)
    return "address band" in chain or "breadcrumb" in chain


def _focus_is_search() -> bool:
    hwnd = _gui_focus_hwnd()
    return bool(hwnd) and _hwnd_looks_like_search(hwnd)


def _click_control_client(hwnd: int) -> None:
    """Click the center of a specific control (client coords — not a screen click)."""
    import win32con
    import win32gui

    try:
        left, top, right, bottom = win32gui.GetClientRect(hwnd)
        cx = max(4, (right - left) // 2)
        cy = max(2, (bottom - top) // 2)
        lparam = (cy << 16) | (cx & 0xFFFF)
        win32gui.SendMessage(hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lparam)
        win32gui.SendMessage(hwnd, win32con.WM_LBUTTONUP, 0, lparam)
    except Exception:
        pass


def _read_dialog_folder(hwnd: int) -> str:
    """Current folder shown in the Save dialog (CDM path, then address bar)."""
    import win32gui

    buf = ctypes.create_unicode_buffer(2048)
    try:
        n = ctypes.windll.user32.SendMessageW(hwnd, 0x0466, 2048, buf)  # CDM_GETFOLDERPATH
        if n and buf.value.strip():
            return buf.value.strip()
    except Exception:
        pass

    found: list[str] = []
    address_labeled: list[str] = []

    def _visit(child: int) -> None:
        try:
            cls = win32gui.GetClassName(child)
        except Exception:
            return
        text = ""
        try:
            text = (win32gui.GetWindowText(child) or "").strip()
        except Exception:
            text = ""
        if cls == "ToolbarWindow32" and text:
            if text.lower().startswith("address:"):
                address_labeled.append(text)
            elif _hwnd_looks_like_address(child):
                found.append(text)
        if cls == "Edit" and text and ("\\" in text or (len(text) > 1 and text[1] == ":")):
            if _hwnd_looks_like_address(child) and not _hwnd_looks_like_search(child):
                found.append(text)

    _walk_descendants(hwnd, _visit)
    for raw in address_labeled + list(reversed(found)):
        cleaned = raw.strip()
        if cleaned.lower().startswith("address:"):
            cleaned = cleaned[8:].strip()
        if cleaned:
            return cleaned
    return ""


def _focus_filename_edit(hwnd: int) -> int:
    import win32gui

    edit = _find_filename_edit_hwnd(hwnd)
    if not edit:
        return 0
    try:
        win32gui.SetFocus(edit)
    except Exception:
        pass
    return edit


def _wait_for_filename_edit(hwnd: int, *, timeout_s: float = 2.5) -> int:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        edit = _find_filename_edit_hwnd(hwnd)
        if edit:
            return edit
        time.sleep(0.08)
    return 0


def _notify_filename_edit_changed(edit_hwnd: int) -> None:
    """WM_SETTEXT alone does not commit in the common Save dialog — notify parent."""
    import win32con
    import win32gui

    try:
        parent = win32gui.GetParent(edit_hwnd)
        ctrl_id = win32gui.GetDlgCtrlID(edit_hwnd)
        if parent and ctrl_id:
            win32gui.SendMessage(
                parent,
                win32con.WM_COMMAND,
                (win32con.EN_CHANGE << 16) | (ctrl_id & 0xFFFF),
                edit_hwnd,
            )
    except Exception:
        pass


def _commit_filename_field(hwnd: int, dest: Path, *, full_path: bool = False) -> bool:
    """
    Commit the PO file name in the File name box so Save uses it.

    Alt+N is the Save dialog accelerator for File name (not the search box).
    """
    import win32con

    _focus_dialog(hwnd)
    edit = _focus_filename_edit(hwnd) or _find_filename_edit_hwnd(hwnd)
    if not edit:
        return False

    want = str(dest) if full_path else dest.name
    _set_clipboard(want)
    _send_alt_key("n")
    time.sleep(0.25)
    _send_ctrl_a()
    _send_ctrl_v()
    time.sleep(0.2)
    _notify_filename_edit_changed(edit)
    # Tab out — Save dialog commits filename on focus loss.
    _send_vk(win32con.VK_TAB)
    time.sleep(0.25)
    _send_alt_key("n")
    time.sleep(0.15)

    edit = _find_filename_edit_hwnd(hwnd)
    if edit and _filename_matches(edit, dest):
        return True

    # Fallback: WM_SETTEXT + notify + Tab
    _focus_filename_edit(hwnd)
    _send_alt_key("n")
    time.sleep(0.15)
    _set_edit_text(edit, want)
    _notify_filename_edit_changed(edit)
    time.sleep(0.15)
    _send_vk(win32con.VK_TAB)
    time.sleep(0.25)
    _send_alt_key("n")
    time.sleep(0.15)
    edit = _find_filename_edit_hwnd(hwnd)
    return bool(edit and _filename_matches(edit, dest))


def _read_filename_field(hwnd: int) -> str:
    edit = _find_filename_edit_hwnd(hwnd)
    return _read_edit_text(edit) if edit else ""


def _clear_filename_if_path(hwnd: int) -> None:
    edit = _find_filename_edit_hwnd(hwnd)
    if not edit:
        return
    current = _read_edit_text(edit)
    if current and (
        "\\" in current or "/" in current or (len(current) > 1 and current[1] == ":")
    ):
        _set_edit_text(edit, "")
        time.sleep(0.12)


def _enter_filename_with_retry(hwnd: int, dest: Path) -> bool:
    """Enter PO filename after folder is set; commit with Tab so Save sees it."""
    want = dest.name
    attempts = _filename_entry_attempts()

    for attempt in range(1, attempts + 1):
        _focus_dialog(hwnd)
        if not _wait_for_filename_edit(hwnd, timeout_s=2.5):
            _log(f"Filename field not ready (attempt {attempt}/{attempts})…")
            time.sleep(0.4)
            continue

        _clear_filename_if_path(hwnd)
        _log(f"Entering file name (attempt {attempt}/{attempts})…")

        if _commit_filename_field(hwnd, dest):
            current = _read_filename_field(hwnd)
            _log(f"File name committed: {current!r}")
            return True

        current = _read_filename_field(hwnd)
        have = current if current else "(empty)"
        _log(f"File name not committed — want {want!r}, have {have!r}")
        time.sleep(0.5)

    _log(f"ERROR: could not enter file name {want!r}.")
    return False


def _dialog_title(hwnd: int) -> str:
    import win32gui

    try:
        return (win32gui.GetWindowText(hwnd) or "").strip()
    except Exception:
        return ""


def _dialog_has_filename_combo(hwnd: int) -> bool:
    import win32gui

    try:
        return bool(win32gui.FindWindowEx(hwnd, 0, "ComboBoxEx32", None))
    except Exception:
        return False


def _score_save_dialog(hwnd: int) -> int:
    title = _dialog_title(hwnd).lower()
    score = 0
    if _dialog_has_filename_combo(hwnd):
        score += 50
    if "save print output" in title:
        score += 40
    if "save as" in title or title == "save":
        score += 20
    return score


def _enum_save_dialog_hwnds() -> list[tuple[int, int]]:
    try:
        import win32gui
    except ImportError:
        return []

    found: list[tuple[int, int]] = []

    def _cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        if win32gui.GetClassName(hwnd) != "#32770":
            return True
        score = _score_save_dialog(hwnd)
        if score > 0:
            found.append((score, hwnd))
        return True

    win32gui.EnumWindows(_cb, None)
    found.sort(key=lambda x: x[0], reverse=True)
    return found


def find_save_as_dialog_hwnd(*, log: bool = True) -> int:
    found = _enum_save_dialog_hwnds()
    if not found:
        return 0
    hwnd = found[0][1]
    if log:
        _log(f"Save dialog: {_dialog_title(hwnd)!r}")
    return hwnd


def _dialog_still_open(hwnd: int) -> bool:
    import win32gui

    try:
        return win32gui.IsWindow(hwnd) and win32gui.IsWindowVisible(hwnd)
    except Exception:
        return False


def _focus_dialog(hwnd: int) -> None:
    import win32api
    import win32gui
    import win32process

    try:
        win32gui.ShowWindow(hwnd, 5)
    except Exception:
        pass
    try:
        fg = win32gui.GetForegroundWindow()
        cur_tid = win32api.GetCurrentThreadId()
        fg_tid, _ = win32process.GetWindowThreadProcessId(fg) if fg else (0, 0)
        dlg_tid, _ = win32process.GetWindowThreadProcessId(hwnd)
        attached: list[int] = []
        for tid in (fg_tid, dlg_tid):
            if tid and tid != cur_tid and tid not in attached:
                try:
                    win32gui.AttachThreadInput(tid, cur_tid, True)
                    attached.append(tid)
                except Exception:
                    pass
        try:
            win32gui.BringWindowToTop(hwnd)
            win32gui.SetForegroundWindow(hwnd)
        finally:
            for tid in attached:
                try:
                    win32gui.AttachThreadInput(tid, cur_tid, False)
                except Exception:
                    pass
    except Exception:
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass


def _walk_descendants(parent_hwnd: int, visit) -> None:
    import win32gui

    def _child_cb(child: int, _) -> bool:
        visit(child)
        _walk_descendants(child, visit)
        return True

    try:
        win32gui.EnumChildWindows(parent_hwnd, _child_cb, None)
    except Exception:
        pass


def _edit_from_combo(combo_hwnd: int) -> int:
    import win32gui

    edit = win32gui.FindWindowEx(combo_hwnd, 0, "Edit", None)
    if edit:
        return edit
    combo = win32gui.FindWindowEx(combo_hwnd, 0, "ComboBox", None)
    if combo:
        return win32gui.FindWindowEx(combo, 0, "Edit", None)
    return 0


def _find_filename_edit_hwnd(parent_hwnd: int) -> int:
    """File name box only — never the address bar or the search box to its right."""
    import win32gui

    try:
        combo = win32gui.GetDlgItem(parent_hwnd, 1148)  # cmb13 File name
        if combo:
            edit = _edit_from_combo(combo)
            if edit and not _hwnd_looks_like_search(edit) and not _hwnd_looks_like_address(edit):
                return edit
    except Exception:
        pass

    combo_boxes: list[int] = []

    def _visit(hwnd: int) -> None:
        if win32gui.GetClassName(hwnd) == "ComboBoxEx32":
            if not _hwnd_looks_like_search(hwnd) and not _hwnd_looks_like_address(hwnd):
                combo_boxes.append(hwnd)

    _walk_descendants(parent_hwnd, _visit)

    for combo_ex in combo_boxes:
        edit = _edit_from_combo(combo_ex)
        if edit:
            return edit

    edits: list[int] = []

    def _visit_edit(hwnd: int) -> None:
        if win32gui.GetClassName(hwnd) != "Edit" or not win32gui.IsWindowVisible(hwnd):
            return
        if _hwnd_looks_like_search(hwnd) or _hwnd_looks_like_address(hwnd):
            return
        edits.append(hwnd)

    _walk_descendants(parent_hwnd, _visit_edit)
    return edits[-1] if edits else 0


def _find_save_button_hwnd(parent_hwnd: int) -> int:
    import win32gui

    save_btn = 0

    def _visit(hwnd: int) -> None:
        nonlocal save_btn
        if save_btn:
            return
        if win32gui.GetClassName(hwnd) != "Button":
            return
        text = (win32gui.GetWindowText(hwnd) or "").replace("&", "").strip().lower()
        if text == "save":
            save_btn = hwnd

    _walk_descendants(parent_hwnd, _visit)
    return save_btn


def _read_edit_text(edit_hwnd: int) -> str:
    import win32con
    import win32gui

    try:
        n = win32gui.SendMessage(edit_hwnd, win32con.WM_GETTEXTLENGTH, 0, 0)
        if n <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(n + 2)
        win32gui.SendMessage(edit_hwnd, win32con.WM_GETTEXT, n + 1, buf)
        return buf.value.strip()
    except Exception:
        return ""


def _set_edit_text(edit_hwnd: int, text: str) -> bool:
    import win32con
    import win32gui

    try:
        win32gui.SendMessage(edit_hwnd, win32con.EM_SETSEL, 0, -1)
        win32gui.SendMessage(edit_hwnd, win32con.WM_SETTEXT, 0, text)
        return True
    except Exception:
        return False


def _filename_matches(edit_hwnd: int, dest: Path) -> bool:
    if not edit_hwnd:
        return False
    current = _read_edit_text(edit_hwnd).strip().strip('"')
    if not current:
        return False
    # Full destination path is valid — Save writes to that exact location.
    if _paths_equal(current, dest):
        return True
    try:
        current_path = Path(current)
        if _paths_equal(current_path.with_suffix(dest.suffix), dest):
            return True
    except Exception:
        pass
    # Other UNC/drive paths in the field are not a PO filename.
    if "\\" in current or "/" in current:
        return False
    if len(current) > 1 and current[1] == ":":
        return False
    want_name = dest.name.strip()
    want_stem = dest.stem.strip()
    c = current.lower()
    return c == want_name.lower() or c == want_stem.lower()


def _enum_visible_dialog_hwnds() -> list[tuple[int, str]]:
    try:
        import win32gui
    except ImportError:
        return []

    found: list[tuple[int, str]] = []

    def _cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        cls = win32gui.GetClassName(hwnd) or ""
        if cls != "#32770":
            return True
        title = (win32gui.GetWindowText(hwnd) or "").strip()
        found.append((hwnd, title))
        return True

    win32gui.EnumWindows(_cb, None)
    return found


def _dialog_text_blob(hwnd: int) -> str:
    parts = [_dialog_title(hwnd), *_safe_enum_child_text(hwnd)]
    return " ".join(p for p in parts if p)


def _safe_enum_child_text(hwnd: int) -> list[str]:
    import win32gui

    parts: list[str] = []

    def _cb(child, _):
        try:
            text = (win32gui.GetWindowText(child) or "").strip()
            if text:
                parts.append(text)
        except Exception:
            pass
        return True

    try:
        win32gui.EnumChildWindows(hwnd, _cb, None)
    except Exception:
        pass
    return parts


def _click_ok_on_dialog(hwnd: int, *, label: str = "dialog") -> bool:
    import win32con
    import win32gui

    _focus_dialog(hwnd)
    time.sleep(0.15)
    for dlg_id in (1, 2):
        try:
            ok_btn = win32gui.GetDlgItem(hwnd, dlg_id)
            if not ok_btn:
                continue
            text = (win32gui.GetWindowText(ok_btn) or "").strip().lower().replace("&", "")
            if text not in ("ok", ""):
                continue
            win32gui.PostMessage(ok_btn, win32con.BM_CLICK, 0, 0)
            _log(f"Clicked OK on {label!r}.")
            time.sleep(0.35)
            return True
        except Exception:
            continue
    return False


def dismiss_worldship_could_not_print_dialog(*, timeout_s: float = 8.0) -> bool:
    """
  WorldShip shows a small OK dialog when Save Print Output closes without printing.
  Click OK so the batch can open the next Save dialog.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for hwnd, title in _enum_visible_dialog_hwnds():
            blob = _dialog_text_blob(hwnd).lower()
            if "could not print" in blob or "unable to print" in blob:
                if _click_ok_on_dialog(hwnd, label=title or "could not print"):
                    return True
        time.sleep(0.25)
    return False


def recover_after_failed_worldship_save(
    *,
    previous_hwnd: int = 0,
    timeout_s: float = 20.0,
) -> int:
    """After a failed save: dismiss Could not print, wait for the next Save dialog."""
    dismiss_worldship_could_not_print_dialog()
    time.sleep(0.6)
    return wait_for_next_save_dialog(previous_hwnd=previous_hwnd, timeout_s=timeout_s)


def _click_save_button(hwnd: int) -> bool:
    """
    Click the Save button on the Save Print Output As dialog only.

    Never send Alt+S or Enter — Alt+S activates Stop on Automatic Processing Progress
    if that window has focus, which halts the rest of the batch.
    """
    import win32con
    import win32gui

    _focus_dialog(hwnd)
    time.sleep(0.2)
    btn = _find_save_button_hwnd(hwnd)
    if not btn:
        _log("ERROR: Save button not found on Save Print Output As dialog.")
        return False
    try:
        win32gui.SendMessage(btn, win32con.BM_CLICK, 0, 0)
    except Exception:
        try:
            win32gui.PostMessage(btn, win32con.BM_CLICK, 0, 0)
        except Exception:
            _log("ERROR: could not click Save button.")
            return False
    time.sleep(0.25)
    _log("Clicked Save button (no keyboard shortcuts).")
    return True


def dismiss_save_as_dialog_esc() -> None:
    """Close a stray Save dialog without saving (warehouse-print rows)."""
    import win32con

    hwnd = find_save_as_dialog_hwnd(log=False)
    if not hwnd:
        return
    _focus_dialog(hwnd)
    time.sleep(0.15)
    _send_vk(win32con.VK_ESCAPE)
    time.sleep(0.4)
    _log("Dismissed Save dialog (Escape).")


def _find_address_band(dialog_hwnd: int) -> int:
    import win32gui

    found = 0

    def _visit(hwnd: int) -> None:
        nonlocal found
        if found:
            return
        cls = win32gui.GetClassName(hwnd) or ""
        if cls in ("Address Band Root", "Breadcrumb Parent"):
            found = hwnd

    _walk_descendants(dialog_hwnd, _visit)
    return found


def _find_address_toolbar(dialog_hwnd: int) -> int:
    import win32gui

    found = 0

    def _visit(hwnd: int) -> None:
        nonlocal found
        if found:
            return
        if win32gui.GetClassName(hwnd) != "ToolbarWindow32":
            return
        if not _hwnd_looks_like_address(hwnd):
            return
        found = hwnd

    _walk_descendants(dialog_hwnd, _visit)
    return found


def _find_address_edit(dialog_hwnd: int) -> int:
    import win32gui

    found = 0

    def _visit(hwnd: int) -> None:
        nonlocal found
        if found:
            return
        if win32gui.GetClassName(hwnd) != "Edit" or not win32gui.IsWindowVisible(hwnd):
            return
        if _hwnd_looks_like_search(hwnd):
            return
        if _hwnd_looks_like_address(hwnd):
            found = hwnd

    _walk_descendants(dialog_hwnd, _visit)
    return found


def _send_ctrl_l() -> None:
    import win32api
    import win32con

    win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)
    win32api.keybd_event(ord("L"), 0, 0, 0)
    win32api.keybd_event(ord("L"), 0, win32con.KEYEVENTF_KEYUP, 0)
    win32api.keybd_event(win32con.VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)


def _activate_address_bar(dialog_hwnd: int) -> int:
    """Put the address bar into edit mode. Never clicks the search box."""
    import win32gui

    _focus_dialog(dialog_hwnd)
    band = _find_address_band(dialog_hwnd)
    toolbar = _find_address_toolbar(dialog_hwnd)
    target = toolbar or band
    if target:
        try:
            win32gui.SetFocus(target)
        except Exception:
            pass
        _click_control_client(target)
        time.sleep(0.25)
    else:
        _send_ctrl_l()
        time.sleep(0.25)

    deadline = time.monotonic() + 1.2
    while time.monotonic() < deadline:
        edit = _find_address_edit(dialog_hwnd)
        if edit:
            try:
                win32gui.SetFocus(edit)
            except Exception:
                pass
            return edit
        time.sleep(0.08)
    return _find_address_edit(dialog_hwnd)


def _send_enter_for_folder_nav(dialog_hwnd: int, edit_hwnd: int) -> bool:
    """Enter only while the address bar (or File name) has focus — never search or Documents."""
    import win32con
    import win32gui

    _focus_dialog(dialog_hwnd)
    try:
        win32gui.SetFocus(edit_hwnd)
    except Exception:
        pass
    time.sleep(0.1)
    if _focus_is_nav_pane() or _focus_is_search():
        _log("Focus is on search or the folder tree — not sending Enter.")
        try:
            win32gui.SetFocus(edit_hwnd)
        except Exception:
            pass
        time.sleep(0.1)
        if _focus_is_nav_pane() or _focus_is_search():
            return False
    _send_vk(win32con.VK_RETURN)
    return True


def _wait_until_folder(hwnd: int, folder: Path, *, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        current = _read_dialog_folder(hwnd)
        if current and _is_local_user_shell_folder(current):
            time.sleep(0.12)
            continue
        if _folder_looks_correct(current, folder):
            return True
        time.sleep(0.12)
    current = _read_dialog_folder(hwnd)
    return _folder_looks_correct(current, folder)


def _set_folder_via_address_bar(hwnd: int, folder: Path) -> bool:
    """Paste the vendor path into the address bar and Enter."""
    folder_str = str(folder)
    edit = _activate_address_bar(hwnd)
    if not edit:
        _log("Address bar edit not found.")
        return False
    if _focus_is_search():
        _log("Search box has focus — not pasting the folder path there.")
        return False

    _set_edit_text(edit, folder_str)
    _set_clipboard(folder_str)
    _send_ctrl_a()
    _send_ctrl_v()
    time.sleep(0.2)
    return _send_enter_for_folder_nav(hwnd, edit)


def _navigate_to_folder(hwnd: int, folder: Path, *, force: bool = False) -> bool:
    """
    Verify the address bar; change folder only when needed.

    Uses the address bar (not Alt+D, not the search box). Skip when this dialog
    is already in the target folder.
    """
    global _last_nav_folder
    folder = folder.resolve()
    current = _read_dialog_folder(hwnd)
    if not force and current and _folder_looks_correct(current, folder):
        _log(f"Address bar already in target folder ({folder.name})")
        _last_nav_folder = folder
        _focus_dialog(hwnd)
        _wait_for_filename_edit(hwnd, timeout_s=0.5)
        return True

    if current and _is_local_user_shell_folder(current):
        _log(f"Dialog is in {current!r} — navigating back to the vendor folder.")

    _log(f"Setting folder: {folder}")
    _focus_dialog(hwnd)
    time.sleep(0.2)

    if not _set_folder_via_address_bar(hwnd, folder):
        _last_nav_folder = None
        return False

    ok = _wait_until_folder(hwnd, folder, timeout_s=max(_folder_nav_pause_s(), 2.5))
    if ok:
        time.sleep(_filename_settle_s())
        _last_nav_folder = folder
        _log(f"Folder confirmed: {folder}")
        return True

    shown = _read_dialog_folder(hwnd) or "(unknown)"
    _log(f"WARN: folder after navigate is {shown}, want {folder}")
    _last_nav_folder = None
    return False


def _prepare_save_dialog(
    hwnd: int,
    dest: Path,
    *,
    force_folder: bool = False,
    po: str = "",
    sku: str = "",
) -> bool:
    """
    Save Print Output As — folder first, pause, then PO filename:

    1. Verify expected PO / SKU / folder
    2. Verify address bar; change folder only if needed
    3. Pause for folder to load
    4. Enter PO in File name, verify (retry entry if empty)
    """
    hwnd = find_save_as_dialog_hwnd(log=False) or hwnd
    _focus_dialog(hwnd)

    po_label = po or dest.stem
    _log(f"Verify — PO {po_label!r}, SKU {sku!r}")
    _log(f"Verify — folder: {dest.parent}")
    _log(f"Verify — file:   {dest.name}")
    dialog_name = _read_filename_field(hwnd) or "(empty)"
    _log(f"Dialog file name now: {dialog_name!r}")

    nav_ok = _navigate_to_folder(hwnd, dest.parent, force=force_folder)
    hwnd = find_save_as_dialog_hwnd(log=False) or hwnd
    _focus_dialog(hwnd)

    pause_s = _after_folder_pause_s()
    _log(f"Pausing {pause_s:.1f}s after folder change…")
    time.sleep(pause_s)

    if not nav_ok:
        shown = _read_dialog_folder(hwnd) or "(unknown)"
        _log(f"WARN: folder not confirmed ({shown}) — will retry if Save does not land on disk.")

    if not _enter_filename_with_retry(hwnd, dest):
        return False

    edit = _find_filename_edit_hwnd(hwnd)
    final_name = _read_edit_text(edit) if edit else ""
    _log(f"Final file name before Save: {final_name!r}")
    return _assert_ready_to_save(hwnd, dest)


def _assert_ready_to_save(hwnd: int, dest: Path) -> bool:
    edit = _find_filename_edit_hwnd(hwnd)
    current = _read_edit_text(edit) if edit else ""
    if not edit or not _filename_matches(edit, dest):
        _log(f"ERROR: refusing Save — filename field is {current!r}, want {dest.name!r}.")
        return False

    folder = _read_dialog_folder(hwnd)
    if folder and _is_local_user_shell_folder(folder):
        _log(f"ERROR: refusing Save — dialog folder is {folder!r} (Documents/Desktop/Downloads).")
        return False
    if folder and not _folder_looks_correct(folder, dest.parent):
        _log(f"ERROR: refusing Save — dialog folder is {folder!r}, want {dest.parent}.")
        return False
    return True


def _file_stat_snapshot(dest: Path) -> tuple[float, int] | None:
    if not dest.is_file():
        return None
    try:
        st = dest.stat()
        return (st.st_mtime, st.st_size)
    except OSError:
        return None


def _dest_file_saved(
    dest: Path,
    *,
    min_bytes: int,
    save_clicked_at: float,
    before: tuple[float, int] | None,
) -> bool:
    """
    True only when dest was written or updated by this Save click.

    Uses wall-clock mtimes (never time.monotonic() — that caused false positives
    when an older PDF with the same name already existed on the share).
    """
    if not dest.is_file():
        return False
    try:
        st = dest.stat()
    except OSError:
        return False
    if st.st_size < min_bytes:
        return False
    if st.st_mtime < save_clicked_at - 2.0:
        return False
    if before is None:
        return True
    prev_mtime, prev_size = before
    return st.st_mtime > prev_mtime + 0.05 or st.st_size != prev_size


def dismiss_overwrite_prompt(*, timeout_s: float = 4.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        hwnd = find_save_as_dialog_hwnd(log=False)
        if not hwnd:
            return
        title = _dialog_title(hwnd).lower()
        if "confirm" in title or "replace" in title or "already exists" in title:
            _send_alt_key("y")
            return
        time.sleep(0.15)


def _wait_for_save_result(
    hwnd: int,
    dest: Path,
    *,
    save_clicked_at: float,
    before: tuple[float, int] | None,
    timeout_s: float,
    min_bytes: int,
) -> bool:
    """Dialog must close AND the PDF must be newly written at the exact target path."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        dialog_open = _dialog_still_open(hwnd)
        file_ok = _dest_file_saved(
            dest,
            min_bytes=min_bytes,
            save_clicked_at=save_clicked_at,
            before=before,
        )
        if file_ok and not dialog_open:
            _log(f"Confirmed on disk: {dest} ({dest.stat().st_size:,} bytes)")
            return True
        if not dialog_open:
            for _ in range(60):
                if _dest_file_saved(
                    dest,
                    min_bytes=min_bytes,
                    save_clicked_at=save_clicked_at,
                    before=before,
                ):
                    _log(f"Confirmed on disk: {dest} ({dest.stat().st_size:,} bytes)")
                    return True
                time.sleep(0.25)
            _log(
                "Dialog closed but the PDF was not updated at the target path "
                "(filename not saved or wrong folder)."
            )
            return False
        time.sleep(0.3)
    if _dialog_still_open(hwnd):
        _log("ERROR: Save dialog still open after Save click.")
    elif _dest_file_saved(
        dest, min_bytes=min_bytes, save_clicked_at=save_clicked_at, before=before
    ):
        _log("ERROR: PDF updated but Save dialog is still open (will block next label).")
    return False


def _click_save_and_confirm(
    hwnd: int,
    dest: Path,
    *,
    before: tuple[float, int] | None,
    min_bytes: int,
) -> bool:
    if not _commit_filename_field(hwnd, dest):
        _log("ERROR: refusing Save — file name could not be committed.")
        return False
    if not _assert_ready_to_save(hwnd, dest):
        return False

    pause = _pause_s("WORLDSHIP_SAVE_BEFORE_CLICK_S", 0.35)
    time.sleep(pause)

    # Re-read immediately before Save — never click if PO filename is empty or wrong.
    if not _assert_ready_to_save(hwnd, dest):
        current = _read_filename_field(hwnd) or "(empty)"
        _log(
            f"ERROR: refusing Save — filename not ready before click "
            f"(have {current!r}, want {dest.name!r})."
        )
        return False

    save_clicked_at = time.time()
    if not _click_save_button(hwnd):
        return False
    time.sleep(0.45)
    dismiss_overwrite_prompt()
    return _wait_for_save_result(
        hwnd,
        dest,
        save_clicked_at=save_clicked_at,
        before=before,
        timeout_s=25.0,
        min_bytes=min_bytes,
    )


def _worldship_save_once(
    hwnd: int,
    dest: Path,
    *,
    before: tuple[float, int] | None,
    min_bytes: int,
    po: str = "",
    sku: str = "",
) -> bool:
    """Folder → pause → PO → verify → Save. One retry if Save does not complete."""
    hwnd = find_save_as_dialog_hwnd(log=False) or hwnd

    for attempt in range(1, 3):
        if attempt > 1:
            _log(f"Retry {attempt}/2: folder → pause → PO → Save…")
            hwnd = find_save_as_dialog_hwnd(log=False) or hwnd
            if not _dialog_still_open(hwnd):
                return False

        if not _prepare_save_dialog(
            hwnd, dest, force_folder=(attempt > 1), po=po, sku=sku
        ):
            if attempt < 2:
                continue
            return False

        if _click_save_and_confirm(hwnd, dest, before=before, min_bytes=min_bytes):
            return True

        if attempt < 2 and _dialog_still_open(hwnd):
            _log("Save did not finish — retrying once.")
            continue
        return False

    return False


def wait_for_save_dialog_handoff(
    previous_hwnd: int,
    *,
    timeout_s: float = 15.0,
    saved_dest: Path | None = None,
) -> bool:
    """
    Wait until the Save dialog we just used (previous_hwnd) is done.

    WorldShip often opens the *next* Save dialog within 1–2s. That is success:
    - previous hwnd closed, or
    - a Save dialog is visible but the filename is no longer the file we saved
      (Windows may reuse the same hwnd for the new dialog).
    """
    if not previous_hwnd:
        return True

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not _dialog_still_open(previous_hwnd):
            current = find_save_as_dialog_hwnd(log=False)
            if current and current != previous_hwnd:
                _log(
                    "Previous Save dialog closed; next Save Print Output dialog "
                    "is already open."
                )
            else:
                _log("Previous Save dialog closed.")
            return True

        if saved_dest is not None:
            edit = _find_filename_edit_hwnd(previous_hwnd)
            if edit and not _filename_matches(edit, saved_dest):
                _log(
                    "Save dialog shows the next shipment (filename changed) — continuing."
                )
                return True

        time.sleep(0.2)

    if _dialog_still_open(previous_hwnd):
        if saved_dest is not None:
            edit = _find_filename_edit_hwnd(previous_hwnd)
            if edit and not _filename_matches(edit, saved_dest):
                return True
        _log(
            "ERROR: Same Save Print Output dialog is still open after save "
            f"(hwnd={previous_hwnd})."
        )
        return False
    return True


def wait_until_save_dialog_closed(
    *, timeout_s: float = 20.0, previous_hwnd: int = 0
) -> bool:
    """Wait for a dialog to close. Prefer wait_for_save_dialog_handoff when hwnd is known."""
    if previous_hwnd:
        return wait_for_save_dialog_handoff(previous_hwnd, timeout_s=timeout_s)

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not find_save_as_dialog_hwnd(log=False):
            return True
        time.sleep(0.35)
    if find_save_as_dialog_hwnd(log=False):
        _log("ERROR: Save dialog still visible.")
        return False
    return True


def _min_label_bytes() -> int:
    raw = (os.environ.get("WORLDSHIP_MIN_LABEL_BYTES") or "800").strip()
    try:
        return max(100, int(raw))
    except ValueError:
        return 800


def wait_for_next_save_dialog(*, previous_hwnd: int, timeout_s: float) -> int:
    """
    Return hwnd for the next Save Print Output dialog.

    Accepts the next dialog if it is already open once previous_hwnd has closed.
    """
    if previous_hwnd and not _dialog_still_open(previous_hwnd):
        current = find_save_as_dialog_hwnd(log=False)
        if current:
            _log(f"Next Save dialog (already open): {_dialog_title(current)!r}")
            return current

    if not wait_for_save_dialog_handoff(previous_hwnd, timeout_s=min(timeout_s, 12.0)):
        return 0

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        found = _enum_save_dialog_hwnds()
        if not found:
            time.sleep(0.35)
            continue
        hwnd = found[0][1]
        if previous_hwnd and hwnd == previous_hwnd and _dialog_still_open(previous_hwnd):
            time.sleep(0.35)
            continue
        _log(f"Next Save dialog: {_dialog_title(hwnd)!r}")
        return hwnd
    return 0


def fill_save_as_dialog(
    dest: Path,
    *,
    timeout_s: float = 45.0,
    min_bytes: int | None = None,
    dialog_hwnd: int | None = None,
    po: str = "",
    sku: str = "",
) -> bool:
    """Save Print Output As: vendor folder + PO.pdf, verify exact path on disk."""
    dest = dest.resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    before = _file_stat_snapshot(dest)
    if min_bytes is None:
        min_bytes = _min_label_bytes()

    hwnd = dialog_hwnd or find_save_as_dialog_hwnd(log=dialog_hwnd is None)
    if not hwnd:
        _log("ERROR: Save As dialog not found.")
        return False

    _log(f"Target folder: {dest.parent}")
    _log(f"Target file:   {dest.name}")
    if before:
        _log(f"Existing file on share (mtime {before[0]:.0f}, {before[1]:,} bytes) — will require update after Save.")

    if _worldship_save_once(
        hwnd, dest, before=before, min_bytes=min_bytes, po=po, sku=sku
    ):
        return wait_for_save_dialog_handoff(hwnd, timeout_s=8.0, saved_dest=dest)

    if not _dialog_still_open(hwnd):
        _log(
            "WARN: Save dialog closed but file was not written to the target path."
        )
        dismiss_worldship_could_not_print_dialog()
        return False

    return False


def wait_for_save_as_dialog(*, timeout_s: float) -> int:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        found = _enum_save_dialog_hwnds()
        if found:
            hwnd = found[0][1]
            _log(f"Save dialog: {_dialog_title(hwnd)!r}")
            return hwnd
        time.sleep(0.35)
    return 0
