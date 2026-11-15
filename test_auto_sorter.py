"""Self-checks. Run: python test_auto_sorter.py   (pytest also works)

Everything runs inside a temporary AUTO_SORTER_HOME, and every test that sorts
files points the destinations at a temp folder, so your real settings, logs and
folders are never touched.
"""
import contextlib
import copy
import os
import pathlib
import tempfile
import time

os.environ["AUTO_SORTER_HOME"] = tempfile.mkdtemp(prefix="auto-sorter-test-")

import auto_sorter as mod  # noqa: E402
import platform_utils as pu  # noqa: E402

mod.init()


@contextlib.contextmanager
def isolated():
    """Temp dir + settings restored afterwards. Destinations point inside the temp dir."""
    saved = {name: copy.deepcopy(getattr(mod, name))
             for name in ("DESTINATIONS", "EXTENSIONS", "SETTINGS", "NOTIFICATIONS")}
    real_complete = mod.is_file_complete
    with tempfile.TemporaryDirectory() as d:
        for cat in list(mod.DESTINATIONS):
            mod.DESTINATIONS[cat] = os.path.join(d, cat.lower())
        mod.SETTINGS["watch_folders"] = [os.path.join(d, "downloads")]
        os.makedirs(mod.SETTINGS["watch_folders"][0])
        mod.NOTIFICATIONS["enabled"] = False
        mod.is_file_complete = lambda *a, **k: True  # skip the 3s stability wait
        try:
            yield d
        finally:
            mod.is_file_complete = real_complete
            for name, value in saved.items():
                target = getattr(mod, name)
                target.clear()
                target.update(value)


def test_safe_move_new_destination():
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "a.txt")
        out_dir = os.path.join(d, "out")
        pathlib.Path(src).write_text("hello")
        out = mod.safe_move(src, out_dir, "rename")  # also creates out_dir
        assert out == os.path.join(out_dir, "a.txt")
        assert not os.path.exists(src)


def test_safe_move_renames_on_name_clash_different_content():
    with tempfile.TemporaryDirectory() as d:
        out_dir = os.path.join(d, "out")
        os.makedirs(out_dir)
        pathlib.Path(os.path.join(out_dir, "a.txt")).write_text("existing")
        src = os.path.join(d, "a.txt")
        pathlib.Path(src).write_text("different")
        out = mod.safe_move(src, out_dir, "rename")
        assert out == os.path.join(out_dir, "a(1).txt")


def test_safe_move_drops_identical_content():
    with tempfile.TemporaryDirectory() as d:
        out_dir = os.path.join(d, "out")
        os.makedirs(out_dir)
        pathlib.Path(os.path.join(out_dir, "a.txt")).write_text("same")
        src = os.path.join(d, "a.txt")
        pathlib.Path(src).write_text("same")
        out = mod.safe_move(src, out_dir, "rename")
        assert out is None
        assert not os.path.exists(src)


def test_safe_move_replace_overwrites():
    with tempfile.TemporaryDirectory() as d:
        out_dir = os.path.join(d, "out")
        os.makedirs(out_dir)
        pathlib.Path(os.path.join(out_dir, "a.txt")).write_text("old")
        src = os.path.join(d, "a.txt")
        pathlib.Path(src).write_text("new!")
        out = mod.safe_move(src, out_dir, "replace")
        assert pathlib.Path(out).read_text() == "new!"


def test_extension_categorization():
    def categorize(name):
        e = pathlib.Path(name).suffix.lstrip(".").lower()
        return mod.ext_category(e)

    assert categorize("report.pdf") == "Documents"
    assert categorize("clip.mp4") == "Videos"
    assert categorize("archive.tar.gz") == "Archives"
    assert categorize(".bashrc") is None
    assert categorize("noext") is None


def test_deep_merge_fills_missing_keys():
    base = {"a": 1, "s": {"x": 1, "y": 2}}
    override = {"s": {"y": 99}}
    assert mod._deep_merge(base, override) == {"a": 1, "s": {"x": 1, "y": 99}}


def test_should_exclude_file():
    assert mod.should_exclude_file("Thumbs.db")
    assert mod.should_exclude_file(".DS_Store")
    assert not mod.should_exclude_file("report.pdf")


def test_exclude_patterns_are_wildcards_not_substrings():
    with isolated():
        mod.SETTINGS["exclude_patterns"] = ["tmp", "*.ini"]
        assert mod.should_exclude_file("Desktop.INI")
        assert not mod.should_exclude_file("attempt.pdf")


def test_match_name_rule():
    mod.SETTINGS["name_rules"] = [{"match": "*invoice*", "dest": "Documents/Invoices"}]
    try:
        assert mod.match_name_rule("ACME-Invoice-2024.pdf") == "Documents/Invoices"
        assert mod.match_name_rule("holiday.jpg") is None
    finally:
        mod.SETTINGS["name_rules"] = []


