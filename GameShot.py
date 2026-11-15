"""
GameShot - Steam-style game screenshot tool for Windows
- Auto-elevates to Administrator (so hotkeys work inside elevated games)
- GPU-level capture (Desktop Duplication) that works in DX/Vulkan/OpenGL games
- Full settings UI, system tray, start-with-Windows, toast notifications
"""

import base64
import ctypes
import datetime
import io
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

APP_NAME = "GameShot"
APP_VERSION = "1.0"

# ------------------------------------------------------------------ #
#  Windows plumbing: DPI, admin elevation, single instance
# ------------------------------------------------------------------ #
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin():
    if getattr(sys, "frozen", False):
        exe, params = sys.executable, subprocess.list2cmdline(sys.argv[1:])
    else:
        exe = sys.executable
        params = subprocess.list2cmdline([os.path.abspath(sys.argv[0])] + sys.argv[1:])
    ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
    if ret <= 32:
        _r = tk.Tk()
        _r.withdraw()
        messagebox.showerror(APP_NAME, "GameShot needs Administrator rights to capture "
                                       "reliably inside games.")
    sys.exit(0)


_k32 = ctypes.WinDLL("kernel32", use_last_error=True)


_k32.CreateMutexW.restype = ctypes.c_void_p
_k32.CreateEventW.restype = ctypes.c_void_p
_k32.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p]
_k32.OpenEventW.restype = ctypes.c_void_p
_k32.OpenEventW.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_wchar_p]
_k32.SetEvent.argtypes = [ctypes.c_void_p]
_k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
SHOW_EVENT = "Global\\GameShotShowEvent"
_mutex_handle = None


def already_running() -> bool:
    global _mutex_handle
    _mutex_handle = _k32.CreateMutexW(None, False, "Global\\GameShotSingletonMutex")
    return ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS


def signal_running_instance():
    """Second launch: ask the running copy to show its window."""
    h = _k32.OpenEventW(0x0002, False, SHOW_EVENT)  # EVENT_MODIFY_STATE
    if h:
        _k32.SetEvent(h)


def listen_for_show(callback):
    h = _k32.CreateEventW(None, False, False, SHOW_EVENT)

    def loop():
        while True:
            if _k32.WaitForSingleObject(h, 0xFFFFFFFF) == 0:
                callback()

    threading.Thread(target=loop, daemon=True).start()


# ------------------------------------------------------------------ #
#  Win32 helpers: windows, monitors, clipboard
# ------------------------------------------------------------------ #
class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", RECT),
                ("rcWork", RECT), ("dwFlags", ctypes.c_ulong)]


user32.GetForegroundWindow.restype = ctypes.c_void_p
user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(RECT)]
user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
user32.MonitorFromWindow.restype = ctypes.c_void_p
user32.MonitorFromWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MONITORINFO)]
user32.GetParent.restype = ctypes.c_void_p
user32.GetParent.argtypes = [ctypes.c_void_p]
user32.GetWindowLongW.restype = ctypes.c_long
user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
user32.SetWindowLongW.restype = ctypes.c_long
user32.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]


def window_title(hwnd) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    t = buf.value.strip() or "Desktop"
    t = "".join(c for c in t if c not in '<>:"/\\|?*').strip()[:60]
    return t or "Desktop"


def list_monitors():
    """Returns [(hmonitor, (l, t, r, b), is_primary), ...]"""
    result = []
    Proc = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                              ctypes.POINTER(RECT), ctypes.c_void_p)

    def cb(hmon, _dc, _rc, _d):
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
        r = mi.rcMonitor
        result.append((hmon, (r.left, r.top, r.right, r.bottom), bool(mi.dwFlags & 1)))
        return 1

    user32.EnumDisplayMonitors(None, None, Proc(cb), 0)
    return result


DESKTOP = "Desktop"

SHELL_CLASSES = {"progman", "workerw", "shell_traywnd", "shell_secondarytraywnd",
                 "windows.ui.core.corewindow"}

# Programs that are never treated as games (unless you add them yourself in the Games tab)
NON_GAME_EXES = frozenset({
    "explorer.exe", "applicationframehost.exe", "shellexperiencehost.exe",
    "startmenuexperiencehost.exe", "searchhost.exe", "searchapp.exe", "textinputhost.exe",
    "systemsettings.exe", "taskmgr.exe", "chrome.exe", "msedge.exe", "firefox.exe",
    "brave.exe", "opera.exe", "vivaldi.exe", "iexplore.exe", "discord.exe", "slack.exe",
    "teams.exe", "ms-teams.exe", "zoom.exe", "spotify.exe", "vlc.exe", "mpc-hc.exe",
    "mpc-hc64.exe", "mpc-be.exe", "mpc-be64.exe", "potplayermini64.exe", "wmplayer.exe",
    "obs64.exe", "obs32.exe", "notepad.exe", "notepad++.exe", "code.exe", "devenv.exe",
    "cmd.exe", "powershell.exe", "pwsh.exe", "windowsterminal.exe", "conhost.exe",
    "python.exe", "pythonw.exe", "gameshot.exe", "steam.exe", "steamwebhelper.exe",
    "epicgameslauncher.exe", "galaxyclient.exe", "battle.net.exe", "origin.exe",
    "eadesktop.exe", "ubisoftconnect.exe", "upc.exe", "riotclientservices.exe",
    "riotclientux.exe", "winword.exe", "excel.exe", "powerpnt.exe", "outlook.exe",
    "onenote.exe", "mspaint.exe", "photoshop.exe",
})

# Folders that mean "this exe lives inside a game library"
LIBRARY_HINTS = (
    "\\steamapps\\common\\", "\\epic games\\", "\\gog galaxy\\games\\", "\\gog games\\",
    "\\riot games\\", "\\ubisoft game launcher\\games\\", "\\origin games\\", "\\ea games\\",
    "\\electronic arts\\", "\\xboxgames\\", "\\rockstar games\\", "\\itch\\apps\\", "\\games\\",
)
NOT_GAME_FOLDERS = {"launcher", "riot client", "epic online services", "directxredist",
                    "redist", "_commonredist", "installer"}

