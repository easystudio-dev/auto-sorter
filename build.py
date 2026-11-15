"""Build the standalone tray app for the OS you run this on.

    pip install -r requirements.txt pyinstaller
    python build.py

Output: dist/AutoSorter.exe on Windows, dist/AutoSorter on Linux.
PyInstaller cannot cross-compile, so build each OS on that OS (CI does both).
"""
import os
import subprocess
import sys

import app

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    build_dir = os.path.join(HERE, "build")
    os.makedirs(build_dir, exist_ok=True)
    icon_path = os.path.join(build_dir, "icon.ico")
    app.make_icon(256).save(icon_path, sizes=[(16, 16), (32, 32), (48, 48), (64, 64),
                                              (128, 128), (256, 256)])

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile", "--windowed",
        "--name", "AutoSorter",
        "--icon", icon_path,
        "--specpath", build_dir,
    ]
    # pystray picks its backend at runtime, so PyInstaller cannot see it.
    if sys.platform == "win32":
        cmd += ["--hidden-import", "pystray._win32"]
    else:
        cmd += ["--hidden-import", "pystray._xorg", "--collect-submodules", "Xlib"]
    cmd.append(os.path.join(HERE, "app.py"))

    subprocess.run(cmd, check=True, cwd=HERE)
    print("\nBuilt:", os.path.join(HERE, "dist", "AutoSorter.exe" if sys.platform == "win32" else "AutoSorter"))


if __name__ == "__main__":
    main()
