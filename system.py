"""Системные мелочи: значок, автозапуск через Планировщик, журнал, одна копия программы."""
import ctypes
import logging
import os
import socket
import sys
import threading

from PIL import Image, ImageDraw

from lamp import CONFIG_DIR

APP_NAME = "LotusLanternPC"
INSTANCE_PORT = 47321
log = logging.getLogger(APP_NAME)


def make_icon(rgb, on=True, connected=True, size=64):
    s = size * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    fill = tuple(rgb) if on else (70, 72, 82)
    d.ellipse((s * .06, s * .06, s * .94, s * .94), fill=fill + (255,), outline=(255, 255, 255, 235), width=int(s * .08))
    if not connected:
        d.ellipse((s * .58, s * .58, s * .98, s * .98), fill=(255, 70, 75, 255), outline=(16, 17, 22, 255), width=int(s * .05))
    return img.resize((size, size), Image.LANCZOS)


# ---------------------------------------------------------------- autostart / single instance

# Автозапуск — задача Планировщика «при входе в систему» с задержкой: ключ Run запускал программу
# слишком рано (до панели задач и Bluetooth), и она не появлялась в трее.
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
TASK_NAME = APP_NAME
_autostart_cache = None


def _launch_target():
    if getattr(sys, "frozen", False):
        return sys.executable, "--tray"
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return pyw, f'"{os.path.abspath(sys.argv[0])}" --tray'


def _schtasks(*args):
    import subprocess
    return subprocess.run(["schtasks", *args], capture_output=True, text=True, creationflags=0x08000000)


def _remove_run_key():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, APP_NAME)
    except OSError:
        pass


def autostart_enabled():
    global _autostart_cache
    if _autostart_cache is None:
        _autostart_cache = _schtasks("/query", "/tn", TASK_NAME).returncode == 0
    return _autostart_cache


def set_autostart(enable):
    global _autostart_cache
    from xml.sax.saxutils import escape
    _remove_run_key()
    if not enable:
        _schtasks("/delete", "/tn", TASK_NAME, "/f")
        _autostart_cache = False
        return
    exe, args = _launch_target()
    user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}"
    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{escape(user)}</UserId><Delay>PT15S</Delay></LogonTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>{escape(user)}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author"><Exec><Command>{escape(exe)}</Command><Arguments>{escape(args)}</Arguments>
    <WorkingDirectory>{escape(os.path.dirname(exe))}</WorkingDirectory></Exec></Actions>
</Task>"""
    path = os.path.join(CONFIG_DIR, "task.xml")
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-16") as f:
        f.write(xml)
    r = _schtasks("/create", "/tn", TASK_NAME, "/xml", path, "/f")
    _autostart_cache = r.returncode == 0
    if r.returncode != 0:
        log.error("schtasks create failed: %s %s", r.stdout, r.stderr)


def migrate_autostart():
    """Старая версия писала ключ Run — переносим его в Планировщик (и обновляем путь к exe)."""
    import winreg
    if not getattr(sys, "frozen", False):
        return  # запуск из исходников (разработка) не должен перенастраивать автозапуск
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
        had_run_key = True
    except OSError:
        had_run_key = False
    if had_run_key or autostart_enabled():
        set_autostart(True)


def wait_for_taskbar(timeout=90):
    """При входе в Windows ждём, пока появится панель задач — иначе значок в трее не создастся."""
    import time
    end = time.monotonic() + timeout
    while time.monotonic() < end and not ctypes.windll.user32.FindWindowW("Shell_TrayWnd", None):
        time.sleep(1)


def setup_logging():
    import logging.handlers
    os.makedirs(CONFIG_DIR, exist_ok=True)
    h = logging.handlers.RotatingFileHandler(os.path.join(CONFIG_DIR, "app.log"), maxBytes=512_000,
                                             backupCount=1, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, handlers=[h], format="%(asctime)s %(levelname)s %(threadName)s: %(message)s")
    lg = log
    # в собранном exe без консоли stdout/stderr = None — направляем всё в журнал
    stream = h.stream
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream
    sys.excepthook = lambda t, v, tb: lg.critical("необработанная ошибка", exc_info=(t, v, tb))
    threading.excepthook = lambda a: lg.critical("ошибка в потоке %s", a.thread, exc_info=(a.exc_type, a.exc_value, a.exc_traceback))
    return lg



def claim_single_instance(notify=True):
    """Возвращает серверный сокет, либо None, если программа уже запущена (тогда просим её показать окно)."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        srv.bind(("127.0.0.1", INSTANCE_PORT))
        srv.listen(2)
        return srv
    except OSError:
        srv.close()
        if not notify:
            return None
        try:
            with socket.create_connection(("127.0.0.1", INSTANCE_PORT), timeout=2) as c:
                c.sendall(b"show")
        except OSError:
            pass
        return None


def work_area():
    import ctypes.wintypes
    rect = ctypes.wintypes.RECT()
    ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)  # SPI_GETWORKAREA
    return rect.left, rect.top, rect.right, rect.bottom
