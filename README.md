# Auto Sorter

Watches your Downloads folder and files new downloads into `~/Documents`,
`~/Pictures`, `~/Videos`, `~/Music`, `~/Archives`, `~/Software` by type. Runs as
a background systemd user service.

## Features

- Watches one or more folders, sorts by extension
- Optional filename rules (`*invoice*` → `Documents/Invoices`)
- Optional `Other` catch-all bucket for unknown types
- Optional date subfolders (`2026/09`), based on the file's own timestamp
- Duplicate-content detection (same bytes, any name → dropped)
- Waits for downloads to finish before moving
- Move ledger with `--undo-last` and `--where`
- Desktop notifications, batched (one popup per `notify_batch_seconds`), rotating log
- Config hot-reloads on save (except `watch_folders`, which needs a restart)

## Install

```bash
./install.sh
sudo loginctl enable-linger $USER   # optional: start at boot without logging in
```

`install.sh` creates the `.env` venv, installs `watchdog`, and enables the
`auto-sorter` systemd user service.

### Service commands

```bash
systemctl --user status auto-sorter
systemctl --user restart auto-sorter      # after editing the code
journalctl --user -u auto-sorter -f       # live logs
```

## Run manually

```bash
.env/bin/python auto-sorter.py            # foreground
.env/bin/python auto-sorter.py --dry-run  # log actions, move nothing
```

## CLI

| Flag | Effect |
|------|--------|
| `--dry-run` | Log what would move, move nothing |
| `--organize-existing` | Sort files already in the watched folders once, then exit |
| `--cleanup` | Delete stale `*.crdownload`/`*.part`/`*.tmp` (>24h) and exit |
| `--stats` | Print the running totals and exit |
| `--undo-last [N]` | Move the last N sorted files back where they came from (default 1) |
| `--where NAME` | Show where files matching NAME were moved |

## Config

`~/.config/download_sorter.json` — created on first run, missing keys are
back-filled on startup. Saving the file reloads it live.

```jsonc
{
  "destinations": {
    "Documents": "~/Documents",
    "Other": "~/Downloads/Other"
  },
  "extensions": {
    "Documents": ["pdf", "docx", "txt"]
  },
  "settings": {
    "watch_folders": ["~/Downloads", "~/Desktop"],
    "organize_by_date": false,
    "date_format": "%Y/%m",
    "duplicate_action": "rename",        // rename | skip | replace
    "min_file_size_kb": 0,
    "max_file_age_days": 0,              // 0 = any age
    "dry_run": false,
    "auto_cleanup_temp": true,
    "organize_existing_on_start": false,
    "catch_all": false,                 // unknown extensions -> catch_all_category
    "catch_all_category": "Other",
    "name_rules": [
      { "match": "*invoice*",   "dest": "Documents/Invoices" },
      { "match": "Screenshot*", "dest": "Pictures/Screenshots" }
    ],
    "exclude_patterns": [".DS_Store", "Thumbs.db"]
  },
  "notifications": {
    "enabled": true,
    "show_summary": true,
    "summary_interval_minutes": 60,
    "notify_batch_seconds": 120        // min gap between popups; 0 = only the periodic summary
  }
}
```

Name rules win over extension matching. A rule `dest` is `Category` or
`Category/Sub/Dir`; the first segment must be a key in `destinations`.

## Logs

`~/.Script_Logs/` — `download_sorter.log` (rotates at 5 MB × 3),
`sorter_stats.json`, `moves.jsonl` (the undo ledger).

## Tests

```bash
.env/bin/python test_auto_sorter.py
```

## Uninstall

```bash
systemctl --user disable --now auto-sorter
rm ~/.config/systemd/user/auto-sorter.service
```
