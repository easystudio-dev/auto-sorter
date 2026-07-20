import os
import shutil
import json
import logging
import time
import argparse
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# =========================
# ENHANCED CONFIG & SETUP
# =========================
DOWNLOADS_FOLDER = os.path.expanduser("~/Downloads")
BASE_DESTINATION = {
    "Documents": os.path.expanduser("~/Documents"),
    "Pictures": os.path.expanduser("~/Pictures"),
    "Videos": os.path.expanduser("~/Videos"),
    "Music": os.path.expanduser("~/Music"),
    "Archives": os.path.expanduser("~/Archives"),  # Or ~/Documents/Archives
    "Programs": os.path.expanduser("~/Software")   # Or ~/bin for executables
}

# Create all destination folders
for folder in BASE_DESTINATION.values():
    os.makedirs(folder, exist_ok=True)

CONFIG_PATH = os.path.expanduser("~/.config/download_sorter.json")
ENHANCED_DEFAULT_CONFIG = {
    "extensions": {
        "Documents": ["pdf", "docx", "txt", "xlsx", "pptx", "doc", "rtf", "odt", "csv"],
        "Pictures": ["jpg", "jpeg", "png", "gif", "bmp", "svg", "webp", "tiff", "ico"],
        "Videos": ["mp4", "mkv", "avi", "mov", "wmv", "flv", "webm", "m4v", "3gp"],
        "Music": ["mp3", "wav", "flac", "aac", "ogg", "wma", "m4a"],
        "Archives": ["zip", "rar", "7z", "tar", "gz", "bz2", "xz"],
        "Programs": ["exe", "msi", "deb", "rpm", "dmg", "pkg", "appimage"]
    },
    "settings": {
        "organize_by_date": False,
        "date_format": "%Y/%m",  # Year/Month folders
        "duplicate_action": "rename",  # "rename", "skip", "replace"
        "min_file_size_kb": 0,  # Skip files smaller than this
        "max_file_age_days": 0,  # Only process files newer than this (0 = all files)
        "dry_run": False,  # Test mode - log actions without moving files
        "auto_cleanup_temp": True,  # Remove temp files older than 24h
        "exclude_patterns": [".DS_Store", "Thumbs.db", "desktop.ini"]
    },
    "notifications": {
        "enabled": True,
        "show_summary": True,
        "summary_interval_minutes": 60
    }
}

# Initialize config
if not os.path.exists(CONFIG_PATH):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(ENHANCED_DEFAULT_CONFIG, f, indent=4)

with open(CONFIG_PATH) as f:
    CONFIG = json.load(f)

EXTENSIONS = CONFIG.get("extensions", ENHANCED_DEFAULT_CONFIG["extensions"])
SETTINGS = CONFIG.get("settings", ENHANCED_DEFAULT_CONFIG["settings"])
NOTIFICATIONS = CONFIG.get("notifications", ENHANCED_DEFAULT_CONFIG["notifications"])

# =========================
# ENHANCED LOGGING SETUP
# =========================
LOG_DIR = os.path.expanduser("~/.Script_Logs")
LOG_FILE = os.path.join(LOG_DIR, "download_sorter.log")
STATS_FILE = os.path.join(LOG_DIR, "sorter_stats.json")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()  # Also log to console
    ]
)

def log_message(message, level="info"):
    getattr(logging, level)(message)

# =========================
# STATISTICS & MONITORING
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
            "errors": 0
        }
        
        if os.path.exists(self.stats_file):
            try:
                with open(self.stats_file) as f:
                    loaded_stats = json.load(f)
                return {**default_stats, **loaded_stats}
            except:
                return default_stats
        return default_stats
    
    def save_stats(self):
        with open(self.stats_file, "w") as f:
            json.dump(self.stats, f, indent=2)
    
    def record_file_moved(self, category, file_size_mb):
        self.stats["total_files_processed"] += 1
        self.stats["files_by_category"][category] = self.stats["files_by_category"].get(category, 0) + 1
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
# HELPER FUNCTIONS
# =========================
def is_file_complete(file_path, check_interval=1, checks=3):
    """Check if file is completely downloaded by monitoring size and mtime stability."""
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
    """Calculate MD5 hash of file for duplicate detection."""
    hash_md5 = hashlib.md5()
    try:
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(chunk_size), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()
    except:
        return None

