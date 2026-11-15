r"""Builds GameShot.exe (single file, no console window, requests Administrator on launch)."""
import base64
import os
import subprocess
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)

# Restore the original app source when the repository is freshly checked out.
source_path = os.path.join(HERE, "GameShot.py")
payload_path = os.path.join(HERE, "GameShot.py.zlib.b64")
if not os.path.exists(source_path):
    with open(payload_path, "r", encoding="ascii") as stream:
        source = zlib.decompress(base64.b64decode(stream.read())).decode("utf-8")
    with open(source_path, "w", encoding="utf-8", newline="") as stream:
        stream.write(source)

if not os.path.exists("icon.ico"):
    subprocess.run([sys.executable, "make_icon.py"], check=True)

cmd = [
    sys.executable, "-m", "PyInstaller",
    "--noconfirm", "--clean", "--onefile", "--noconsole", "--uac-admin",
    "--name", "GameShot", "--icon", "icon.ico",
    "--collect-all", "dxcam",
    "--collect-all", "comtypes",
    "--collect-all", "pystray",
    "--collect-all", "numpy",
    "--collect-all", "PIL",
    "--hidden-import", "pystray._win32",
    "--hidden-import", "keyboard._winkeyboard",
    "--hidden-import", "PIL.ImageTk",
    "GameShot.py",
]
print(" ".join(cmd))
subprocess.run(cmd, check=True)
print("\nDone -> dist\\GameShot.exe")
