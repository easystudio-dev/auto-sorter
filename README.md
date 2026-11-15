# Auto Sorter

Keeps your Downloads folder tidy. New files are moved into **Documents**,
**Pictures**, **Videos**, **Music**, **Archives** and **Software** by type, as
soon as they finish downloading. Works on **Windows** and **Linux**.

## Install (no programming needed)

Download the latest version from the
[Releases page](https://github.com/easystudio-dev/auto-sorter/releases/latest).

### Windows

1. Download **AutoSorter-windows.exe**.
2. Move it somewhere permanent, for example your Documents folder. (If you move
   it later, open it once from the new place.)
3. Double-click it.
   - Windows may say *"Windows protected your PC"*, because the app isn't
     signed. Click **More info**, then **Run anyway**.
4. A folder icon appears in the system tray, at the bottom right. You may
   need to click the **^** arrow to see it.

### Linux

1. Download **AutoSorter-linux-x86_64.tar.gz**.
2. Extract it: right-click it and choose **Extract Here**.
3. Move the extracted **AutoSorter** file somewhere permanent, for example
   `~/Applications`.
4. Double-click it. If nothing happens, right-click it, open **Properties**,
   then **Permissions**, and tick **Allow executing file as program**.
   - Or, in a terminal: `chmod +x AutoSorter && ./AutoSorter`
5. A folder icon appears in the system tray.

That's it. Auto Sorter now **starts by itself every time you log in**.

## Using it

Right-click the tray icon:

| Menu item | What it does |
|-----------|--------------|
| **Pause sorting** | Stop sorting new files until you un-tick it |
| **Sort now** | Sort everything already in Downloads |
| **Undo last move** | Put the last sorted file back in Downloads |
| **Open settings** | Change folders and rules (see below) |
| **Open log** | See what was moved and why |
| **Start when I log in** | Turn starting at login on or off |
| **Quit** | Stop Auto Sorter until you open it again |

Files still downloading are left alone until they finish. If a file with the
same name already exists, the new one is saved as `name(1).pdf`. Exact copies
of a file that's already there are deleted.

## Settings

**Open settings** opens the settings file in a text editor. Changes apply as
soon as you save. If the file has a typo, Auto Sorter tells you and keeps
using the previous settings until you fix it.

Where the file lives:

- Windows: `%APPDATA%\AutoSorter\config.json`
- Linux: `~/.config/auto-sorter/config.json`

The most common changes:

```jsonc
{
  "destinations": {
    "Pictures": "D:\\Photos"            // Windows paths need double backslashes
  },
  "settings": {
    "watch_folders": ["C:\\Users\\you\\Downloads", "C:\\Users\\you\\Desktop"],
    "organize_by_date": true,           // Pictures\2026\11\photo.jpg
    "catch_all": true,                  // unknown file types go to "Other"
    "name_rules": [                     // filename rules win over file type
      { "match": "*invoice*",   "dest": "Documents/Invoices" },
      { "match": "Screenshot*", "dest": "Pictures/Screenshots" }
    ]
  }
}
```

<details>
<summary>All settings</summary>

```jsonc
{
  "destinations": { "Documents": "...", "Other": "..." },   // category -> folder
  "extensions":   { "Documents": ["pdf", "docx", "txt"] },  // category -> file types
  "settings": {
    "watch_folders": ["..."],
    "organize_by_date": false,
    "date_format": "%Y/%m",
    "duplicate_action": "rename",       // rename | skip | replace
    "min_file_size_kb": 0,
    "max_file_age_days": 0,             // 0 = any age
    "dry_run": false,                   // log only, move nothing
    "auto_cleanup_temp": true,          // delete leftover *.crdownload/*.part over 24h old
    "organize_existing_on_start": false,
    "catch_all": false,
    "catch_all_category": "Other",
    "name_rules": [],                   // "dest" is Category or Category/Sub/Folder
    "exclude_patterns": [".DS_Store", "Thumbs.db", "desktop.ini"]  // wildcards: "*.ini"
  },
  "notifications": {
    "enabled": true,
    "show_summary": true,
    "summary_interval_minutes": 60,
    "notify_batch_seconds": 120         // min gap between popups
  }
}
```

</details>

## Troubleshooting

- **No tray icon on Linux (GNOME / Ubuntu).** GNOME hides tray icons by
  default. Install the *AppIndicator and KStatusNotifierItem Support*
  extension. Sorting keeps working even without the icon.
- **"Auto Sorter is already running".** Only one copy runs at a time. Look for
  the icon in the tray.
- **My antivirus flagged it.** Some antivirus tools flag unsigned one-file
  apps by mistake. You can check the source in this repository, or run it
  from source (below).
- **Where did my file go?** Check **Open log**, or run
  `auto_sorter.py --where NAME` from source.

## Uninstall

1. Right-click the tray icon, un-tick **Start when I log in**, then click
   **Quit**.
2. Delete the AutoSorter app file.
3. Optional: delete the settings and logs.
   - Windows: `%APPDATA%\AutoSorter` and `%LOCALAPPDATA%\AutoSorter`
   - Linux: `~/.config/auto-sorter` and `~/.local/state/auto-sorter`

---

## For developers

### Run from source

```bash
python -m venv .env
.env/bin/pip install -r requirements.txt        # Windows: .env\Scripts\pip
.env/bin/python app.py                          # tray app
.env/bin/python auto_sorter.py                  # headless, foreground
.env/bin/python auto_sorter.py --dry-run        # log actions, move nothing
```

| Flag | Effect |
|------|--------|
| `--dry-run` | Log what would move, move nothing |
| `--organize-existing` | Sort files already in the watched folders once, then exit |
| `--cleanup` | Delete stale `*.crdownload`/`*.part`/`*.tmp` (>24h) and exit |
| `--stats` | Print the running totals and exit |
| `--undo-last [N]` | Move the last N sorted files back (default 1) |
| `--where NAME` | Show where files matching NAME were moved |
| `--paths` | Print where settings and logs are stored |

### Headless Linux service (systemd)

For a server or a machine without a desktop:

```bash
./install.sh                          # venv + systemd user service
sudo loginctl enable-linger $USER     # optional: start at boot without logging in
journalctl --user -u auto-sorter -f   # live logs
```

The tray app and the service can't run at the same time. A lock file stops the
second copy, and the service won't restart-loop when that happens.

### Files

| File | Purpose |
|------|---------|
| `auto_sorter.py` | Sorting engine and CLI |
| `app.py` | Tray app, the entry point of the standalone build |
| `platform_utils.py` | Windows/Linux differences: folders, autostart, notifications, lock |
| `build.py` | Builds the standalone app with PyInstaller |
| `.github/workflows/build.yml` | Builds Windows + Linux apps; publishes them on `v*` tags |

Logs, stats and the undo ledger (`moves.jsonl`) live in the data folder
(`--paths`). On Linux, files from versions before 1.0
(`~/.config/download_sorter.json`, `~/.Script_Logs/`) are copied over on first
start.

### Tests

```bash
.env/bin/python test_auto_sorter.py
```

The tests run in a temporary `AUTO_SORTER_HOME`, so they never touch your real
settings or folders.

### Build and release

```bash
.env/bin/pip install pyinstaller
.env/bin/python build.py              # -> dist/AutoSorter(.exe), for the current OS
```

To publish downloads, push a version tag. CI builds both apps and attaches them
to a GitHub Release:

```bash
git tag v1.0.0 && git push origin v1.0.0
```
