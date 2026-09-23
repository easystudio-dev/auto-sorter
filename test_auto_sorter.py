"""Minimal self-checks. Run: .env/bin/python test_auto_sorter.py

Note: importing the module writes ~/.config/download_sorter.json because config
setup runs at import time. No files are moved by import.
"""
import importlib.util
import os
import pathlib
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "auto_sorter", os.path.join(_HERE, "auto-sorter.py")
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def test_safe_move_new_destination():
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "a.txt")
        out_dir = os.path.join(d, "out")
        os.makedirs(out_dir)
        pathlib.Path(src).write_text("hello")
        out = mod.safe_move(src, out_dir, "rename")
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


def test_match_name_rule():
    mod.SETTINGS["name_rules"] = [{"match": "*invoice*", "dest": "Documents/Invoices"}]
    try:
        assert mod.match_name_rule("ACME-Invoice-2024.pdf") == "Documents/Invoices"
        assert mod.match_name_rule("holiday.jpg") is None
    finally:
        mod.SETTINGS["name_rules"] = []


def test_date_path_uses_file_mtime():
    with tempfile.TemporaryDirectory() as d:
        saved = dict(mod.SETTINGS)
        mod.DESTINATIONS["Documents"] = os.path.join(d, "docs")
        mod.SETTINGS["organize_by_date"] = True
        mod.SETTINGS["date_format"] = "%Y-%m"
        try:
            f = os.path.join(d, "x.pdf")
            pathlib.Path(f).write_text("x")
            old = time.mktime((2020, 3, 15, 12, 0, 0, 0, 0, -1))
            os.utime(f, (old, old))
            folder = mod.get_destination_path("Documents", f)
            assert folder.endswith(os.path.join("docs", "2020-03")), folder
        finally:
            mod.SETTINGS.clear()
            mod.SETTINGS.update(saved)


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


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok  {_name}")
    print("all passed")
