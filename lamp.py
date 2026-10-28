"""Фоновый контроллер лампы: держит соединение, переподключается, шлёт команды очередью."""
import asyncio
import json
import logging
import os
import threading

from bleak import BleakClient, BleakScanner

from protocol import (LIGHT_MODE_CCT, LIGHT_MODE_NONE, LIGHT_MODE_RGB, SUPPORTED_PREFIXES, WRITE_CHAR,
                      cmd_brightness, cmd_color, cmd_color_temp, cmd_mode, cmd_power, cmd_speed, for_device,
                      is_encrypted_name)

log = logging.getLogger("LotusLanternPC.lamp")

CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "LotusLanternPC")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

DEFAULTS = {
    "address": None, "name": None,
    "on": True, "color": [255, 120, 0], "brightness": 100,
    "scene": "color", "mode": 7, "speed": 50, "temp": 50,
}


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


class Lamp:
    """Всё общение с Bluetooth идёт в отдельном потоке со своим asyncio-циклом.

    Публичные методы (power, set_color, ...) потокобезопасны: их зовут и окно, и трей.
    Команды с одинаковым ключом схлопываются — при быстром движении ползунка уходит только последнее значение.
    """

    def __init__(self, on_change):
        self.on_change = on_change          # вызывается из любого потока, без аргументов
        self.state = load_config()
        self.connected = False
        self.status = "Запуск…"
        self.client: BleakClient | None = None
        self._pending: dict[str, bytes] = {}
        self._wake = asyncio.Event()
        self._save_timer = None
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        asyncio.run_coroutine_threadsafe(self._main(), self.loop)

    # ------------------------------------------------------------ public API

    def power(self, on: bool):
        self._set(on=on)
        self._submit("power", cmd_power(on))
        if on:
            self._resend_scene()

    def toggle(self):
        self.power(not self.state["on"])

    def set_color(self, rgb):
        switched = self.state["scene"] != "color"
        self._set(color=list(rgb), scene="color", on=True)
        self._submit("scene", cmd_color(*rgb))
        if switched:  # как приложение: при смене палитры яркость уходит с новым режимом
            self._submit("bright", self._brightness_cmd())

    def set_brightness(self, level: int):
        self._set(brightness=int(level))
        self._submit("bright", self._brightness_cmd())

    def _brightness_cmd(self):
        light_mode = {"color": LIGHT_MODE_RGB, "white": LIGHT_MODE_CCT}.get(self.state["scene"], LIGHT_MODE_NONE)
        return cmd_brightness(int(self.state["brightness"]), light_mode)

    def set_temp(self, temp: int):  # 0 = тёплый, 100 = холодный
        switched = self.state["scene"] != "white"
        self._set(temp=int(temp), scene="white", on=True)
        self._submit("scene", cmd_color_temp(100 - int(temp), int(temp)))
        if switched:
            self._submit("bright", self._brightness_cmd())

    def set_mode(self, mode: int):
        self._set(mode=int(mode), scene="mode", on=True)
        self._submit("scene", cmd_mode(int(mode)))

    def set_speed(self, speed: int):
        self._set(speed=int(speed))
        self._submit("speed", cmd_speed(int(speed)))

    def reconnect(self, forget=False):
        """Переподключиться; forget=True — забыть лампу и найти заново."""
        def go():
            if forget:
                self.state["address"] = None
                self.state["name"] = None
            asyncio.ensure_future(self._drop())
        self.loop.call_soon_threadsafe(go)

    def shutdown(self):
        self._save()
        try:
            asyncio.run_coroutine_threadsafe(self._drop(), self.loop).result(timeout=3)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ internals

    def _set(self, **kw):
        self.state.update(kw)
        self._schedule_save()
        self.on_change()

    def _resend_scene(self):
        s = self.state
        if s["scene"] == "white":
            self._submit("scene", cmd_color_temp(100 - s["temp"], s["temp"]))
        elif s["scene"] == "mode":
            self._submit("scene", cmd_mode(s["mode"]))
        else:
            self._submit("scene", cmd_color(*s["color"]))

    def _submit(self, key, cmd):
        def put():
            self._pending.pop(key, None)
            self._pending[key] = cmd
            self._wake.set()
        self.loop.call_soon_threadsafe(put)

    def _status(self, connected, text):
        self.connected, self.status = connected, text
        self.on_change()

    def _schedule_save(self):
        if self._save_timer:
            self._save_timer.cancel()
        self._save_timer = threading.Timer(1.0, self._save)
        self._save_timer.daemon = True
        self._save_timer.start()

    def _save(self):
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=1)
        except OSError:
            pass

    async def _drop(self):
        c, self.client = self.client, None
        self.connected = False
        if c:
            try:
                await c.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self._wake.set()

    def _on_disconnect(self, client):
        if client is self.client:
            self.client = None
            self._status(False, "Связь потеряна, переподключаюсь…")
            self.loop.call_soon_threadsafe(self._wake.set)

    async def _find(self):
        self._status(False, "Ищу лампу…")
        found = await BleakScanner.discover(timeout=6, return_adv=True)
        best = None
        for d, adv in found.values():
            name = d.name or adv.local_name or ""
            if name.upper().startswith(SUPPORTED_PREFIXES) and (best is None or adv.rssi > best[2]):
                best = (name, d.address, adv.rssi)
        if not best:
            raise RuntimeError("лампа не найдена")
        self._set(name=best[0], address=best[1])

    async def _connect(self):
        if not self.state.get("address"):
            await self._find()
        name = self.state.get("name") or "лампа"
        self._status(False, f"Подключаюсь к {name}…")
        # Windows иногда отдаёт неполный список сервисов при первом подключении — переподключаемся
        for _ in range(3):
            client = BleakClient(self.state["address"], disconnected_callback=self._on_disconnect)
            await client.connect(timeout=15)
            if client.services.get_characteristic(WRITE_CHAR):
                break
            await client.disconnect()
            await asyncio.sleep(1)
        else:
            raise RuntimeError("лампа не отвечает")
        self.client = client
        self.encrypted = is_encrypted_name(self.state.get("name"))
        self._status(True, f"Подключено: {name}")
        # приложение шлёт яркость (с режимом палитры) при открытии экрана цвета — делаем так же
        self._submit("bright", self._brightness_cmd())
        log.info("подключено к %s %s", name, self.state["address"])

    async def _main(self):
        failures = 0
        while True:
            if not (self.client and self.client.is_connected):
                try:
                    await self._connect()
                    failures = 0
                except Exception as e:  # noqa: BLE001
                    failures += 1
                    log.warning("подключение не удалось: %r", e)
                    self._status(False, f"Нет связи: {e}" if str(e) else "Нет связи с лампой")
                    # после нескольких неудач по сохранённому адресу — искать заново
                    if failures >= 4 and self.state.get("address"):
                        self.state["address"] = None
                        failures = 0
                    self._wake.clear()
                    try:
                        await asyncio.wait_for(self._wake.wait(), min(5 * failures, 30))
                    except TimeoutError:
                        pass
                    continue
            if not self._pending:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), 2)
                except TimeoutError:
                    pass
                continue
            key = next(iter(self._pending))
            cmd = self._pending.pop(key)
            try:
                await self.client.write_gatt_char(WRITE_CHAR, for_device(cmd, self.encrypted), response=False)
            except Exception:  # noqa: BLE001
                self._pending.setdefault(key, cmd)
                await self._drop()
                continue
            await asyncio.sleep(0.03)
