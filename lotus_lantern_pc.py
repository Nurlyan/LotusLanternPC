"""Lotus Lantern PC — управление LED-подсветкой ELK-BLEDOM с компьютера.

Стеклянная панель (HTML в WebView2) выпадает из трея над часами и прячется, когда фокус уходит.
Запуск с ключом --tray стартует сразу в трее (так программа запускается вместе с Windows).
"""
import colorsys
import ctypes
import json
import os
import socket
import sys
import threading
import time

import pystray
import webview

from lamp import CONFIG_DIR, Lamp
from protocol import COLORS, MODES
from system import (APP_NAME, INSTANCE_PORT, autostart_enabled, claim_single_instance, log, make_icon,
                    migrate_autostart, set_autostart, setup_logging, wait_for_taskbar, work_area)

W, H = 348, 540          # размер панели (= окна) в логических пикселях
RADIUS = 26              # скругление окна, как у стеклянной панели в CSS
MARGIN = 12              # отступ от края рабочей области
SHOW_MS, HIDE_MS = 260, 170


def resource(*parts):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


def kelvin_rgb(t):
    warm, cold = (255, 170, 80), (205, 225, 255)
    return [round(warm[i] + (cold[i] - warm[i]) * t) for i in range(3)]


def scene_rgb(s):
    if s["scene"] == "white":
        return kelvin_rgb(s["temp"] / 100)
    if s["scene"] == "mode":
        return [170, 110, 255]
    return list(s["color"])


def scene_text(s):
    if s["scene"] == "white":
        t = s["temp"]
        return "Белый · " + ("тёплый" if t < 34 else "холодный" if t > 66 else "нейтральный")
    if s["scene"] == "mode":
        return "Эффект · " + dict(MODES).get(s["mode"], str(s["mode"]))
    r, g, b = s["color"]
    h, sat, _ = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    if sat < 0.2:
        return "Цвет · белый"
    names = ["красный", "оранжевый", "жёлтый", "салатовый", "зелёный", "бирюзовый",
             "голубой", "синий", "фиолетовый", "пурпурный", "розовый", "красный"]
    return "Цвет · " + names[round(h * 12) % 12]


class Api:
    """Методы, которые вызывает страница (window.pywebview.api.*)."""

    def __init__(self, app):
        self._app = app

    def toggle(self):
        self._app.lamp.toggle()

    def brightness(self, v):
        self._app.lamp.set_brightness(max(1, min(100, int(v))))

    def color(self, r, g, b):
        self._app.lamp.set_color((int(r), int(g), int(b)))

    def temp(self, t):
        self._app.lamp.set_temp(int(t))

    def mode(self, m):
        self._app.lamp.set_mode(int(m))

    def speed(self, v):
        self._app.lamp.set_speed(int(v))

    def rescan(self):
        self._app.lamp.reconnect(forget=True)

    def picking(self, flag):
        self._app.picking = bool(flag)

    def hide(self):
        self._app.hide()

    def quit(self):
        threading.Thread(target=self._app.quit_app, daemon=True).start()