user32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
kernel32.OpenProcess.restype = ctypes.c_void_p
kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
kernel32.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                                ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong)]
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]


def window_class(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def window_process(hwnd):
    """(pid, full exe path) of the process that owns the window; path may be ''."""
    pid = ctypes.c_ulong(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    path = ""
    h = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if h:
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_ulong(1024)
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                path = buf.value
        finally:
            kernel32.CloseHandle(h)
    return pid.value, path


def is_borderless_fullscreen(hwnd) -> bool:
    """Window covers its whole monitor and has no title bar (typical for games)."""
    r = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return False
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    if not user32.GetMonitorInfoW(user32.MonitorFromWindow(hwnd, 2), ctypes.byref(mi)):
        return False
    m = mi.rcMonitor
    covers = (r.left <= m.left and r.top <= m.top and r.right >= m.right and r.bottom >= m.bottom)
    has_caption = (user32.GetWindowLongW(hwnd, -16) & 0xC00000) == 0xC00000  # WS_CAPTION
    return covers and not has_caption


def library_folder(path: str) -> str:
    """'D:\\SteamLibrary\\steamapps\\common\\Hades\\Hades.exe' -> 'Hades'"""
    norm = path.replace("/", "\\")
    low = norm.lower()
    for hint in LIBRARY_HINTS:
        i = low.find(hint)
        if i >= 0:
            parts = [x for x in norm[i + len(hint):].split("\\") if x]
            if len(parts) >= 2 and parts[0].lower() not in NOT_GAME_FOLDERS:
                return parts[0]
    return ""


def clean_game_name(text: str) -> str:
    text = "".join(ch for ch in (text or "") if ch not in '™®©<>:"/\\|?*' and ch.isprintable())
    text = " ".join(text.split()).strip(" .")
    return text[:60].strip(" .")


def show_without_focus(win):
    """Show a Tk window topmost, click-through and WITHOUT stealing focus from the game."""
    win.update_idletasks()
    hwnd = user32.GetParent(win.winfo_id()) or win.winfo_id()
    ex = user32.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
    # WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TRANSPARENT
    user32.SetWindowLongW(hwnd, -20, ex | 0x08000000 | 0x00000080 | 0x00000020)
    # HWND_TOPMOST, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW
    user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010 | 0x0040)
    user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE


def copy_image_to_clipboard(img):
    out = io.BytesIO()
    img.convert("RGB").save(out, "BMP")
    data = out.getvalue()[14:]  # strip BMP file header -> CF_DIB
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    if not user32.OpenClipboard(None):
        return
    try:
        user32.EmptyClipboard()
        h = kernel32.GlobalAlloc(0x0042, len(data))  # GMEM_MOVEABLE | GMEM_ZEROINIT
        p = kernel32.GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        kernel32.GlobalUnlock(h)
        user32.SetClipboardData(8, h)  # CF_DIB
    finally:
        user32.CloseClipboard()


# ------------------------------------------------------------------ #
#  Settings
# ------------------------------------------------------------------ #
SETTINGS_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
SETTINGS_FILE = os.path.join(SETTINGS_DIR, "settings.json")

DEFAULTS = {
    "hotkey": "f12",
    "save_folder": os.path.join(os.path.expanduser("~"), "Pictures", "GameScreenshots"),
    "image_format": "png",
    "jpg_quality": 95,
    "filename_template": "{game}_{date}_{time}",
    "subfolder_by_game": True,
    "auto_detect_games": True,
    "monitor": "auto",
    "capture_area": "monitor",     # "monitor" or "window"
    "delay": 0,
    "play_sound": True,
    "show_toast": True,
    "toast_position": "Top right",
    "copy_to_clipboard": False,
    "start_with_windows": False,
    "start_minimized": False,
    "minimize_to_tray": True,
    "theme": "Dark",
}


MODIFIER_KEYS = {"ctrl", "left ctrl", "right ctrl", "shift", "left shift", "right shift",
                 "alt", "left alt", "right alt", "alt gr", "windows", "left windows",
                 "right windows"}


def load_settings():
    cfg = dict(DEFAULTS)
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in data.items():
            if k not in DEFAULTS:
                continue
            if k == "monitor":
                if v == "auto" or (type(v) is int and v >= 0):
                    cfg[k] = v
            elif type(v) is type(DEFAULTS[k]):
                cfg[k] = v
    except Exception:
        pass
    if cfg["image_format"] not in ("png", "jpg", "webp"):
        cfg["image_format"] = DEFAULTS["image_format"]
    if cfg["capture_area"] not in ("monitor", "window"):
        cfg["capture_area"] = DEFAULTS["capture_area"]
    if cfg["theme"] not in ("Dark", "Light"):
        cfg["theme"] = DEFAULTS["theme"]
    if cfg["toast_position"] not in ("Top left", "Top right", "Bottom left", "Bottom right"):
        cfg["toast_position"] = DEFAULTS["toast_position"]
    return cfg


def save_settings(cfg):
    try:
        os.makedirs(SETTINGS_DIR, exist_ok=True)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        print("Could not save settings:", e)


# ------------------------------------------------------------------ #
#  Remembered games (games.json)
# ------------------------------------------------------------------ #
GAMES_FILE = os.path.join(SETTINGS_DIR, "games.json")


def load_games():
    data = {"games": {}, "ignored": []}
    try:
        with open(GAMES_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        for exe, g in raw.get("games", {}).items():
            if isinstance(g, dict) and isinstance(g.get("name"), str) and g["name"].strip():
                cnt = g.get("count", 0)
                data["games"][str(exe).lower()] = {
                    "name": g["name"], "path": str(g.get("path", "")),
                    "count": cnt if type(cnt) is int else 0, "last": str(g.get("last", ""))}
        data["ignored"] = [x.lower() for x in raw.get("ignored", []) if isinstance(x, str)]
    except Exception:
        pass
    return data


def save_games(data):
    try:
        os.makedirs(SETTINGS_DIR, exist_ok=True)
        with open(GAMES_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print("Could not save games:", e)


# ------------------------------------------------------------------ #
#  Autostart via Task Scheduler (runs elevated, no UAC prompt at login)
# ------------------------------------------------------------------ #
def set_autostart(enabled: bool) -> bool:
    """Task Scheduler entry that runs elevated at logon, even on battery, with no time limit."""
    if enabled:
        if getattr(sys, "frozen", False):
            exe, args = sys.executable, "--minimized"
            workdir = os.path.dirname(exe)
        else:
            exe = sys.executable.replace("python.exe", "pythonw.exe")
            script = os.path.abspath(sys.argv[0])
            args = f'"{script}" --minimized'
            workdir = os.path.dirname(script)

        def esc(x):
            return x.replace("'", "''")

        ps = (
            "$u = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name;"
            f"$a = New-ScheduledTaskAction -Execute '{esc(exe)}' -Argument '{esc(args)}' "
            f"-WorkingDirectory '{esc(workdir)}';"
            "$t = New-ScheduledTaskTrigger -AtLogOn -User $u;"
            "$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
            "-ExecutionTimeLimit ([TimeSpan]::Zero);"
            "$p = New-ScheduledTaskPrincipal -UserId $u -LogonType Interactive -RunLevel Highest;"
            f"Register-ScheduledTask -TaskName '{APP_NAME}' -Action $a -Trigger $t "
            "-Settings $s -Principal $p -Force | Out-Null")
    else:
        ps = (f"Unregister-ScheduledTask -TaskName '{APP_NAME}' -Confirm:$false "
              "-ErrorAction SilentlyContinue")
    enc = base64.b64encode(ps.encode("utf-16-le")).decode()
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                            "Bypass", "-EncodedCommand", enc],
                           creationflags=0x08000000, capture_output=True)
        return r.returncode == 0
    except Exception:
        return False


# ------------------------------------------------------------------ #
#  Icon
# ------------------------------------------------------------------ #
def make_icon(size=64):
    """App/tray icon: screenshot crop-marks around a camera lens (same art as icon.ico)."""
    from PIL import Image, ImageDraw
    n = 512
    bg = Image.new("RGBA", (n, n))
    d = ImageDraw.Draw(bg)
    top, bottom = (52, 112, 255), (14, 22, 72)
    for y in range(n):
        t = y / (n - 1)
        d.line([(0, y), (n, y)], fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)) + (255,))
    mask = Image.new("L", (n, n), 0)
    m = int(n * 0.02)
    ImageDraw.Draw(mask).rounded_rectangle([m, m, n - m, n - m], radius=int(n * 0.22), fill=255)
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    img.paste(bg, (0, 0), mask)
    d = ImageDraw.Draw(img)
    a, b = int(n * 0.20), int(n * 0.80)
    L, t = int(n * 0.21), int(n * 0.065)
    white = (245, 248, 255, 255)
    for rect in ((a, a, a + L, a + t), (a, a, a + t, a + L), (b - L, a, b, a + t),
                 (b - t, a, b, a + L), (a, b - t, a + L, b), (a, b - L, a + t, b),
                 (b - L, b - t, b, b), (b - t, b - L, b, b)):
        d.rounded_rectangle(rect, radius=t // 2, fill=white)
    c = n // 2
    for rad, col in ((0.19, (70, 225, 255, 255)), (0.12, (12, 20, 58, 255)),
                     (0.075, (40, 120, 255, 255))):
        R = int(n * rad)
        d.ellipse([c - R, c - R, c + R, c + R], fill=col)
    R = int(n * 0.03)
    ox, oy = int(n * 0.03), int(n * 0.035)
    d.ellipse([c - ox - R, c - oy - R, c - ox + R, c - oy + R], fill=(255, 255, 255, 235))
    return img.resize((size, size), Image.LANCZOS)


# ------------------------------------------------------------------ #
#  App
# ------------------------------------------------------------------ #
THEMES = {
    "Dark": dict(bg="#161a23", panel="#1e2430", fg="#e6e9ef", muted="#8b93a7",
                 accent="#4da3ff", entry="#262d3c"),
    "Light": dict(bg="#f2f4f8", panel="#ffffff", fg="#1b2030", muted="#6a7288",
                  accent="#1a73e8", entry="#eef1f6"),
}


class GameShotApp:
    def __init__(self, start_minimized=False):
        self.cfg = load_settings()
        self.q = queue.Queue()
        self.cameras = {}
        self.hotkey_handle = None
        self.recent = []
        self.recording_hotkey = False
        self.tray = None
        self.preview_img = None
        self._lock = threading.Lock()
        self._shot_lock = threading.Lock()
        self._busy = False
        self._last_shot = 0.0
        self.last_frames = {}
        self.games = load_games()
        self.games_lock = threading.Lock()

        self.root = tk.Tk()
        self.root.title(f"{APP_NAME} {APP_VERSION}")
        scale = max(self.root.winfo_fpixels("1i") / 96.0, 1.0)
        max_h = self.root.winfo_screenheight() - 80
        self.root.geometry(f"{int(800 * scale)}x{min(int(720 * scale), max_h)}")
        self.root.minsize(int(760 * scale), min(int(660 * scale), max_h))
        try:
            from PIL import ImageTk
            self._icon_tk = ImageTk.PhotoImage(make_icon(64))
            self.root.iconphoto(True, self._icon_tk)
        except Exception:
            pass
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.style = ttk.Style(self.root)
        self.style.theme_use("clam")
        self.build_ui()
        self.apply_theme()
        self.refresh_games()
        self.register_hotkey()
        self.start_tray()
        listen_for_show(lambda: self.q.put(("show",)))
        self.root.after(100, self.poll_queue)

        if start_minimized and self.tray:
            self.root.withdraw()

    # ---------------------------- UI ---------------------------- #
    def build_ui(self):
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=12, pady=12)
        self.tab_main = ttk.Frame(nb, padding=16)
        self.tab_set = ttk.Frame(nb, padding=16)
        self.tab_games = ttk.Frame(nb, padding=16)
        self.tab_about = ttk.Frame(nb, padding=16)
        nb.add(self.tab_main, text="  Capture  ")
        nb.add(self.tab_games, text="  Games  ")
        nb.add(self.tab_set, text="  Settings  ")
        nb.add(self.tab_about, text="  About  ")
        self.build_main_tab()
        self.build_games_tab()
        self.build_settings_tab()
        self.build_about_tab()

    def build_main_tab(self):
        t = self.tab_main
        self.status_var = tk.StringVar()
        ttk.Label(t, textvariable=self.status_var, style="Title.TLabel").pack(anchor="w")
        self.update_status()

        row = ttk.Frame(t)
        row.pack(fill="x", pady=(12, 8))
        ttk.Button(row, text="Take screenshot now (3s delay)",
                   command=lambda: self.threaded_capture(delay_override=3)).pack(side="left")
        ttk.Button(row, text="Open screenshot folder",
                   command=self.open_folder).pack(side="left", padx=8)

        self.preview = tk.Label(t, text="No screenshot yet", width=48, height=11)
        self.preview.pack(pady=8, fill="x")

        ttk.Label(t, text="Recent screenshots (double-click to open)").pack(anchor="w", pady=(8, 2))
        self.listbox = tk.Listbox(t, height=7, activestyle="none", borderwidth=0,
                                  highlightthickness=0)
        self.listbox.pack(fill="both", expand=True)
        self.listbox.bind("<Double-Button-1>", self.open_selected)

    def build_settings_tab(self):
        t = self.tab_set
        t.columnconfigure(0, weight=1)

        # --- Hotkey
        f = ttk.LabelFrame(t, text="Hotkey", padding=10)
        f.grid(row=0, column=0, sticky="ew", pady=4)
        self.hotkey_var = tk.StringVar(value=self.cfg["hotkey"].upper())
        ttk.Label(f, textvariable=self.hotkey_var, style="Key.TLabel", width=18).grid(row=0, column=0)
        self.hotkey_btn = ttk.Button(f, text="Change...", command=self.record_hotkey)
        self.hotkey_btn.grid(row=0, column=1, padx=8)
        ttk.Label(f, text="Delay (sec):").grid(row=0, column=2, padx=(20, 4))
        self.delay_var = tk.IntVar(value=self.cfg["delay"])
        ttk.Spinbox(f, from_=0, to=30, width=4, textvariable=self.delay_var).grid(row=0, column=3)
        self.bind_var("delay", self.delay_var)

        # --- Saving
        f = ttk.LabelFrame(t, text="Saving", padding=10)
        f.grid(row=1, column=0, sticky="ew", pady=4)
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text="Folder:").grid(row=0, column=0, sticky="w")
        self.folder_var = tk.StringVar(value=self.cfg["save_folder"])
        ttk.Entry(f, textvariable=self.folder_var).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(f, text="Browse", command=self.browse_folder).grid(row=0, column=2)
        self.bind_var("save_folder", self.folder_var)

        ttk.Label(f, text="Format:").grid(row=1, column=0, sticky="w", pady=(8, 0))
        fm = ttk.Frame(f)
        fm.grid(row=1, column=1, sticky="w", padx=6, pady=(8, 0))
        self.format_var = tk.StringVar(value=self.cfg["image_format"])
        ttk.Combobox(fm, textvariable=self.format_var, values=["png", "jpg", "webp"],
                     width=6, state="readonly").pack(side="left")
        self.bind_var("image_format", self.format_var)
        ttk.Label(fm, text="  JPG/WebP quality:").pack(side="left")
        self.quality_var = tk.IntVar(value=self.cfg["jpg_quality"])
        ttk.Spinbox(fm, from_=50, to=100, width=5, textvariable=self.quality_var).pack(side="left")
        self.bind_var("jpg_quality", self.quality_var)

        ttk.Label(f, text="File name:").grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.tpl_var = tk.StringVar(value=self.cfg["filename_template"])
        ttk.Entry(f, textvariable=self.tpl_var).grid(row=2, column=1, sticky="ew", padx=6, pady=(8, 0))
        ttk.Label(f, text="{game} {date} {time}", style="Muted.TLabel").grid(row=2, column=2, pady=(8, 0))
        self.bind_var("filename_template", self.tpl_var)

        self.sub_var = tk.BooleanVar(value=self.cfg["subfolder_by_game"])
        ttk.Checkbutton(f, text="Sort into a subfolder per game", variable=self.sub_var
                        ).grid(row=3, column=1, sticky="w", padx=6, pady=(8, 0))
        self.bind_var("subfolder_by_game", self.sub_var)
        self.detect_var = tk.BooleanVar(value=self.cfg["auto_detect_games"])
        ttk.Checkbutton(f, text="Auto-detect and remember games (everything else = Desktop)",
                        variable=self.detect_var).grid(row=4, column=1, columnspan=2,
                                                       sticky="w", padx=6, pady=(8, 0))
        self.bind_var("auto_detect_games", self.detect_var)

        # --- Capture
        f = ttk.LabelFrame(t, text="Capture", padding=10)
        f.grid(row=2, column=0, sticky="ew", pady=4)
        ttk.Label(f, text="Monitor:").grid(row=0, column=0, sticky="w")
        self.monitors = list_monitors()
        labels = ["Auto (monitor the game is on)"] + [
            f"Monitor {i + 1}: {m[1][2] - m[1][0]}x{m[1][3] - m[1][1]}"
            + (" (primary)" if m[2] else "") for i, m in enumerate(self.monitors)]
        self.mon_box = ttk.Combobox(f, values=labels, state="readonly", width=34)
        cur = self.cfg["monitor"]
        self.mon_box.current(0 if cur == "auto" or int(cur) >= len(self.monitors) else int(cur) + 1)
        self.mon_box.grid(row=0, column=1, sticky="w", padx=6)
        self.mon_box.bind("<<ComboboxSelected>>", self.on_monitor_change)

        ttk.Label(f, text="Area:").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.area_var = tk.StringVar(value="Active window" if self.cfg["capture_area"] == "window"
                                     else "Full monitor")
        a = ttk.Combobox(f, textvariable=self.area_var, values=["Full monitor", "Active window"],
                         state="readonly", width=16)
        a.grid(row=1, column=1, sticky="w", padx=6, pady=(8, 0))
        a.bind("<<ComboboxSelected>>", lambda e: self.set_cfg(
            "capture_area", "window" if self.area_var.get() == "Active window" else "monitor"))

        self.clip_var = tk.BooleanVar(value=self.cfg["copy_to_clipboard"])
        ttk.Checkbutton(f, text="Also copy screenshot to clipboard", variable=self.clip_var
                        ).grid(row=2, column=1, sticky="w", padx=6, pady=(8, 0))
        self.bind_var("copy_to_clipboard", self.clip_var)

        # --- Notifications & behaviour
        f = ttk.LabelFrame(t, text="Notifications & behavior", padding=10)
        f.grid(row=3, column=0, sticky="ew", pady=4)
        self.sound_var = tk.BooleanVar(value=self.cfg["play_sound"])
        ttk.Checkbutton(f, text="Play sound", variable=self.sound_var).grid(row=0, column=0, sticky="w")
        self.bind_var("play_sound", self.sound_var)
        self.toast_var = tk.BooleanVar(value=self.cfg["show_toast"])
        ttk.Checkbutton(f, text="Show popup", variable=self.toast_var).grid(row=0, column=1, sticky="w", padx=12)
        self.bind_var("show_toast", self.toast_var)
        self.pos_var = tk.StringVar(value=self.cfg["toast_position"])
        ttk.Combobox(f, textvariable=self.pos_var, state="readonly", width=14,
                     values=["Top left", "Top right", "Bottom left", "Bottom right"]
                     ).grid(row=0, column=2)
        self.bind_var("toast_position", self.pos_var)

        self.auto_var = tk.BooleanVar(value=self.cfg["start_with_windows"])
        ttk.Checkbutton(f, text="Start with Windows (no UAC prompt)", variable=self.auto_var
                        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.bind_var("start_with_windows", self.auto_var)
        self.min_var = tk.BooleanVar(value=self.cfg["start_minimized"])
        ttk.Checkbutton(f, text="Start minimized to tray", variable=self.min_var
                        ).grid(row=1, column=2, sticky="w", pady=(8, 0))
        self.bind_var("start_minimized", self.min_var)
        self.tray_var = tk.BooleanVar(value=self.cfg["minimize_to_tray"])
        ttk.Checkbutton(f, text="Closing the window keeps it running in the tray",
                        variable=self.tray_var).grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self.bind_var("minimize_to_tray", self.tray_var)

        ttk.Label(f, text="Theme:").grid(row=3, column=0, sticky="w", pady=(8, 0))
        self.theme_var = tk.StringVar(value=self.cfg["theme"])
        ttk.Combobox(f, textvariable=self.theme_var, values=list(THEMES), state="readonly",
                     width=10).grid(row=3, column=1, sticky="w", pady=(8, 0))
        self.bind_var("theme", self.theme_var)

    def build_games_tab(self):
        t = self.tab_games
        ttk.Label(t, text="Remembered games", style="Title.TLabel").pack(anchor="w")
        ttk.Label(t, style="Muted.TLabel", justify="left", text=(
            "Games are detected automatically and remembered by their program, so the folder\n"
            "name never changes. Anything that is not a game is filed under 'Desktop'.")
                  ).pack(anchor="w", pady=(2, 10))
        box = ttk.Frame(t)
        box.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(box, columns=("name", "exe", "shots", "last"),
                                 show="headings", selectmode="browse")
        for col, text, w in (("name", "Game", 230), ("exe", "Program", 170),
                             ("shots", "Shots", 60), ("last", "Last screenshot", 140)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=w, anchor="w" if col in ("name", "exe") else "center")
        sb = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        row = ttk.Frame(t)
        row.pack(fill="x", pady=(10, 0))
        ttk.Button(row, text="Add game...", command=self.add_game).pack(side="left")
        ttk.Button(row, text="Rename", command=self.rename_game).pack(side="left", padx=8)
        ttk.Button(row, text="Open folder", command=self.open_game_folder).pack(side="left")
        ttk.Button(row, text="Forget (never a game)", command=self.forget_game
                   ).pack(side="left", padx=8)

    def refresh_games(self):
        self.tree.delete(*self.tree.get_children())
        with self.games_lock:
            items = sorted(self.games["games"].items(),
                           key=lambda kv: kv[1].get("last", ""), reverse=True)
        for exe, g in items:
            last = g.get("last", "")[:16].replace("T", " ")
            self.tree.insert("", "end", iid=exe, values=(g["name"], exe, g.get("count", 0), last))

    def selected_game(self):
        sel = self.tree.selection()
        return sel[0] if sel else None

    def add_game(self):
        path = filedialog.askopenfilename(title="Pick the game's .exe",
                                          filetypes=[("Programs", "*.exe")])
        if not path:
            return
        exe = os.path.basename(path).lower()
        stem = os.path.splitext(os.path.basename(path))[0]
        name = simpledialog.askstring(APP_NAME, "Name for this game:", parent=self.root,
                                      initialvalue=clean_game_name(library_folder(path) or stem))
        name = clean_game_name(name or "")
        if not name:
            return
        with self.games_lock:
            old = self.games["games"].get(exe, {})
            self.games["games"][exe] = {"name": name, "path": path,
                                        "count": old.get("count", 0), "last": old.get("last", "")}
            self.games["ignored"] = [x for x in self.games["ignored"] if x != exe]
            save_games(self.games)
        self.refresh_games()

    def rename_game(self):
        exe = self.selected_game()
        if not exe:
            return
        name = simpledialog.askstring(APP_NAME, "New name:", parent=self.root,
                                      initialvalue=self.games["games"][exe]["name"])
        name = clean_game_name(name or "")
        if not name:
            return
        with self.games_lock:
            self.games["games"][exe]["name"] = name
            save_games(self.games)
        self.refresh_games()

    def forget_game(self):
        exe = self.selected_game()
        if not exe:
            return
        if not messagebox.askyesno(APP_NAME, f"Forget {exe} and never treat it as a game?\n"
                                             "Its screenshots will be filed under Desktop."):
            return
        with self.games_lock:
            self.games["games"].pop(exe, None)
            if exe not in self.games["ignored"]:
                self.games["ignored"].append(exe)
            save_games(self.games)
        self.refresh_games()

    def open_game_folder(self):
        exe = self.selected_game()
        if not exe:
            return
        folder = os.path.join(self.cfg["save_folder"], self.games["games"][exe]["name"])
        if os.path.isdir(folder):
            os.startfile(folder)
        else:
            self.open_folder()

    def build_about_tab(self):
        t = self.tab_about
        ttk.Label(t, text=f"{APP_NAME} {APP_VERSION}", style="Title.TLabel").pack(anchor="w")
        ttk.Label(t, justify="left", text=(
            "Captures your game with GPU-level screen capture, so it works in\n"
            "DirectX, Vulkan and OpenGL games without injecting into them.\n\n"
            "Tips:\n"
            " - If a game gives black/empty screenshots, use borderless-windowed mode.\n"
            " - Settings are saved automatically.\n"
            f" - Settings file: {SETTINGS_FILE}\n"
            f" - Running as administrator: {'yes' if is_admin() else 'no'}")).pack(anchor="w", pady=10)

    # -------------------------- settings plumbing -------------------------- #
    def bind_var(self, key, var):
        var.trace_add("write", lambda *a: self.on_var(key, var))

    def on_var(self, key, var):
        try:
            self.set_cfg(key, var.get())
        except tk.TclError:
            pass  # half-typed spinbox value

    def set_cfg(self, key, value):
        self.cfg[key] = value
        save_settings(self.cfg)
        if key == "theme":
            self.apply_theme()
        elif key == "start_with_windows":
            def work():
                if not set_autostart(bool(value)):
                    self.q.put(("error", "Could not change the start-with-Windows task."))
            threading.Thread(target=work, daemon=True).start()

    def on_monitor_change(self, _e):
        i = self.mon_box.current()
        self.set_cfg("monitor", "auto" if i == 0 else i - 1)

    def browse_folder(self):
        d = filedialog.askdirectory(initialdir=self.cfg["save_folder"])
        if d:
            self.folder_var.set(os.path.normpath(d))

    def apply_theme(self):
        c = THEMES.get(self.cfg["theme"], THEMES["Dark"])
        s = self.style
        self.root.configure(bg=c["bg"])
        s.configure(".", background=c["bg"], foreground=c["fg"], fieldbackground=c["entry"],
                    bordercolor=c["panel"], lightcolor=c["panel"], darkcolor=c["panel"])
        s.configure("TFrame", background=c["panel"])
        s.configure("TLabel", background=c["panel"], foreground=c["fg"])
        s.configure("Title.TLabel", font=("Segoe UI", 15, "bold"), foreground=c["accent"])
        s.configure("Key.TLabel", font=("Consolas", 14, "bold"), foreground=c["accent"],
                    background=c["entry"], anchor="center", padding=6)
        s.configure("Muted.TLabel", foreground=c["muted"])
        s.configure("TLabelframe", background=c["panel"], bordercolor=c["muted"])
        s.configure("TLabelframe.Label", background=c["panel"], foreground=c["accent"])
        s.configure("TButton", background=c["accent"], foreground="#ffffff", padding=7, borderwidth=0)
        s.map("TButton", background=[("active", c["fg"]), ("pressed", c["muted"])],
              foreground=[("active", c["bg"])])
        s.configure("TCheckbutton", background=c["panel"], foreground=c["fg"])
        s.map("TCheckbutton", background=[("active", c["panel"])])
        s.configure("TNotebook", background=c["bg"], borderwidth=0)
        s.configure("TNotebook.Tab", background=c["bg"], foreground=c["muted"], padding=(10, 6))
        s.map("TNotebook.Tab", background=[("selected", c["panel"])],
              foreground=[("selected", c["accent"])])
        s.configure("TEntry", fieldbackground=c["entry"], foreground=c["fg"])
        s.configure("TCombobox", fieldbackground=c["entry"], foreground=c["fg"],
                    background=c["entry"], arrowcolor=c["fg"])
        s.configure("TSpinbox", fieldbackground=c["entry"], foreground=c["fg"], arrowcolor=c["fg"])
        s.map("TCombobox", fieldbackground=[("readonly", c["entry"])],
              foreground=[("readonly", c["fg"])],
              selectbackground=[("readonly", c["entry"])],
              selectforeground=[("readonly", c["fg"])])
        s.configure("Treeview", background=c["entry"], fieldbackground=c["entry"],
                    foreground=c["fg"], borderwidth=0, rowheight=26)
        s.map("Treeview", background=[("selected", c["accent"])],
              foreground=[("selected", "#ffffff")])
        s.configure("Treeview.Heading", background=c["panel"], foreground=c["accent"],
                    relief="flat")
        self.root.option_add("*TCombobox*Listbox.background", c["entry"])
        self.root.option_add("*TCombobox*Listbox.foreground", c["fg"])
        self.listbox.configure(bg=c["entry"], fg=c["fg"], selectbackground=c["accent"],
                               selectforeground="#ffffff")
        self.preview.configure(bg=c["entry"], fg=c["muted"])

    def update_status(self):
        self.status_var.set(f"Ready - press {self.cfg['hotkey'].upper()} in any game")

    # ---------------------------- hotkey ---------------------------- #
    def register_hotkey(self):
        import keyboard
        self.unregister_hotkey()
        try:
            self.hotkey_handle = keyboard.add_hotkey(self.cfg["hotkey"], self.threaded_capture)
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Could not register hotkey '{self.cfg['hotkey']}': {e}")

    def unregister_hotkey(self):
        import keyboard
        if self.hotkey_handle is not None:
            try:
                keyboard.remove_hotkey(self.hotkey_handle)
            except Exception:
                pass
            self.hotkey_handle = None

    def record_hotkey(self):
        if self.recording_hotkey:
            return
        self.recording_hotkey = True
        self.unregister_hotkey()
        self.hotkey_var.set("Press keys...")
        self.hotkey_btn.state(["disabled"])

        def worker():
            import keyboard
            try:
                hk = keyboard.read_hotkey(suppress=False)
            except Exception:
                hk = None
            self.q.put(("hotkey", hk))

        threading.Thread(target=worker, daemon=True).start()

    # ---------------------------- capture ---------------------------- #
    def get_camera(self, idx):
        import dxcam
        with self._lock:
            cam = self.cameras.get(idx)
            if cam is None:
                try:
                    cam = dxcam.create(output_idx=idx, output_color="RGB")
                except Exception:
                    idx = 0
                    cam = self.cameras.get(0) or dxcam.create(output_idx=0, output_color="RGB")
                self.cameras[idx] = cam
            return idx, cam

    def identify_game(self, hwnd):
        """Returns (name, exe_key, exe_path). Non-games are named 'Desktop' with exe_key None."""
        if not self.cfg["auto_detect_games"]:
            return (window_title(hwnd) if hwnd else DESKTOP), None, ""
        if not hwnd or window_class(hwnd).lower() in SHELL_CLASSES:
            return DESKTOP, None, ""
        pid, path = window_process(hwnd)
        if pid == os.getpid():
            return DESKTOP, None, ""
        title = window_title(hwnd)
        if not path:  # protected process: judge by the window alone
            if is_borderless_fullscreen(hwnd):
                return (clean_game_name(title) or DESKTOP), None, ""
            return DESKTOP, None, ""
        exe = os.path.basename(path).lower()
        with self.games_lock:
            if exe in self.games["ignored"]:
                return DESKTOP, None, ""
            known = self.games["games"].get(exe)
            if known:
                return known["name"], exe, path
        if exe in NON_GAME_EXES:
            return DESKTOP, None, ""
        folder = library_folder(path)
        if not (folder or is_borderless_fullscreen(hwnd)):
            return DESKTOP, None, ""
        stem = os.path.splitext(os.path.basename(path))[0]
        name = (clean_game_name(folder) or clean_game_name(title if title != DESKTOP else "")
                or clean_game_name(stem) or "Game")
        return name, exe, path

    def record_shot(self, exe, name, path):
        """Remember the game (first screenshot) and keep its stats up to date."""
        if not exe:
            return
        with self.games_lock:
            g = self.games["games"].setdefault(
                exe, {"name": name, "path": path, "count": 0, "last": ""})
            g["count"] = int(g.get("count", 0)) + 1
            g["last"] = datetime.datetime.now().isoformat(timespec="seconds")
            if path:
                g["path"] = path
            save_games(self.games)
        self.q.put(("games",))

    def grab_frame(self, idx, cam):
        """Desktop Duplication only delivers a frame when the screen changed, so on a
        static screen grab() returns None. Then the last frame we saw is still an
        accurate picture of the screen."""
        import numpy as np
        for _ in range(40):
            frame = cam.grab()
            if frame is not None:
                frame = np.array(frame)
                self.last_frames[idx] = frame
                return frame
            time.sleep(0.015)
        return self.last_frames.get(idx)

    def stop_cameras(self):
        with self._lock:
            for cam in self.cameras.values():
                try:
                    cam.stop()
                except Exception:
                    pass

    def threaded_capture(self, delay_override=None):
        with self._shot_lock:
            # ignore key auto-repeat and overlapping captures
            if self._busy or time.time() - self._last_shot < 0.5:
                return
            self._busy = True
        threading.Thread(target=self.capture, args=(delay_override,), daemon=True).start()

    def capture(self, delay_override=None):
        from PIL import Image
        try:
            try:
                delay = delay_override if delay_override is not None else int(self.cfg["delay"])
            except (TypeError, ValueError):
                delay = 0
            delay = max(0, min(int(delay), 30))
            if delay:
                self.q.put(("status", f"Capturing in {delay} s..."))
                time.sleep(delay)

            hwnd = user32.GetForegroundWindow()
            game, exe_key, exe_path = self.identify_game(hwnd)
            monitors = list_monitors()

            if self.cfg["monitor"] == "auto":
                target = user32.MonitorFromWindow(hwnd, 2)
                idx = next((i for i, m in enumerate(monitors) if m[0] == target), 0)
            else:
                idx = min(int(self.cfg["monitor"]), max(len(monitors) - 1, 0))

            idx, cam = self.get_camera(idx)
            frame = self.grab_frame(idx, cam)
            if frame is None:
                self.q.put(("error", "No frame received. Try borderless-windowed mode."))
                return

            img = Image.fromarray(frame)

            if self.cfg["capture_area"] == "window" and hwnd and idx < len(monitors):
                r = RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(r))
                ml, mt = monitors[idx][1][0], monitors[idx][1][1]
                box = (max(r.left - ml, 0), max(r.top - mt, 0),
                       min(r.right - ml, img.width), min(r.bottom - mt, img.height))
                if box[2] - box[0] > 50 and box[3] - box[1] > 50:
                    img = img.crop(box)

            now = datetime.datetime.now()
            try:
                name = self.cfg["filename_template"].format(
                    game=game.replace(" ", "_"), date=now.strftime("%Y-%m-%d"),
                    time=now.strftime("%H-%M-%S"))
            except Exception:
                name = ""
            name = "".join(ch for ch in name if ch not in '<>:"/\\|?*').strip()
            if not name:
                name = now.strftime("%Y-%m-%d_%H-%M-%S")
            fmt = self.cfg["image_format"]
            folder = self.cfg["save_folder"]
            if self.cfg["subfolder_by_game"]:
                folder = os.path.join(folder, game)
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, f"{name}.{fmt}")
            n = 1
            while os.path.exists(path):
                path = os.path.join(folder, f"{name}_{n}.{fmt}")
                n += 1

            if fmt in ("jpg", "webp"):
                img.save(path, quality=max(1, min(int(self.cfg["jpg_quality"]), 100)))
            else:
                img.save(path)

            if self.cfg["copy_to_clipboard"]:
                copy_image_to_clipboard(img)
            if self.cfg["play_sound"]:
                import winsound
                winsound.MessageBeep(winsound.MB_OK)

            thumb = img.copy()
            thumb.thumbnail((360, 200))
            self.record_shot(exe_key, game, exe_path)
            self.q.put(("saved", path, thumb, game))
        except Exception as e:
            self.q.put(("error", f"Screenshot failed: {e}"))
        finally:
            self._last_shot = time.time()
            self._busy = False

    # ---------------------------- UI events ---------------------------- #
    def poll_queue(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "saved":
                    self.on_saved(msg[1], msg[2], msg[3] if len(msg) > 3 else DESKTOP)
                elif kind in ("error", "status"):
                    self.status_var.set(msg[1])
                elif kind == "hotkey":
                    self.on_hotkey_recorded(msg[1])
                elif kind == "games":
                    self.refresh_games()
                elif kind == "show":
                    self.show_window()
                elif kind == "shot":
                    self.threaded_capture()
                elif kind == "folder":
                    self.open_folder()
                elif kind == "quit":
                    self.quit()
                    return
        except queue.Empty:
            pass
        self.root.after(100, self.poll_queue)

    def on_hotkey_recorded(self, hk):
        self.recording_hotkey = False
        self.hotkey_btn.state(["!disabled"])
        note = None
        if hk and hk != "esc":
            parts = [x.strip() for x in hk.split("+")]
            if all(x in MODIFIER_KEYS for x in parts):
                note = "Hotkey needs a normal key too, not only Ctrl/Shift/Alt/Win."
            else:
                self.cfg["hotkey"] = hk
                save_settings(self.cfg)
        self.hotkey_var.set(self.cfg["hotkey"].upper())
        self.update_status()
        if note:
            self.status_var.set(note)
        self.register_hotkey()

    def on_saved(self, path, thumb, game=DESKTOP):
        from PIL import ImageTk
        self.status_var.set(f"Saved [{game}]: {os.path.basename(path)}")
        self.recent.insert(0, path)
        self.recent = self.recent[:30]
        self.listbox.delete(0, "end")
        for p in self.recent:
            self.listbox.insert("end", p)
        self.preview_img = ImageTk.PhotoImage(thumb)
        self.preview.configure(image=self.preview_img, text="", width=0, height=0)
        if self.cfg["show_toast"]:
            self.show_toast(path, thumb, game)

    def show_toast(self, path, thumb, game=DESKTOP):
        from PIL import ImageTk
        c = THEMES.get(self.cfg["theme"], THEMES["Dark"])
        top = tk.Toplevel(self.root)
        top.withdraw()
        top.overrideredirect(True)
        top.configure(bg=c["accent"])
        inner = tk.Frame(top, bg=c["panel"])
        inner.pack(padx=2, pady=2)
        small = thumb.copy()
        small.thumbnail((160, 90))
        top._img = ImageTk.PhotoImage(small)
        tk.Label(inner, image=top._img, bg=c["panel"]).pack(side="left", padx=8, pady=8)
        tk.Label(inner, text=f"Screenshot saved\n{game}", justify="left", fg=c["fg"], bg=c["panel"],
                 font=("Segoe UI", 11, "bold")).pack(side="left", padx=(0, 16))
        top.update_idletasks()
        w, h = top.winfo_reqwidth(), top.winfo_reqheight()
        sw, sh = top.winfo_screenwidth(), top.winfo_screenheight()
        pos = self.cfg["toast_position"]
        x = 20 if "left" in pos else sw - w - 20
        y = 20 if "Top" in pos else sh - h - 60
        top.geometry(f"{w}x{h}+{x}+{y}")
        top.attributes("-alpha", 0.96)
        show_without_focus(top)
        top.after(2200, top.destroy)

    def open_folder(self):
        os.makedirs(self.cfg["save_folder"], exist_ok=True)
        os.startfile(self.cfg["save_folder"])

    def open_selected(self, _e):
        sel = self.listbox.curselection()
        if sel:
            os.startfile(self.recent[sel[0]])

    # ---------------------------- tray / lifecycle ---------------------------- #
    def start_tray(self):
        try:
            import pystray
        except ImportError:
            return
        menu = pystray.Menu(
            pystray.MenuItem("Open GameShot", lambda *a: self.q.put(("show",)), default=True),
            pystray.MenuItem("Take screenshot", lambda *a: self.q.put(("shot",))),
            pystray.MenuItem("Open screenshot folder", lambda *a: self.q.put(("folder",))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", lambda *a: self.q.put(("quit",))))
        self.tray = pystray.Icon(APP_NAME, make_icon(64), APP_NAME, menu)
        self.tray.run_detached()

    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def on_close(self):
        if self.cfg["minimize_to_tray"] and self.tray:
            self.root.withdraw()
        else:
            self.quit()

    def quit(self):
        self.unregister_hotkey()
        self.stop_cameras()
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    if sys.platform != "win32":
        sys.exit("GameShot is Windows-only.")
    if not is_admin():
        relaunch_as_admin()
    if already_running():
        if "--minimized" not in sys.argv:
            signal_running_instance()
        sys.exit(0)
    GameShotApp(start_minimized="--minimized" in sys.argv).run()


if __name__ == "__main__":
    main()
