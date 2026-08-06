#!/usr/bin/env python3
"""Тянет тарифы WB и кладёт их в tarify.tsv.

Три запроса:
1. Комиссии по предметам — для «Куртки» FBO и FBS.
2. Тарифы коробов — хранение и логистика по каждому складу WB.
3. Отчёт о реализации за прошлый месяц — из него считаем СПП по предметам
   (скидку покупателю WB даёт поверх нашей цены за свой счёт).
4. MPSTATS (если задан MPSTATS_TOKEN) — выкуп по нишам за тот же месяц.
5. Тот же отчёт с 1 января — накопленный доход кабинета: по нему считаются
   налоговые пороги и НДС (база — то, что заплатил покупатель).

Колонки разделяем табуляцией, а не запятой: тогда запятая свободна для самих
чисел, и русская Google Таблица читает «37,5» как число, а не как текст.

Токен берётся из переменной окружения WB_TOKEN. В коде и в файлах его нет.
Если WB не ответил — старый tarify.tsv не трогаем, выходим с ошибкой.
"""

import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta

API_COMMISSION = "https://common-api.wildberries.ru/api/v1/tariffs/commission?locale=ru"
API_BOX = "https://common-api.wildberries.ru/api/v1/tariffs/box?date={day}"
API_REPORT = ("https://statistics-api.wildberries.ru/api/v5/supplier/reportDetailByPeriod"
              "?dateFrom={start}&dateTo={end}&limit=100000&rrdid={rrd}")
TIMEOUT_SEC = 30
REPORT_TIMEOUT_SEC = 180     # отчёт о реализации большой, отдаётся медленно
REPORT_PAUSE_SEC = 65        # WB пускает в этот отчёт раз в минуту
REPORT_MAX_PAGES = 3
YEAR_MAX_PAGES = 12       # годовой проход длиннее месячного
CABINET = os.environ.get("WB_CABINET_NAME", "NESS").strip() or "NESS"
SPP_MIN_REVENUE = 100_000    # предметы мельче не пишем: статистики мало, цифра случайная
OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tarify.tsv")

HEADER = [
    "Что",
    "Предмет или склад",
    "Комиссия % · Хранение 1-й литр ₽/сутки",
    "Хранение доп. литр ₽/сутки",
    "Логистика 1-й литр ₽",
    "Логистика доп. литр ₽",
    "Обновлено",
    # ↓ новые колонки дописаны В КОНЕЦ нарочно: старые остались на своих местах,
    # поэтому формулы Google-книг и разбор портала не поехали
    "Логистика FBS 1-й литр ₽",
    "Логистика FBS доп. литр ₽",
]

# какие предметы забираем; имена — как их называет сам WB
SUBJECTS = ("Куртки", "Свитеры", "Джемперы", "Кардиганы", "Платья",
            "Брюки", "Лонгсливы", "Худи", "Свитшоты", "Толстовки",
            "Костюмы", "Пижамы", "Ночные сорочки", "Блузки", "Рубашки",
            "Брюки спортивные", "Топы", "Топы спортивные")

# поле ответа WB → название схемы работы человеческим языком
SCHEMES = {
    "paidStorageKgvp": "FBO",   # товар лежит на складе WB
    "kgvpMarketplace": "FBS",   # товар лежит у продавца
}

# поля тарифа короба в том порядке, в котором пишем их в файл
BOX_FIELDS = (
    "boxStorageBase",     # хранение, первый литр, ₽ в сутки
    "boxStorageLiter",    # хранение, каждый следующий литр
    "boxDeliveryBase",    # логистика до клиента, первый литр
    "boxDeliveryLiter",   # логистика, каждый следующий литр
)

# то же самое, но когда товар лежит у продавца (FBS). WB зовёт это «маркетплейс»
# и считает по складу, куда сдаём заказы; у части складов ставка отличается от FBO
FBS_FIELDS = (
    "boxDeliveryMarketplaceBase",    # логистика FBS, первый литр, ₽
    "boxDeliveryMarketplaceLiter",   # логистика FBS, каждый следующий литр
)


