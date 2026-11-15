"""Auto Sorter tray app. This is what the standalone AutoSorter.exe / AutoSorter
binary runs: double-click it and it sorts new files in the background, with a
tray icon for Pause, Sort now, Undo, Settings and Start-at-login.

From source: python app.py
"""
import os
import sys

import auto_sorter as core
import platform_utils as pu

BLUE = (37, 99, 235, 255)
GREY = (128, 128, 128, 255)

WELCOME = ("Auto Sorter is running. New files in your Downloads folder will be sorted "
           "into Documents, Pictures, Videos and more. It starts when you log in; "
           "right-click the tray icon to change that.")
CONFIG_ERROR = ("Your settings file has a mistake, so the default settings are in use. "
                "Choose 'Open settings' from the tray icon to fix it.")


def make_icon(size=64, paused=False):
    """Folder with a down arrow, drawn in code so the repo needs no image files."""
    from PIL import Image, ImageDraw

    s = size / 64
    color = GREY if paused else BLUE
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([4 * s, 8 * s, 30 * s, 22 * s], radius=int(4 * s), fill=color)   # tab
    d.rounded_rectangle([4 * s, 14 * s, 60 * s, 58 * s], radius=int(6 * s), fill=color)  # body
    d.rectangle([28 * s, 22 * s, 36 * s, 38 * s], fill="white")                          # arrow shaft
    d.polygon([(19 * s, 36 * s), (45 * s, 36 * s), (32 * s, 51 * s)], fill="white")      # arrow head
    return img


def _try(fn, *args):
    try:
        fn(*args)
        return True
    except OSError as e:
        core.log_message(f"⚠️ {fn.__name__} failed: {e}", "error")
        return False


def first_run_setup():
    """Turn on start-at-login the first time, so a double-click is the whole setup.
    Returns True on the first run."""
    marker = os.path.join(core.LOG_DIR, "welcomed")
    if os.path.exists(marker):
        if pu.autostart_enabled():
            _try(pu.set_autostart, True)  # refresh the path in case the app was moved
        return False
    _try(pu.set_autostart, True)
    open(marker, "w").close()
    return True


def _folder_names():
    return ", ".join(os.path.basename(f.rstrip("\\/")) or f for f in core.watch_folders())


def run_tray(watcher, welcome, config_ok):
    """Show the tray icon and block until Quit. Raises if this desktop has no tray."""
    import pystray
    from pystray import Menu, MenuItem as Item

    def safe(fn):
        def action(icon, item):
            try:
                fn(icon)
            except Exception as e:
                core.log_message(f"❌ Menu action failed: {e}", "error")
        return action

    def toggle_pause(icon):
        watcher.sorter.paused = not watcher.sorter.paused
        icon.icon = make_icon(paused=watcher.sorter.paused)
        icon.title = "Auto Sorter (paused)" if watcher.sorter.paused else "Auto Sorter"
        core.log_message("⏸️ Paused" if watcher.sorter.paused else "▶️ Resumed")
        icon.update_menu()  # Windows only re-reads checkmarks when asked

    def toggle_autostart(icon):
        pu.set_autostart(not pu.autostart_enabled())
        icon.update_menu()

    def undo(icon):
        lines = core.undo_last(1)
        for line in lines:
            core.log_message(line)
        core.desktop_notify("Auto Sorter", lines[0])

    def quit_app(icon):
        watcher.stop()
        icon.stop()

    menu = Menu(
        Item(lambda item: f"Watching: {_folder_names()}", lambda: None, enabled=False),
        Menu.SEPARATOR,
        Item("Pause sorting", safe(toggle_pause), checked=lambda item: watcher.sorter.paused),
        Item("Sort now", safe(lambda icon: watcher.sort_now())),
        Item("Undo last move", safe(undo)),
        Menu.SEPARATOR,
        Item("Open settings", safe(lambda icon: pu.open_file(core.CONFIG_PATH))),
        Item("Open log", safe(lambda icon: pu.open_file(core.LOG_FILE))),
        Item("Start when I log in", safe(toggle_autostart),
             checked=lambda item: pu.autostart_enabled()),
        Menu.SEPARATOR,
        Item(f"Quit (v{core.__version__})", safe(quit_app)),
    )
    icon = pystray.Icon(pu.APP_ID, make_icon(), "Auto Sorter", menu)

    def setup(icon):
        icon.visible = True
        if getattr(icon, "HAS_NOTIFICATION", False):
            core.NOTIFY_HOOK = lambda title, body: icon.notify(body, title)
        try:
            watcher.start()
        except Exception as e:
            core.log_message(f"❌ Could not start watching: {e}", "error")
            core.desktop_notify("Auto Sorter", f"Could not start watching your folders: {e}")
            return
        if not config_ok:
            core.desktop_notify("Auto Sorter", CONFIG_ERROR)
        elif welcome:
            core.desktop_notify("Auto Sorter", WELCOME)

    icon.run(setup=setup)


def main():
    config_ok = core.init()
    lock = pu.SingleInstance("watcher")
    if not lock.acquire():
        core.log_message("Auto Sorter is already running; this copy will exit.")
        core.desktop_notify("Auto Sorter",
                            "Auto Sorter is already running. Look for its icon in the system tray.")
        return 0

    welcome = first_run_setup()
    watcher = core.Watcher()
    try:
        run_tray(watcher, welcome, config_ok)
    except Exception as e:  # no tray on this desktop (or no display at all)
        core.log_message(f"⚠️ Tray icon unavailable ({e}); sorting without it.", "error")
        core.NOTIFY_HOOK = None
        if not watcher.running:
            core.desktop_notify("Auto Sorter", CONFIG_ERROR if not config_ok else
                                WELCOME if welcome else "Auto Sorter is running.")
        core.serve_forever(watcher)
    return 0


if __name__ == "__main__":
    sys.exit(main())
