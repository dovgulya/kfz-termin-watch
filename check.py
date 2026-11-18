#!/usr/bin/env python3
"""Kfz-Zulassung Nürnberg: ловит свободные термины и шлёт пуш через ntfy.

Услуга: cnc 73 = "Gebrauchtfahrzeug aus dem Inland umschreiben oder zulassen".
Места: Ordnungsamt (Rathenauplatz), Bürgeramt Ost, Bürgeramt Süd.

Переменные окружения:
  NTFY_TOPIC   — секретное имя канала ntfy (обязательно)
  TARGET_LAST  — последняя интересная дата, по умолчанию 2026-11-23
  PRIORITY_DAY — день, который выделять, по умолчанию 2026-11-20 (пятница)
  STATE_FILE   — файл для антиспама, по умолчанию state/last.txt
  TEST_NOTIFY  — "1": отправить тестовый пуш с текущей картиной и выйти
"""
import datetime as dt
import hashlib
import json
import os
import re
import urllib.request
import http.cookiejar
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Berlin")
BASE = "https://nuernberg.termine-reservieren.de"
CNC = 73
LOCS = [
    (5, "Ordnungsamt, Rathenauplatz 18"),
    (32, "Bürgeramt Ost, Fischbacher Hauptstr. 121"),
    (12, "Bürgeramt Süd, Hans-Traut-Str. 8"),
]
BOOK_URL = "https://www.nuernberg.de/internet/stadtportal/behoerdenwegweiser/dienstleistung/umschreibung_gebrauchtwagen.html"
WD = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
WD_FULL = ["ПОНЕДЕЛЬНИК", "ВТОРНИК", "СРЕДА", "ЧЕТВЕРГ", "ПЯТНИЦА", "СУББОТА", "ВОСКРЕСЕНЬЕ"]

TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
TARGET_LAST = dt.date.fromisoformat(os.environ.get("TARGET_LAST", "2026-11-23"))
PRIORITY_DAY = dt.date.fromisoformat(os.environ.get("PRIORITY_DAY", "2026-11-20"))
STATE_FILE = os.environ.get("STATE_FILE", "state/last.txt")
TEST = os.environ.get("TEST_NOTIFY") == "1"


def fetch_slots(loc):
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    op.addheaders = [("User-Agent", "Mozilla/5.0 (kfz-termin-watch)")]
    op.open(f"{BASE}/select2?md=8", timeout=25).read()
    html = op.open(f"{BASE}/directentry?mdt=8&loc={loc}&cnc-{CNC}=1", timeout=25).read().decode("utf-8", "replace")
    ok = "suggest_btn" in html or "Kein freier Termin" in html
    days = {}
    parts = re.split(r'<div id="panel_(\d+)">', html)
    for i in range(1, len(parts), 2):
        day = dt.datetime.fromtimestamp(int(parts[i]), TZ).date()
        free = re.findall(r'<button type="submit" class="suggest_btn[^"]*" title="(\d\d:\d\d)"', parts[i + 1])
        if free:
            days[day] = free
    return days, ok


def push(title, body, priority="urgent", tags="rotating_light"):
    req = urllib.request.Request(
        f"https://ntfy.sh/{TOPIC}",
        data=body.encode("utf-8"),
        headers={
            "Title": title.encode("utf-8"),
            "Priority": priority,
            "Tags": tags,
            "Click": BOOK_URL,
        },
        method="POST",
    )
    urllib.request.urlopen(req, timeout=20).read()


def main():
    now = dt.datetime.now(TZ)
    if not TOPIC:
        raise SystemExit("NTFY_TOPIC не задан")
    if now.date() > TARGET_LAST and not TEST:
        print("Срок прошёл, проверка не нужна.")
        return

    hits, earliest, broken = [], None, []
    for loc, name in LOCS:
        try:
            days, ok = fetch_slots(loc)
        except Exception as e:  # сеть/сайт
            days, ok = {}, False
            print(f"{name}: ошибка {e}")
        if not ok:
            broken.append(name)
        for day, times in sorted(days.items()):
            earliest = day if earliest is None or day < earliest else earliest
            if now.date() <= day <= TARGET_LAST:
                hits.append((day, name, times))
        print(f"{name}: " + (", ".join(f"{d:%d.%m}({len(t)})" for d, t in sorted(days.items())) or "нет слотов"))

    hits.sort(key=lambda h: (h[0] != PRIORITY_DAY, h[0]))
    lines = []
    for day, name, times in hits:
        mark = "🔥 " if day == PRIORITY_DAY else ""
        shown = ", ".join(times[:12]) + (f" … (+{len(times) - 12})" if len(times) > 12 else "")
        lines.append(f"{mark}{WD[day.weekday()]} {day:%d.%m}: {name}\n{shown}")
    body = "\n\n".join(lines) + f"\n\nЗапись: {BOOK_URL}" if hits else ""

    if TEST:
        nearest = f"{earliest:%d.%m}" if earliest else "нет"
        summary = body or f"До {TARGET_LAST:%d.%m} слотов нет. Ближайший свободный: {nearest}"
        push("Тест: проверка терминов работает", summary + f"\nПроверено {now:%d.%m %H:%M}",
             priority="default", tags="white_check_mark")
        print("Тестовый пуш отправлен")
        return

    # Антиспам: шлём, только если набор слотов изменился.
    sig = hashlib.sha1(json.dumps([(str(d), n, t) for d, n, t in hits], ensure_ascii=False).encode()).hexdigest() if hits else ""
    prev = open(STATE_FILE).read().strip() if os.path.exists(STATE_FILE) else ""
    if hits and sig != prev:
        fri = any(d == PRIORITY_DAY for d, _, _ in hits)
        title = f"{WD_FULL[PRIORITY_DAY.weekday()]} {PRIORITY_DAY:%d.%m} — есть Kfz-термин!" if fri else f"Kfz-термин до {TARGET_LAST:%d.%m} освободился"
        push(title, body)
        print("Пуш отправлен:", title)
    elif not hits:
        print(f"До {TARGET_LAST:%d.%m} слотов нет. Ближайший: {earliest}")
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    open(STATE_FILE, "w").write(sig)
    if len(broken) == len(LOCS):
        raise SystemExit("Сайт записи не ответил ни для одного места")


if __name__ == "__main__":
    main()