def explain_http_error(code: int) -> str:
    messages = {
        401: "WB не принял токен. Проверь, что он не отозван и выдан на чтение.",
        403: "Токену не хватает прав на тарифы. Выпусти новый и отметь категорию «Тарифы».",
        429: "WB просит подождать — слишком много запросов. Попробуем в следующий раз.",
    }
    return messages.get(code, f"WB вернул ошибку {code}. Тарифы не обновлены.")


def call_wb(url: str, token: str, timeout: int = TIMEOUT_SEC) -> dict:
    """Один запрос в WB. Кидает понятную ошибку, если не пустило."""
    request = urllib.request.Request(
        url,
        headers={"Authorization": token, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise SystemExit(explain_http_error(error.code)) from error
    except urllib.error.URLError as error:
        raise SystemExit(f"WB не отвечает: {error.reason}. Тарифы не обновлены.") from error


def as_russian_number(value) -> str:
    """37.5 → «37,5». Google Таблица с русской локалью читает это как число.

    Пустоту и прочерк («-» у складов без коробов) превращаем в пустую ячейку.
    """
    text = str(value).strip() if value is not None else ""
    if text in ("", "-"):
        return ""
    return text.replace(".", ",")


def commission_rows(token: str, today: str) -> list:
    payload = call_wb(API_COMMISSION, token)
    report = payload.get("report")
    if not report:
        raise SystemExit("WB ответил, но справочник комиссий пустой. Тарифы не обновлены.")

    rows = []
    for item in report:
        subject = item.get("subjectName")
        if subject not in SUBJECTS:
            continue
        for field, scheme in SCHEMES.items():
            value = item.get(field)
            if value is None:
                continue
            # ключ «FBO Свитеры» — по нему таблица категории ищет свою комиссию
            rows.append([f"{scheme} {subject}", subject,
                         as_russian_number(value), "", "", "", today, "", ""])
            # старые ключи «FBO»/«FBS» без предмета оставляем для живой
            # таблицы курток — она ищет комиссию именно по ним
            if subject == "Куртки":
                rows.append([scheme, subject,
                             as_russian_number(value), "", "", "", today, "", ""])
    if not rows:
        raise SystemExit(f"В справочнике WB не нашлись предметы: {', '.join(SUBJECTS)}")
    return sorted(rows)


def warehouse_rows(token: str, today: str) -> list:
    payload = call_wb(API_BOX.format(day=today), token)
    warehouses = payload.get("response", {}).get("data", {}).get("warehouseList") or []
    if not warehouses:
        raise SystemExit("WB ответил, но список складов пустой. Тарифы не обновлены.")

    # подсказка на будущее: какие поля вообще отдаёт WB по складу
    print("поля склада в ответе WB:", ", ".join(sorted(warehouses[0].keys())))

    rows = []
    for warehouse in warehouses:
        name = (warehouse.get("warehouseName") or "").strip()
        if not name:
            continue
        values = [as_russian_number(warehouse.get(field)) for field in BOX_FIELDS]
        if not any(values):
            continue  # склад без тарифов коробов
        fbs = [as_russian_number(warehouse.get(field)) for field in FBS_FIELDS]
        rows.append(["Склад", name, *values, today, *fbs])
    if not rows:
        raise SystemExit("Ни у одного склада нет тарифов коробов. Тарифы не обновлены.")
    return sorted(rows, key=lambda row: row[1])


def previous_month(today: date) -> tuple:
    """Границы прошлого полного месяца: («2026-07-01», «2026-08-01»)."""
    first_this_month = today.replace(day=1)
    last_prev_month = first_this_month - timedelta(days=1)
    return last_prev_month.replace(day=1).isoformat(), first_this_month.isoformat()


def spp_rows(token: str, today: str) -> list:
    """СПП по предметам из отчёта о реализации за прошлый месяц.

    Цена продавца (retail_price_withdisc_rub) — сколько причитается нам,
    retail_amount — сколько на самом деле заплатил покупатель. Разница и есть
    скидка постоянного покупателя: её WB даёт за свой счёт.
    """
    start, end = previous_month(date.fromisoformat(today))
    seller, client = {}, {}
    rrd, pages = 0, 0
    while pages < REPORT_MAX_PAGES:
        rows = call_wb(API_REPORT.format(start=start, end=end, rrd=rrd),
                       token, REPORT_TIMEOUT_SEC) or []
        if not rows:
            break
        for row in rows:
            if (row.get("doc_type_name") or "").strip() != "Продажа":
                continue
            subject = (row.get("subject_name") or "").strip()
            if not subject:
                continue
            seller[subject] = seller.get(subject, 0) + (row.get("retail_price_withdisc_rub") or 0)
            client[subject] = client.get(subject, 0) + (row.get("retail_amount") or 0)
        rrd = rows[-1].get("rrd_id") or 0
        pages += 1
        if len(rows) < 100000:
            break
        time.sleep(REPORT_PAUSE_SEC)

    result = []
    total_seller, total_client = sum(seller.values()), sum(client.values())
    if total_seller >= SPP_MIN_REVENUE:
        # средняя по кабинету: ею портал переводит выручку из цен покупателя
        # в наши цены, когда считает долю рекламы
        result.append(["СПП", "ВСЕГО", as_russian_number(
            round(100 * (1 - total_client / total_seller), 1)), "", "", "", today, "", ""])
    for subject, revenue in seller.items():
        if revenue < SPP_MIN_REVENUE:
            continue
        percent = round(100 * (1 - client.get(subject, 0) / revenue), 1)
        if not 0 <= percent <= 95:
            continue  # мусорная строка: скидки такого размера у WB не бывает
        result.append(["СПП", subject, as_russian_number(percent),
                       "", "", "", today, "", ""])
    return sorted(result, key=lambda row: row[1])


# ---- выкуп по нишам: внешняя аналитика MPSTATS -------------------------------
# Свой процент выкупа у продавца всегда важнее, но полезно видеть, сколько
# выкупают в нише целиком. MPSTATS отдаёт это в карточке товара полем purchase.
API_MPSTATS = "https://mpstats.io/api/wb/get/category?path={path}&d1={start}&d2={end}"
MPSTATS_TIMEOUT_SEC = 90
MPSTATS_GAP_SEC = 1          # не долбим чужой сервис
NICHE_PATHS = {
    "Блузки-Рубашки": "Женщинам/Блузки и рубашки",
    "Брюки": "Женщинам/Брюки",
    "Кардиганы": "Женщинам/Офис/Кардиганы",
    "Костюмы": "Женщинам/Костюмы",
    "Куртки": "Женщинам/Верхняя одежда/Куртка",
    "Лонгсливы": "Женщинам/Лонгсливы",
    "Ночные сорочки": "Женщинам/Одежда для дома/Пижамы и сорочки",
    "Пижамы": "Женщинам/Одежда для дома/Пижамы и сорочки",
    "Платья": "Женщинам/Офис/Платья",
    "Свитеры": "Женщинам/Большие размеры/Пуловеры, кофты, свитеры",
    "Свитшоты": "Женщинам/Толстовки, свитшоты и худи",
    "Толстовки": "Женщинам/Толстовки, свитшоты и худи",
    "Топы": "Женщинам/Футболки и топы",
    "Худи": "Женщинам/Для невысоких/Худи"
}


def mpstats_purchase(path: str, token: str, start: str, end: str):
    """Средний выкуп по нише: (выкуп, выкуп после возвратов) или (None, None)."""
    url = API_MPSTATS.format(path=urllib.parse.quote(path), start=start, end=end)
    request = urllib.request.Request(url, headers={"X-Mpstats-TOKEN": token})
    try:
        with urllib.request.urlopen(request, timeout=MPSTATS_TIMEOUT_SEC) as response:
            rows = (json.load(response) or {}).get("data") or []
    except (urllib.error.HTTPError, urllib.error.URLError, ValueError) as error:
        print(f"  ниша «{path}» не ответила: {error}")
        return None, None
    got = [r.get("purchase") for r in rows if r.get("purchase")]
    after = [r.get("purchase_after_return") for r in rows if r.get("purchase_after_return")]
    if not got:
        return None, None
    return (round(sum(got) / len(got), 1),
            round(sum(after) / len(after), 1) if after else None)


def buyout_rows(today: str) -> list:
    """Строки «Выкуп» по нишам за прошлый месяц. Без токена — пустой список."""
    token = os.environ.get("MPSTATS_TOKEN", "").strip()
    if not token:
        print("MPSTATS_TOKEN не задан — выкуп по нишам пропускаем")
        return []
    start, end = previous_month(date.fromisoformat(today))
    rows = []
    for category, path in sorted(NICHE_PATHS.items()):
        purchase, after = mpstats_purchase(path, token, start, end)
        if purchase is None:
            continue
        # колонка 3 — выкуп, колонка 4 — он же после возвратов (обе были пустые у комиссий)
        rows.append(["Выкуп", category, as_russian_number(purchase),
                     as_russian_number(after), "", "", today, "", ""])
        time.sleep(MPSTATS_GAP_SEC)
    return rows


def income_rows(token: str, today: str) -> list:
    """Доход кабинета с 1 января: база налоговых порогов и НДС.

    Считаем по retail_amount — это то, что заплатил покупатель. Скидку постоянного
    покупателя WB даёт за свой счёт, в базу она не входит: так же считает налог
    книга финансиста, сверено 07.08.2026.
    """
    day = date.fromisoformat(today)
    start, end = date(day.year, 1, 1).isoformat(), today
    sold = back = 0.0
    rrd, pages = 0, 0
    while pages < YEAR_MAX_PAGES:
        rows = call_wb(API_REPORT.format(start=start, end=end, rrd=rrd),
                       token, REPORT_TIMEOUT_SEC) or []
        if not rows:
            break
        for row in rows:
            kind = (row.get("doc_type_name") or "").strip()
            amount = row.get("retail_amount") or 0
            if kind == "Продажа":
                sold += amount
            elif kind == "Возврат":
                back += amount
        rrd = rows[-1].get("rrd_id") or 0
        pages += 1
        if len(rows) < 100000:
            break
        time.sleep(REPORT_PAUSE_SEC)
    if sold <= 0:
        return []
    # колонка 3 — доход с начала года, колонка 4 — возвраты (для проверки)
    return [["Доход", CABINET, as_russian_number(round(sold - back)),
             as_russian_number(round(back)), "", "", today, "", ""]]


def write_table(rows: list) -> None:
    with open(OUT_PATH, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, delimiter="\t")
        writer.writerow(HEADER)
        writer.writerows(rows)


def main() -> None:
    token = os.environ.get("WB_TOKEN", "").strip()
    if not token:
        raise SystemExit("Нет токена: переменная WB_TOKEN пустая.")

    today = date.today().isoformat()
    commissions = commission_rows(token, today)
    warehouses = warehouse_rows(token, today)

    # СПП — приятное дополнение, а не главное: если токену не хватит прав
    # на статистику или WB промолчит, тарифы всё равно обновим
    try:
        spp = spp_rows(token, today)
    except SystemExit as error:
        print(f"СПП не собрана ({error}) — пишем файл без неё")
        spp = []

    # выкуп по нишам — тоже необязательный: нет ключа MPSTATS или сервис молчит,
    # значит в файле просто не будет этих строк, а тарифы обновятся как обычно
    try:
        buyout = buyout_rows(today)
    except Exception as error:                 # noqa: BLE001 — чужой сервис, любая беда
        print(f"выкуп по нишам не собран ({error}) — пишем файл без него")
        buyout = []

    try:
        income = income_rows(token, today)
    except SystemExit as error:
        print(f"доход с начала года не собран ({error})")
        income = []

    write_table(commissions + warehouses + spp + buyout + income)

    for scheme, subject, value, *_ in commissions:
        print(f"{subject} {scheme}: {value}% (на {today})")
    for _, subject, percent, *_ in spp:
        print(f"СПП {subject}: {percent}%")
    for _, cab, money, back, *_ in income:
        print(f"доход кабинета {cab} с начала года: {money} руб (возвратов {back})")
    for _, category, percent, after, *_ in buyout:
        print(f"выкуп в нише {category}: {percent}% (после возвратов {after}%)")
    print(f"складов с тарифами коробов: {len(warehouses)}")
    example = warehouses[0]
    print(f"пример: {example[1]} — хранение {example[2]} + {example[3]}/литр, "
          f"логистика {example[4]} + {example[5]}/литр")
    print(f"записано: {OUT_PATH}")


if __name__ == "__main__":
    sys.exit(main())