class GlassApp:
    def __init__(self, instance_sock, start_hidden):
        self.start_hidden = start_hidden
        self.visible = False
        self.loaded = False
        self.picking = False
        self._hidden_at = 0.0
        self._dirty = threading.Event()
        self._quitting = False
        self._anim = 0  # номер текущей анимации окна; новая отменяет старую
        self.lamp = Lamp(self._dirty.set)

        with open(resource("ui", "panel.html"), encoding="utf-8") as f:
            html = f.read()
        self.window = webview.create_window(
            "Подсветка", html=html, js_api=Api(self), width=W, height=H, resizable=False,
            frameless=True, easy_drag=False, hidden=True, on_top=True, shadow=False, background_color="#07080c")
        self.window.events.loaded += self._on_loaded

        self.tray = self._new_tray()
        threading.Thread(target=self._run_tray, name="tray", daemon=True).start()
        threading.Thread(target=self._listen, args=(instance_sock,), name="instance", daemon=True).start()
        threading.Thread(target=self._pusher, name="push", daemon=True).start()

    def run(self):
        webview.start(gui="edgechromium", private_mode=False, storage_path=os.path.join(CONFIG_DIR, "webview"))

    # -------------------------------------------------- окно (WinForms под pywebview)

    def _ui(self, fn):
        from System import Action
        form = self.window.native
        if form.InvokeRequired:
            form.Invoke(Action(fn))
        else:
            fn()

    def _js(self, code):
        threading.Thread(target=lambda: self._safe_js(code), daemon=True).start()

    def _safe_js(self, code):
        try:
            self.window.evaluate_js(code)
        except Exception:  # noqa: BLE001
            log.exception("evaluate_js")

    def _on_loaded(self):
        self.loaded = True
        self._safe_js(f"initModes({json.dumps(MODES, ensure_ascii=False)})")
        self._safe_js(f"initColors({json.dumps(COLORS, ensure_ascii=False)})")
        self._push()
        self._safe_js(f"firstTab({json.dumps(self.lamp.state)})")
        self._ui(self._setup_form)
        self._ui(self._park)
        if not self.start_hidden:
            self.show()

    def _setup_form(self):
        from System import EventHandler
        form = self.window.native
        form.ShowInTaskbar = False
        self._hwnd = ctypes.c_void_p(form.Handle.ToInt64())
        # окно — сама стеклянная панель: свою форму задаём регионом, без квадратной рамки
        no_round = ctypes.c_int(1)                        # DWMWCP_DONOTROUND
        ctypes.windll.dwmapi.DwmSetWindowAttribute(self._hwnd, 33, ctypes.byref(no_round), 4)
        # не показывать в Alt+Tab (WS_EX_TOOLWINDOW)
        user32 = ctypes.windll.user32
        user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.SetWindowLongPtrW(self._hwnd, -20, user32.GetWindowLongPtrW(self._hwnd, -20) | 0x80)
        form.Deactivate += EventHandler(lambda s, e: self._on_deactivate())

    def _park(self):
        """Спрятанная панель не скрывается, а стоит за кромкой панели задач, обрезанная до нуля:
        WebView2 не рисует скрытое окно, и при выезде оно было бы пустым."""
        form = self.window.native
        l, t, r, b = work_area()
        form.Left, form.Top = r - form.Width - MARGIN, b
        self._clip(form.Width, form.Height, 0)
        ctypes.windll.user32.ShowWindow(self._hwnd, 4)    # SW_SHOWNOACTIVATE — без кражи фокуса
        self._js("park(true)")

    def _clip(self, w, h, visible):
        """Скруглённая форма окна w×h, срезанная снизу до `visible` пикселей —
        так окно «выезжает» из-за кромки панели задач, а не появляется целиком."""
        gdi = ctypes.windll.gdi32
        r = round(RADIUS * w / W) * 2
        rgn = gdi.CreateRoundRectRgn(0, 0, w + 1, h + 1, r, r)
        if visible < h:
            cut = gdi.CreateRectRgn(0, 0, w + 1, max(0, visible))
            gdi.CombineRgn(rgn, rgn, cut, 1)              # RGN_AND
            gdi.DeleteObject(cut)
        ctypes.windll.user32.SetWindowRgn(self._hwnd, rgn, True)  # регион теперь принадлежит окну

    def _on_deactivate(self):
        if self.visible and not self.picking and not self._quitting:
            threading.Thread(target=self.hide, daemon=True).start()

    def _slide(self, form, y0, y1, edge, ms, ease, done=None):
        """Двигает окно от y0 к y1; всё, что ниже `edge` (кромка панели задач), обрезается.
        Кадры идут в своём потоке по DwmFlush — в такт с частотой монитора (таймер WinForms
        тикает не чаще ~64 раз в секунду, и на 240 Гц это выглядело как 60 fps)."""
        self._anim += 1
        gen, x, w, h = self._anim, form.Left, form.Width, form.Height
        user32, dwm = ctypes.windll.user32, ctypes.windll.dwmapi

        def frame(top):
            moving_up = top < frame.top
            if not moving_up:                             # сначала срезать, потом двигать — не заезжать на панель задач
                self._clip(w, h, edge - top)
            user32.SetWindowPos(self._hwnd, None, x, top, 0, 0, 0x0015)  # NOSIZE | NOZORDER | NOACTIVATE
            if moving_up:
                self._clip(w, h, edge - top)
            frame.top = top
        frame.top = y0

        def run():
            t0 = time.perf_counter()
            while gen == self._anim:
                p = min(1.0, (time.perf_counter() - t0) * 1000 / ms)
                top = round(y0 + (y1 - y0) * ease(p))
                if top != frame.top or p == 0:
                    frame(top)
                if p >= 1:
                    if done:
                        done()
                    return
                if dwm.DwmFlush() != 0:                   # композиция выключена — хотя бы ~1 мс
                    time.sleep(0.001)
        threading.Thread(target=run, name="slide", daemon=True).start()

    def show(self):
        if self.visible or not self.loaded or self._quitting:
            return
        self.visible = True

        def do():
            form = self.window.native
            l, t, r, b = work_area()
            x, y = r - form.Width - MARGIN, b - form.Height - MARGIN
            form.Left, form.Top = x, b
            self._clip(form.Width, form.Height, 0)     # пока целиком «за» панелью задач
            if not form.Visible:
                form.Show()
            form.TopMost = True
            form.Activate()
            self._slide(form, b, y, b, SHOW_MS, lambda p: 1 - (1 - p) ** 4)   # ease-out quart
        self._js("park(false)")
        self._ui(do)

    def hide(self):
        if not self.visible:
            return
        self.visible = False
        self._hidden_at = time.monotonic()

        def do():
            form = self.window.native
            b = work_area()[3]

            def done():
                if self.visible:                          # пока уезжало, панель открыли снова
                    return
                self._js("park(true)")
                user32 = ctypes.windll.user32
                if user32.GetForegroundWindow() == self._hwnd.value:   # отдать фокус, чтобы ввод не уходил в невидимое окно
                    user32.SetForegroundWindow(user32.FindWindowW("Shell_TrayWnd", None))
            self._slide(form, form.Top, b, b, HIDE_MS, lambda p: p ** 3, done)  # ease-in cubic
        self._ui(do)

    def toggle_window(self):
        if self.visible:
            self.hide()
        elif time.monotonic() - self._hidden_at > 0.4:   # щелчок по значку уже спрятал панель
            self.show()

    # -------------------------------------------------- состояние -> страница и трей

    def _push(self):
        s, L = self.lamp.state, self.lamp
        data = dict(on=s["on"], color=list(s["color"]), brightness=s["brightness"], scene=s["scene"],
                    mode=s["mode"], speed=s["speed"], temp=s["temp"], connected=L.connected,
                    status=L.status, name=s.get("name") or "", sceneText=scene_text(s))
        self._safe_js(f"applyState({json.dumps(data, ensure_ascii=False)})")

    def _pusher(self):
        last_icon = None
        while not self._quitting:
            self._dirty.wait()
            time.sleep(0.06)
            self._dirty.clear()
            if self.loaded:
                self._push()
            s = self.lamp.state
            icon_key = (tuple(scene_rgb(s)), s["on"], self.lamp.connected)
            try:
                if icon_key != last_icon:
                    self.tray.icon = make_icon(*icon_key)
                    last_icon = icon_key
                self.tray.title = f"Подсветка — {'вкл' if s['on'] else 'выкл'}, {s['brightness']}% · " + \
                                  ("подключено" if self.lamp.connected else "нет связи")
                self.tray.update_menu()
            except Exception:  # noqa: BLE001
                pass

    # -------------------------------------------------- трей

    def _new_tray(self):
        return pystray.Icon(APP_NAME, make_icon(scene_rgb(self.lamp.state), connected=False), "Подсветка", self._tray_menu())

    def _run_tray(self):
        for attempt in range(40):
            try:
                self.tray.run()
                return
            except Exception:  # noqa: BLE001
                log.exception("значок в трее не создался (попытка %d)", attempt + 1)
                if self._quitting:
                    return
                time.sleep(3)
                self.tray = self._new_tray()
                self._dirty.set()

    def _tray_menu(self):
        L, s = self.lamp, self.lamp.state
        Item, Menu = pystray.MenuItem, pystray.Menu

        def do(fn, *a):
            return lambda icon, item: fn(*a)

        def is_bright(v):
            return lambda item: s["brightness"] == v

        def is_mode(m):
            return lambda item: s["scene"] == "mode" and s["mode"] == m

        def toggle_autostart(icon, item):
            set_autostart(not autostart_enabled())

        colors = [Item(n, do(L.set_color, rgb)) for n, rgb in COLORS]
        whites = [Item(n, do(L.set_temp, t)) for n, t in (("Тёплый", 0), ("Нейтральный", 50), ("Холодный", 100))]
        brights = [Item(f"{v}%", do(L.set_brightness, v), checked=is_bright(v), radio=True) for v in (10, 25, 50, 75, 100)]
        effects = [Item(n, do(L.set_mode, m), checked=is_mode(m), radio=True) for m, n in MODES]

        return Menu(
            Item("Открыть панель", lambda icon, item: self.toggle_window(), default=True),
            Menu.SEPARATOR,
            Item("Включено", lambda icon, item: L.toggle(), checked=lambda item: s["on"]),
            Item("Цвет", Menu(*colors)),
            Item("Белый свет", Menu(*whites)),
            Item("Яркость", Menu(*brights)),
            Item("Эффекты", Menu(*effects)),
            Menu.SEPARATOR,
            Item("Переподключить", lambda icon, item: L.reconnect()),
            Item("Запускать вместе с Windows", toggle_autostart, checked=lambda item: autostart_enabled()),
            Item("Выход", lambda icon, item: self.quit_app()),
        )

    def quit_app(self):
        if self._quitting:
            return
        self._quitting = True
        log.info("выход")
        self._dirty.set()
        try:
            self.tray.stop()
        except Exception:  # noqa: BLE001
            pass
        self.lamp.shutdown()
        self.window.destroy()

    def _listen(self, srv):
        while True:
            try:
                conn, _ = srv.accept()
                with conn:
                    if conn.recv(16) == b"show":
                        self.show()
            except OSError:
                return


def main():
    setup_logging()
    tray_mode = "--tray" in sys.argv
    log.info("старт %s args=%s", sys.executable, sys.argv[1:])
    if tray_mode:
        wait_for_taskbar()
    for _ in range(10):  # при входе в Windows сокеты могут быть ещё не готовы
        sock = claim_single_instance(notify=not tray_mode)
        if sock is not None:
            break
        if not tray_mode:
            log.info("уже запущено — показываю панель")
            return
        time.sleep(3)
    else:
        log.info("уже запущено (или порт %d занят) — выхожу", INSTANCE_PORT)
        return
    try:
        migrate_autostart()
    except Exception:  # noqa: BLE001
        log.exception("не удалось обновить автозапуск")
    try:
        GlassApp(sock, start_hidden=tray_mode).run()
    except Exception:  # noqa: BLE001
        log.exception("падение")
        raise
    os._exit(0)  # фоновые потоки (Bluetooth, трей) не держат процесс


if __name__ == "__main__":
    main()
