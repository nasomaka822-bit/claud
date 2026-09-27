#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Мониторинг тендеров на зачистку резервуаров.

Скрипт читает RSS-ленты РосТендера и ЕИС, страницу тендеров ЛУКОЙЛа и закупочные
страницы компаний, отбирает новые тендеры по ключевым словам и региону и
присылает их в Telegram и (или) на почту. Настройки лежат в config.toml рядом
со скриптом; при первом запуске файл создаётся сам.

Команды:
    python tender_monitor.py                 одна проверка (для планировщика задач)
    python tender_monitor.py --loop          проверять постоянно, раз в interval_minutes
    python tender_monitor.py --test          отправить тестовое уведомление
    python tender_monitor.py --get-chat-id   узнать chat_id для Telegram
    python tender_monitor.py --setup-cert    скачать сертификат Минцифры для ЕИС
    python tender_monitor.py --probe         проверить каждый источник и показать, что из него достаётся
    python tender_monitor.py --dry-run       показать подходящие тендеры, ничего не отправляя
    python tender_monitor.py --check "текст" проверить, пройдёт ли название тендера фильтр
    python tender_monitor.py --status        состояние источников

Нужны Python 3.11+ и библиотеки:  pip install requests beautifulsoup4
"""
from __future__ import annotations

import argparse
import atexit
import hashlib
import html as htmllib
import json
import logging
import logging.handlers
import os
import random
import re
import smtplib
import sqlite3
import ssl
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        print("Нужен Python 3.11 или новее (или пакет tomli: pip install tomli).")
        sys.exit(1)

try:
    import requests
    from bs4 import BeautifulSoup, NavigableString, UnicodeDammit
except ModuleNotFoundError:
    print("Не хватает библиотек. Установите их командой:\n    pip install requests beautifulsoup4")
    sys.exit(1)

VERSION = "1.1"
BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.toml"
DB_PATH = BASE_DIR / "tender_monitor.db"
LOG_PATH = BASE_DIR / "tender_monitor.log"
LOCK_PATH = BASE_DIR / "tender_monitor.lock"
BUNDLE_PATH = BASE_DIR / "ca_bundle_ru.pem"
PROBE_REPORT_PATH = BASE_DIR / "probe_report.txt"

BOT_NAME = "TenderMonitor"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    f"(KHTML, like Gecko) Chrome/126.0 Safari/537.36 {BOT_NAME}/{VERSION}"
)
EIS_RSS = "https://zakupki.gov.ru/epz/order/extendedsearch/rss.html"
RU_ZONES = (".ru", ".su", ".xn--p1ai", ".рф")
CERT_URLS = [
    "https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt",
    "https://gu-st.ru/content/lending/russian_trusted_sub_ca_pem.crt",
]
FAIL_ALERT_AFTER = 3        # сколько проверок подряд источник может не отвечать до предупреждения
MAX_SEPARATE_MESSAGES = 8   # больше новых тендеров за раз — одно сводное сообщение
TG_LIMIT = 4000             # запас до лимита Telegram в 4096 символов
DEDUP_DAYS = 21             # один и тот же тендер из разных источников не присылать повторно
MSK = timezone(timedelta(hours=3))

log = logging.getLogger("tender_monitor")

DEFAULT_CONFIG = r'''# Настройки мониторинга тендеров (формат TOML).
# Строки пишутся в кавычках, списки — в квадратных скобках, после # — комментарий.
# Файл перечитывается при каждой проверке, перезапускать скрипт не нужно.

[settings]
# Как часто проверять источники в режиме --loop, минут (не меньше 5).
interval_minutes = 30

# true — присылать только тендеры из ваших регионов (списки ниже).
# Тендеры, у которых регион определить не удалось, приходят всегда.
only_my_regions = true

# Как подписывать тендеры из ваших регионов в уведомлениях.
region_label = "ЮФО"

# Коды регионов. Для закупок ЕИС регион берётся из КПП заказчика в номере ИКЗ.
# 01 Адыгея, 08 Калмыкия, 23 Краснодарский край, 30 Астраханская обл.,
# 34 Волгоградская обл., 61 Ростовская обл., 91 Крым, 92 Севастополь.
region_codes = ["01", "08", "23", "30", "34", "61", "91", "92"]

# Начала слов, по которым регион узнаётся в месте поставки, заказчике или названии.
# Строчными буквами, «ё» пишите как «е».
region_words = [
  "краснодар", "кубан", "адыге", "майкоп",
  "ростов-на-дону", "ростовская обл", "ростовской обл",
  "волгоград", "астрахан", "калмык", "калмыц", "элист",
  "крым", "севастопол", "симферопол", "керч", "евпатори", "феодоси", "ялт", "джанко", "алушт",
  "новороссийск", "туапсе", "сочи", "анап", "геленджик", "армавир", "ейск", "тихорецк",
  "кропоткин", "темрюк", "таман", "славянск-на-кубани", "славянский район", "белореченск",
  "кореновск", "усть-лабинск", "выселк", "абинск", "лабинск", "горячий ключ", "афипск", "ильск",
  "таганрог", "волгодонск", "новочеркасск", "батайск", "сальск", "каменск-шахтинск",
  "миллерово", "морозовск", "цимлянск", "зерноград",
  "камышин", "котлубан", "урюпинск", "фролово", "калач-на-дону", "ахтубинск", "харабал",
]

# Регионы в адресах РосТендера (rostender.info/region/...). По ним точно определяется
# регион тендеров РосТендера. Если меняете region_codes, поменяйте и этот список.
rostender_regions = [
  "krasnodarskij-kraj", "rostovskaya-oblast", "volgogradskaya-oblast", "astrahanskaya-oblast",
  "adygeya-respublika", "kalmykiya-respublika", "krym-respublika", "sevastopol-gorod",
]

# Не присылать тендеры, опубликованные раньше, чем столько дней назад.
max_age_days = 30

# Присылать закупки ЕИС, у которых в ленте нет названия объекта (так бывает
# у малых закупок). ЕИС нашёл ваш запрос в документах такой закупки.
eis_notify_unnamed = true

# При первом запуске прислать одно сообщение со списком подходящих тендеров,
# которые уже есть в лентах. Дальше приходят только новые.
first_run_digest = true

# Соблюдать robots.txt на страницах компаний. RSS-ленты читаются всегда.
respect_robots = true

# Прокси для запросов к источникам, например "http://user:pass@host:3128".
# ЕИС открывается только с российских адресов. Пусто — без прокси.
proxy = ""


[telegram]
enabled = true
# Токен бота от @BotFather, вида "1234567890:AAH...".
bot_token = ""
# Ваш chat_id. Напишите боту любое сообщение и выполните:
#     python tender_monitor.py --get-chat-id
# Для группы: добавьте бота в группу, напишите в ней сообщение и выполните ту же команду.
chat_id = ""
# Прокси только для Telegram. Пусто — без прокси.
proxy = ""


[email]
enabled = false
smtp_host = "smtp.yandex.ru"
smtp_port = 465
# "ssl" для порта 465, "starttls" для порта 587.
security = "ssl"
username = ""
# Для Яндекса и Mail.ru нужен пароль приложения, а не пароль от почты.
password = ""
sender = ""
recipients = []


[certs]
# zakupki.gov.ru и часть сайтов компаний работают на сертификате Минцифры.
# Команда  python tender_monitor.py --setup-cert  скачает его с gu-st.ru
# (официальный адрес со страницы gosuslugi.ru/crt) и положит рядом со скриптом.
# Сертификат применяется только к сайтам в зонах .ru, .su и .рф.
ca_files = ["russian_trusted_root_ca_pem.crt", "russian_trusted_sub_ca_pem.crt"]


# ---------------------------------------------------------------------------
# ЧТО ИСКАТЬ
# Тендер подходит, если в его названии рядом стоят слово-действие и слово-объект
# (между ними не больше max_gap других слов) или встречается одна из фраз words.
# Пишите начала слов строчными буквами: «чистк» найдёт «зачистка», «очистки»,
# «прочистку»; «резервуар» найдёт «резервуаров». Выключить правило: enabled = false.
# ---------------------------------------------------------------------------

[[rules]]
name = "Зачистка резервуаров"
enabled = true
actions = ["чистк", "пропарк", "дегазац", "промывк", "мойк", "откачк", "размыв"]
objects = ["резервуар", "емкост", "цистерн", "автоцистерн", "бензовоз", "топливозаправщик",
           "рвс", "ргс", "жбр", "танк", "бак", "нефтехранилищ", "мазутохранилищ",
           "топливохранилищ", "нефтеловушк", "шламонакопител", "мазут", "подтоварн",
           "нефтесодерж", "нефтебаз", "гсм", "горюч", "средств хранени"]
max_gap = 6

[[rules]]
name = "Нефтешлам и донные отложения"
enabled = true
words = ["нефтешлам", "донных отложений", "донные отложения", "шлам очистки емкостей"]

[[rules]]
name = "Демонтаж и ремонт резервуаров"
enabled = false
actions = ["демонтаж", "ремонт", "реконструкц", "модернизац", "перевооружен", "замен"]
objects = ["резервуар", "рвс", "ргс", "резервуарн", "емкост"]
max_gap = 4

[[rules]]
name = "Градуировка и диагностика резервуаров"
enabled = false
actions = ["градуировк", "калибровк", "поверк", "диагностир", "освидетельствован",
           "дефектоскоп", "толщинометр", "обследован"]
objects = ["резервуар", "рвс", "ргс", "емкост"]
max_gap = 4

# Тендер пропускается, если в названии есть одно из этих слов (начала слов).
# Если не работаете с газом, добавьте "газгольдер", "суг", "пропан".
[exclude]
words = ["септик", "септич", "выгребн", "жбо", "бытовых отход", "хозяйственно-бытов", "фекальн",
         "сооружений канализации", "канализационных очистных", "канализационных насосных",
         "биологической очистки", "благоустройств", "озеленен",
         "противогаз", "молок", "пищев", "бассейн", "аквариум"]


# ---------------------------------------------------------------------------
# ГДЕ ИСКАТЬ
# type = "rss"  — любая RSS-лента (РосТендер, ЕИС, другие агрегаторы);
# type = "eis"  — поиск ЕИС по фразе, адрес RSS-ленты скрипт соберёт сам;
#                 можно указать laws = ["44"] или ["223"] и only_open = false;
# type = "html" — страница со списком тендеров.
#
# Необязательные настройки источника:
#   every_minutes = 120 — проверять источник не чаще, чем раз в столько минут;
#   page_url, pages     — читать несколько страниц списка: page_url — адрес следующих
#                         страниц, где {page} — номер страницы (2, 3, …), а {offset} —
#                         сколько тендеров пропустить (тогда нужен page_size);
#   item_link           — регулярное выражение для ссылок на карточки тендеров: каждая
#                         такая ссылка — отдельный тендер. Пишите его в одинарных кавычках,
#                         например item_link = 'example\.ru/tenders/(\d+)';
#   item_marker         — текст, который есть ровно один раз в каждом тендере на странице,
#                         если у тендеров нет своих ссылок, например item_marker = "Прием заявок до";
#   require = [...]     — брать только тендеры, где есть одно из этих слов
#                         (например, названия нужных дочерних обществ).
# Для rostender.info, ahstep.ru, lukoil.ru и zakupki.eurochem.ru скрипт уже знает,
# как устроены страницы. Проверить все источники и посмотреть, что из них достаётся:
#     python tender_monitor.py --probe
# Выключить источник: enabled = false. Новый источник при первом чтении
# только запоминает текущие тендеры и присылает о них одну сводку.
# ---------------------------------------------------------------------------

# РосТендер собирает закупки с ЕИС, ЭТП ГПБ, ТЭК-Торга, Сбербанк-АСТ, B2B-Center,
# РТС-тендер и сайтов компаний. В RSS-ленте категории лежат только тендеры
# последних дней, поэтому страница категории читается дополнительно, раз в 2 часа.
[[sources]]
name = "РосТендер: зачистка резервуаров"
type = "rss"
url = "https://rostender.info/rss-category-839.xml"

[[sources]]
name = "РосТендер: зачистка резервуаров (страница)"
type = "html"
url = "https://rostender.info/category/tendery-na-zachistku-rezervuarov"
every_minutes = 120

[[sources]]
name = "РосТендер: зачистка емкостей"
type = "rss"
url = "https://rostender.info/rss-category-1748.xml"

[[sources]]
name = "РосТендер: зачистка емкостей (страница)"
type = "html"
url = "https://rostender.info/category/tendery-zachistka-emkostej"
every_minutes = 120

[[sources]]
name = "РосТендер: утилизация нефтешламов"
type = "rss"
url = "https://rostender.info/rss-category-1303.xml"

[[sources]]
name = "РосТендер: утилизация нефтешламов (страница)"
type = "html"
url = "https://rostender.info/category/tendery-utilizaciya-nefteshlamov"
every_minutes = 120

[[sources]]
name = "РосТендер: зачистка нефтепроводов"
type = "rss"
url = "https://rostender.info/rss-category-1790.xml"

[[sources]]
name = "РосТендер: демонтаж резервуаров"
type = "rss"
url = "https://rostender.info/rss-category-3469.xml"

[[sources]]
name = "РосТендер: закупки АЗС"
type = "rss"
url = "https://rostender.info/rss-category-2563.xml"

# RSS-лента этой отрасли не обновляется с 2025 года, а страница живая.
[[sources]]
name = "РосТендер: отрасль «Промышленные резервуары и ёмкости»"
type = "html"
url = "https://rostender.info/tendery-promyshlennye-rezervuary-i-emkosti-remont-i-obslujivanie"
every_minutes = 60

# Закупки агрохолдингов со всей России, которые собирает РосТендер.
[[sources]]
name = "РосТендер: агрохолдинги"
type = "rss"
url = "https://rostender.info/rss-category-1537.xml"

[[sources]]
name = "РосТендер: агрохолдинги (страница)"
type = "html"
url = "https://rostender.info/category/tendery-agroholdingov"
every_minutes = 120

# Сайт «Юг Руси» строит список закупок JavaScript'ом, поэтому берём его через РосТендер.
[[sources]]
name = "РосТендер: МЭЗ «Юг Руси»"
type = "rss"
url = "https://rostender.info/rss-category-1012.xml"

[[sources]]
name = "РосТендер: МЭЗ «Юг Руси» (страница)"
type = "html"
url = "https://rostender.info/category/tendery-mez-yug-rusi"
every_minutes = 120

# ЕИС (zakupki.gov.ru): 44-ФЗ и 223-ФЗ, этап подачи заявок. Сюда попадают и закупки
# госкомпаний по 223-ФЗ: Роснефть, Транснефть, Газпром, РЖД, порты.
# Нужны сертификат Минцифры (--setup-cert) и российский IP-адрес.
[[sources]]
name = "ЕИС: зачистка резервуаров"
type = "eis"
query = "зачистка резервуаров"

[[sources]]
name = "ЕИС: очистка резервуаров"
type = "eis"
query = "очистка резервуаров"

[[sources]]
name = "ЕИС: зачистка емкостей"
type = "eis"
query = "зачистка емкостей"

[[sources]]
name = "ЕИС: очистка емкостей"
type = "eis"
query = "очистка емкостей"

[[sources]]
name = "ЕИС: зачистка цистерн"
type = "eis"
query = "зачистка цистерн"

[[sources]]
name = "ЕИС: пропарка резервуаров"
type = "eis"
query = "пропарка резервуаров"

[[sources]]
name = "ЕИС: дегазация резервуаров"
type = "eis"
query = "дегазация резервуаров"

[[sources]]
name = "ЕИС: нефтешлам"
type = "eis"
query = "нефтешлам"

# Страницы закупок компаний.
# ЛУКОЙЛ сортирует список по сроку подачи заявок, а не по дате публикации:
# новый тендер может оказаться на любой странице. Поэтому скрипт читает все
# страницы (около 60), но не чаще раза в 4 часа.
[[sources]]
name = "ЛУКОЙЛ: тендеры группы"
type = "html"
url = "https://lukoil.ru/Company/Tendersandauctions/Tenders/TendersofLukoilgroup"
page_url = "https://lukoil.ru/Company/Tendersandauctions/Tenders/TendersofLukoilgroup?take=10&skip={offset}"
page_size = 10
pages = 200
every_minutes = 240

# «Степь» тоже сортирует по сроку подачи; открытые закупки помещаются на 2 страницы.
[[sources]]
name = "Агрохолдинг «Степь»"
type = "html"
url = "https://www.ahstep.ru/tender"
page_url = "https://www.ahstep.ru/tender?page={page}"
pages = 2

[[sources]]
name = "Агрокомплекс им. Ткачёва"
type = "html"
url = "https://tender.zao-agrokomplex.ru/purchase/"

[[sources]]
name = "Астон"
type = "html"
url = "https://aston.ru/tenders/current-purchases/"

[[sources]]
name = "ОТЭКО (Таманьнефтегаз)"
type = "html"
url = "https://www.oteko.ru/suppliers/what_are_we_buying/"

[[sources]]
name = "НМТП"
type = "html"
url = "https://www.nmtp.info/holding/announcement/"

[[sources]]
name = "Черноморнефтегаз"
type = "html"
url = "https://gas.crimea.ru/gosudarstvennye-zakupki"

# Портал закупок ЕвроХима по всей России; берём только заводы в ЮФО:
# ЕвроХим-БМУ (Белореченск) и ЕвроХим-ВолгаКалий (Котельниково).
[[sources]]
name = "ЕвроХим: БМУ и ВолгаКалий"
type = "html"
url = "https://zakupki.eurochem.ru/aktualnye-zakupki1"
page_url = "https://zakupki.eurochem.ru/aktualnye-zakupki1?cat_page={page}"
pages = 3
require = ["бму", "белореченск", "волгакалий", "волгасервис", "котельников"]

[[sources]]
name = "Зерновой терминал КСК (Новороссийск)"
type = "html"
url = "https://www.gt-ksk.com/about/tenders/"
every_minutes = 120
'''


# ---------------------------------------------------------------------------
# Общие вспомогательные функции
# ---------------------------------------------------------------------------

class ConfigError(Exception):
    """Ошибка в config.toml."""


class FetchError(Exception):
    """Источник не ответил или ответил не тем, что ожидалось."""


def norm(text: str) -> str:
    """Нижний регистр, ё→е, неразрывные пробелы и лишние пробелы убраны."""
    text = (text or "").replace(" ", " ").replace("ё", "е").replace("Ё", "Е")
    return re.sub(r"\s+", " ", text).strip().lower()


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace(" ", " ")).strip()


def sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def truncate(text: str, limit: int) -> str:
    text = clean(text)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def esc(text: str) -> str:
    return htmllib.escape(text or "", quote=False)


def esc_attr(text: str) -> str:
    return htmllib.escape(text or "", quote=True)


def short_err(exc: object) -> str:
    text = clean(str(exc)) or exc.__class__.__name__
    return truncate(text, 300)


HTTP_HINTS = {
    401: "сайт требует авторизацию",
    403: "сайт закрыл доступ для автоматических запросов",
    404: "страница не найдена, возможно, адрес изменился",
    410: "страница удалена, возможно, адрес изменился",
    429: "сайт просит обращаться реже",
}


def describe_status(code: int) -> str:
    hint = HTTP_HINTS.get(code) or ("ошибка на стороне сайта" if code >= 500 else "")
    return f"HTTP {code}" + (f": {hint}" if hint else "")


def describe_request_error(exc: BaseException) -> str:
    """Короткое понятное описание сетевой ошибки вместо длинного текста requests."""
    text = str(exc)
    low = text.lower()
    if isinstance(exc, requests.exceptions.ProxyError):
        m = re.search(r"Tunnel connection failed: ([^'\")]+)", text)
        return "прокси не пропускает соединение" + (f" ({clean(m.group(1))})" if m else "")
    if isinstance(exc, requests.exceptions.Timeout):
        return "сайт не ответил вовремя"
    if isinstance(exc, requests.exceptions.ConnectionError):
        if any(s in low for s in ("nameresolution", "getaddrinfo", "name or service not known",
                                  "nodename nor servname", "name resolution", "11001")):
            return "адрес сайта не найден: нет интернета или сайт перестал работать"
        if "refused" in low or "10061" in low:
            return "сайт отклонил соединение"
        if any(s in low for s in ("reset", "aborted", "remotedisconnected", "10054", "eof occurred")):
            return "сайт оборвал соединение (так бывает, когда сайт не пускает зарубежные IP-адреса или роботов)"
        return "нет соединения с сайтом"
    return short_err(exc)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def is_ru_zone(host: str) -> bool:
    host = (host or "").lower().rstrip(".")
    return host.endswith(RU_ZONES)


def strip_html(text: str) -> str:
    """HTML из описания ленты → простой текст с переносами строк."""
    if not text:
        return ""
    for _ in range(3):
        if "<" not in text and "&lt;" not in text and "&amp;" not in text:
            break
        text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>", "\n", text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = htmllib.unescape(text)
    lines = [clean(line) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def parse_kv(text: str) -> dict[str, str]:
    """Строки вида «Ключ: значение» → словарь (ключи без изменений)."""
    result: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = clean(key), clean(value).rstrip(";").strip()
        if key and key not in result and len(key) <= 80:
            result[key] = value
    return result


def kv_get(kv: dict[str, str], *needles: str) -> str:
    for key, value in kv.items():
        low = norm(key)
        if any(n in low for n in needles):
            return value
    return ""


def format_price(raw: str) -> str:
    if not raw:
        return ""
    m = re.search(r"\d[\d\s ]*(?:[.,]\d{1,2})?", raw)
    if not m:
        return ""
    digits = re.sub(r"[\s ]", "", m.group(0)).replace(",", ".")
    try:
        value = float(digits)
    except ValueError:
        return ""
    if value <= 0:
        return ""
    return f"{value:,.0f}".replace(",", " ") + " ₽"


def parse_date(raw: str) -> datetime | None:
    raw = clean(raw)
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError, IndexError):
        pass
    m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", raw)
    if m:
        try:
            return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)), tzinfo=MSK)
        except ValueError:
            return None
    m = re.search(r"\b(\d{2})\.(\d{2})\.(\d{2})\b", raw)  # 25.09.26
    if m:
        try:
            return datetime(2000 + int(m.group(3)), int(m.group(2)), int(m.group(1)), tzinfo=MSK)
        except ValueError:
            return None
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


DEADLINE_RE = re.compile(
    r"(?i)(?:при[её]м\w*|подач\w*|окончани\w*)[^0-9]{0,40}?(\d{2}\.\d{2}\.\d{4}(?:\s+\d{1,2}:\d{2})?)"
)
PRICE_RE = re.compile(
    r"(?i)(?:цена|нмц\w*|сумма)[^0-9]{0,30}(\d[\d  ]*(?:[.,]\d{1,2})?)\s*(?:руб|₽|р\.)"
)
CUSTOMER_RE = re.compile(r"(?i)заказчик(?:\(и\))?\s*:\s*(.{3,160}?)(?=\s+(?:организатор|документы|при[её]м|окончани|начальн|цена|место)|$)")


# ---------------------------------------------------------------------------
# Данные
# ---------------------------------------------------------------------------

@dataclass
class Item:
    uid: str
    title: str
    link: str
    source: str = ""
    text: str = ""            # дополнительный текст для поиска слов (предмет закупки)
    context: str = ""         # строка списка целиком: для региона и фильтра require, не для поиска слов
    place: str = ""
    customer: str = ""
    price: str = ""
    deadline: str = ""
    published: str = ""       # ISO-строка или пусто
    region_code: str = ""     # из ИКЗ (ЕИС)
    unnamed: bool = False     # ЕИС не прислал название объекта закупки
    query: str = ""           # поисковая фраза ленты ЕИС
    rules: list[str] = field(default_factory=list)
    region: str = "unknown"   # my / other / unknown

    def published_dt(self) -> datetime | None:
        if not self.published:
            return None
        try:
            return datetime.fromisoformat(self.published)
        except ValueError:
            return None

    def fingerprint(self) -> str:
        if self.unnamed:
            return "uid:" + self.uid
        words = re.sub(r"[^\w]+", " ", norm(self.title)).strip()
        return "t:" + sha(words[:160])


@dataclass
class Source:
    name: str
    type: str
    url: str
    enabled: bool = True
    format: str = "auto"      # как разбирать RSS: auto, eis, rostender, generic
    page_url: str = ""        # адрес следующих страниц с {page} или {offset}
    pages: int = 1
    page_size: int = 10
    every_minutes: int = 0    # проверять не чаще, чем раз в столько минут
    item_link: str = ""       # регулярное выражение для ссылок на карточки тендеров
    item_marker: str = ""     # текст, который есть один раз в каждом тендере на странице
    require: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.type}|{self.url}"

    def page_urls(self) -> list[str]:
        urls = [self.url]
        if self.page_url and self.pages > 1:
            for n in range(2, self.pages + 1):
                offset = (n - 1) * self.page_size
                urls.append(self.page_url.replace("{page}", str(n)).replace("{offset}", str(offset)))
        return urls


def _int_option(raw: dict, name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(raw.get(name, default))
    except (TypeError, ValueError):
        log.warning("Настройка %s = %r не число, беру %d", name, raw.get(name), default)
        return default
    return max(low, min(value, high))


def eis_url(query: str, laws: list[str], only_open: bool) -> str:
    params = {
        "searchString": query,
        "morphology": "on",
        "pageNumber": "1",
        "sortBy": "UPDATE_DATE",
        "sortDirection": "false",
    }
    if "44" in laws:
        params["fz44"] = "on"
    if "223" in laws:
        params["fz223"] = "on"
    if only_open:
        params["af"] = "on"
    return EIS_RSS + "?" + urlencode(params)


def load_sources(cfg: dict) -> list[Source]:
    sources: list[Source] = []
    for raw in cfg.get("sources", []):
        kind = str(raw.get("type", "rss")).lower().strip()
        name = str(raw.get("name") or raw.get("url") or raw.get("query") or "Источник")
        enabled = bool(raw.get("enabled", True))
        fmt = str(raw.get("format", "auto")).lower().strip()
        if kind == "eis":
            query = str(raw.get("query", "")).strip()
            if not query:
                log.warning("Источник «%s»: не указан query, пропускаю", name)
                continue
            laws = [str(x) for x in raw.get("laws", ["44", "223"])]
            url = eis_url(query, laws, bool(raw.get("only_open", True)))
            src = Source(name, "rss", url, enabled, "eis")
        elif kind in ("rss", "html"):
            url = str(raw.get("url", "")).strip()
            if not url.startswith(("http://", "https://")):
                log.warning("Источник «%s»: неверный url, пропускаю", name)
                continue
            src = Source(name, kind, url, enabled, fmt)
            src.page_url = str(raw.get("page_url", "")).strip()
            src.pages = _int_option(raw, "pages", 1, 1, 200)
            src.page_size = _int_option(raw, "page_size", 10, 1, 1000)
            src.item_marker = str(raw.get("item_marker", "")).strip()
            src.item_link = str(raw.get("item_link", "")).strip()
            if src.item_link:
                try:
                    re.compile(src.item_link)
                except re.error as exc:
                    log.warning("Источник «%s»: ошибка в item_link (%s), разбираю страницу без него", name, exc)
                    src.item_link = ""
            require = raw.get("require", [])
            src.require = [norm(str(w)) for w in (require if isinstance(require, list) else [require])
                           if str(w).strip()]
        else:
            log.warning("Источник «%s»: неизвестный type = %r, пропускаю", name, kind)
            continue
        src.every_minutes = _int_option(raw, "every_minutes", 0, 0, 7 * 24 * 60)
        sources.append(src)
    return sources


# ---------------------------------------------------------------------------
# Отбор по словам и регионам
# ---------------------------------------------------------------------------

def _stem_re(stem: str) -> str:
    stem = norm(stem)
    if len(stem) <= 3:
        # короткие основы (рвс, бак) — только целым словом с коротким окончанием
        return re.escape(stem) + r"[а-яa-z]{0,3}\b"
    return re.escape(stem) + r"\w*"


class Matcher:
    def __init__(self, cfg: dict):
        self.rules: list[tuple[str, list[re.Pattern]]] = []
        for rule in cfg.get("rules", []):
            if not rule.get("enabled", True):
                continue
            name = str(rule.get("name", "Правило"))
            patterns: list[re.Pattern] = []
            gap = max(0, min(int(rule.get("max_gap", 6)), 15))
            actions = [norm(str(a)) for a in rule.get("actions", []) if str(a).strip()]
            objects = [str(o) for o in rule.get("objects", []) if str(o).strip()]
            if actions and objects:
                acts = "|".join(re.escape(a) for a in actions)
                objs = "|".join(_stem_re(o) for o in objects)
                patterns.append(re.compile(rf"(?:{acts})\w*(?:\W+\w+){{0,{gap}}}?\W+(?:{objs})"))
                patterns.append(re.compile(rf"(?<!\w)(?:{objs})(?:\W+\w+){{0,{gap}}}?\W+\w*(?:{acts})"))
            for phrase in rule.get("words", []):
                if str(phrase).strip():
                    patterns.append(re.compile(re.escape(norm(str(phrase)))))
            if patterns:
                self.rules.append((name, patterns))
        self.exclude = [norm(str(w)) for w in cfg.get("exclude", {}).get("words", []) if str(w).strip()]

    def excluded_by(self, texts: list[str]) -> str:
        joined = " ".join(texts)
        for word in self.exclude:
            if re.search(r"(?<!\w)" + re.escape(word), joined):
                return word
        return ""

    def match(self, fields: list[str]) -> list[str]:
        texts = [norm(f) for f in fields if f and f.strip()]
        if not texts or self.excluded_by(texts):
            return []
        hits = []
        for name, patterns in self.rules:
            if any(p.search(t) for p in patterns for t in texts):
                hits.append(name)
        return hits


ROSTENDER_REGION_RE = re.compile(r"rostender\.info/region/([a-z0-9-]+)")


class RegionFilter:
    def __init__(self, settings: dict):
        self.codes = {str(c).zfill(2) for c in settings.get("region_codes", [])}
        words = [norm(str(w)) for w in settings.get("region_words", []) if str(w).strip()]
        self.pattern = re.compile("|".join(r"(?<!\w)" + re.escape(w) for w in words)) if words else None
        self.slugs = {str(s).strip().lower() for s in settings.get("rostender_regions", []) if str(s).strip()}

    def classify(self, item: Item) -> str:
        # у РосТендера регион записан в адресе тендера: /region/rostovskaya-oblast/... — это точнее слов
        # («Волгоградский проспект» в Москве не должен сделать тендер волгоградским)
        m = ROSTENDER_REGION_RE.search(item.link or "")
        if m and self.slugs:
            return "my" if m.group(1) in self.slugs else "other"
        blob = norm(" ".join([item.place, item.customer, item.title, item.text, item.context]))
        if self.pattern and self.pattern.search(blob):
            return "my"
        if item.region_code:
            return "my" if item.region_code in self.codes else "other"
        if item.place.strip():
            return "other"
        return "unknown"


# ---------------------------------------------------------------------------
# Сертификат Минцифры
# ---------------------------------------------------------------------------

def _pem_from_file(path: Path) -> str:
    data = path.read_bytes()
    if b"-----BEGIN CERTIFICATE-----" in data:
        return data.decode("ascii", errors="ignore")
    return ssl.DER_cert_to_PEM_cert(data)


def prepare_bundle(cfg: dict) -> str | None:
    """Собирает файл доверенных сертификатов: стандартные + сертификаты Минцифры."""
    names = cfg.get("certs", {}).get("ca_files", [])
    files = [BASE_DIR / str(n) for n in names if (BASE_DIR / str(n)).is_file()]
    if not files:
        return None
    try:
        import certifi
        base_path = Path(certifi.where())
    except Exception:  # certifi ставится вместе с requests, но на всякий случай
        base_path = None
    sources_mtime = max([f.stat().st_mtime for f in files] + ([base_path.stat().st_mtime] if base_path else []))
    if BUNDLE_PATH.is_file() and BUNDLE_PATH.stat().st_mtime >= sources_mtime:
        return str(BUNDLE_PATH)
    parts = []
    if base_path:
        parts.append(base_path.read_text(encoding="ascii", errors="ignore"))
    for f in files:
        try:
            parts.append(_pem_from_file(f))
        except Exception as exc:
            log.warning("Не удалось прочитать сертификат %s: %s", f.name, short_err(exc))
    bundle = "\n".join(p.strip() for p in parts if p.strip()) + "\n"
    BUNDLE_PATH.write_text(bundle, encoding="ascii")
    try:
        ssl.create_default_context(cafile=str(BUNDLE_PATH))
    except ssl.SSLError as exc:
        log.warning("Файл сертификатов собран с ошибкой (%s), использую стандартные", short_err(exc))
        return None
    return str(BUNDLE_PATH)


# ---------------------------------------------------------------------------
# Загрузка страниц
# ---------------------------------------------------------------------------

class Robots:
    """robots.txt по RFC 9309: группы агентов, «*» и «$» в путях, побеждает самое длинное правило.
    Стандартный urllib.robotparser звёздочки не понимает, а РосТендер ими пользуется."""

    def __init__(self, text: str, agent: str = BOT_NAME):
        groups: list[tuple[list[str], list[tuple[bool, str]]]] = []
        agents: list[str] = []
        rules: list[tuple[bool, str]] = []
        in_agents = False
        for raw_line in text.lstrip("﻿").splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            name, _, value = line.partition(":")
            name, value = name.strip().lower(), value.strip()
            if name == "user-agent":
                if not in_agents:
                    agents, rules = [], []
                    groups.append((agents, rules))
                agents.append(value.lower())
                in_agents = True
            elif name in ("allow", "disallow"):
                in_agents = False
                if groups:
                    rules.append((name == "allow", value))
        token = agent.lower()
        chosen = [r for a, r in groups if token in a]
        if not chosen:
            chosen = [r for a, r in groups if "*" in a]
        self.rules: list[tuple[bool, int, re.Pattern]] = []
        for group_rules in chosen:
            for allow, pattern in group_rules:
                if not pattern:
                    continue  # пустой Disallow ничего не запрещает
                anchored = pattern.endswith("$")
                body = pattern[:-1] if anchored else pattern
                regex = "".join(".*" if ch == "*" else re.escape(ch) for ch in body)
                self.rules.append((allow, len(pattern), re.compile(regex + ("$" if anchored else ""))))

    def allowed(self, url: str) -> bool:
        parts = urlparse(url)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        best_len, best_allow = -1, True
        for allow, length, regex in self.rules:
            if regex.match(path) and (length > best_len or (length == best_len and allow)):
                best_len, best_allow = length, allow
        return best_allow


class Http:
    def __init__(self, cfg: dict):
        settings = cfg.get("settings", {})
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5",
        })
        proxy = str(settings.get("proxy", "")).strip()
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})
        self.bundle = prepare_bundle(cfg)
        self.respect_robots = bool(settings.get("respect_robots", True))
        self.robots: dict[str, Robots | None] = {}
        self.last_hit: dict[str, float] = {}
        self.down: dict[str, str] = {}  # сайты, которые не ответили в эту проверку

    def verify_for(self, url: str):
        host = urlparse(url).hostname or ""
        return self.bundle if (self.bundle and is_ru_zone(host)) else True

    def _pace(self, url: str) -> None:
        host = urlparse(url).hostname or ""
        wait = self.last_hit.get(host, 0) + 1.5 - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self.last_hit[host] = time.monotonic()

    def check_robots(self, url: str) -> None:
        """Правила robots.txt по RFC 9309. Бросает FetchError, если страницу читать нельзя."""
        parts = urlparse(url)
        base = f"{parts.scheme}://{parts.netloc}"
        if base not in self.robots:
            try:
                self._pace(base)
                resp = self.session.get(base + "/robots.txt", timeout=(10, 20), verify=self.verify_for(base))
            except requests.RequestException as exc:
                # сайт не ответил: настоящую причину (сертификат, блокировка, сеть) покажет запрос страницы
                log.debug("robots.txt %s: %s", base, exc)
                return
            if resp.status_code >= 500:
                raise FetchError(f"robots.txt сайта временно недоступен ({describe_status(resp.status_code)}), "
                                 "страница пропущена до следующей проверки")
            # 4xx: файла нет или он закрыт, ограничений нет
            text = resp.content.decode("utf-8-sig", errors="replace")
            self.robots[base] = Robots(text) if resp.status_code < 400 else None
        robots = self.robots[base]
        if robots is not None and not robots.allowed(url):
            raise FetchError("robots.txt сайта запрещает автоматический доступ к этой странице")

    def get(self, url: str, *, check_robots: bool = False) -> requests.Response:
        host = urlparse(url).hostname or ""
        if host in self.down:
            # сайт уже не ответил в эту проверку: не ждём повторно на каждой его ленте
            raise FetchError(self.down[host])
        if check_robots and self.respect_robots:
            self.check_robots(url)
        last = ""
        network_error = False
        attempts = 3
        for attempt in range(attempts):
            self._pace(url)
            try:
                resp = self.session.get(url, timeout=(15, 45), verify=self.verify_for(url))
            except requests.exceptions.SSLError as exc:
                if "certificate verify failed" in str(exc).lower():
                    if is_ru_zone(host) and not self.bundle:
                        raise FetchError(
                            "сайт использует сертификат Минцифры. Выполните: python tender_monitor.py --setup-cert"
                        ) from exc
                    raise FetchError(f"ошибка сертификата: {short_err(exc)}") from exc
                # обрыв соединения во время TLS-рукопожатия (так часто блокируют зарубежные IP) — не сертификат
                log.debug("%s: %s", url, exc)
                last = describe_request_error(exc)
                network_error = True
                if attempt < attempts - 1:
                    time.sleep(2 * (attempt + 1))
                continue
            except requests.RequestException as exc:
                log.debug("%s: %s", url, exc)
                last = describe_request_error(exc)
                network_error = True
                if attempt < attempts - 1:
                    time.sleep(2 * (attempt + 1))
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                last = describe_status(resp.status_code)
                network_error = False
                if attempt < attempts - 1:
                    retry_after = resp.headers.get("Retry-After", "")
                    time.sleep(min(int(retry_after), 60) if retry_after.isdigit() else 3 * (attempt + 1))
                continue
            if resp.status_code >= 400:
                raise FetchError(describe_status(resp.status_code))
            return resp
        if network_error:
            self.down[host] = last
        raise FetchError(last or "нет ответа от сайта")


# ---------------------------------------------------------------------------
# Разбор RSS
# ---------------------------------------------------------------------------

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _decode_xml(content: bytes) -> str:
    m = re.match(rb"\s*<\?xml[^>]*encoding=[\"']([\w.-]+)[\"']", content)
    encodings = [m.group(1).decode("ascii")] if m else []
    encodings += ["utf-8", "cp1251"]
    for enc in encodings:
        try:
            return content.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return content.decode("utf-8", errors="replace")


def _entry_from_element(el: ET.Element) -> dict[str, str]:
    entry = {"title": "", "link": "", "description": "", "date": "", "guid": ""}
    for child in el:
        name = _local(child.tag)
        text = "".join(child.itertext()).strip()
        if name == "title" and not entry["title"]:
            entry["title"] = text
        elif name == "link":
            href = child.get("href")
            rel = child.get("rel", "alternate")
            if href and rel == "alternate" and not entry["link"]:
                entry["link"] = href
            elif text and not entry["link"]:
                entry["link"] = text
        elif name in ("description", "summary", "content", "encoded") and not entry["description"]:
            entry["description"] = text
        elif name in ("pubdate", "updated", "published", "date") and not entry["date"]:
            entry["date"] = text
        elif name in ("guid", "id") and not entry["guid"]:
            entry["guid"] = text
    return entry


def _entries_by_regex(text: str) -> list[dict[str, str]]:
    def tag(block: str, name: str) -> str:
        m = re.search(rf"<{name}\b[^>]*>(.*?)</{name}>", block, re.S | re.I)
        if not m:
            return ""
        value = m.group(1).strip()
        cdata = re.match(r"<!\[CDATA\[(.*)\]\]>$", value, re.S)
        return cdata.group(1) if cdata else htmllib.unescape(value)

    entries = []
    for block in re.findall(r"<(?:item|entry)\b[^>]*>(.*?)</(?:item|entry)>", text, re.S | re.I):
        entries.append({
            "title": strip_html(tag(block, "title")),
            "link": tag(block, "link"),
            "description": tag(block, "description") or tag(block, "summary"),
            "date": tag(block, "pubDate") or tag(block, "updated") or tag(block, "dc:date"),
            "guid": tag(block, "guid") or tag(block, "id"),
        })
    return entries


def parse_feed(content: bytes) -> list[dict[str, str]]:
    head = content[:1500].lstrip().lower()
    if not head:
        raise FetchError("источник вернул пустой ответ")
    if not head.startswith(b"<?xml") and (b"<!doctype html" in head or head.startswith(b"<html")):
        raise FetchError("вместо RSS пришла веб-страница: возможно, адрес ленты изменился")
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        text = _decode_xml(content)
        text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text)
        text = re.sub(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)", "&amp;", text)
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            entries = _entries_by_regex(text)
            if not entries and "<rss" not in text[:2000].lower() and "<feed" not in text[:2000].lower():
                raise FetchError("ответ не похож на RSS-ленту")
            return entries
    if _local(root.tag) not in ("rss", "feed", "rdf"):
        raise FetchError(f"ответ не похож на RSS-ленту (корневой элемент <{_local(root.tag)}>)")
    return [_entry_from_element(el) for el in root.iter() if _local(el.tag) in ("item", "entry")]


def _rostender_item(src: Source, entry: dict, link: str) -> Item:
    desc = strip_html(entry["description"])
    if "\n" not in desc:
        # описание одной строкой: «Предмет тендера: …; Место поставки: …; Цена: …»
        desc = re.sub(r";\s*(?=[А-ЯЁA-Z][^:;]{2,40}:)", "\n", desc)
    kv = parse_kv(desc)
    subject = kv_get(kv, "предмет")
    m = re.search(r"/(?:tender/)?(\d{6,})(?:[-/?#]|$)", link)
    uid = f"rostender:{m.group(1)}" if m else "rt:" + sha(link or entry["title"])
    rss_title = clean(entry["title"])
    # РосТендер обрезает заголовок в ленте («Оказание услуг по зачистке и...»), полное название — в описании
    title = subject if subject and (not rss_title or _is_cut(rss_title, subject)) else (rss_title or subject)
    return Item(
        uid=uid, title=title, link=link, source=src.name,
        text=subject if norm(subject) != norm(title) else "",
        place=kv_get(kv, "место поставки", "регион"),
        price=format_price(kv_get(kv, "цена")),
        published=_iso(parse_date(entry["date"])),
    )


def _eis_item(src: Source, entry: dict, link: str) -> Item:
    desc = strip_html(entry["description"])
    if "Найденный результат" in desc:
        desc = desc.split("Найденный результат", 1)[1]
    kv = parse_kv(desc)
    obj = kv_get(kv, "наименование объекта закупки", "наименование закупки", "предмет")
    unnamed = norm(obj) in ("", "null", "-", "нет")
    customer = kv_get(kv, "наименование заказчика", "заказчик", "организация")
    ikz = re.sub(r"\D", "", kv_get(kv, "икз", "идентификационный код"))
    region_code = ikz[13:15] if len(ikz) >= 22 else ""
    reg = parse_qs(urlparse(link).query).get("regNumber", [""])[0]
    uid = "eis:" + (reg or ikz or sha(link or entry["title"]))
    published = parse_date(kv_get(kv, "размещено")) or parse_date(entry["date"])
    query = parse_qs(urlparse(src.url).query).get("searchString", [""])[0]
    return Item(
        uid=uid, title=clean(entry["title"]) if unnamed else clean(obj), link=link, source=src.name,
        customer=customer, price=format_price(kv_get(kv, "начальная цена", "цена")),
        published=_iso(published), region_code=region_code, unnamed=unnamed, query=query,
    )


def _generic_rss_item(src: Source, entry: dict, link: str) -> Item:
    desc = strip_html(entry["description"])
    uid = "rss:" + sha(entry["guid"] or link or entry["title"])
    return Item(
        uid=uid, title=clean(entry["title"]) or truncate(desc, 200), link=link, source=src.name,
        text=truncate(desc, 1500), price=format_price(_first(PRICE_RE, desc)),
        deadline=_first(DEADLINE_RE, desc), published=_iso(parse_date(entry["date"])),
    )


def _is_cut(short: str, full: str) -> bool:
    """Заголовок обрезан многоточием, а полный текст начинается так же."""
    stem = re.sub(r"\s*(?:\.\.\.|…)\s*$", "", short)
    return stem != short and norm(full).startswith(norm(stem)[:60])


def _first(pattern: re.Pattern, text: str) -> str:
    m = pattern.search(text or "")
    return clean(m.group(1)) if m else ""


def _iso(dt: datetime | None) -> str:
    return dt.isoformat() if dt else ""


def items_from_feed(src: Source, content: bytes) -> list[Item]:
    fmt = src.format
    if fmt not in ("eis", "rostender", "generic"):
        host = (urlparse(src.url).hostname or "").lower()
        if host.endswith("zakupki.gov.ru"):
            fmt = "eis"
        elif host.endswith(("rostender.info", "komtender.ru")):
            fmt = "rostender"
        else:
            fmt = "generic"
    parser = {"eis": _eis_item, "rostender": _rostender_item}.get(fmt, _generic_rss_item)
    items = []
    for entry in parse_feed(content):
        link = urljoin(src.url, entry["link"].strip()) if entry["link"] else src.url
        items.append(parser(src, entry, link))
    return items


# ---------------------------------------------------------------------------
# Разбор HTML-страниц
# ---------------------------------------------------------------------------

HEADINGS = ["h1", "h2", "h3", "h4", "h5", "h6"]
VOLATILE_RE = re.compile(
    r"(?i)(?:осталось|остаётся|остается)\s+\d+\s*\w*|\d+\s*(?:дн\w*|час\w*|мин\w*)\s+назад|\bсегодня\b|\bвчера\b"
    r"|(?:просмотр\w*|views?)\s*:?\s*\d+"
)
# Как устроены страницы известных сайтов (проверено в сентябре 2026):
#  - РосТендер: у каждого тендера ссылка /region/<регион>/<город>/<номер>-tender-…, 20 тендеров на странице;
#  - «Степь»: ссылки /tenders/tender<номер>, название закупки — текст ссылки;
#  - ЕвроХим: каждая закупка ведёт на свою карточку на b2b-center.ru;
#  - ЛУКОЙЛ: у тендеров нет своих ссылок, но в каждом один раз написано «Прием заявок до».
HTML_PRESETS: dict[str, dict[str, str]] = {
    "rostender.info": {"item_link": r"rostender\.info/region/[a-z0-9-]+/(?:[a-z0-9-]+/)?(\d{6,})-tender"},
    "ahstep.ru": {"item_link": r"ahstep\.ru/tenders/tender(\d+)"},
    "zakupki.eurochem.ru": {"item_link": r"b2b-center\.ru/.*?tender-(\d+)"},
    "lukoil.ru": {"item_marker": "Прием заявок до"},
}
# «АО Агрохолдинг «СТЕПЬ» объявляет о проведении запроса предложений на: «…»» → «…».
# Двоеточие обязательно: иначе «…на выполнение работ по зачистке резервуаров на «Шесхарис»» стало бы «Шесхарис».
ANNOUNCE_RE = re.compile(
    r"(?is)^.{0,200}?(?:объявля\w*|проводит|приглаша\w*)\b.{0,200}?\b(?:на|по)\s*:\s*[«\"“](.{8,})[»\"”]\s*\.?$"
)
LABEL_RE = re.compile(
    r"(?i)^(?:№|номер|заказчик|организатор|документ|при[её]м заявок|дата|срок|статус|принять участие|подробнее"
    r"|способ|регион|место|окончание|начальная цена)"
)
FILE_HREF_RE = re.compile(r"(?i)/filesystem/|\.(?:zip|rar|7z|docx?|xlsx?|pdf|rtf|odt)(?:[?#]|$)")
JS_APP_RE = re.compile(
    r"(?i)id=[\"'](?:app|root|__next|__nuxt)[\"']|__NEXT_DATA__|window\.__NUXT__|ng-version=|data-reactroot"
)
ROW_TAGS = ("li", "tr", "article")
TOP_TAGS = ("body", "html", "[document]", "main")


def html_rules(src: Source) -> tuple[str, str]:
    """(item_link, item_marker) для источника: из config.toml или из встроенных правил для сайта."""
    host = (urlparse(src.url).hostname or "").lower()
    preset: dict[str, str] = {}
    for domain, rules in HTML_PRESETS.items():
        if host == domain or host.endswith("." + domain):
            preset = rules
            break
    return src.item_link or preset.get("item_link", ""), src.item_marker or preset.get("item_marker", "")


def tidy_title(title: str) -> str:
    """Название без меняющихся фрагментов («осталось 3 дня») и без шапки «…объявляет … на: «…»»."""
    title = clean(VOLATILE_RE.sub(" ", title))
    m = ANNOUNCE_RE.match(title)
    return clean(m.group(1)) if m else title


def _title_pair(raw: str) -> tuple[str, str]:
    """(название, полный текст для поиска слов, если название укорочено)."""
    full = clean(VOLATILE_RE.sub(" ", raw))
    title = tidy_title(full)
    return title, (full if title != full else "")


def safe_urljoin(base: str, href: str) -> str | None:
    try:
        return urljoin(base, href)
    except ValueError:  # ссылки вида http://[object Object]/x
        return None


def html_text(resp: requests.Response) -> str:
    """Текст страницы в правильной кодировке: из заголовка ответа или из <meta charset>."""
    if "charset=" in resp.headers.get("Content-Type", "").lower():
        return resp.text
    return UnicodeDammit(resp.content, is_html=True).unicode_markup or ""


def _soup(resp: requests.Response) -> BeautifulSoup:
    ctype = resp.headers.get("Content-Type", "").lower()
    if ctype and "html" not in ctype and "xml" not in ctype:
        raise FetchError(f"страница вернула не HTML ({ctype.split(';')[0]})")
    soup = BeautifulSoup(html_text(resp), "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "iframe", "template"]):
        tag.decompose()
    return soup


def _page_level_tag(tag) -> bool:
    if tag.name in ("h1", "nav", "select"):
        return True
    return tag.name == "input" and (tag.get("type") or "text").lower() in ("text", "search", "date")


def _signature(node) -> tuple[str, tuple[str, ...]]:
    return node.name, tuple(sorted(node.get("class") or []))


class _Climber:
    """Поднимается от ссылки или метки тендера к строке списка.

    Строка — самый крупный блок, в котором нет других тендеров, но не выше блока страницы
    (с заголовком h1, фильтром, меню) и не выше li/tr/article. Если на странице несколько
    тендеров, запоминается, как выглядит строка (тег и классы), чтобы на странице с одним
    тендером не захватить всю страницу.
    """

    def __init__(self, hint: dict):
        self.hint = hint
        self.page_level: dict[int, bool] = {}
        self.text_len: dict[int, int] = {}

    def _is_page_level(self, node) -> bool:
        """Блок уровня страницы: в нём заголовок h1, меню или фильтр (выпадающий список, поле поиска)."""
        key = id(node)
        if key not in self.page_level:
            self.page_level[key] = node.name in TOP_TAGS or node.find(_page_level_tag) is not None
        return self.page_level[key]

    def _too_long(self, node) -> bool:
        key = id(node)
        if key not in self.text_len:
            self.text_len[key] = len(node.get_text(" ", strip=True))
        return self.text_len[key] > 3000

    def climb(self, start, count_below: dict[int, int]):
        row = start
        node = start.parent
        learned = self.hint.get("row")
        while node is not None:
            if learned and _signature(row) == learned:
                break
            if count_below.get(id(node), 0) > 1 or self._is_page_level(node) or self._too_long(node):
                break
            row = node
            if row.name in ROW_TAGS:
                break
            node = node.parent
        return row

    def learn(self, rows: list) -> None:
        if len(rows) >= 2:
            sigs = [_signature(r) for r in rows]
            sig = max(set(sigs), key=sigs.count)
            if sig[1] or sig[0] in ROW_TAGS:  # строка без классов слишком похожа на внутренние блоки
                self.hint["row"] = sig


def _texts(el):
    """Текстовые узлы без комментариев HTML."""
    return [s for s in el.find_all(string=True) if type(s) is NavigableString]


def _longest_text(el) -> str:
    best = ""
    for s in _texts(el):
        text = clean(s)
        if re.fullmatch(r"[\d\s.,:;№/()+-]*", text) or LABEL_RE.match(text):
            continue
        if len(text) > len(best):
            best = text
    return best


def _rostender_row(item: Item) -> None:
    """Номер, дата публикации и место из строки списка РосТендера."""
    m = re.search(r"/(\d{6,})-tender|/tender/(\d{6,})", item.link)
    if m:
        item.uid = "rostender:" + (m.group(1) or m.group(2))
    text = item.context
    pub = re.search(r"№\s*\d{6,}\s+от\s+(\d{2}\.\d{2}\.\d{2,4})", text)
    if pub:
        item.published = _iso(parse_date(pub.group(1)))
    place = re.search(r"Окончание[^0-9]{0,25}\d{2}\.\d{2}\.\d{4}(?:\s+\d{1,2}:\d{2})?\s+(.{2,200}?)\s+Закупки в регионе",
                      text)
    if place:
        item.place = clean(place.group(1))


def _link_items(src: Source, soup, pattern: str, base: str, host: str, hint: dict) -> list[Item]:
    rx = re.compile(pattern, re.I)
    found: list[tuple[object, str, str]] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        url = safe_urljoin(base, href)
        m = rx.search(url) if url else None
        if not m:
            continue
        ident = next((g for g in m.groups() if g), "") if m.groups() else ""
        found.append((a, url, ident or url))
    # сколько разных тендеров внутри каждого блока страницы — за один проход снизу вверх
    keys_below: dict[int, set[str]] = {}
    for a, _, key in found:
        node = a.parent
        while node is not None:
            keys_below.setdefault(id(node), set()).add(key)
            node = node.parent
    count_below = {k: len(v) for k, v in keys_below.items()}
    groups: dict[str, list[tuple[object, str]]] = {}
    for a, url, key in found:
        groups.setdefault(key, []).append((a, url))
    climber = _Climber(hint)
    rows, items = [], []
    for key, anchors in groups.items():
        first, url = anchors[0]
        row = climber.climb(first, count_below)
        rows.append(row)
        raw = max((a.get_text(" ", strip=True) for a, _ in anchors), key=len)
        if len(clean(raw)) < 12:  # ссылка вида «Подробнее» или номер: берём самый длинный текст строки
            raw = _longest_text(row) or raw
        title, full = _title_pair(raw)
        if len(title) < 5:
            continue
        context = clean(VOLATILE_RE.sub(" ", row.get_text(" ", strip=True)))[:1500]
        ident = key if key != url else sha(url)
        item = Item(
            uid=f"html:{host}:{ident}", title=title[:400], link=url, source=src.name, text=full, context=context,
            customer=_first(CUSTOMER_RE, context), price=format_price(_first(PRICE_RE, context)),
            deadline=_first(DEADLINE_RE, context),
        )
        if "rostender.info" in url:
            _rostender_row(item)
        items.append(item)
    climber.learn(rows)
    return items


def _block_title(block) -> str:
    for tag in block.find_all(HEADINGS):
        text = clean(tag.get_text(" ", strip=True))
        if len(text) >= 10:
            return text
    for tag in block.find_all(class_=re.compile(r"(?i)title|name|subject|caption")):
        text = clean(tag.get_text(" ", strip=True))
        if len(text) >= 10 and not LABEL_RE.match(text):
            return text
    for s in _texts(block):
        text = clean(s)
        if len(text) < 15 or LABEL_RE.match(text):
            continue
        anchor = s.find_parent("a")
        if anchor is not None and FILE_HREF_RE.search(anchor.get("href", "")):
            continue  # название файла документации, а не тендера
        return text
    return ""


def _marker_items(src: Source, soup, marker: str, host: str, hint: dict) -> list[Item]:
    mark = norm(marker)
    nodes = [s for s in soup.find_all(string=lambda t: type(t) is NavigableString and mark in norm(t))]
    count_below: dict[int, int] = {}
    for s in nodes:
        node = s.parent
        while node is not None:
            count_below[id(node)] = count_below.get(id(node), 0) + 1
            node = node.parent
    climber = _Climber(hint)
    blocks: list = []
    for s in nodes:
        block = climber.climb(s.parent, count_below)
        if not any(block is b for b in blocks):
            blocks.append(block)
    climber.learn(blocks)
    items = []
    for block in blocks:
        title, full = _title_pair(_block_title(block))
        if len(title) < 5:
            continue
        context = clean(VOLATILE_RE.sub(" ", block.get_text(" ", strip=True)))[:2000]
        number = re.search(r"(?:^|\s)№\s*:\s*([^\s,;]{2,40})", context)
        ident = number.group(1) if number else sha(norm(title))
        items.append(Item(
            uid=f"html:{host}:{ident}", title=title[:400], link=src.url, source=src.name, text=full, context=context,
            customer=_first(CUSTOMER_RE, context), price=format_price(_first(PRICE_RE, context)),
            deadline=_first(DEADLINE_RE, context),
        ))
    return items


def _heading_block(heading, limit: int = 1500) -> str:
    parts = [heading.get_text(" ", strip=True)]
    node = heading
    for _ in range(3):
        sibling = node.next_sibling
        collected = False
        while sibling is not None and sum(len(p) for p in parts) < limit:
            name = getattr(sibling, "name", None)
            if name in HEADINGS or (name and sibling.find(HEADINGS)):
                return clean(" ".join(parts))[:limit]
            text = sibling.get_text(" ", strip=True) if name else str(sibling).strip()
            if text:
                parts.append(text)
                collected = True
            sibling = sibling.next_sibling
        if collected or node.parent is None or node.parent.name in ("body", "[document]"):
            break
        node = node.parent
    return clean(" ".join(parts))[:limit]


def _container_text(el, limit: int = 1200) -> str:
    best = el.get_text(" ", strip=True)
    node = el.parent
    for _ in range(4):
        if node is None or node.name in ("body", "html", "[document]"):
            break
        text = node.get_text(" ", strip=True)
        if len(text) > limit:
            break
        best = text
        if node.name in ROW_TAGS:
            break
        node = node.parent
    return clean(best)


def _generic_items(src: Source, soup, base: str, host: str) -> list[Item]:
    """Страница неизвестного устройства: заголовки, длинные ссылки и отдельные строки текста."""
    for tag in soup(["nav", "footer", "header"]):
        tag.decompose()
    seen_titles: set[str] = set()
    candidates: list[tuple[str, str, str, str]] = []

    def add(raw: str, block: str, link: str) -> None:
        # убираем меняющиеся фрагменты («осталось 3 дня»), иначе тендер будет «новым» каждый день
        title, full = _title_pair(raw)
        if len(title) < 15:
            return
        key = norm(title)
        if key in seen_titles:
            return
        seen_titles.add(key)
        candidates.append((title[:400], full, clean(VOLATILE_RE.sub(" ", block))[:1500], link))

    for heading in soup.find_all(HEADINGS):
        anchor = heading.find("a", href=True)
        link = (safe_urljoin(base, anchor["href"]) if anchor else None) or base
        add(heading.get_text(" ", strip=True), _heading_block(heading), link)
    for anchor in soup.find_all("a", href=True):
        text = anchor.get_text(" ", strip=True)
        href = anchor["href"].strip()
        if len(text) < 20 or FILE_HREF_RE.search(href):
            continue
        link = None if href.startswith(("javascript:", "mailto:", "tel:", "#")) else safe_urljoin(base, href)
        add(text, _container_text(anchor), link or base)
    for el in soup.find_all(["li", "tr", "article", "p", "dd", "div", "td"]):
        # только «листовые» блоки: без вложенных строк и абзацев
        if el.find(["li", "tr", "article", "p", "div", "table", "ul", "ol", "section"]):
            continue
        if any(len(a.get_text(strip=True)) >= 20 for a in el.find_all("a")):
            continue
        text = el.get_text(" ", strip=True)
        if 25 <= len(text) <= 1200:
            add(text[:300], text, base)

    # Если одна строка целиком входит в другую (заголовок и весь блок вокруг него),
    # оставляем более короткую, чтобы не прислать один тендер дважды.
    kept: list[tuple[str, str, str, str]] = []
    kept_keys: list[str] = []
    for title, full, block, link in sorted(candidates, key=lambda c: len(c[0])):
        key = norm(title)
        if any(len(k) >= 20 and k in key for k in kept_keys):
            continue
        kept.append((title, full, block, link))
        kept_keys.append(key)

    return [
        Item(
            uid=f"html:{host}:{sha(norm(title))}", title=title, link=link, source=src.name, text=full,
            context=block, customer=_first(CUSTOMER_RE, block), price=format_price(_first(PRICE_RE, block)),
            deadline=_first(DEADLINE_RE, block),
        )
        for title, full, block, link in kept
    ]


def items_from_html(src: Source, resp: requests.Response, hint: dict | None = None) -> list[Item]:
    soup = _soup(resp)
    host = (urlparse(src.url).hostname or "").lower()
    base = resp.url or src.url
    hint = hint if hint is not None else {}
    item_link, item_marker = html_rules(src)
    if item_link:
        return _link_items(src, soup, item_link, base, host, hint)
    if item_marker:
        return _marker_items(src, soup, item_marker, host, hint)
    return _generic_items(src, soup, base, host)


def _has_any(words: list[str], text: str) -> bool:
    text = norm(text)
    return any(re.search(r"(?<!\w)" + re.escape(w), text) for w in words)


def _require(src: Source, items: list[Item], info: dict) -> list[Item]:
    if not src.require:
        return items
    kept = [i for i in items if _has_any(src.require, " ".join((i.title, i.text, i.place, i.customer, i.context)))]
    info["required"] = (len(items), len(kept))
    return kept


def collect(src: Source, http: Http, info: dict | None = None) -> list[Item]:
    """Все тендеры источника. В info (если передан) — подробности для --probe."""
    info = info if info is not None else {}
    if src.type == "rss":
        items = items_from_feed(src, http.get(src.url).content)
        info["pages"] = [(src.url, len(items))]
        return _require(src, items, info)
    if src.type != "html":
        raise FetchError(f"неизвестный тип источника {src.type}")
    items: list[Item] = []
    uids: set[str] = set()
    hint: dict = {}
    info["pages"] = []
    urls = src.page_urls()
    for n, url in enumerate(urls, 1):
        try:
            resp = http.get(url, check_robots=True)
            page_items = items_from_html(src, resp, hint)
        except Exception as exc:
            if n == 1:
                raise
            note = str(exc) if isinstance(exc, FetchError) else f"{exc.__class__.__name__}: {short_err(exc)}"
            info.setdefault("notes", []).append(f"страница {n} не прочиталась: {note}")
            log.info("%s: страница %d не прочиталась (%s)", src.name, n, note)
            break
        info["pages"].append((url, len(page_items)))
        if n == 1:
            info["html"] = html_text(resp)
            if not page_items:
                raise FetchError("на странице не найдено ни одного тендера: возможно, список строится "
                                 "JavaScript'ом или сайт изменил вёрстку")
        fresh = []
        for item in page_items:
            if item.uid not in uids:
                uids.add(item.uid)
                fresh.append(item)
        if not fresh:
            break  # страницы кончились или пошли повторы
        items.extend(fresh)
        if n == len(urls) and n > 1:
            note = (f"прочитано страниц: {n} (столько задано в pages), а новые тендеры ещё шли: "
                    f"увеличьте pages для этого источника")
            info.setdefault("notes", []).append(note)
            log.warning("%s: %s", src.name, note)
    return _require(src, items, info)


# ---------------------------------------------------------------------------
# Хранилище
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    uid TEXT PRIMARY KEY, source TEXT, title TEXT, link TEXT,
    matched INTEGER DEFAULT 0, first_seen TEXT
);
CREATE TABLE IF NOT EXISTS sources (
    key TEXT PRIMARY KEY, name TEXT, bootstrapped INTEGER DEFAULT 0,
    last_ok TEXT, last_error TEXT, fail_count INTEGER DEFAULT 0,
    alerted INTEGER DEFAULT 0, last_count INTEGER DEFAULT 0, last_check TEXT
);
CREATE TABLE IF NOT EXISTS sent (fingerprint TEXT PRIMARY KEY, uid TEXT, sent_at TEXT);
CREATE INDEX IF NOT EXISTS sent_uid ON sent (uid);
CREATE TABLE IF NOT EXISTS outbox (uid TEXT PRIMARY KEY, payload TEXT, created TEXT, attempts INTEGER DEFAULT 0);
"""


