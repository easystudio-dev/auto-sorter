"""Auto Sorter engine and command-line interface.

`python auto_sorter.py` watches in the foreground with no tray icon (for servers,
systemd, or terminals). `app.py` and the standalone builds run the tray app.
"""
import argparse
import fnmatch
import hashlib
import json
import logging
import logging.handlers
import os
import queue
import shutil
import signal
import sys
import threading
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

import platform_utils as pu

__version__ = "1.0.0"

# =========================
# PATHS
# =========================
CONFIG_PATH = os.path.join(pu.config_dir(), "config.json")
LOG_DIR = pu.data_dir()
LOG_FILE = os.path.join(LOG_DIR, "auto_sorter.log")
STATS_FILE = os.path.join(LOG_DIR, "stats.json")
MOVE_LOG = os.path.join(LOG_DIR, "moves.jsonl")
LEDGER_MAX_LINES = 5000  # ponytail: rewrite-to-trim, fine for one line per move
LEDGER_LOCK = threading.Lock()  # the tray's Undo runs on a different thread from the sorter

TEMP_EXTENSIONS = (".crdownload", ".part", ".tmp", ".temp")

# Where versions before 1.0 kept their files on Linux.
_LEGACY_FILES = (
    ("~/.config/download_sorter.json", CONFIG_PATH),
    ("~/.Script_Logs/sorter_stats.json", STATS_FILE),
    ("~/.Script_Logs/moves.jsonl", MOVE_LOG),
)


# =========================
# CONFIG & SETUP
# =========================
def default_config():
    """Defaults for this computer, using the user's real Documents/Downloads/... folders."""
    home = os.path.expanduser("~")
    downloads = pu.user_folder("Downloads")
    return {
        "destinations": {
            "Documents": pu.user_folder("Documents"),
            "Pictures": pu.user_folder("Pictures"),
            "Videos": pu.user_folder("Videos"),
            "Music": pu.user_folder("Music"),
            "Archives": os.path.join(home, "Archives"),
            "Programs": os.path.join(home, "Software"),
            "Other": os.path.join(downloads, "Other"),
        },
        "extensions": {
            "Documents": ["pdf", "docx", "txt", "xlsx", "pptx", "doc", "rtf", "odt", "csv"],
            "Pictures": ["jpg", "jpeg", "png", "gif", "bmp", "svg", "webp", "tiff", "ico"],
            "Videos": ["mp4", "mkv", "avi", "mov", "wmv", "flv", "webm", "m4v", "3gp"],
            "Music": ["mp3", "wav", "flac", "aac", "ogg", "wma", "m4a"],
            "Archives": ["zip", "rar", "7z", "tar", "gz", "bz2", "xz"],
            "Programs": ["exe", "msi", "deb", "rpm", "dmg", "pkg", "appimage"]
        },
        "settings": {
            "watch_folders": [downloads],       # one path or a list; changes apply on save
            "organize_by_date": False,
            "date_format": "%Y/%m",             # subfolder pattern when organize_by_date is on
            "duplicate_action": "rename",       # "rename", "skip", "replace"
            "min_file_size_kb": 0,
            "max_file_age_days": 0,             # 0 = process all files regardless of age
            "dry_run": False,
            "auto_cleanup_temp": True,          # delete *.crdownload/*.part/*.tmp older than 24h
            "organize_existing_on_start": False,
            "catch_all": False,                 # unknown extensions -> catch_all_category
            "catch_all_category": "Other",
            "name_rules": [],                   # [{"match": "*invoice*", "dest": "Documents/Invoices"}]
            "exclude_patterns": [".DS_Store", "Thumbs.db", "desktop.ini"]  # wildcards allowed
        },
        "notifications": {
            "enabled": True,
            "show_summary": True,
            "summary_interval_minutes": 60,
            "notify_batch_seconds": 120   # min gap between desktop popups; 0 = only the periodic summary
        }
    }


# Filled in place by init()/reload_config(), so other modules can hold references.
DESTINATIONS = {}
EXTENSIONS = {}
SETTINGS = {}
NOTIFICATIONS = {}
NOTIFY_HOOK = None  # the tray app sets this to show popups through its icon
_DEFAULTS = None
_initialized = False


