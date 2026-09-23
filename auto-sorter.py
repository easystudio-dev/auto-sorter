import os
import shutil
import json
import logging
import logging.handlers
import time
import argparse
import hashlib
import subprocess
import fnmatch
from collections import Counter
from datetime import datetime
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# =========================
# CONFIG & SETUP
# =========================
CONFIG_PATH = os.path.expanduser("~/.config/download_sorter.json")
ENHANCED_DEFAULT_CONFIG = {
    "destinations": {
        "Documents": "~/Documents",
        "Pictures": "~/Pictures",
        "Videos": "~/Videos",
        "Music": "~/Music",
        "Archives": "~/Archives",
        "Programs": "~/Software",
        "Other": "~/Downloads/Other",
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
        "watch_folders": ["~/Downloads"],   # one path or a list; changing this needs a restart
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
        "exclude_patterns": [".DS_Store", "Thumbs.db", "desktop.ini"]
    },
    "notifications": {
        "enabled": True,
        "show_summary": True,
        "summary_interval_minutes": 60,
        "notify_batch_seconds": 120   # min gap between desktop popups; 0 = only the periodic summary
    }
}


def _deep_merge(base, override):
    """Recursively merge override onto a copy of base (base = defaults)."""
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _load_merged_config():
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    user = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                user = json.load(f)
        except (json.JSONDecodeError, OSError):
            user = {}
    return _deep_merge(ENHANCED_DEFAULT_CONFIG, user)


CONFIG = _load_merged_config()

# Write back once at startup so new default keys land in the user's file.
with open(CONFIG_PATH, "w") as f:
    json.dump(CONFIG, f, indent=4)

DESTINATIONS = CONFIG["destinations"]
EXTENSIONS = CONFIG["extensions"]
SETTINGS = CONFIG["settings"]
NOTIFICATIONS = CONFIG["notifications"]


def watch_folders():
    """Expanded list of folders to watch (accepts a string or a list in config)."""
    raw = SETTINGS.get("watch_folders") or SETTINGS.get("downloads_folder") or "~/Downloads"
    if isinstance(raw, str):
        raw = [raw]
    return [os.path.expanduser(p) for p in raw]


def reload_config():
    """Re-read the config file and apply it in place (globals keep their identity)."""
    try:
        with open(CONFIG_PATH) as f:
            user = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log_message(f"⚠️ Config reload skipped (unreadable): {e}", "error")
        return
    merged = _deep_merge(ENHANCED_DEFAULT_CONFIG, user)
    for target, key in ((DESTINATIONS, "destinations"), (EXTENSIONS, "extensions"),
                        (SETTINGS, "settings"), (NOTIFICATIONS, "notifications")):
        target.clear()
        target.update(merged[key])
    log_message("♻️ Config reloaded")


# =========================
# LOGGING
# =========================
LOG_DIR = os.path.expanduser("~/.Script_Logs")
LOG_FILE = os.path.join(LOG_DIR, "download_sorter.log")
STATS_FILE = os.path.join(LOG_DIR, "sorter_stats.json")
MOVE_LOG = os.path.join(LOG_DIR, "moves.jsonl")
LEDGER_MAX_LINES = 5000  # ponytail: rewrite-to-trim, fine for one line per move
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3
        ),
        logging.StreamHandler(),
    ],
)


def log_message(message, level="info"):
    getattr(logging, level)(message)