class Store:
    def __init__(self, path: Path | str):
        self.db = sqlite3.connect(str(path), timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(sources)")}
        if "last_check" not in columns:  # база от версии 1.0
            self.db.execute("ALTER TABLE sources ADD COLUMN last_check TEXT")
        self.db.commit()

    def due(self, src: Source, state: sqlite3.Row) -> bool:
        """Пора ли проверять источник с настройкой every_minutes."""
        if not src.every_minutes or not state["bootstrapped"] or not state["last_check"]:
            return True
        try:
            last = datetime.fromisoformat(state["last_check"])
        except ValueError:
            return True
        # запас 2 минуты: проверки в цикле идут с небольшим разбросом по времени
        return now_utc() - last >= timedelta(minutes=src.every_minutes) - timedelta(minutes=2)

    def touch(self, src: Source) -> None:
        self.db.execute("UPDATE sources SET last_check = ? WHERE key = ?", (now_utc().isoformat(), src.key))

    def is_empty(self) -> bool:
        return self.db.execute("SELECT COUNT(*) FROM sources WHERE bootstrapped = 1").fetchone()[0] == 0

    def is_seen(self, uid: str) -> bool:
        return self.db.execute("SELECT 1 FROM seen WHERE uid = ?", (uid,)).fetchone() is not None

    def needs_check(self, item: Item) -> bool:
        """Новый тендер или тот же источник показал его под другим названием, а уведомления о нём не было.
        Второе бывает, если страница в прошлый раз была разобрана неверно (например, тендер был один на странице)."""
        row = self.db.execute("SELECT source, title FROM seen WHERE uid = ?", (item.uid,)).fetchone()
        if row is None:
            return True
        if row["source"] != item.source or norm(row["title"] or "") == norm(item.title[:500]):
            return False
        return self.db.execute("SELECT 1 FROM sent WHERE uid = ?", (item.uid,)).fetchone() is None

    def mark_seen(self, item: Item) -> None:
        self.db.execute(
            "INSERT INTO seen (uid, source, title, link, matched, first_seen) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(uid) DO UPDATE SET title = excluded.title, matched = excluded.matched",
            (item.uid, item.source, item.title[:500], item.link, int(bool(item.rules)), now_utc().isoformat()),
        )

    def source_state(self, src: Source) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM sources WHERE key = ?", (src.key,)).fetchone()
        if row is None:
            self.db.execute("INSERT INTO sources (key, name) VALUES (?, ?)", (src.key, src.name))
            row = self.db.execute("SELECT * FROM sources WHERE key = ?", (src.key,)).fetchone()
        return row

    def source_ok(self, src: Source, count: int) -> None:
        self.db.execute(
            "UPDATE sources SET name = ?, last_ok = ?, last_error = NULL, fail_count = 0, alerted = 0, "
            "bootstrapped = 1, last_count = ? WHERE key = ?",
            (src.name, now_utc().isoformat(), count, src.key),
        )

    def source_fail(self, src: Source, error: str) -> int:
        self.db.execute(
            "UPDATE sources SET name = ?, last_error = ?, fail_count = fail_count + 1 WHERE key = ?",
            (src.name, error, src.key),
        )
        return self.db.execute("SELECT fail_count FROM sources WHERE key = ?", (src.key,)).fetchone()[0]

    def set_alerted(self, src: Source) -> None:
        self.db.execute("UPDATE sources SET alerted = 1 WHERE key = ?", (src.key,))

    def sent_recently(self, fingerprint: str) -> bool:
        cutoff = (now_utc() - timedelta(days=DEDUP_DAYS)).isoformat()
        row = self.db.execute("SELECT sent_at FROM sent WHERE fingerprint = ?", (fingerprint,)).fetchone()
        return row is not None and row["sent_at"] >= cutoff

    def mark_sent(self, fingerprint: str, uid: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO sent (fingerprint, uid, sent_at) VALUES (?,?,?)",
            (fingerprint, uid, now_utc().isoformat()),
        )

    def enqueue(self, item: Item) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO outbox (uid, payload, created) VALUES (?,?,?)",
            (item.uid, json.dumps(asdict(item), ensure_ascii=False), now_utc().isoformat()),
        )

    def outbox(self) -> list[Item]:
        items = []
        for row in self.db.execute("SELECT payload FROM outbox ORDER BY created"):
            try:
                items.append(Item(**json.loads(row["payload"])))
            except (TypeError, ValueError):
                continue
        return items

    def outbox_done(self, uids: list[str]) -> None:
        self.db.executemany("DELETE FROM outbox WHERE uid = ?", [(u,) for u in uids])

    def outbox_failed(self, uids: list[str]) -> None:
        self.db.executemany("UPDATE outbox SET attempts = attempts + 1 WHERE uid = ?", [(u,) for u in uids])
        dropped = self.db.execute("DELETE FROM outbox WHERE attempts > 48").rowcount
        if dropped:
            log.error("Не удалось отправить %d уведомлений за 48 попыток, удаляю их из очереди", dropped)

    def prune(self) -> None:
        cutoff = (now_utc() - timedelta(days=400)).isoformat()
        self.db.execute("DELETE FROM seen WHERE first_seen < ?", (cutoff,))
        self.db.execute("DELETE FROM sent WHERE sent_at < ?", (cutoff,))

    def commit(self) -> None:
        self.db.commit()