def _deep_merge(base, override):
    """Recursively merge override onto a copy of base (base = defaults)."""
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _read_user_config():
    """The user's settings ({} if there is no file). Raises OSError/ValueError if unreadable."""
    if not os.path.exists(CONFIG_PATH):
        return {}
    # utf-8-sig: Notepad may save the file with a byte-order mark
    with open(CONFIG_PATH, encoding="utf-8-sig") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("the file must contain a JSON object { ... }")
    return data


def _apply_config(merged):
    for target, key in ((DESTINATIONS, "destinations"), (EXTENSIONS, "extensions"),
                        (SETTINGS, "settings"), (NOTIFICATIONS, "notifications")):
        target.clear()
        target.update(merged[key])


def current_config():
    return {"destinations": DESTINATIONS, "extensions": EXTENSIONS,
            "settings": SETTINGS, "notifications": NOTIFICATIONS}


def _write_config(config):
    """Write via a temp file so the hot-reload watcher never reads half a file."""
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4)
    os.replace(tmp, CONFIG_PATH)


def _migrate_legacy():
    if pu.IS_WINDOWS or os.environ.get("AUTO_SORTER_HOME"):
        return
    for old, new in _LEGACY_FILES:
        old = os.path.expanduser(old)
        if os.path.exists(old) and not os.path.exists(new):
            shutil.copy2(old, new)
            log_message(f"📦 Copied old {old} to {new}")


def _setup_logging():
    handlers = [logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8", delay=True,
    )]
    if sys.stderr is not None:  # None in the windowed Windows build
        try:
            sys.stderr.reconfigure(errors="replace")  # emoji on a cp1252 console
        except (AttributeError, ValueError):
            pass
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=handlers,
        force=True,
    )


def init():
    """Create folders, start logging, load settings. Returns False if the settings
    file has an error (defaults are used and the file is left untouched)."""
    global _DEFAULTS, _initialized
    os.makedirs(LOG_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    _setup_logging()
    _migrate_legacy()
    _DEFAULTS = default_config()
    ok = True
    try:
        user = _read_user_config()
    except (OSError, ValueError) as e:
        user, ok = {}, False
        log_message(f"❌ Settings file {CONFIG_PATH} has an error ({e}). "
                    "Using defaults until it is fixed.", "error")
    _apply_config(_deep_merge(_DEFAULTS, user))
    if ok:
        # Write back once at startup so new default keys land in the user's file.
        _write_config(current_config())
    _initialized = True
    return ok


def watch_folders():
    """Expanded list of folders to watch (accepts a string or a list in config)."""
    raw = SETTINGS.get("watch_folders") or SETTINGS.get("downloads_folder") or "~/Downloads"
    if isinstance(raw, str):
        raw = [raw]
    return [os.path.expanduser(p) for p in raw]


def reload_config():
    """Re-read the config file and apply it in place. Returns True if watch_folders changed."""
    try:
        user = _read_user_config()
    except (OSError, ValueError) as e:
        log_message(f"⚠️ Config reload skipped (unreadable): {e}", "error")
        desktop_notify("Auto Sorter", "Your settings file has a mistake, so the change was not applied.")
        return False
    before = watch_folders()
    _apply_config(_deep_merge(_DEFAULTS or default_config(), user))
    log_message("♻️ Config reloaded")
    return watch_folders() != before


# =========================
# LOGGING
# =========================
def log_message(message, level="info"):
    getattr(logging, level)(message)


def desktop_notify(title, body):
    """Best-effort desktop notification; silently no-ops if unavailable."""
    if not NOTIFICATIONS.get("enabled", True):
        return
    try:
        if NOTIFY_HOOK:
            NOTIFY_HOOK(title, body)
        else:
            pu.notify(title, body)
    except Exception as e:  # a failed popup must never stop sorting
        logging.debug("notification failed: %s", e)


# =========================
# MOVE LEDGER (undo / lookup)
# =========================
def record_move(src, dest):
    """Append a src->dest move so it can be looked up or undone later."""
    try:
        with LEDGER_LOCK:
            with open(MOVE_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "src": src, "dest": dest,
                }) + "\n")
            with open(MOVE_LOG, encoding="utf-8") as f:
                lines = f.readlines()
            if len(lines) > LEDGER_MAX_LINES:
                with open(MOVE_LOG, "w", encoding="utf-8") as f:
                    f.writelines(lines[-LEDGER_MAX_LINES:])
    except OSError as e:
        log_message(f"❌ Could not record move: {e}", "error")


