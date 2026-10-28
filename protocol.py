"""BLE-протокол ламп ELK-BLEDOM, восстановленный из APK Lotus Lantern (com.easylink.colorful).

Сервис 0000fff0, характеристика записи 0000fff3, команды по 9 байт 7E .. EF.
Устройства, которые рекламируются как "ELK-*...", требуют шифрования команд.
"""
import os

WRITE_CHAR = "0000fff3-0000-1000-8000-00805f9b34fb"
SUPPORTED_PREFIXES = ("ELK-", "ELK~", "LED LIGHT STRIP", "XSL-", "MELK", "BLEDOM")

PRESET_KEY = bytes([42, 127, 193, 148, 51, 222, 69, 224, 139, 17, 92, 166, 9, 242, 125, 184])


def _encrypt(cmd: bytes) -> bytes:
    """EncryptionDecryptionKt.encryptBytes: 9 байт -> 21 байт."""
    rnd = os.urandom(12)
    ks = bytes(
        (((rnd[i] * 27) & 255) ^ ((rnd[(i + 3) % 12] + 55) & 255) ^ (rnd[(i + 7) % 12] >> 2) ^ ((i * 85) & 255)) & 255
        for i in range(9)
    )
    head = bytes(cmd[i] ^ ks[i] for i in range(9))
    tail = bytes(rnd[i] ^ PRESET_KEY[i % 16] for i in range(12))
    return head + tail


def for_device(cmd: bytes, encrypted: bool) -> bytes:
    """GattPacket.forDevice."""
    if encrypted and cmd[2] in (1, 3, 4):
        c = bytearray(cmd)
        c[0], c[8] = 0xAA, 0x55
        return _encrypt(bytes(c))
    return cmd


def is_encrypted_name(name: str) -> bool:
    return "ELK-*" in (name or "")


def cmd_power(on: bool) -> bytes:
    return bytes([0x7E, 0x04, 0x04, int(on), 0x00, int(on), 0xFF, 0x00, 0xEF])


def cmd_color(r: int, g: int, b: int) -> bytes:
    return bytes([0x7E, 0x07, 0x05, 0x03, r, g, b, 0x10, 0xEF])


# Режим палитры, который приложение шлёт вместе с яркостью (ColorChangeFragment.getPaletteMode):
# 1 — цвет (RGB), 3 — белый (CCT), 2 — одноцветный; экран эффектов шлёт без режима (0xFF).
LIGHT_MODE_RGB, LIGHT_MODE_SINGLE, LIGHT_MODE_CCT, LIGHT_MODE_NONE = 1, 2, 3, 0xFF


def cmd_brightness(level: int, light_mode: int = LIGHT_MODE_NONE) -> bytes:  # 0..100
    return bytes([0x7E, 0x04, 0x01, level, light_mode, 0xFF, 0xFF, 0x00, 0xEF])


def cmd_mode(mode: int) -> bytes:  # индекс эффекта, отправляется как mode + 128
    return bytes([0x7E, 0x05, 0x03, (mode + 128) & 255, 0x03, 0xFF, 0xFF, 0x00, 0xEF])


def cmd_speed(speed: int) -> bytes:  # 0..100
    return bytes([0x7E, 0x04, 0x02, speed, 0xFF, 0xFF, 0xFF, 0x00, 0xEF])


def cmd_color_temp(warm: int, cold: int) -> bytes:  # 0..100 каждая
    return bytes([0x7E, 0x06, 0x05, 0x02, warm, cold, 0xFF, 0x08, 0xEF])


MODES = [
    (7, "Прыжки: RGB"), (8, "Прыжки: 7 цветов"),
    (9, "Плавно: 3 цвета"), (10, "Плавно: 7 цветов"),
    (11, "Плавно: красный"), (12, "Плавно: зелёный"), (13, "Плавно: синий"),
    (14, "Плавно: жёлтый"), (15, "Плавно: голубой"), (16, "Плавно: пурпурный"),
    (17, "Плавно: белый"), (18, "Плавно: красный-зелёный"), (19, "Плавно: красный-синий"),
    (20, "Плавно: зелёный-синий"),
    (21, "Мерцание: 7 цветов"), (22, "Мерцание: красный"), (23, "Мерцание: зелёный"),
    (24, "Мерцание: синий"), (25, "Мерцание: жёлтый"), (26, "Мерцание: голубой"),
    (27, "Мерцание: пурпурный"), (28, "Мерцание: белый"),
]

# Готовые цвета — чистые значения каналов, как в самом приложении (ColorSelectorAdapter:
# жёлтый, белый, голубой, красный, зелёный, синий). Жёлтый — первый.
COLORS = [
    ("Жёлтый", (255, 255, 0)), ("Оранжевый", (255, 100, 0)), ("Красный", (255, 0, 0)),
    ("Малиновый", (255, 0, 100)), ("Пурпурный", (255, 0, 255)), ("Фиолетовый", (128, 0, 255)),
    ("Синий", (0, 0, 255)), ("Голубой", (0, 255, 255)), ("Бирюзовый", (0, 255, 128)),
    ("Зелёный", (0, 255, 0)), ("Белый", (255, 255, 255)),
]