# ---------------------------------------------------------------------------
# Уведомления
# ---------------------------------------------------------------------------

REGION_NAMES = {"other": "другой регион", "unknown": "регион не определён"}


def item_html(item: Item, label: str) -> str:
    region = label if item.region == "my" else REGION_NAMES.get(item.region, "")
    head = esc(", ".join(item.rules) or "Тендер")
    lines = [f"<b>{head}</b>" + (f" · {esc(region)}" if region else "")]
    title = esc(truncate(item.title, 350))
    lines.append(f'<a href="{esc_attr(item.link)}">{title}</a>' if item.link.startswith("http") else title)
    if item.customer:
        lines.append("Заказчик: " + esc(truncate(item.customer, 200)))
    if item.place:
        lines.append("Место: " + esc(truncate(item.place, 200)))
    if item.price:
        lines.append("Цена: " + esc(item.price))
    if item.deadline:
        lines.append("Приём заявок до: " + esc(item.deadline))
    if item.unnamed:
        lines.append("Названия нет в ленте ЕИС, запрос найден в документах закупки.")
    lines.append("Источник: " + esc(item.source))
    return "\n".join(lines)


def item_text(item: Item, label: str) -> str:
    region = label if item.region == "my" else REGION_NAMES.get(item.region, "")
    lines = [(", ".join(item.rules) or "Тендер") + (f" · {region}" if region else ""), truncate(item.title, 350)]
    for name, value in (("Заказчик", item.customer), ("Место", item.place), ("Цена", item.price),
                        ("Приём заявок до", item.deadline)):
        if value:
            lines.append(f"{name}: {truncate(value, 200)}")
    if item.unnamed:
        lines.append("Названия нет в ленте ЕИС, запрос найден в документах закупки.")
    lines.append(f"Источник: {item.source}")
    if item.link:
        lines.append(item.link)
    return "\n".join(lines)