def should_exclude_file(filename):
    """Check if file should be excluded based on patterns."""
    for pattern in SETTINGS.get("exclude_patterns", []):
        if pattern.lower() in filename.lower():
            return True
    return False

def get_destination_path(category, filename):
    """Get the final destination path, optionally organized by date."""
    base_dest = BASE_DESTINATION[category]
    
    if SETTINGS.get("organize_by_date", False):
        date_folder = datetime.now().strftime(SETTINGS.get("date_format", "%Y/%m"))
        dest_folder = os.path.join(base_dest, date_folder)
        os.makedirs(dest_folder, exist_ok=True)
        return dest_folder
    
    return base_dest

def safe_move(src, dest_folder, duplicate_action="rename"):
    """Enhanced file moving with duplicate handling options."""
    filename = os.path.basename(src)
    dest_path = os.path.join(dest_folder, filename)
    
    if not os.path.exists(dest_path):
        shutil.move(src, dest_path)
        return dest_path
    
    # Handle duplicates based on action
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
            new_filename = f"{base}({counter}){ext}"
            dest_path = os.path.join(dest_folder, new_filename)
            counter += 1
        shutil.move(src, dest_path)
        return dest_path

def cleanup_temp_files():
    """Remove old temporary files from Downloads folder."""
    temp_extensions = [".crdownload", ".part", ".tmp", ".temp"]
    cutoff_time = time.time() - (24 * 3600)  # 24 hours ago
    
    cleaned = 0
    for file in os.listdir(DOWNLOADS_FOLDER):
        file_path = os.path.join(DOWNLOADS_FOLDER, file)
        if (os.path.isfile(file_path) and 
            any(file.lower().endswith(ext) for ext in temp_extensions) and
            os.path.getmtime(file_path) < cutoff_time):
            try:
                os.remove(file_path)
                cleaned += 1
                log_message(f"🧹 Cleaned old temp file: {file}")
            except Exception as e:
                log_message(f"❌ Could not clean {file}: {e}", "error")
    
    if cleaned > 0:
        log_message(f"🧹 Cleaned {cleaned} old temporary files")

# =========================
# ENHANCED MAIN SORTER
# =========================
class EnhancedDownloadSorter(FileSystemEventHandler):
    def __init__(self):
        super().__init__()
        self._recently_processed = {}
        self.stats = SorterStats()
        self.last_summary = time.time()
        
        # Initial cleanup if enabled
        if SETTINGS.get("auto_cleanup_temp", True):
            cleanup_temp_files()

    def on_created(self, event):
        if event.is_directory:
            return
        self._process_event(event.src_path)

    def on_modified(self, event):
        if event.is_directory:
            return
        self._process_event(event.src_path)

    def _process_event(self, file_path):
        now = time.time()
        
        # Show periodic summary
        if (NOTIFICATIONS.get("show_summary", True) and 
            now - self.last_summary > NOTIFICATIONS.get("summary_interval_minutes", 60) * 60):
            log_message(self.stats.get_summary())
            self.last_summary = now
        
        # Debounce processing
        last_time = self._recently_processed.get(file_path, 0)
        if now - last_time < 10:
            return
        self._recently_processed[file_path] = now

        self.sort_file(file_path)

    def sort_file(self, file_path):
        """Enhanced file sorting with all new features."""
        if not os.path.isfile(file_path):
            return

        filename = os.path.basename(file_path)
        
        # Check exclusion patterns
        if should_exclude_file(filename):
            log_message(f"⚠️ Excluding file: {filename}")
            return

        # Skip temporary files
        if filename.lower().endswith((".crdownload", ".part", ".tmp", ".temp")):
            log_message(f"⏳ Skipping temporary file: {filename}")
            return

        # Check file age if configured
        max_age_days = SETTINGS.get("max_file_age_days", 0)
        if max_age_days > 0:
            file_age = (time.time() - os.path.getmtime(file_path)) / (24 * 3600)
            if file_age > max_age_days:
                log_message(f"⏳ Skipping old file: {filename} (age: {file_age:.1f} days)")
                return

        # Wait for file completion
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

        # Check minimum file size
        try:
            file_size_kb = os.path.getsize(file_path) / 1024
            min_size = SETTINGS.get("min_file_size_kb", 0)
            if min_size > 0 and file_size_kb < min_size:
                log_message(f"⏳ Skipping small file: {filename} ({file_size_kb:.1f} KB)")
                return
        except OSError:
            log_message(f"❌ Could not get size for {filename}", "error")
            return

        # Determine file category
        if "." not in filename:
            log_message(f"⚠️ No extension found for: {filename}")
            return

        file_ext = filename.split(".")[-1].lower()
        category = None
        
        for cat, extensions in EXTENSIONS.items():
            if file_ext in extensions:
                category = cat
                break

        if not category:
            log_message(f"⚠️ Unknown file type '.{file_ext}' for: {filename}")
            return

        # Get destination folder
        try:
            dest_folder = get_destination_path(category, filename)
            log_message(f"➡️ Processing {filename} → {category}")

            # Dry run mode
            if SETTINGS.get("dry_run", False):
                log_message(f"🔍 [DRY RUN] Would move: {filename} → {dest_folder}")
                return

            # Move file
            start_time = time.time()
            duplicate_action = SETTINGS.get("duplicate_action", "rename")
            final_path = safe_move(file_path, dest_folder, duplicate_action)
            
            if final_path:  # File was moved (not skipped)
                duration = round(time.time() - start_time, 2)
                file_size_mb = round(file_size_kb / 1024, 2)
                
                # Update statistics
                self.stats.record_file_moved(category, file_size_mb)
                
                log_message(f"✅ Moved: {filename} ({file_size_kb:.1f} KB) → {category} in {duration}s")
                
                if NOTIFICATIONS.get("enabled", True):
                    log_message(f"📁 Total files processed: {self.stats.stats['total_files_processed']}")

        except Exception as e:
            log_message(f"❌ Error processing {filename}: {e}", "error")
            self.stats.record_error()

