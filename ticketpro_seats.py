#!/usr/bin/env python3
#Free seats per session of a ticketpro.by group event, once or in watch mode.
#
#  python ticketpro_seats.py                       one-shot table for the default event
#  python ticketpro_seats.py <event url>           another group event
#  python ticketpro_seats.py --watch 300           re-check every 300 s, report when seats appear
#  python ticketpro_seats.py --date 2026-12-12     only sessions on that date (repeatable)
#  python ticketpro_seats.py --from 2026-12-20 --to 2027-01-10   only sessions in that range (inclusive)
#  python ticketpro_seats.py --once state.json     one check for a scheduler, state kept in the file
#  python ticketpro_seats.py --telegram-test       send a test message to Telegram
#
#Telegram: set TELEGRAM_BOT_TOKEN (from @BotFather) and TELEGRAM_CHAT_ID (from @userinfobot);
#when both are set, watch mode alerts that chat once a session gains more than --min-new seats (10)
#
#Data sources (no auth needed):
#  - group page HTML                    -> group event id, link to the JS chunk with server actions
#  - Next.js server action getGroupEventsApi -> sessions (externalId, startTime), max 20 per call
#  - legacy-widget .../events/<externalId>/places -> every seat with its status;
#    a seat is free when status == AVAILABLE, a fan-zone ("group") entry carries availableCount

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

DEFAULT_URL = "https://ticketpro.by/ru/event/bilety-na-novogodnyaya-akvatoriya"
SITE = "https://ticketpro.by"
PLACES_URL = "https://legacy-widget.ticketpro.by/api/v1/booking-service/events/{}/places?widgetId=9"
WIDGET_URL = "https://widget.ticketpro.by/event/{}?widget_id=9"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"
PAGE_SIZE = 20

WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def http(url, data=None, headers=None, timeout=30, attempts=4):
    #The site drops connections under parallel load; a dropped or timed-out request is retried
    #with a growing pause, HTTP error statuses are not
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8")
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            if attempt == attempts: raise
            time.sleep(2 * attempt)


def discover(event_url):
    #Group id comes from the RSC payload embedded in the page; the action id from the JS chunk
    #that declares getGroupEventsApi - it changes with every site deploy, so it is looked up each run
    html = http(event_url)
    slug = event_url.rstrip("/").rsplit("/", 1)[-1]
    m = re.search(r'\\"id\\":\\"([0-9a-f-]{36})\\",\\"externalId\\":[^,]*,\\"slug\\":\\"' + re.escape(slug) + r'\\"', html) \
        or re.search(r'\\"id\\":\\"([0-9a-f-]{36})\\"(?:(?!\\"id\\").){0,200}?\\"slug\\":\\"' + re.escape(slug) + r'\\",\\"name\\":null,\\"type\\":\\"GROUP', html)
    if not m: raise RuntimeError("Group event id not found on the page - is this a group (multi-date) event?")
    group_id = m.group(1)

    for chunk in sorted(set(re.findall(r'/_next/static/chunks/[\w-]+\.js', html))):
        js = http(SITE + chunk)
        a = re.search(r'createServerReference\)\("([0-9a-f]{40,})"[^)]*?"getGroupEventsApi"', js)
        if a: return group_id, a.group(1)
    raise RuntimeError("getGroupEventsApi server action not found in the page scripts - site layout changed")


def list_sessions(event_url, group_id, action_id):
    sessions, offset = [], 0
    while True:
        body = json.dumps([group_id, {"offset": offset, "limit": PAGE_SIZE}]).encode()
        text = http(event_url, data=body, headers={"Accept": "text/x-component", "next-action": action_id, "Content-Type": "text/plain;charset=UTF-8"})
        pairs = re.findall(r'"externalId":(\d+)(?:(?!"externalId").)*?"startTime":"([^"]+)"', text, re.S)
        for ext_id, start in pairs:
            sessions.append({"id": int(ext_id), "start": dt.datetime.fromisoformat(start)})
        total = re.search(r'"pagination":\{[^}]*"total":(\d+)', text)
        offset += PAGE_SIZE
        if not pairs or not total or offset >= int(total.group(1)): break
    unique = {s["id"]: s for s in sessions}
    return sorted(unique.values(), key=lambda s: s["start"])