def test_date_path_uses_file_mtime():
    with isolated() as d:
        mod.SETTINGS["organize_by_date"] = True
        mod.SETTINGS["date_format"] = "%Y-%m"
        f = os.path.join(d, "x.pdf")
        pathlib.Path(f).write_text("x")
        old = time.mktime((2020, 3, 15, 12, 0, 0, 0, 0, -1))
        os.utime(f, (old, old))
        folder = mod.get_destination_path("Documents", f)
        assert folder.endswith(os.path.join("documents", "2020-03")), folder


def test_sort_file_end_to_end():
    with isolated() as d:
        src = os.path.join(mod.watch_folders()[0], "report.pdf")
        pathlib.Path(src).write_text("pdf bytes")
        mod.EnhancedDownloadSorter().sort_file(src)
        dest = os.path.join(d, "documents", "report.pdf")
        assert os.path.exists(dest)
        assert mod.read_ledger()[-1]["dest"] == dest


def test_firefox_placeholder_left_alone():
    with isolated() as d:
        src = os.path.join(mod.watch_folders()[0], "report.pdf")
        pathlib.Path(src).write_text("")                 # Firefox's empty placeholder
        pathlib.Path(src + ".part").write_text("data")  # the real download in progress
        mod.EnhancedDownloadSorter().sort_file(src)
        assert os.path.exists(src)
        assert not os.path.exists(os.path.join(d, "documents"))


def test_dry_run_creates_no_folders():
    with isolated() as d:
        mod.SETTINGS["dry_run"] = True
        src = os.path.join(mod.watch_folders()[0], "song.mp3")
        pathlib.Path(src).write_text("mp3")
        mod.EnhancedDownloadSorter().sort_file(src)
        assert os.path.exists(src)
        assert not os.path.exists(os.path.join(d, "music"))


def test_ledger_record_find_and_undo():
    with tempfile.TemporaryDirectory() as d:
        saved_log = mod.MOVE_LOG
        mod.MOVE_LOG = os.path.join(d, "moves.jsonl")
        try:
            src = os.path.join(d, "orig", "a.txt")
            dest = os.path.join(d, "sorted", "a.txt")
            os.makedirs(os.path.dirname(src))
            os.makedirs(os.path.dirname(dest))
            pathlib.Path(dest).write_text("data")  # simulate the file already moved
            mod.record_move(src, dest)
            assert len(mod.find_in_ledger("a.txt")) == 1
            mod.undo_last(1)
            assert os.path.exists(src)
            assert not os.path.exists(dest)
            assert mod.read_ledger() == []
        finally:
            mod.MOVE_LOG = saved_log


def test_undo_never_overwrites():
    with tempfile.TemporaryDirectory() as d:
        saved_log = mod.MOVE_LOG
        mod.MOVE_LOG = os.path.join(d, "moves.jsonl")
        try:
            src = os.path.join(d, "a.txt")
            pathlib.Path(src).write_text("new file with the same name")
            pathlib.Path(os.path.join(d, "a(restored).txt")).write_text("earlier restore")
            dest = os.path.join(d, "sorted.txt")
            pathlib.Path(dest).write_text("moved")
            mod.record_move(src, dest)
            mod.undo_last(1)
            assert pathlib.Path(os.path.join(d, "a(restored 2).txt")).read_text() == "moved"
            assert pathlib.Path(os.path.join(d, "a(restored).txt")).read_text() == "earlier restore"
        finally:
            mod.MOVE_LOG = saved_log


def test_broken_config_is_not_overwritten():
    with open(mod.CONFIG_PATH, encoding="utf-8") as f:
        good = f.read()
    try:
        broken = '{"settings": {"watch_folders": ["~/Downloads"],}'  # trailing comma
        with open(mod.CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write(broken)
        assert mod.init() is False
        with open(mod.CONFIG_PATH, encoding="utf-8") as f:
            assert f.read() == broken
        assert mod.DESTINATIONS  # defaults still loaded
    finally:
        with open(mod.CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write(good)
        assert mod.init() is True


def test_config_with_notepad_bom_loads():
    with open(mod.CONFIG_PATH, encoding="utf-8") as f:
        good = f.read()
    try:
        with open(mod.CONFIG_PATH, "w", encoding="utf-8-sig") as f:
            f.write(good)
        assert mod.init() is True
    finally:
        with open(mod.CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write(good)


def test_desktop_exec_quoting():
    assert pu._desktop_quote("/usr/bin/AutoSorter") == "/usr/bin/AutoSorter"
    assert pu._desktop_quote("/home/me/My Apps/AutoSorter") == '"/home/me/My Apps/AutoSorter"'
    assert pu._desktop_quote("/a/100%") == "/a/100%%"


def test_single_instance_lock():
    first = pu.SingleInstance("test-lock")
    assert first.acquire()
    if not pu.IS_WINDOWS:  # flock is per open file, so a second open in-process conflicts
        assert not pu.SingleInstance("test-lock").acquire()


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok  {_name}")
    print("all passed")
