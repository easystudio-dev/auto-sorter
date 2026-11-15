"""Everything that differs between Windows and Linux lives here: where settings and
logs are kept, the user's real Downloads/Documents/... folders, start-at-login,
opening files, notifications, and the one-copy-at-a-time lock."""
import os
import subprocess
import sys

IS_WINDOWS = sys.platform == "win32"
APP_NAME = "AutoSorter"    # Windows folder / registry name
APP_ID = "auto-sorter"     # Linux folder / file name


def _home(*parts):
    return os.path.join(os.path.expanduser("~"), *parts)


# =========================
# LOCATIONS
# =========================
def config_dir():
    """Settings folder. AUTO_SORTER_HOME overrides it (used by the tests)."""
    override = os.environ.get("AUTO_SORTER_HOME")
    if override:
        return os.path.join(override, "config")
    if IS_WINDOWS:
        return os.path.join(os.environ.get("APPDATA") or _home("AppData", "Roaming"), APP_NAME)
    return os.path.join(os.environ.get("XDG_CONFIG_HOME") or _home(".config"), APP_ID)


def data_dir():
    """Logs, stats and the undo ledger."""
    override = os.environ.get("AUTO_SORTER_HOME")
    if override:
        return os.path.join(override, "data")
    if IS_WINDOWS:
        return os.path.join(os.environ.get("LOCALAPPDATA") or _home("AppData", "Local"), APP_NAME)
    return os.path.join(os.environ.get("XDG_STATE_HOME") or _home(".local", "state"), APP_ID)


_WINDOWS_FOLDER_IDS = {  # KNOWNFOLDERID values from the Windows SDK
    "Downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
    "Documents": "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
    "Pictures": "{33E28130-4E1E-4676-835A-98395C3BC3BB}",
    "Videos": "{18989B1D-99B5-455B-841C-AB7C74E4DDFC}",
    "Music": "{4BD8D571-6D19-48D3-BE97-422220080E43}",
}
_XDG_NAMES = {
    "Downloads": "DOWNLOAD",
    "Documents": "DOCUMENTS",
    "Pictures": "PICTURES",
    "Videos": "VIDEOS",
    "Music": "MUSIC",
}


def _windows_known_folder(guid):
    import ctypes
    import uuid

    folder_id = (ctypes.c_byte * 16).from_buffer_copy(uuid.UUID(guid).bytes_le)
    buf = ctypes.c_wchar_p()
    if ctypes.windll.shell32.SHGetKnownFolderPath(
            ctypes.byref(folder_id), 0, None, ctypes.byref(buf)) != 0:
        return None
    try:
        return buf.value
    finally:
        ctypes.windll.ole32.CoTaskMemFree(buf)


def user_folder(name):
    """The user's real Downloads/Documents/Pictures/Videos/Music folder.

    Follows OneDrive or moved folders on Windows and translated folder names on
    Linux (e.g. ~/Téléchargements). Falls back to ~/<name>.
    """
    path = None
    try:
        if IS_WINDOWS:
            path = _windows_known_folder(_WINDOWS_FOLDER_IDS[name])
        else:
            out = subprocess.run(
                ["xdg-user-dir", _XDG_NAMES[name]],
                capture_output=True, text=True, timeout=5, env=clean_env(),
            )
            path = out.stdout.strip()
    except (OSError, subprocess.SubprocessError, AttributeError, KeyError):
        path = None
    # xdg-user-dir prints $HOME for folders that are not set up
    if (path and os.path.isdir(path)
            and os.path.normcase(os.path.abspath(path)) != os.path.normcase(_home())):
        return path
    return _home(name)


# =========================
# RUNNING OTHER PROGRAMS
# =========================
def clean_env():
    """Environment for child programs. A PyInstaller one-file build points
    LD_LIBRARY_PATH at its bundled libraries, which breaks xdg-open and friends."""
    env = dict(os.environ)
    if getattr(sys, "frozen", False) and not IS_WINDOWS:
        original = env.get("LD_LIBRARY_PATH_ORIG")
        if original:
            env["LD_LIBRARY_PATH"] = original
        else:
            env.pop("LD_LIBRARY_PATH", None)
    return env


def open_file(path):
    """Open a settings or log file in a text editor."""
    if IS_WINDOWS:
        subprocess.Popen(["notepad.exe", path])
    else:
        subprocess.Popen(
            ["xdg-open", path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=clean_env(),
        )


def notify(title, body):
    """Popup for when there is no tray icon to show it. Linux only; no-op on Windows."""
    if IS_WINDOWS:
        return
    subprocess.run(
        ["notify-send", "--app-name", "Auto Sorter", title, body],
        check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=clean_env(), timeout=5,
    )


# =========================
# START AT LOGIN
# =========================
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_DESKTOP_RESERVED = set(' \t\n"\'\\><~|&;$*?#()`')


def launch_command():
    """Command that starts the tray app the way it is running now."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    python = sys.executable
    if IS_WINDOWS:
        pythonw = os.path.join(os.path.dirname(python), "pythonw.exe")
        if os.path.exists(pythonw):
            python = pythonw  # no console window at login
    return [python, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py")]


def _desktop_file():
    return os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or _home(".config"), "autostart", APP_ID + ".desktop"
    )


def _desktop_quote(arg):
    """Quote one argument for an Exec= line in a .desktop file (freedesktop spec)."""
    arg = arg.replace("%", "%%")
    if any(c in _DESKTOP_RESERVED for c in arg):
        for ch in '\\"`$':
            arg = arg.replace(ch, "\\" + ch)
        arg = f'"{arg}"'
    return arg.replace("\\", "\\\\")  # .desktop string escaping, on top of Exec quoting


def autostart_enabled():
    if IS_WINDOWS:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
                winreg.QueryValueEx(key, APP_NAME)
            return True
        except OSError:
            return False
    return os.path.exists(_desktop_file())


def set_autostart(enabled):
    """Turn start-at-login on or off for the current user. No admin rights needed."""
    if IS_WINDOWS:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            if enabled:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ,
                                  subprocess.list2cmdline(launch_command()))
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
        return

    path = _desktop_file()
    if not enabled:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    exec_line = " ".join(_desktop_quote(a) for a in launch_command())
    with open(path, "w", encoding="utf-8") as f:
        f.write(
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=Auto Sorter\n"
            "Comment=Sorts new downloads into folders\n"
            f"Exec={exec_line}\n"
            "Terminal=false\n"
            "X-GNOME-Autostart-enabled=true\n"
        )


# =========================
# ONE COPY AT A TIME
# =========================
class SingleInstance:
    """File lock so the tray app and a terminal copy never sort the same folder at once.
    The OS releases the lock when the process exits, even after a crash."""

    def __init__(self, name):
        self.path = os.path.join(data_dir(), name + ".lock")
        self._file = None

    def acquire(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        f = open(self.path, "a+")
        try:
            if IS_WINDOWS:
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return False
        self._file = f
        return True