def desktop_notify(title, body):
    """Best-effort desktop notification; silently no-ops if unavailable."""
    if not NOTIFICATIONS.get("enabled", True):
        return
    try:
        subprocess.run(
            ["notify-send", title, body],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except (FileNotFoundError, OSError):
        pass


# =========================
# MOVE LEDGER (undo / lookup)
# =========================
def record_move(src, dest):
    """Append a src->dest move so it can be looked up or undone later."""
    try:
        with open(MOVE_LOG, "a") as f:
            f.write(json.dumps({
                "ts": datetime.now().isoformat(timespec="seconds"),
                "src": src, "dest": dest,
            }) + "\n")
        with open(MOVE_LOG) as f:
            lines = f.readlines()
        if len(lines) > LEDGER_MAX_LINES:
            with open(MOVE_LOG, "w") as f:
                f.writelines(lines[-LEDGER_MAX_LINES:])
    except OSError as e:
        log_message(f"❌ Could not record move: {e}", "error")


def read_ledger():
    if not os.path.exists(MOVE_LOG):
        return []
    out = []
    with open(MOVE_LOG) as f:
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


def undo_last(count=1):
    entries = read_ledger()
    if not entries:
        print("Move log is empty - nothing to undo.")
        return
    to_undo = entries[-count:]
    done = 0
    for e in reversed(to_undo):
        src, dest = e["src"], e["dest"]
        if not os.path.exists(dest):
            print(f"skip (already gone): {dest}")
            continue
        os.makedirs(os.path.dirname(src), exist_ok=True)
        target = src
        if os.path.exists(target):
            base, ext = os.path.splitext(src)
            target = f"{base}(restored){ext}"
        shutil.move(dest, target)
        print(f"↩️  {dest}  ->  {target}")
        done += 1
    remaining = entries[: len(entries) - len(to_undo)]
    with open(MOVE_LOG, "w") as f:
        for e in remaining:
            f.write(json.dumps(e) + "\n")
    print(f"Undid {done} move(s).")


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
            "session_start": datetime.now().isoformat(),
            "errors": 0,
        }
        if os.path.exists(self.stats_file):
            try:
                with open(self.stats_file) as f:
                    loaded_stats = json.load(f)
                return {**default_stats, **loaded_stats}
            except (json.JSONDecodeError, OSError):
                return default_stats
        return default_stats

    def save_stats(self):
        with open(self.stats_file, "w") as f:
            json.dump(self.stats, f, indent=2)

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
Session started: {self.stats['session_start']}
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


def should_exclude_file(filename):
    for pattern in SETTINGS.get("exclude_patterns", []):
        if pattern.lower() in filename.lower():
            return True
    return False


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


def _category_base(category, file_path):
    base = os.path.expanduser(
        DESTINATIONS.get(category) or DESTINATIONS.get("Other", "~/Downloads/Other")
    )
    os.makedirs(base, exist_ok=True)
    if SETTINGS.get("organize_by_date", False):
        try:
            ts = os.path.getmtime(file_path)
        except OSError:
            ts = time.time()
        date_folder = datetime.fromtimestamp(ts).strftime(
            SETTINGS.get("date_format", "%Y/%m")
        )
        base = os.path.join(base, date_folder)
        os.makedirs(base, exist_ok=True)
    return base


def get_destination_path(category, file_path):
    """Destination folder for a category, with optional date subfolder from file mtime."""
    return _category_base(category, file_path)


def resolve_named_dest(dest_spec, file_path):
    """'Category/Sub/Dir' -> absolute folder, or None if the category is unknown."""
    parts = [p for p in dest_spec.split("/") if p]
    if not parts or parts[0] not in DESTINATIONS:
        return None
    folder = os.path.join(_category_base(parts[0], file_path), *parts[1:])
    os.makedirs(folder, exist_ok=True)
    return folder