def read_ledger():
    if not os.path.exists(MOVE_LOG):
        return []
    out = []
    with open(MOVE_LOG, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def find_in_ledger(name):
    name = name.lower()
    return [
        e for e in read_ledger()
        if name in os.path.basename(e["dest"]).lower()
        or name in os.path.basename(e["src"]).lower()
    ]


def _free_restore_path(path):
    """path, or 'name(restored).ext', 'name(restored 2).ext', ... - never an existing file."""
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    target = f"{base}(restored){ext}"
    n = 2
    while os.path.exists(target):
        target = f"{base}(restored {n}){ext}"
        n += 1
    return target


def undo_last(count=1):
    """Move the last `count` sorted files back. Returns lines describing what happened."""
    with LEDGER_LOCK:
        entries = read_ledger()
        if not entries:
            return ["Move log is empty - nothing to undo."]
        to_undo = entries[-count:]
        kept = entries[: len(entries) - len(to_undo)]
        failed = []
        lines = []
        done = 0
        for e in reversed(to_undo):
            src, dest = e["src"], e["dest"]
            if not os.path.exists(dest):
                lines.append(f"skip (already gone): {dest}")
                continue
            try:
                os.makedirs(os.path.dirname(src), exist_ok=True)
                target = _free_restore_path(src)
                shutil.move(dest, target)
            except OSError as err:
                lines.append(f"❌ could not move back {dest}: {err}")
                failed.append(e)  # keep it in the ledger so it can be retried
                continue
            lines.append(f"↩️  {dest}  ->  {target}")
            done += 1
        with open(MOVE_LOG, "w", encoding="utf-8") as f:
            for e in kept + failed[::-1]:
                f.write(json.dumps(e) + "\n")
    lines.append(f"Undid {done} move(s).")
    return lines


# =========================
# STATISTICS
# =========================
class SorterStats:
    def __init__(self):
        self.stats_file = STATS_FILE
        self.stats = self.load_stats()

    def load_stats(self):
        default_stats = {
            "total_files_processed": 0,
            "files_by_category": {},
            "total_size_moved_mb": 0,
            "last_summary": None,
            "session_start": datetime.now().isoformat(),  # first run ever, not this session
            "errors": 0,
        }
        if os.path.exists(self.stats_file):
            try:
                with open(self.stats_file, encoding="utf-8") as f:
                    loaded_stats = json.load(f)
                return {**default_stats, **loaded_stats}
            except (json.JSONDecodeError, OSError):
                return default_stats
        return default_stats

    def save_stats(self):
        try:
            with open(self.stats_file, "w", encoding="utf-8") as f:
                json.dump(self.stats, f, indent=2)
        except OSError as e:
            log_message(f"❌ Could not save stats: {e}", "error")

    def record_file_moved(self, category, file_size_mb):
        self.stats["total_files_processed"] += 1
        self.stats["files_by_category"][category] = (
            self.stats["files_by_category"].get(category, 0) + 1
        )
        self.stats["total_size_moved_mb"] += file_size_mb
        self.save_stats()

    def record_error(self):
        self.stats["errors"] += 1
        self.save_stats()

    def get_summary(self):
        return f"""
📊 DOWNLOAD SORTER SUMMARY
Total files processed: {self.stats['total_files_processed']}
Total size moved: {self.stats['total_size_moved_mb']:.2f} MB
Errors encountered: {self.stats['errors']}
Files by category: {json.dumps(self.stats['files_by_category'], indent=2)}
Tracking since: {self.stats['session_start']}
"""


# =========================
# HELPERS
# =========================
def is_file_complete(file_path, check_interval=1, checks=3):
    """Check if a file is fully written by watching size and mtime for stability."""
    try:
        stable_count = 0
        prev_size = -1
        prev_mtime = -1
        for _ in range(checks):
            stat = os.stat(file_path)
            size = stat.st_size
            mtime = stat.st_mtime
            if size == prev_size and mtime == prev_mtime:
                stable_count += 1
            else:
                stable_count = 0
                prev_size = size
                prev_mtime = mtime
            if stable_count >= 2:
                return True
            time.sleep(check_interval)
        return False
    except (FileNotFoundError, OSError):
        return False


def download_in_progress(file_path):
    """Firefox creates an empty foo.pdf next to foo.pdf.part while downloading;
    Chrome and Edge use foo.pdf.crdownload. Leave such files alone until the
    real one lands (its rename fires another event)."""
    if any(os.path.exists(file_path + ext) for ext in TEMP_EXTENSIONS):
        return True
    try:
        return os.path.getsize(file_path) == 0
    except OSError:
        return True


def get_file_hash(file_path, chunk_size=8192):
    """MD5 of a file, for duplicate-content detection."""
    hash_md5 = hashlib.md5()
    try:
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(chunk_size), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()
    except OSError:
        return None


def _same_content(a, b):
    try:
        if os.path.getsize(a) != os.path.getsize(b):
            return False
    except OSError:
        return False
    hash_a = get_file_hash(a)
    return hash_a is not None and hash_a == get_file_hash(b)


def should_exclude_file(filename):
    """True if filename matches an exclude pattern (wildcards allowed, case-insensitive)."""
    name = filename.lower()
    return any(fnmatch.fnmatch(name, p.lower()) for p in SETTINGS.get("exclude_patterns", []))


def ext_category(file_ext):
    for cat, exts in EXTENSIONS.items():
        if file_ext in exts:
            return cat
    return None


def match_name_rule(filename):
    """Return the 'dest' spec of the first matching name rule, else None."""
    for rule in SETTINGS.get("name_rules", []):
        pattern = rule.get("match")
        dest = rule.get("dest")
        if pattern and dest and fnmatch.fnmatch(filename.lower(), pattern.lower()):
            return dest
    return None


def get_destination_path(category, file_path):
    """Destination folder for a category, with optional date subfolder from file mtime.
    Folders are created by safe_move, so a dry run creates nothing."""
    base = os.path.expanduser(
        DESTINATIONS.get(category) or DESTINATIONS.get("Other", "~/Downloads/Other")
    )
    if SETTINGS.get("organize_by_date", False):
        try:
            ts = os.path.getmtime(file_path)
        except OSError:
            ts = time.time()
        date_folder = datetime.fromtimestamp(ts).strftime(
            SETTINGS.get("date_format", "%Y/%m")
        )
        base = os.path.join(base, date_folder)
    return base


def resolve_named_dest(dest_spec, file_path):
    """'Category/Sub/Dir' -> absolute folder, or None if the category is unknown."""
    parts = [p for p in dest_spec.split("/") if p]
    if not parts or parts[0] not in DESTINATIONS:
        return None
    return os.path.join(get_destination_path(parts[0], file_path), *parts[1:])


def safe_move(src, dest_folder, duplicate_action="rename"):
    """Move src into dest_folder, handling duplicate content and name clashes."""
    os.makedirs(dest_folder, exist_ok=True)
    filename = os.path.basename(src)
    dest_path = os.path.join(dest_folder, filename)

    if not os.path.exists(dest_path):
        shutil.move(src, dest_path)
        return dest_path

    # Identical content already at destination = true duplicate, drop the incoming copy
    if _same_content(src, dest_path):
        log_message(f"🗑️ Duplicate content, removing incoming {filename}")
        os.remove(src)
        return None

    if duplicate_action == "skip":
        log_message(f"⏭️ Skipping {filename} - already exists in destination")
        return None
    elif duplicate_action == "replace":
        shutil.move(src, dest_path)  # overwrites in one step
        return dest_path
    else:  # rename (default)
        base, ext = os.path.splitext(filename)
        counter = 1
        while os.path.exists(dest_path):
            dest_path = os.path.join(dest_folder, f"{base}({counter}){ext}")
            counter += 1
        shutil.move(src, dest_path)
        return dest_path


def cleanup_temp_files():
    """Remove stale browser temp files from every watched folder."""
    cutoff_time = time.time() - (24 * 3600)
    cleaned = 0
    for folder in watch_folders():
        if not os.path.isdir(folder):
            continue
        for name in os.listdir(folder):
            path = os.path.join(folder, name)
            try:
                if (os.path.isfile(path)
                        and name.lower().endswith(TEMP_EXTENSIONS)
                        and os.path.getmtime(path) < cutoff_time):
                    os.remove(path)
                    cleaned += 1
                    log_message(f"🧹 Cleaned old temp file: {name}")
            except OSError as e:
                log_message(f"❌ Could not clean {name}: {e}", "error")
    if cleaned:
        log_message(f"🧹 Cleaned {cleaned} old temporary files")


# =========================
# MAIN SORTER
# =========================
_STOP = object()
_FLUSH = object()


class EnhancedDownloadSorter(FileSystemEventHandler):
    """Receives folder events and sorts files on one worker thread, so a slow
    download never holds up events for other files."""

    def __init__(self):
        super().__init__()
        self._notify_buffer = []
        self.stats = SorterStats()
        self.last_summary = time.time()
        self.paused = False  # pauses automatic sorting; "Sort now" still works
        self._queue = queue.Queue()
        self._pending = set()
        self._pending_lock = threading.Lock()
        self._worker = None

    # lifecycle ------------------------------------------------------------
    def start(self):
        if SETTINGS.get("auto_cleanup_temp", True):
            cleanup_temp_files()
        self._worker = threading.Thread(target=self._work, name="sorter", daemon=True)
        self._worker.start()

    def stop(self, timeout=30):
        if self._worker:
            self._queue.put(_STOP)
            self._worker.join(timeout)
            self._worker = None
        self.flush_notifications()

    # watchdog events -------------------------------------------------------
    def on_created(self, event):
        if not event.is_directory:
            self.enqueue(event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            self.enqueue(event.src_path)

    def on_moved(self, event):
        # Browsers write foo.crdownload then rename to foo.pdf - the rename is
        # the cleanest "download finished" signal we get.
        if not event.is_directory:
            self.enqueue(event.dest_path)

    # queue ----------------------------------------------------------------
    def enqueue(self, path, manual=False):
        """Queue a file once, however many events arrive for it while it waits."""
        with self._pending_lock:
            if path in self._pending:
                return
            self._pending.add(path)
        self._queue.put((path, manual))

    def enqueue_existing(self, manual=False, announce=False):
        """Queue every file already sitting in the watched folders."""
        for folder in watch_folders():
            if not os.path.isdir(folder):
                continue
            for name in os.listdir(folder):
                path = os.path.join(folder, name)
                if os.path.isfile(path):
                    self.enqueue(path, manual=manual)
        if announce:
            self._queue.put(_FLUSH)

    def _work(self):
        last_flush = time.time()
        while True:
            try:
                item = self._queue.get(timeout=1)
            except queue.Empty:
                item = None
            if item is _STOP:
                return
            if item is _FLUSH:
                if self._notify_buffer:
                    self.flush_notifications()
                else:
                    desktop_notify("Auto Sorter", "Nothing new to sort.")
                last_flush = time.time()
            elif item is not None:
                path, manual = item
                with self._pending_lock:
                    self._pending.discard(path)
                if manual or not self.paused:
                    try:
                        self._process_event(path)
                    except Exception as e:  # keep the worker alive whatever happens
                        log_message(f"❌ Unexpected error on {path}: {e}", "error")
                        self.stats.record_error()
            gap = NOTIFICATIONS.get("notify_batch_seconds", 120)
            if gap > 0 and time.time() - last_flush > gap:
                self.flush_notifications()
                last_flush = time.time()

    # notification batching ----------------------------------------------------
    def flush_notifications(self):
        if not self._notify_buffer:
            return
        n = len(self._notify_buffer)
        if n == 1:
            desktop_notify("Auto Sorter", f"1 file sorted → {self._notify_buffer[0]}")
        else:
            parts = ", ".join(
                f"{count} {cat}" for cat, count in Counter(self._notify_buffer).most_common()
            )
            desktop_notify("Auto Sorter", f"{n} files sorted ({parts})")
        self._notify_buffer.clear()

    # core -------------------------------------------------------------------
    def _process_event(self, file_path):
        now = time.time()

        if (NOTIFICATIONS.get("show_summary", True)
                and now - self.last_summary
                > NOTIFICATIONS.get("summary_interval_minutes", 60) * 60):
            log_message(self.stats.get_summary())
            self.flush_notifications()
            self.last_summary = now

        self.sort_file(file_path)

    def sort_file(self, file_path):
        if not os.path.isfile(file_path):
            return

        filename = os.path.basename(file_path)

        if should_exclude_file(filename):
            log_message(f"⚠️ Excluding file: {filename}")
            return

        if filename.lower().endswith(TEMP_EXTENSIONS):
            log_message(f"⏳ Skipping temporary file: {filename}")
            return

        if download_in_progress(file_path):
            log_message(f"⏳ Download still in progress: {filename}")
            return

        max_age_days = SETTINGS.get("max_file_age_days", 0)
        if max_age_days > 0:
            file_age = (time.time() - os.path.getmtime(file_path)) / (24 * 3600)
            if file_age > max_age_days:
                log_message(f"⏳ Skipping old file: {filename} (age: {file_age:.1f} days)")
                return

        max_retries = 7
        for attempt in range(1, max_retries + 1):
            if is_file_complete(file_path, check_interval=1, checks=3):
                break
            log_message(f"⏳ Waiting for {filename} to complete... (Attempt {attempt}/{max_retries})")
            time.sleep(2)
        else:
            log_message(f"❌ File {filename} did not stabilize; skipping", "error")
            self.stats.record_error()
            return

        try:
            file_size_kb = os.path.getsize(file_path) / 1024
            min_size = SETTINGS.get("min_file_size_kb", 0)
            if min_size > 0 and file_size_kb < min_size:
                log_message(f"⏳ Skipping small file: {filename} ({file_size_kb:.1f} KB)")
                return
        except OSError:
            log_message(f"❌ Could not get size for {filename}", "error")
            return

        # --- decide category + destination folder ---------------------------
        category = None
        dest_folder = None

        rule_spec = match_name_rule(filename)
        if rule_spec:
            resolved = resolve_named_dest(rule_spec, file_path)
            if resolved:
                category, dest_folder = rule_spec.split("/")[0], resolved
            else:
                log_message(
                    f"⚠️ name_rule dest '{rule_spec}' has an unknown category; ignoring rule"
                )

        if dest_folder is None:
            file_ext = Path(filename).suffix.lstrip(".").lower()
            if not file_ext:
                log_message(f"⚠️ No extension found for: {filename}")
                return
            category = ext_category(file_ext)
            if not category:
                if SETTINGS.get("catch_all", False):
                    category = SETTINGS.get("catch_all_category", "Other")
                else:
                    log_message(f"⚠️ Unknown file type '.{file_ext}' for: {filename}")
                    return
            dest_folder = get_destination_path(category, file_path)

        # --- move ---------------------------------------------------------
        try:
            log_message(f"➡️ Processing {filename} → {category}")

            if SETTINGS.get("dry_run", False):
                log_message(f"🔍 [DRY RUN] Would move: {filename} → {dest_folder}")
                return

            start_time = time.time()
            duplicate_action = SETTINGS.get("duplicate_action", "rename")
            final_path = safe_move(file_path, dest_folder, duplicate_action)

            if final_path:
                duration = round(time.time() - start_time, 2)
                self.stats.record_file_moved(category, file_size_kb / 1024)
                record_move(file_path, final_path)
                self._notify_buffer.append(category)
                log_message(
                    f"✅ Moved: {filename} ({file_size_kb:.1f} KB) → {category} in {duration}s"
                )
        except OSError as e:
            log_message(f"❌ Error processing {filename}: {e}", "error")
            self.stats.record_error()


class ConfigReloadHandler(FileSystemEventHandler):
    """Reload the config file when it changes on disk."""

    def __init__(self, watcher):
        super().__init__()
        self._watcher = watcher
        self._timer = None
        self._lock = threading.Lock()

    def on_any_event(self, event):
        if event.is_directory or event.event_type in ("opened", "closed_no_write"):
            return
        target = os.path.abspath(CONFIG_PATH)
        paths = (event.src_path, getattr(event, "dest_path", "") or "")
        if not any(p and os.path.abspath(p) == target for p in paths):
            return
        # Editors save in several steps; reload once the file has been quiet for 1s.
        with self._lock:
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(1.0, self._watcher.on_config_changed)
            self._timer.daemon = True
            self._timer.start()


class Watcher:
    """Sorter + folder observer + settings hot-reload, shared by the CLI and the tray app."""

    def __init__(self):
        self.sorter = EnhancedDownloadSorter()
        self.observer = None
        self._config_handler = ConfigReloadHandler(self)

    @property
    def running(self):
        return self.observer is not None

    def start(self):
        log_message(f"🚀 Auto Sorter {__version__} started. Watching: {', '.join(watch_folders())}")
        self.sorter.start()
        self.observer = Observer()
        self._schedule()
        self.observer.start()
        if SETTINGS.get("organize_existing_on_start", False):
            self.sorter.enqueue_existing()

    def _schedule(self):
        self.observer.unschedule_all()
        for folder in watch_folders():
            try:
                os.makedirs(folder, exist_ok=True)
                self.observer.schedule(self.sorter, folder, recursive=False)
            except OSError as e:
                log_message(f"❌ Cannot watch {folder}: {e}", "error")
        self.observer.schedule(self._config_handler, os.path.dirname(CONFIG_PATH), recursive=False)

    def on_config_changed(self):
        if reload_config() and self.observer:
            log_message(f"👀 Now watching: {', '.join(watch_folders())}")
            self._schedule()

    def sort_now(self):
        """Sort everything already in the watched folders, then show one popup."""
        self.sorter.enqueue_existing(manual=True, announce=True)

    def stop(self):
        if self.observer:
            self.observer.stop()
            self.observer.join()
            self.observer = None
        self.sorter.stop()
        log_message("🛑 Auto Sorter stopped.")
        log_message(self.sorter.stats.get_summary())


def serve_forever(watcher):
    """Run until Ctrl+C or SIGTERM (e.g. `systemctl stop`), then shut down cleanly."""
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    if not watcher.running:
        watcher.start()
    try:
        while not stop.wait(1):
            pass
    except KeyboardInterrupt:
        pass
    watcher.stop()


# =========================
# CLI
# =========================
EXIT_ALREADY_RUNNING = 3  # the systemd unit does not restart on this code


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description="Auto Sorter - Downloads folder organizer")
    parser.add_argument("--version", action="version", version=f"Auto Sorter {__version__}")
    parser.add_argument("--dry-run", action="store_true",
                        help="Log what would move without moving anything")
    parser.add_argument("--cleanup", action="store_true",
                        help="Delete stale temp files and exit")
    parser.add_argument("--stats", action="store_true",
                        help="Print statistics and exit")
    parser.add_argument("--organize-existing", action="store_true",
                        help="Sort files already in the watched folders once, then exit")
    parser.add_argument("--undo-last", nargs="?", type=int, const=1, default=None,
                        metavar="N", help="Undo the last N moves (default 1) and exit")
    parser.add_argument("--where", metavar="NAME",
                        help="Show where files matching NAME were moved, then exit")
    parser.add_argument("--paths", action="store_true",
                        help="Print where settings and logs are stored, then exit")
    return parser.parse_args(argv)


def organize_existing_files():
    log_message("🔄 Organizing existing files in watched folders...")
    if SETTINGS.get("auto_cleanup_temp", True):
        cleanup_temp_files()
    sorter = EnhancedDownloadSorter()
    processed = 0
    for folder in watch_folders():
        if not os.path.isdir(folder):
            continue
        for filename in os.listdir(folder):
            file_path = os.path.join(folder, filename)
            if os.path.isfile(file_path):
                sorter.sort_file(file_path)
                processed += 1
    sorter.flush_notifications()
    log_message(f"✅ Finished organizing {processed} existing files")
    log_message(sorter.stats.get_summary())


# =========================
# MAIN
# =========================
def main(argv=None):
    args = parse_arguments(argv)
    init()

    if args.paths:
        print(f"Settings: {CONFIG_PATH}\nLogs:     {LOG_DIR}")
        return 0

    if args.stats:
        print(SorterStats().get_summary())
        return 0

    if args.where:
        matches = find_in_ledger(args.where)
        if not matches:
            print(f"No moves recorded for '{args.where}'.")
        for e in matches:
            print(f"{e['ts']}  {e['src']}  ->  {e['dest']}")
        return 0

    if args.undo_last is not None:
        print("\n".join(undo_last(args.undo_last)))
        return 0

    if args.cleanup:
        cleanup_temp_files()
        return 0

    if args.organize_existing:
        organize_existing_files()
        return 0

    if args.dry_run:
        SETTINGS["dry_run"] = True
        log_message("🔍 DRY RUN MODE ENABLED - No files will actually be moved")

    lock = pu.SingleInstance("watcher")
    if not lock.acquire():
        print("Auto Sorter is already running (tray app or another terminal).", file=sys.stderr)
        return EXIT_ALREADY_RUNNING

    log_message(f"📊 Settings: {json.dumps(SETTINGS, indent=2)}")
    serve_forever(Watcher())
    return 0


if __name__ == "__main__":
    sys.exit(main())