def count_free(session):
    data = json.loads(http(PLACES_URL.format(session["id"])))
    if "seats" not in data: return {**session, "error": data.get("message", "no data")}
    free, total, prices = 0, 0, Counter()
    for seat in data["seats"]:
        price = (seat.get("price") or {}).get("value")
        if seat.get("type") == "GROUP" or "availableCount" in seat:
            n = seat.get("availableCount") or 0
            total += seat.get("totalCount") or n
        else:
            n = 1 if seat.get("status") == "AVAILABLE" else 0
            total += 1
        free += n
        if n and price is not None: prices[price] += n
    return {**session, "free": free, "total": total, "prices": prices}


def collect(event_url, is_wanted):
    group_id, action_id = discover(event_url)
    sessions = list_sessions(event_url, group_id, action_id)
    sessions = [s for s in sessions if is_wanted(s["start"].date())]
    with ThreadPoolExecutor(max_workers=4) as pool:
        return list(pool.map(count_free, sessions))


def fmt_prices(prices):
    return ", ".join(f"{p:g}×{n}" for p, n in sorted(prices.items()))


def print_table(results):
    print(f"{'Дата':<14}{'Время':<7}{'Свободно':>9}{'Всего':>7}  Цены свободных (BYN×шт)")
    day = None
    for r in results:
        d = r["start"]
        label = f"{d:%d.%m} {WEEKDAYS[d.weekday()]}" if d.date() != day else ""
        day = d.date()
        if "error" in r:
            print(f"{label:<14}{d:%H:%M}  ошибка: {r['error']}")
            continue
        print(f"{label:<14}{d:%H:%M}  {r['free']:>7}{r['total']:>7}  {fmt_prices(r['prices'])}")
    free_total = sum(r.get("free", 0) for r in results)
    with_free = sum(1 for r in results if r.get("free"))
    print(f"\nСеансов: {len(results)}, со свободными местами: {with_free}, свободных мест всего: {free_total}")


def telegram_send(text):
    #Returns False when Telegram is not configured or the send failed; the monitor keeps running either way
    token, chat_id = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id: return False
    body = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}).encode()
    try:
        http(f"https://api.telegram.org/bot{token}/sendMessage", data=body, headers={"Content-Type": "application/json"})
        return True
    except Exception as ex:
        print(f"  Telegram: не отправлено ({ex})")
        return False


def alert(new_seats):
    lines = []
    for r, delta in new_seats:
        d = r["start"]
        lines.append(f'<a href="{WIDGET_URL.format(r["id"])}">{d:%d.%m} {WEEKDAYS[d.weekday()]} {d:%H:%M}</a>: '
                     f'+{delta}, свободно {r["free"]} ({fmt_prices(r["prices"])} BYN)')
    telegram_send("🎟 Появились места:\n" + "\n".join(lines))


def process(results, state, min_new, schedule_text):
    #state["previous"]: free seats per session at the last check (console report of every increase);
    #state["baseline"]: free seats at the last Telegram alert or start - Telegram fires once a session
    #gains more than min_new seats over it, so small releases spread over several checks still add up.
    #The baseline follows the count down when seats sell out, so a later release is counted in full.
    #Session ids are string keys so the state survives a JSON round trip
    stamp = dt.datetime.now().strftime("%H:%M:%S")
    previous, baseline = state.get("previous", {}), dict(state.get("baseline", {}))
    free_of = {str(r["id"]): r.get("free", 0) for r in results}

    if not previous:
        print(f"[{stamp}] старт мониторинга")
        print_table(results)
        if telegram_send(f"Мониторинг запущен: {len(results)} сеансов, свободно мест {sum(free_of.values())}, {schedule_text}"):
            print("Telegram: подключён")
        else:
            print("Telegram: не настроен (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)")
    else:
        appeared = [(r, free_of[str(r["id"])] - previous.get(str(r["id"]), 0)) for r in results if free_of[str(r["id"])] > previous.get(str(r["id"]), 0)]
        if appeared:
            print(f"[{stamp}] ПОЯВИЛИСЬ МЕСТА:")
            for r, delta in appeared:
                print(f"  + {r['start']:%d.%m %H:%M}: +{delta} (сейчас {r['free']})  {WIDGET_URL.format(r['id'])}")
        else:
            print(f"[{stamp}] без новых мест (свободно всего: {sum(free_of.values())})")

        notify = [(r, free_of[str(r["id"])] - baseline.get(str(r["id"]), 0)) for r in results if free_of[str(r["id"])] - baseline.get(str(r["id"]), 0) > min_new]
        if notify:
            print(f"  -> в Telegram: {len(notify)} сеанс(ов), прибавилось больше {min_new} мест")
            alert(notify)
            for r, _ in notify: baseline[str(r["id"])] = r["free"]

    for key, free in free_of.items():
        if key not in baseline or free < baseline[key]: baseline[key] = free
    return {"previous": free_of, "baseline": baseline}