def digest_html(items: list[Item], label: str, header: str) -> list[str]:
    """Сводка несколькими сообщениями, каждое не длиннее лимита Telegram."""
    chunks, current = [], f"<b>{esc(header)}</b>\n"
    for item in items:
        region = label if item.region == "my" else REGION_NAMES.get(item.region, "")
        details = ", ".join(x for x in (truncate(item.place, 80), item.price, region) if x)
        title = esc(truncate(item.title, 220))
        link = f'<a href="{esc_attr(item.link)}">{title}</a>' if item.link.startswith("http") else title
        line = f"\n• {link}" + (f"\n   {esc(details)}" if details else "")
        if len(current) + len(line) > TG_LIMIT:
            chunks.append(current)
            current = ""
        current += line
    if current.strip():
        chunks.append(current)
    return chunks


class Notifier:
    name = "уведомления"

    def send_items(self, items: list[Item], label: str, header: str) -> bool:
        raise NotImplementedError

    def send_note(self, text: str) -> bool:
        raise NotImplementedError


class ConsoleNotifier(Notifier):
    name = "консоль"

    def send_items(self, items, label, header):
        print(f"\n=== {header} ===")
        for item in items:
            print("\n" + item_text(item, label))
        return True

    def send_note(self, text):
        print("\n" + text)
        return True