def safe_move(src, dest_folder, duplicate_action="rename"):
    """Move src into dest_folder, handling duplicate content and name clashes."""
    filename = os.path.basename(src)
    dest_path = os.path.join(dest_folder, filename)

    if not os.path.exists(dest_path):
        shutil.move(src, dest_path)
        return dest_path

    # Identical content already at destination = true duplicate, drop the incoming copy
    if get_file_hash(src) is not None and get_file_hash(src) == get_file_hash(dest_path):
        log_message(f"🗑️ Duplicate content, removing incoming {filename}")
        os.remove(src)
        return None

    if duplicate_action == "skip":
        log_message(f"⏭️ Skipping {filename} - already exists in destination")
        return None
    elif duplicate_action == "replace":
        os.remove(dest_path)
        shutil.move(src, dest_path)
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
    temp_extensions = (".crdownload", ".part", ".tmp", ".temp")
    cutoff_time = time.time() - (24 * 3600)
    cleaned = 0
    for folder in watch_folders():
        if not os.path.isdir(folder):
            continue
        for name in os.listdir(folder):
            path = os.path.join(folder, name)
            if (os.path.isfile(path)
                    and name.lower().endswith(temp_extensions)
                    and os.path.getmtime(path) < cutoff_time):
                try:
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
class EnhancedDownloadSorter(FileSystemEventHandler):
    def __init__(self):
        super().__init__()
        self._recently_processed = {}
        self._notify_buffer = []
        self.stats = SorterStats()
        self.last_summary = time.time()
        if SETTINGS.get("auto_cleanup_temp", True):
            cleanup_temp_files()

    # watchdog events -------------------------------------------------------
    def on_created(self, event):
        if not event.is_directory:
            self._process_event(event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            self._process_event(event.src_path)

    def on_moved(self, event):
        # Browsers write foo.crdownload then rename to foo.pdf - the rename is
        # the cleanest "download finished" signal we get.
        if not event.is_directory:
            self._process_event(event.dest_path)

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

        last_time = self._recently_processed.get(file_path, 0)
        if now - last_time < 10:
            return
        self._recently_processed[file_path] = now

        # ponytail: periodic dict prune, swap for cachetools.TTLCache if churn matters
        if len(self._recently_processed) > 512:
            cutoff = now - 60
            self._recently_processed = {
                p: t for p, t in self._recently_processed.items() if t > cutoff
            }

        self.sort_file(file_path)

    def sort_file(self, file_path):
        if not os.path.isfile(file_path):
            return

        filename = os.path.basename(file_path)

        if should_exclude_file(filename):
            log_message(f"⚠️ Excluding file: {filename}")
            return

        if filename.lower().endswith((".crdownload", ".part", ".tmp", ".temp")):
            log_message(f"⏳ Skipping temporary file: {filename}")
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
                file_size_mb = round(file_size_kb / 1024, 2)
                self.stats.record_file_moved(category, file_size_mb)
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

    def __init__(self):
        super().__init__()
        self._last = 0.0

    def on_any_event(self, event):
        if event.is_directory:
            return
        if os.path.abspath(event.src_path) != os.path.abspath(CONFIG_PATH):
            return
        now = time.time()
        if now - self._last < 1:
            return
        self._last = now
        reload_config()


# =========================
# CLI
# =========================
def parse_arguments():
    parser = argparse.ArgumentParser(description="Auto Sorter - Downloads folder organizer")
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
    return parser.parse_args()


def organize_existing_files():
    log_message("🔄 Organizing existing files in watched folders...")
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
if __name__ == "__main__":
    args = parse_arguments()

    if args.stats:
        print(SorterStats().get_summary())
        raise SystemExit(0)

    if args.where:
        matches = find_in_ledger(args.where)
        if not matches:
            print(f"No moves recorded for '{args.where}'.")
        for e in matches:
            print(f"{e['ts']}  {e['src']}  ->  {e['dest']}")
        raise SystemExit(0)

    if args.undo_last is not None:
        undo_last(args.undo_last)
        raise SystemExit(0)

    if args.cleanup:
        cleanup_temp_files()
        raise SystemExit(0)

    if args.organize_existing:
        organize_existing_files()
        raise SystemExit(0)

    if args.dry_run:
        SETTINGS["dry_run"] = True
        log_message("🔍 DRY RUN MODE ENABLED - No files will actually be moved")

    folders = watch_folders()
    log_message(f"🚀 Auto Sorter started. Watching: {', '.join(folders)}")
    log_message(f"📊 Settings: {json.dumps(SETTINGS, indent=2)}")

    event_handler = EnhancedDownloadSorter()

    if SETTINGS.get("organize_existing_on_start", False):
        for _folder in folders:
            if not os.path.isdir(_folder):
                continue
            for _name in os.listdir(_folder):
                _p = os.path.join(_folder, _name)
                if os.path.isfile(_p):
                    event_handler.sort_file(_p)
        event_handler.flush_notifications()

    observer = Observer()
    for _folder in folders:
        os.makedirs(_folder, exist_ok=True)
        observer.schedule(event_handler, _folder, recursive=False)
    observer.schedule(ConfigReloadHandler(), os.path.dirname(CONFIG_PATH), recursive=False)
    observer.start()

    last_flush = time.time()
    try:
        while True:
            time.sleep(1)
            gap = NOTIFICATIONS.get("notify_batch_seconds", 120)
            if gap > 0 and time.time() - last_flush > gap:
                event_handler.flush_notifications()
                last_flush = time.time()
    except KeyboardInterrupt:
        observer.stop()
        log_message("🛑 Stopping Auto Sorter...")
        event_handler.flush_notifications()
        log_message(event_handler.stats.get_summary())

    observer.join()
    log_message("👋 Auto Sorter stopped.")