def watch(event_url, is_wanted, interval, min_new):
    state = {}
    while True:
        try:
            results = collect(event_url, is_wanted)
        except Exception as ex:
            print(f"[{dt.datetime.now():%H:%M:%S}] ошибка запроса: {ex}")
            time.sleep(interval)
            continue
        state = process(results, state, min_new, f"проверка раз в {interval} с")
        time.sleep(interval)


def check_once(event_url, is_wanted, min_new, state_path):
    #One check per run for schedulers (GitHub Actions): the state file carries previous/baseline
    #between runs; a missing file means the first run, which only reports the start
    try:
        with open(state_path, encoding="utf-8") as f: state = json.load(f)
    except FileNotFoundError:
        state = {}
    results = collect(event_url, is_wanted)
    state = process(results, state, min_new, "проверка по расписанию GitHub Actions")
    with open(state_path, "w", encoding="utf-8") as f: json.dump(state, f)


def main():
    if hasattr(sys.stdout, "reconfigure"): sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Свободные места по сеансам на ticketpro.by")
    ap.add_argument("url", nargs="?", default=DEFAULT_URL, help="страница группового события")
    ap.add_argument("--watch", type=int, metavar="SEC", help="проверять каждые SEC секунд и сообщать о новых местах")
    ap.add_argument("--date", action="append", type=dt.date.fromisoformat, metavar="YYYY-MM-DD", help="только эта дата (можно несколько)")
    ap.add_argument("--from", dest="date_from", type=dt.date.fromisoformat, metavar="YYYY-MM-DD", help="сеансы начиная с этой даты")
    ap.add_argument("--to", dest="date_to", type=dt.date.fromisoformat, metavar="YYYY-MM-DD", help="сеансы по эту дату включительно")
    ap.add_argument("--min-new", type=int, default=10, metavar="N", help="писать в Telegram, когда в сеансе прибавилось больше N мест (по умолчанию 10)")
    ap.add_argument("--once", metavar="STATE.json", help="одна проверка с уведомлением; состояние между запусками хранится в файле")
    ap.add_argument("--json", action="store_true", help="вывести результат как JSON")
    ap.add_argument("--telegram-test", action="store_true", help="отправить тестовое сообщение в Telegram и выйти")
    args = ap.parse_args()
    only_dates = set(args.date or [])
    is_wanted = lambda d: (not only_dates or d in only_dates) \
        and (args.date_from is None or d >= args.date_from) and (args.date_to is None or d <= args.date_to)

    if args.telegram_test:
        print("Отправлено" if telegram_send("Тест: уведомления ticketpro работают") else "Не отправлено: проверьте TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID")
        return

    if args.watch:
        watch(args.url, is_wanted, max(args.watch, 30), args.min_new)
        return

    if args.once:
        check_once(args.url, is_wanted, args.min_new, args.once)
        return

    results = collect(args.url, is_wanted)
    if args.json:
        out = [{"externalId": r["id"], "start": r["start"].isoformat(), "free": r.get("free"), "total": r.get("total"),
                "prices": {str(k): v for k, v in r.get("prices", {}).items()}, "error": r.get("error")} for r in results]
        print(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        print_table(results)


if __name__ == "__main__":
    main()