class TelegramNotifier(Notifier):
    name = "Telegram"

    def __init__(self, cfg: dict):
        self.token = str(cfg.get("bot_token", "")).strip()
        self.chat_id = str(cfg.get("chat_id", "")).strip()
        self.api = str(cfg.get("api_base", "https://api.telegram.org")).rstrip("/")
        self.session = requests.Session()
        proxy = str(cfg.get("proxy", "")).strip()
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})

    def _send(self, text_html: str, text_plain: str | None = None) -> bool:
        payload = {
            "chat_id": self.chat_id, "text": text_html[:4096], "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        for attempt in range(3):
            try:
                resp = self.session.post(f"{self.api}/bot{self.token}/sendMessage", json=payload, timeout=30)
                data = resp.json() if resp.content else {}
            except (requests.RequestException, ValueError) as exc:
                log.warning("Telegram: не удалось отправить (%s)", short_err(exc))
                time.sleep(3 * (attempt + 1))
                continue
            if data.get("ok"):
                return True
            if resp.status_code == 429:
                time.sleep(int(data.get("parameters", {}).get("retry_after", 5)) + 1)
                continue
            description = data.get("description", f"HTTP {resp.status_code}")
            if resp.status_code >= 500 and attempt < 2:
                time.sleep(3 * (attempt + 1))
                continue
            if resp.status_code == 400 and "parse" in description.lower() and text_plain:
                payload.pop("parse_mode", None)
                payload["text"] = text_plain[:4096]
                continue
            if resp.status_code in (401, 404):
                log.error("Telegram: неверный bot_token (%s)", description)
            elif resp.status_code == 403 or "chat not found" in description.lower():
                log.error("Telegram: бот не может написать в чат %s. Откройте бота и нажмите «Запустить» "
                          "(для группы — добавьте его в группу). Ответ: %s", self.chat_id, description)
            else:
                log.error("Telegram: ошибка отправки: %s", description)
            return False
        return False

    def send_items(self, items, label, header):
        if len(items) <= MAX_SEPARATE_MESSAGES:
            ok = True
            for item in items:
                ok = self._send(item_html(item, label), item_text(item, label)) and ok
                time.sleep(1.1)
            return ok
        ok = True
        for chunk in digest_html(items, label, header):
            ok = self._send(chunk) and ok
            time.sleep(1.1)
        return ok

    def send_note(self, text):
        return self._send(esc(text), text)


class EmailNotifier(Notifier):
    name = "почта"

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def _send(self, subject: str, plain: str, html_body: str) -> bool:
        cfg = self.cfg
        msg = EmailMessage()
        recipients = [r for r in cfg.get("recipients", []) if r]
        msg["Subject"] = subject
        msg["From"] = cfg.get("sender") or cfg.get("username") or recipients[0]
        msg["To"] = ", ".join(recipients)
        msg.set_content(plain)
        msg.add_alternative(html_body, subtype="html")
        host, port = cfg.get("smtp_host", ""), int(cfg.get("smtp_port", 465))
        security = str(cfg.get("security", "ssl")).lower()
        try:
            if security == "ssl":
                server = smtplib.SMTP_SSL(host, port, timeout=30, context=ssl.create_default_context())
            else:
                server = smtplib.SMTP(host, port, timeout=30)
                if security == "starttls":
                    server.starttls(context=ssl.create_default_context())
            with server:
                if cfg.get("username"):
                    server.login(cfg["username"], cfg.get("password", ""))
                server.send_message(msg)
            return True
        except (OSError, smtplib.SMTPException) as exc:
            log.error("Почта: не удалось отправить письмо (%s)", short_err(exc))
            return False

    def send_items(self, items, label, header):
        plain = header + "\n\n" + "\n\n".join(item_text(i, label) for i in items)
        body = "<br><br>".join(item_html(i, label).replace("\n", "<br>") for i in items)
        return self._send(f"{header}: {len(items)}", plain, f"<p><b>{esc(header)}</b></p>{body}")

    def send_note(self, text):
        return self._send("Мониторинг тендеров", text, "<p>" + esc(text).replace("\n", "<br>") + "</p>")


def build_notifiers(cfg: dict) -> list[Notifier]:
    notifiers: list[Notifier] = [ConsoleNotifier()]
    tg = cfg.get("telegram", {})
    if tg.get("enabled", False):
        if str(tg.get("bot_token", "")).strip() and str(tg.get("chat_id", "")).strip():
            notifiers.append(TelegramNotifier(tg))
        else:
            log.warning("Telegram включён, но не заполнены bot_token и chat_id в config.toml")
    mail = cfg.get("email", {})
    if mail.get("enabled", False):
        if mail.get("smtp_host") and mail.get("recipients"):
            notifiers.append(EmailNotifier(mail))
        else:
            log.warning("Почта включена, но не заполнены smtp_host и recipients в config.toml")
    return notifiers


def deliver(notifiers: list[Notifier], items: list[Item], label: str, header: str) -> bool:
    """True, если сообщение дошло хотя бы через один внешний канал (или внешних каналов нет)."""
    external = [n for n in notifiers if not isinstance(n, ConsoleNotifier)]
    for n in notifiers:
        if isinstance(n, ConsoleNotifier):
            n.send_items(items, label, header)
    if not external:
        return True
    results = [n.send_items(items, label, header) for n in external]
    return any(results)


def announce(notifiers: list[Notifier], text: str) -> None:
    for n in notifiers:
        n.send_note(text)


# ---------------------------------------------------------------------------
# Основная проверка
# ---------------------------------------------------------------------------

def evaluate(item: Item, matcher: Matcher, regions: RegionFilter, settings: dict) -> bool:
    """Заполняет item.rules и item.region, возвращает True, если тендер надо прислать."""
    fields = [item.title] if item.unnamed else [item.title, item.text]
    item.rules = matcher.match(fields)
    if (not item.rules and item.unnamed and settings.get("eis_notify_unnamed", True)
            and matcher.match([item.query])):
        # у малых закупок ЕИС не присылает название; доверяем, если сам запрос ленты про нашу тему
        item.rules = ["Совпадение в документах ЕИС"]
    if not item.rules:
        return False
    item.region = regions.classify(item)
    if settings.get("only_my_regions", True) and item.region == "other":
        return False
    max_age = int(settings.get("max_age_days", 30) or 0)
    published = item.published_dt()
    if max_age and published and published < now_utc() - timedelta(days=max_age):
        return False
    return True


def run_once(cfg: dict, *, dry_run: bool = False) -> int:
    settings = cfg.get("settings", {})
    label = str(settings.get("region_label", "ваш регион"))
    sources = [s for s in load_sources(cfg) if s.enabled]
    if not sources:
        log.error("В config.toml нет включённых источников")
        return 2
    store = Store(":memory:" if dry_run else DB_PATH)
    http = Http(cfg)
    matcher = Matcher(cfg)
    regions = RegionFilter(settings)
    notifiers = [ConsoleNotifier()] if dry_run else build_notifiers(cfg)
    first_run = store.is_empty()

    new_items: list[Item] = []
    digest: list[Item] = []
    added_sources: list[str] = []
    alerts: list[str] = []
    stats = {"ok": 0, "failed": 0, "fresh": 0, "later": 0}
    run_fps: set[str] = set()

    for src in sources:
        state = store.source_state(src)
        if not store.due(src, state):
            stats["later"] += 1
            log.debug("… %s: проверяется раз в %d мин, сейчас пропускаю", src.name, src.every_minutes)
            continue
        store.touch(src)
        try:
            items = collect(src, http)
        except Exception as exc:  # источник не должен ронять всю проверку
            error = short_err(exc) if isinstance(exc, FetchError) else f"{exc.__class__.__name__}: {short_err(exc)}"
            fails = store.source_fail(src, error)
            stats["failed"] += 1
            log.warning("× %s: %s", src.name, error)
            if state["last_ok"] and fails >= FAIL_ALERT_AFTER and not state["alerted"]:
                alerts.append(f"Источник «{src.name}» не работает {fails} проверки подряд: {error}")
                store.set_alerted(src)
            continue

        bootstrap = not state["bootstrapped"]
        if state["alerted"]:
            alerts.append(f"Источник «{src.name}» снова работает.")
        fresh = matched = 0
        for item in items:
            if not store.needs_check(item):
                continue
            fresh += 1
            wanted = evaluate(item, matcher, regions, settings)
            store.mark_seen(item)
            if not wanted:
                continue
            matched += 1
            fp = item.fingerprint()
            if fp in run_fps or (not bootstrap and store.sent_recently(fp)):
                continue
            run_fps.add(fp)
            if bootstrap:
                digest.append(item)
                if not dry_run:
                    store.mark_sent(fp, item.uid)
            else:
                new_items.append(item)
                if not dry_run:
                    store.mark_sent(fp, item.uid)
                    store.enqueue(item)
        store.source_ok(src, len(items))
        store.commit()
        stats["ok"] += 1
        stats["fresh"] += fresh
        if bootstrap and not first_run:
            added_sources.append(src.name)
        log.info("✓ %s: записей %d, новых %d, подходящих %d%s", src.name, len(items), fresh, matched,
                 " (первое чтение)" if bootstrap else "")

    if dry_run:
        found = digest + new_items
        if found:
            deliver(notifiers, found, label, "Подходящие тендеры в источниках сейчас")
        else:
            print("\nПодходящих тендеров в источниках сейчас нет.")
        log.info("Проверено источников: %d, с ошибкой: %d", stats["ok"], stats["failed"])
        return 0 if stats["ok"] else 2

    # первое сообщение после установки или после добавления источников
    if first_run and not stats["ok"]:
        log.error("Ни один источник не ответил. Проверьте интернет и журнал tender_monitor.log")
    elif first_run and settings.get("first_run_digest", True):
        head = (f"Мониторинг тендеров запущен. Источников работает: {stats['ok']} из {len(sources)}. "
                f"Дальше буду присылать только новые тендеры. "
                f"Что достаётся из каждого источника, покажет команда: python tender_monitor.py --probe")
        announce([n for n in notifiers if not isinstance(n, ConsoleNotifier)] or notifiers, head)
        if digest:
            deliver(notifiers, digest, label, f"Подходящие тендеры в лентах сейчас: {len(digest)}")
    elif added_sources and digest:
        deliver(notifiers, digest, label, "Подходящие тендеры в новых источниках: " + ", ".join(added_sources))

    pending = store.outbox()
    if pending:
        if deliver(notifiers, pending, label, f"Новые тендеры: {len(pending)}"):
            store.outbox_done([i.uid for i in pending])
        else:
            store.outbox_failed([i.uid for i in pending])
            log.error("Уведомления не отправлены, повторю при следующей проверке")
    if alerts:
        announce(notifiers, "\n".join(alerts))

    store.prune()
    store.commit()
    later = f" (ещё {stats['later']} по расписанию позже)" if stats["later"] else ""
    log.info("Итого: источников %d/%d%s, новых записей %d, новых подходящих тендеров %d, ошибок %d",
             stats["ok"], len(sources), later, stats["fresh"], len(new_items), stats["failed"])
    return 0 if stats["ok"] or stats["later"] else 2


# ---------------------------------------------------------------------------
# Команды
# ---------------------------------------------------------------------------

def read_config_text() -> str:
    raw = CONFIG_PATH.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):  # Блокнот может сохранить UTF-8 с меткой BOM
        raw = raw[3:]
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        # старый Блокнот Windows сохраняет файл в кодировке ANSI (cp1251)
        return raw.decode("cp1251", errors="replace")