# =========================
# COMMAND LINE INTERFACE
# =========================
def parse_arguments():
    parser = argparse.ArgumentParser(description="Enhanced Download Folder Organizer")
    parser.add_argument("--dry-run", action="store_true", 
                       help="Test mode - show what would be moved without actually moving")
    parser.add_argument("--cleanup", action="store_true",
                       help="Clean up old temporary files and exit")
    parser.add_argument("--stats", action="store_true",
                       help="Show statistics and exit")
    parser.add_argument("--organize-existing", action="store_true",
                       help="Organize existing files in Downloads folder once and exit")
    return parser.parse_args()

def organize_existing_files():
    """One-time organization of existing files in Downloads folder."""
    log_message("🔄 Organizing existing files in Downloads folder...")
    sorter = EnhancedDownloadSorter()
    
    processed = 0
    for filename in os.listdir(DOWNLOADS_FOLDER):
        file_path = os.path.join(DOWNLOADS_FOLDER, filename)
        if os.path.isfile(file_path):
            sorter.sort_file(file_path)
            processed += 1
    
    log_message(f"✅ Finished organizing {processed} existing files")
    log_message(sorter.stats.get_summary())

# =========================
# MAIN EXECUTION
# =========================
if __name__ == "__main__":
    args = parse_arguments()
    
    # Handle command line options
    if args.stats:
        stats = SorterStats()
        print(stats.get_summary())
        exit(0)
    
    if args.cleanup:
        cleanup_temp_files()
        exit(0)
    
    if args.organize_existing:
        organize_existing_files()
        exit(0)
    
    # Set dry run mode if specified
    if args.dry_run:
        SETTINGS["dry_run"] = True
        log_message("🔍 DRY RUN MODE ENABLED - No files will actually be moved")
    
    # Start monitoring
    log_message("🚀 Enhanced Download Sorter started. Monitoring Downloads folder...")
    log_message(f"📊 Configuration: {json.dumps(SETTINGS, indent=2)}")
    
    event_handler = EnhancedDownloadSorter()
    observer = Observer()
    observer.schedule(event_handler, DOWNLOADS_FOLDER, recursive=False)
    observer.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
        log_message("🛑 Stopping Download Sorter...")
        log_message(event_handler.stats.get_summary())

    observer.join()
    log_message("👋 Download Sorter stopped.")