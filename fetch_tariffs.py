#!/usr/bin/env python3
"""Тянет комиссии WB по курткам и кладёт их в tarify.csv.

Первый тонкий кусок: только комиссия, только FBO и FBS.
Логистика, хранение и склады — следующим шагом.

Токен берётся из переменной окружения WB_TOKEN. В коде и в файлах его нет.
Если WB не ответил — старый tarify.csv не трогаем, выходим с ошибкой.
"""

import csv
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date

API_COMMISSION = "https://common-api.wildberries.ru/api/v1/tariffs/commission?locale=ru"
TIMEOUT_SEC = 30
OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tarify.csv")

# какие предметы забираем; имена — как их называет сам WB
SUBJECTS = ("Куртки",)

# поле ответа WB → название схемы работы человеческим языком
SCHEMES = {
    "paidStorageKgvp": "FBO",   # товар лежит на складе WB
    "kgvpMarketplace": "FBS",   # товар лежит у продавца
}


def explain_http_error(code: int) -> str:
    messages = {
        401: "WB не принял токен. Проверь, что он не отозван и выдан на чтение.",
        403: "Токену не хватает прав на тарифы. Выпусти новый и отметь категорию «Тарифы».",
        429: "WB просит подождать — слишком много запросов. Попробуем в следующий раз.",
    }
    return messages.get(code, f"WB вернул ошибку {code}. Тарифы не обновлены.")


def fetch_commissions(token: str) -> list:
    """Забирает у WB справочник комиссий. Кидает понятную ошибку, если не пустило."""
    request = urllib.request.Request(
        API_COMMISSION,
        headers={"Authorization": token, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SEC) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        raise SystemExit(explain_http_error(error.code)) from error
    except urllib.error.URLError as error:
        raise SystemExit(f"WB не отвечает: {error.reason}. Тарифы не обновлены.") from error

    report = payload.get("report")
    if not report:
        raise SystemExit("WB ответил, но справочник комиссий пустой. Тарифы не обновлены.")
    return report


def pick_rows(report: list) -> list:
    """Оставляет нужные предметы и разворачивает их по схемам работы."""
    today = date.today().isoformat()
    rows = []
    for item in report:
        if item.get("subjectName") not in SUBJECTS:
            continue
        for field, scheme in SCHEMES.items():
            value = item.get(field)
            if value is None:
                continue
            rows.append([scheme, item["subjectName"], value, today])
    return sorted(rows)


def write_csv(rows: list) -> None:
    with open(OUT_PATH, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Схема", "Предмет", "Комиссия %", "Обновлено"])
        writer.writerows(rows)


def main() -> None:
    token = os.environ.get("WB_TOKEN", "").strip()
    if not token:
        raise SystemExit("Нет токена: переменная WB_TOKEN пустая.")

    rows = pick_rows(fetch_commissions(token))
    if not rows:
        raise SystemExit(f"В справочнике WB не нашлись предметы: {', '.join(SUBJECTS)}")

    write_csv(rows)
    for scheme, subject, commission, updated in rows:
        print(f"{subject} {scheme}: {commission}% (на {updated})")
    print(f"записано: {OUT_PATH}")


if __name__ == "__main__":
    sys.exit(main())