def load_config(create: bool = True) -> dict:
    if not CONFIG_PATH.exists():
        if not create:
            raise SystemExit("Нет файла config.toml")
        CONFIG_PATH.write_text(DEFAULT_CONFIG, encoding="utf-8")
        print(f"Создан файл настроек: {CONFIG_PATH}\n"
              "Впишите в него bot_token и chat_id для Telegram (или настройки почты) и запустите скрипт снова.")
        raise SystemExit(0)
    try:
        return tomllib.loads(read_config_text())
    except tomllib.TOMLDecodeError as exc:
        hint = ("Проверьте кавычки, запятые и скобки в этой строке. Регулярные выражения (item_link) "
                "пишите в одинарных кавычках: item_link = 'tender(\\d+)'")
        raise ConfigError(f"Ошибка в config.toml: {exc}\n{hint}") from None
    except OSError as exc:
        raise ConfigError(f"Не удалось прочитать config.toml: {exc}") from None


def setup_logging(verbose: bool) -> None:
    # при выводе в файл через планировщик Windows не падать на символах вне кодовой страницы
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (ValueError, OSError):
                pass
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    file_handler = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(fmt)
    log.addHandler(file_handler)
    if sys.stdout is not None:
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(console)


def acquire_lock(interval_minutes: int) -> bool:
    stale = max(90, interval_minutes * 2 + 10) * 60
    for _ in range(2):
        try:
            fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age = time.time() - LOCK_PATH.stat().st_mtime
            except OSError:
                age = stale + 1
            if age < stale:
                return False
            LOCK_PATH.unlink(missing_ok=True)
            continue
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        atexit.register(lambda: LOCK_PATH.unlink(missing_ok=True))
        return True
    return False


