#!/usr/bin/env python3
"""Тянет тарифы WB и кладёт их в tarify.tsv.

Два запроса:
1. Комиссии по предметам — для «Куртки» FBO и FBS.
2. Тарифы коробов — хранение и логистика по каждому складу WB.

Колонки разделяем табуляцией, а не запятой: тогда запятая свободна для самих
чисел, и русская Google Таблица читает «37,5» как число, а не как текст.

Токен берётся из переменной окружения WB_TOKEN. В коде и в файлах его нет.
Если WB не ответил — старый tarify.tsv не трогаем, выходим с ошибкой.
"""

import csv
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date

API_COMMISSION = "https://common-api.wildberries.ru/api/v1/tariffs/commission?locale=ru"
API_BOX = "https://common-api.wildberries.ru/api/v1/tariffs/box?date={day}"
TIMEOUT_SEC = 30
OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tarify.tsv")

HEADER = [
    "Что",
    "Предмет или склад",
    "Комиссия % · Хранение 1-й литр ₽/сутки",
    "Хранение доп. литр ₽/сутки",
    "Логистика 1-й литр ₽",
    "Логистика доп. литр ₽",
    "Обновлено",
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


def explain_http_error(code: int) -> str:
    messages = {
        401: "WB не принял токен. Проверь, что он не отозван и выдан на чтение.",
        403: "Токену не хватает прав на тарифы. Выпусти новый и отметь категорию «Тарифы».",
        429: "WB просит подождать — слишком много запросов. Попробуем в следующий раз.",
    }
    return messages.get(code, f"WB вернул ошибку {code}. Тарифы не обновлены.")


def call_wb(url: str, token: str) -> dict:
    """Один запрос в WB. Кидает понятную ошибку, если не пустило."""
    request = urllib.request.Request(
        url,
        headers={"Authorization": token, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SEC) as response:
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
                         as_russian_number(value), "", "", "", today])
            # старые ключи «FBO»/«FBS» без предмета оставляем для живой
            # таблицы курток — она ищет комиссию именно по ним
            if subject == "Куртки":
                rows.append([scheme, subject,
                             as_russian_number(value), "", "", "", today])
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
        rows.append(["Склад", name, *values, today])
    if not rows:
        raise SystemExit("Ни у одного склада нет тарифов коробов. Тарифы не обновлены.")
    return sorted(rows, key=lambda row: row[1])


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

    write_table(commissions + warehouses)

    for scheme, subject, value, *_ in commissions:
        print(f"{subject} {scheme}: {value}% (на {today})")
    print(f"складов с тарифами коробов: {len(warehouses)}")
    example = warehouses[0]
    print(f"пример: {example[1]} — хранение {example[2]} + {example[3]}/литр, "
          f"логистика {example[4]} + {example[5]}/литр")
    print(f"записано: {OUT_PATH}")


if __name__ == "__main__":
    sys.exit(main())