def cmd_test(cfg: dict) -> int:
    notifiers = build_notifiers(cfg)
    external = [n for n in notifiers if not isinstance(n, ConsoleNotifier)]
    if not external:
        print("Ни Telegram, ни почта не настроены. Заполните config.toml.")
        return 1
    sample = Item(
        uid="test", title="Оказание услуг по зачистке резервуаров (тестовое уведомление)",
        link="https://rostender.info/category/tendery-na-zachistku-rezervuarov", source="проверка настройки",
        place="г. Волгоград", price=format_price("2129632"), rules=["Зачистка резервуаров"], region="my",
    )
    label = str(cfg.get("settings", {}).get("region_label", "ЮФО"))
    code = 0
    for n in external:
        ok = n.send_items([sample], label, "Тестовое уведомление")
        print(f"{n.name}: {'отправлено' if ok else 'ошибка, подробности выше'}")
        code = code or (0 if ok else 1)
    return code


def cmd_get_chat_id(cfg: dict) -> int:
    tg = cfg.get("telegram", {})
    token = str(tg.get("bot_token", "")).strip()
    if not token:
        print("Сначала впишите bot_token в раздел [telegram] файла config.toml.")
        return 1
    api = str(tg.get("api_base", "https://api.telegram.org")).rstrip("/")
    session = requests.Session()
    if str(tg.get("proxy", "")).strip():
        session.proxies.update({"http": tg["proxy"], "https": tg["proxy"]})
    try:
        data = session.get(f"{api}/bot{token}/getUpdates", timeout=30).json()
    except (requests.RequestException, ValueError) as exc:
        print(f"Не удалось связаться с Telegram: {short_err(exc)}")
        return 1
    if not data.get("ok"):
        print(f"Telegram ответил ошибкой: {data.get('description')}. Проверьте bot_token.")
        return 1
    chats: dict[str, str] = {}
    for update in data.get("result", []):
        for key in ("message", "edited_message", "channel_post", "my_chat_member"):
            chat = (update.get(key) or {}).get("chat")
            if chat:
                title = chat.get("title") or " ".join(
                    x for x in (chat.get("first_name"), chat.get("last_name")) if x) or chat.get("username", "")
                chats[str(chat["id"])] = f"{title} ({chat.get('type')})"
    if not chats:
        print("Бот пока не получил сообщений. Откройте бота в Telegram, нажмите «Запустить» или напишите "
              "ему любое сообщение и выполните команду снова.")
        return 1
    print("Найденные чаты:")
    for chat_id, title in chats.items():
        print(f"  chat_id = \"{chat_id}\"   — {title}")
    if len(chats) == 1:
        chat_id = next(iter(chats))
        text = read_config_text()
        new_text, n = re.subn(r'(?m)^chat_id\s*=\s*""', f'chat_id = "{chat_id}"', text, count=1)
        if n:
            CONFIG_PATH.write_text(new_text, encoding="utf-8")
            print(f"Записал chat_id = \"{chat_id}\" в config.toml. Проверьте отправку: python tender_monitor.py --test")
            return 0
    print("Впишите нужный chat_id в раздел [telegram] файла config.toml.")
    return 0


def cmd_setup_cert(cfg: dict) -> int:
    urls = cfg.get("certs", {}).get("download_urls", CERT_URLS)
    names = cfg.get("certs", {}).get("ca_files", [])
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    saved = 0
    for i, url in enumerate(urls):
        name = names[i] if i < len(names) else Path(urlparse(url).path).name
        try:
            resp = session.get(url, timeout=30)
            resp.raise_for_status()
            data = resp.content
            pem = data.decode("ascii", errors="ignore") if b"-----BEGIN CERTIFICATE-----" in data \
                else ssl.DER_cert_to_PEM_cert(data)
            ssl.create_default_context(cadata=pem)
        except Exception as exc:
            print(f"Не удалось скачать {url}: {short_err(exc)}")
            continue
        (BASE_DIR / name).write_text(pem, encoding="ascii")
        print(f"Сохранён сертификат: {name}")
        saved += 1
    if not saved:
        print("Скачайте сертификаты вручную со страницы https://www.gosuslugi.ru/crt "
              "(«Корневой сертификат» и «Выпускающий сертификат» в формате PEM) и положите рядом со скриптом "
              f"под именами: {', '.join(names)}")
        return 1
    BUNDLE_PATH.unlink(missing_ok=True)
    http = Http(cfg)
    try:
        http.get("https://zakupki.gov.ru/epz/main/public/home.html")
        print("ЕИС открывается, сертификат работает.")
    except FetchError as exc:
        print(f"Сертификаты сохранены, но ЕИС пока не открылась: {exc}. "
              "Если вы за пределами России, ЕИС недоступна без российского прокси.")
    return 0


def describe_forms(html: str) -> list[str]:
    """Поля фильтров на странице (GET-формы): пригодятся, чтобы настроить адрес с фильтром."""
    lines = []
    for form in BeautifulSoup(html, "html.parser").find_all("form"):
        if (form.get("method") or "get").lower() != "get":
            continue
        for select in form.find_all("select")[:4]:
            name = select.get("name") or select.get("id") or "?"
            options = []
            for opt in select.find_all("option")[:10]:
                label = clean(opt.get_text(" ", strip=True))
                value = opt.get("value")
                options.append(f"{label}={value}" if value not in (None, "", label) else label)
            lines.append(f"{name}: " + ", ".join(o for o in options if o))
    return lines[:6]


def probe_hint(src: Source, info: dict, error: str) -> str:
    host = (urlparse(src.url).hostname or "").lower()
    if host.endswith("zakupki.gov.ru"):
        cert = "Выполните python tender_monitor.py --setup-cert. " if "сертификат" in error else ""
        return cert + ("ЕИС открывается только с российских IP-адресов; за границей нужен российский прокси "
                       "(proxy в [settings]).")
    if "сертификат Минцифры" in error:
        return "Выполните: python tender_monitor.py --setup-cert"
    if "ошибка сертификата" in error and is_ru_zone(host):
        return "Возможно, сайт перешёл на сертификат Минцифры: выполните python tender_monitor.py --setup-cert"
    if "не найдено ни одного тендера" in error:
        html = info.get("html", "")
        text_len = len(BeautifulSoup(html, "html.parser").get_text(" ", strip=True)) if html else 0
        if JS_APP_RE.search(html) or (html and text_len < 400):
            return ("Список на этой странице строится JavaScript'ом, скрипт его не видит. "
                    "Поищите заказчика на РосТендере (rostender.info) и добавьте его RSS-ленту.")
        return "Возможно, сайт изменил вёрстку. Пришлите файл probe_report.txt, чтобы поправить разбор."
    if "robots.txt" in error:
        return "Сайт запретил роботам читать эту страницу; источник лучше выключить (enabled = false)."
    if "HTTP 404" in error or "HTTP 410" in error:
        return "Адрес устарел: найдите на сайте компании новую страницу закупок и поменяйте url."
    if "HTTP 403" in error or "оборвал соединение" in error:
        return "Сайт не пускает автоматические запросы или зарубежные IP-адреса."
    return ""


def cmd_probe(cfg: dict, name_filter: str = "") -> int:
    """Проверка источников: из каждого достаёт тендеры и показывает, что получилось. Ничего не отправляет."""
    settings = cfg.get("settings", {})
    sources = load_sources(cfg)
    if name_filter:
        sources = [s for s in sources if norm(name_filter) in norm(s.name)]
        if not sources:
            print(f"Нет источников, в названии которых есть «{name_filter}».")
            return 1
    else:
        sources = [s for s in sources if s.enabled]
    http, matcher, regions = Http(cfg), Matcher(cfg), RegionFilter(settings)
    label = str(settings.get("region_label", "ваш регион"))
    loose = dict(settings, only_my_regions=False, max_age_days=0)
    lines: list[str] = []

    def out(text: str = "") -> None:
        print(text, flush=True)
        lines.append(text)

    out(f"Проверка источников {datetime.now(MSK).strftime('%d.%m.%Y %H:%M')} МСК, версия скрипта {VERSION}")
    out(f"Источников: {len(sources)}. Между запросами к одному сайту пауза, поэтому это займёт несколько минут.")
    ok = failed = empty = 0
    for src in sources:
        out()
        info: dict = {}
        started = time.monotonic()
        try:
            items = collect(src, http, info)
        except Exception as exc:
            failed += 1
            error = str(exc) if isinstance(exc, FetchError) else f"{exc.__class__.__name__}: {short_err(exc)}"
            out(f"✗ {src.name}")
            out(f"    {src.url}")
            out(f"    ошибка: {error}")
            hint = probe_hint(src, info, error)
            if hint:
                out(f"    {hint}")
            continue
        ok += 1
        pages = info.get("pages", [])
        page_note = f", страниц прочитано {len(pages)}" if len(pages) > 1 else ""
        out(f"✓ {src.name}: записей {len(items)}{page_note}, {time.monotonic() - started:.0f} с")
        out(f"    {src.url}")
        if "required" in info:
            out(f"    с нужными словами (require): {info['required'][1]} из {info['required'][0]}")
        for note in info.get("notes", []):
            out(f"    {note}")
        if not items:
            empty += 1
            note = " (у РосТендера в ленте только тендеры последних дней)" if "rostender" in src.url else ""
            out(f"    сейчас пусто{note}")
            continue
        dates = [d for d in (i.published_dt() for i in items) if d]
        if dates:
            out(f"    самая свежая запись: {max(dates).astimezone(MSK).strftime('%d.%m.%Y')}")
        out("    первые записи:")
        for item in items[:3]:
            region = regions.classify(item)
            details = [x for x in (
                item.published_dt().astimezone(MSK).strftime("%d.%m.%Y") if item.published_dt() else "",
                truncate(item.place, 60), item.price, f"до {item.deadline}" if item.deadline else "",
                label if region == "my" else REGION_NAMES.get(region, ""),
            ) if x]
            out(f"      • {truncate(item.title, 120)}")
            if details:
                out(f"        {' | '.join(details)}")
            out(f"        {item.link}")
        by_words = [i for i in items if evaluate(i, matcher, regions, loose)]
        wanted = [i for i in by_words if evaluate(i, matcher, regions, settings)]
        out(f"    подходят по словам: {len(by_words)}, из них пришли бы уведомлением (регион, свежесть): {len(wanted)}")
        for item in by_words[:5]:
            where = label if item.region == "my" else REGION_NAMES.get(item.region, "")
            out(f"      → {truncate(item.title, 110)} ({where})")
        if src.type == "html" and info.get("html"):
            forms = describe_forms(info["html"])
            if forms:
                out("    фильтры на странице: " + "; ".join(truncate(f, 160) for f in forms))
    out()
    out(f"Итого: работают {ok} из {len(sources)}, с ошибкой {failed}, пустых сейчас {empty}.")
    try:
        PROBE_REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"Отчёт сохранён в файл {PROBE_REPORT_PATH.name} рядом со скриптом.")
    except OSError as exc:
        print(f"Не удалось сохранить отчёт: {exc}")
    return 0 if not failed else 1


def cmd_check(cfg: dict, text: str) -> int:
    settings = cfg.get("settings", {})
    matcher = Matcher(cfg)
    excluded = matcher.excluded_by([norm(text)])
    rules = matcher.match([text])
    region = RegionFilter(settings).classify(Item(uid="check", title=text, link=""))
    print(f"Текст: {text}")
    if excluded:
        print(f"Не подходит: слово-исключение «{excluded}»")
    elif rules:
        print("Подходит по правилам: " + ", ".join(rules))
    else:
        print("Не подходит: ни одно правило не сработало")
    label = settings.get("region_label", "ваш регион")
    print(f"Регион: {label}" if region == "my" else "Регион: в тексте не найден")
    return 0


def cmd_status() -> int:
    if not DB_PATH.exists():
        print("Проверок ещё не было.")
        return 0
    store = Store(DB_PATH)
    rows = store.db.execute("SELECT * FROM sources ORDER BY name").fetchall()
    for row in rows:
        mark = "✓" if row["fail_count"] == 0 and row["last_ok"] else "×"
        last = (row["last_ok"] or "никогда")[:16].replace("T", " ")
        err = f" — ошибка: {row['last_error']}" if row["fail_count"] else ""
        print(f"{mark} {row['name']}: последний успех {last} UTC, записей {row['last_count']}{err}")
    seen = store.db.execute("SELECT COUNT(*), SUM(matched) FROM seen").fetchone()
    sent = store.db.execute("SELECT COUNT(*) FROM sent").fetchone()[0]
    queued = store.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
    print(f"\nЗаписей в базе: {seen[0]}, подходящих: {seen[1] or 0}, отправлено уведомлений: {sent}, "
          f"ждут отправки: {queued}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Мониторинг тендеров на зачистку резервуаров")
    parser.add_argument("--loop", action="store_true", help="проверять постоянно с интервалом из config.toml")
    parser.add_argument("--test", action="store_true", help="отправить тестовое уведомление")
    parser.add_argument("--get-chat-id", action="store_true", help="узнать chat_id для Telegram")
    parser.add_argument("--setup-cert", action="store_true", help="скачать сертификат Минцифры для ЕИС")
    parser.add_argument("--probe", nargs="?", const="", default=None, metavar="ИСТОЧНИК",
                        help="проверить источники (все или те, в названии которых есть слово) и сохранить отчёт")
    parser.add_argument("--dry-run", action="store_true", help="показать подходящие тендеры, ничего не отправляя")
    parser.add_argument("--check", metavar="ТЕКСТ", help="проверить, пройдёт ли название фильтр")
    parser.add_argument("--status", action="store_true", help="состояние источников")
    parser.add_argument("--verbose", action="store_true", help="подробный журнал")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)
    if args.status:
        return cmd_status()
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(exc)
        return 2
    if args.test:
        return cmd_test(cfg)
    if args.get_chat_id:
        return cmd_get_chat_id(cfg)
    if args.setup_cert:
        return cmd_setup_cert(cfg)
    if args.check:
        return cmd_check(cfg, args.check)
    if args.probe is not None:
        return cmd_probe(cfg, args.probe)
    if args.dry_run:
        return run_once(cfg, dry_run=True)

    interval = max(5, int(cfg.get("settings", {}).get("interval_minutes", 30)))
    if not acquire_lock(interval):
        log.warning("Проверка уже идёт в другом окне или задаче. Если это не так, удалите файл %s", LOCK_PATH.name)
        return 1
    if not args.loop:
        return run_once(cfg)

    log.info("Мониторинг запущен, проверка каждые %d мин. Остановить: Ctrl+C", interval)
    while True:
        try:
            try:
                cfg = load_config()
            except ConfigError as exc:
                # опечатка в файле, который правят на ходу: работаем со старыми настройками
                log.error("%s\nПродолжаю с прежними настройками.", exc)
            interval = max(5, int(cfg.get("settings", {}).get("interval_minutes", 30)))
            run_once(cfg)
        except KeyboardInterrupt:
            break
        except SystemExit:
            raise
        except Exception:
            log.exception("Сбой проверки, продолжу через %d мин", interval)
        try:
            os.utime(LOCK_PATH)
            time.sleep(interval * 60 + random.randint(0, 60))
        except KeyboardInterrupt:
            break
    log.info("Мониторинг остановлен")
    return 0


if __name__ == "__main__":
    sys.exit(main())
