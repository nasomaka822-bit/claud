# -*- coding: utf-8 -*-
"""
Тесты разбора всех типов источников на сохранённых образцах страниц.

Образцы в папке fixtures повторяют устройство настоящих страниц, каким оно было
27.09.2026: RSS РосТендера и ЕИС, страница категории РосТендера, «Степь», ЛУКОЙЛ,
ЕвроХим, КСК и сайт на JavaScript. Интернет для тестов не нужен.

Запуск из папки со скриптом:
    python -m unittest discover -s tests -v
Живая проверка настоящих сайтов — другая команда:
    python tender_monitor.py --probe
"""
from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIX = HERE / "fixtures"
sys.path.insert(0, str(HERE.parent))

import logging  # noqa: E402

import tender_monitor as tm  # noqa: E402

tm.log.addHandler(logging.NullHandler())  # предупреждения скрипта не мешают выводу тестов

CFG = tm.tomllib.loads(tm.DEFAULT_CONFIG)
SETTINGS = CFG["settings"]
MATCHER = tm.Matcher(CFG)
REGIONS = tm.RegionFilter(SETTINGS)
STRICT = dict(SETTINGS, max_age_days=0)                        # регион учитывается, возраст нет
LOOSE = dict(SETTINGS, max_age_days=0, only_my_regions=False)  # только слова


ISSUERS_TMP = tempfile.TemporaryDirectory()
tm.ISSUERS_DIR = Path(ISSUERS_TMP.name) / "issuer_certs"  # тесты не трогают папку со скриптом


@contextlib.contextmanager
def patched(obj, name, value):
    original = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, original)


class FakeResponse:
    status_code = 200

    def __init__(self, url: str, body: bytes, ctype: str):
        self.url = url
        self.content = body
        self.headers = {"Content-Type": ctype}

    @property
    def text(self) -> str:
        return self.content.decode("utf-8")


class FakeHttp:
    """Вместо интернета отдаёт файлы из fixtures; неизвестный адрес — ошибка 404."""

    def __init__(self, routes: dict[str, str]):
        self.routes = routes
        self.calls: list[str] = []

    def get(self, url: str, *, check_robots: bool = False) -> FakeResponse:
        self.calls.append(url)
        name = self.routes.get(url)
        if name is None:
            raise tm.FetchError(tm.describe_status(404))
        ctype = "application/rss+xml; charset=utf-8" if name.endswith(".xml") else "text/html; charset=utf-8"
        return FakeResponse(url, (FIX / name).read_bytes(), ctype)


def rub(amount: str) -> str:
    """Цена так, как её пишет скрипт: неразрывные пробелы между разрядами."""
    return amount.replace(" ", "\u00a0") + "\u00a0₽"


def source(raw: dict) -> tm.Source:
    return tm.load_sources({"sources": [raw]})[0]


def wanted(items, settings=STRICT) -> list[str]:
    return [i.uid for i in items if tm.evaluate(i, MATCHER, REGIONS, settings)]


class RosTenderRss(unittest.TestCase):
    def setUp(self):
        src = source({"name": "РТ", "type": "rss", "url": "https://rostender.info/rss-category-839.xml"})
        self.items = tm.items_from_feed(src, (FIX / "rostender_rss.xml").read_bytes())

    def test_full_title_instead_of_cut(self):
        a, b, c, d = self.items
        self.assertEqual(a.uid, "rostender:95246665")
        self.assertEqual(a.title, "Комплексная услуга по зачистке и градуировке резервуаров из-под нефтепродуктов "
                                  "с последующей утилизацией шлама. Согласно ТЗ.")
        self.assertEqual(b.title, "Оказание услуг по зачистке ёмкостей (резервуаров) для хранения ГСМ")
        self.assertEqual(c.title, "Выполнение работ позачистке и демонтажу подземных железобетонных резервуаров "
                                  "№337, №338, подлежащих ликвидации")
        self.assertEqual(d.title, "Водонагреватели; Расширительные баки; Насосное оборудование; Компенсаторы")

    def test_place_price_date(self):
        a, b, c, d = self.items
        self.assertEqual(a.place, "п.Сычево, Волоколамский Район")
        self.assertEqual(a.price, "")  # «0 руб.» — цены нет
        self.assertEqual(b.place, "г. Ростов-на-Дону")
        self.assertEqual(b.price, rub("248 919"))
        self.assertEqual(c.place, "г. Сызрань")  # описание одной строкой через «;»
        self.assertEqual(d.place, "г. Москва")
        self.assertTrue(a.published.startswith("2026-09-25T10:45:23+10:00"))

    def test_region_from_link(self):
        self.assertEqual([REGIONS.classify(i) for i in self.items], ["other", "my", "other", "other"])

    def test_matching(self):
        a, b, c, d = self.items
        self.assertEqual(wanted(self.items, LOOSE), [a.uid, b.uid, c.uid])
        self.assertEqual(wanted(self.items), [b.uid])


class RosTenderPage(unittest.TestCase):
    URL = "https://rostender.info/category/tendery-na-zachistku-rezervuarov"

    def setUp(self):
        self.http = FakeHttp({self.URL: "rostender_category.html"})
        self.items = tm.collect(source({"name": "РТ стр.", "type": "html", "url": self.URL}), self.http)

    def test_rows(self):
        uids = [i.uid for i in self.items]
        # ссылка /tender/95249089 из блока «Новые тендеры» без региона в адресе не берётся
        self.assertEqual(uids, ["rostender:95246665", "rostender:95224663", "rostender:95111679"])
        a, b, c = self.items
        self.assertEqual(a.title, "Комплексная услуга по зачистке и градуировке резервуаров из-под нефтепродуктов "
                                  "с последующей утилизацией шлама. Согласно ТЗ.")
        self.assertEqual(a.deadline, "01.10.2026 11:00")
        self.assertEqual(a.place, "п.Сычево, Волоколамский Район")
        self.assertTrue(a.published.startswith("2026-09-25"))
        self.assertNotIn("осталось", a.context)  # меняющийся счётчик дней убран
        self.assertEqual(b.price, rub("248 919"))
        self.assertEqual(c.place, "Городищенский район, поселок Котлубань; г. Волгоград")
        self.assertEqual(c.link, "https://rostender.info/region/volgogradskaya-oblast/volgograd/"
                                 "95111679-tender-okazanie-uslug-po-zachistke-rezervuarov")

    def test_regions_and_matching(self):
        self.assertEqual([REGIONS.classify(i) for i in self.items], ["other", "my", "my"])
        self.assertEqual(wanted(self.items), ["rostender:95224663", "rostender:95111679"])

    def test_same_ids_as_rss(self):
        rss = tm.items_from_feed(source({"name": "РТ", "type": "rss", "url": "https://rostender.info/rss-category-839.xml"}),
                                 (FIX / "rostender_rss.xml").read_bytes())
        self.assertTrue({i.uid for i in rss} & {i.uid for i in self.items})
        self.assertEqual(rss[1].fingerprint(), self.items[1].fingerprint())


class Eis(unittest.TestCase):
    def setUp(self):
        self.src = source({"name": "ЕИС", "type": "eis", "query": "зачистка резервуаров"})
        self.items = tm.items_from_feed(self.src, (FIX / "eis_rss.xml").read_bytes())

    def test_url(self):
        self.assertTrue(self.src.url.startswith("https://zakupki.gov.ru/epz/order/extendedsearch/rss.html?searchString="))
        self.assertIn("fz44=on", self.src.url)
        self.assertIn("fz223=on", self.src.url)

    def test_fields(self):
        a, b, c = self.items
        self.assertEqual(a.uid, "eis:0318100057726000011")
        self.assertEqual(a.title, "Выполнение работ по зачистке технических средств службы горючего")
        self.assertEqual(a.region_code, "23")
        self.assertEqual(a.price, rub("1 450 000"))
        self.assertTrue(a.published.startswith("2026-09-24"))
        self.assertTrue(b.unnamed)
        self.assertEqual(b.region_code, "61")
        self.assertEqual(c.region_code, "77")

    def test_matching(self):
        a, b, c = self.items
        self.assertEqual(wanted(self.items), [a.uid, b.uid])  # c — Москва
        self.assertEqual(b.rules, ["Совпадение в документах ЕИС"])


class AhStep(unittest.TestCase):
    URL = "https://www.ahstep.ru/tender"

    def test_pages_titles_ids(self):
        src = source({"name": "Степь", "type": "html", "url": self.URL,
                      "page_url": self.URL + "?page={page}", "pages": 3})
        http = FakeHttp({self.URL: "ahstep_page1.html", self.URL + "?page=2": "ahstep_page2.html"})
        info: dict = {}
        items = tm.collect(src, http, info)
        self.assertEqual(http.calls, [self.URL, self.URL + "?page=2", self.URL + "?page=3"])
        self.assertEqual(len(items), 6)  # 4 + 3, одна закупка повторилась на второй странице
        self.assertEqual(items[0].uid, "html:www.ahstep.ru:63925926397312")
        self.assertEqual(items[0].title, "Выполнение земляных (вертикальная планировка) на МТФ 9 в ст. Марьинская "
                                         "Ставропольского края")
        self.assertEqual(items[2].title, "Разработка документации по техническому перевооружению ОПО «Склад силосного "
                                         "типа» зернового терминала «СТЕПЬ» в г. Азов Ростовской области")
        self.assertEqual(items[1].link, "https://www.ahstep.ru/tenders/tender63925999000001")
        self.assertEqual(len(info["notes"]), 1)  # третьей страницы нет — это не ошибка
        self.assertEqual(REGIONS.classify(items[1]), "my")
        self.assertEqual([i.title for i in items if tm.evaluate(i, MATCHER, REGIONS, STRICT)],
                         ["Зачистка емкостей ГСМ и резервуаров нефтесклада в г. Азов Ростовской области"])


class Lukoil(unittest.TestCase):
    URL = "https://lukoil.ru/Company/Tendersandauctions/Tenders/TendersofLukoilgroup"

    def setUp(self):
        src = source({"name": "ЛУКОЙЛ", "type": "html", "url": self.URL,
                      "page_url": self.URL + "?take=10&skip={offset}", "page_size": 10, "pages": 70})
        self.http = FakeHttp({self.URL: "lukoil_page1.html",
                              self.URL + "?take=10&skip=10": "lukoil_page2.html",
                              self.URL + "?take=10&skip=20": "lukoil_empty.html"})
        self.items = tm.collect(src, self.http)

    def test_stops_on_empty_page(self):
        self.assertEqual(len(self.http.calls), 3)

    def test_items(self):
        self.assertEqual([i.uid for i in self.items], [
            "html:lukoil.ru:ПК-10-2026", "html:lukoil.ru:1234-26", "html:lukoil.ru:050-0037-26",
            "html:lukoil.ru:050-0040-26", "html:lukoil.ru:LUO/63/08-26/1487"])
        self.assertEqual(self.items[0].title, "Производство, поставка и монтаж мебели и торгового оборудования для "
                                              "строительства/модернизации объектов розничной реализации (АЗС/АЗК)")
        self.assertEqual(self.items[3].title, "Выполнение работ по пропарке и дегазации резервуаров товарного парка "
                                              "в 2027 году")
        self.assertEqual(self.items[4].title, "Поставка ЗИП для факельных установок")
        self.assertEqual(self.items[1].customer, '"ЛУКОЙЛ-Югнефтепродукт"')
        self.assertEqual(self.items[1].deadline, "15.10.2026 0:00")
        self.assertTrue(all(i.link == self.URL for i in self.items))

    def test_documents_are_not_tenders(self):
        self.assertFalse(any("Техническое задание" in i.title for i in self.items))

    def test_matching(self):
        self.assertEqual(wanted(self.items, LOOSE), ["html:lukoil.ru:1234-26", "html:lukoil.ru:050-0040-26"])
        self.assertEqual(REGIONS.classify(self.items[3]), "my")  # Волгограднефтепереработка
        self.assertEqual(REGIONS.classify(self.items[1]), "my")  # Югнефтепродукт
        self.assertEqual(REGIONS.classify(self.items[0]), "unknown")  # головная компания


class EuroChem(unittest.TestCase):
    def test_require_and_links(self):
        url = "https://zakupki.eurochem.ru/aktualnye-zakupki1"
        src = source({"name": "ЕвроХим", "type": "html", "url": url,
                      "require": ["бму", "белореченск", "волгакалий", "волгасервис", "котельников"]})
        info: dict = {}
        items = tm.collect(src, FakeHttp({url: "eurochem.html"}), info)
        self.assertEqual(info["required"], (4, 2))
        self.assertEqual([i.uid for i in items], ["html:zakupki.eurochem.ru:4616001", "html:zakupki.eurochem.ru:4608364"])
        self.assertTrue(items[0].link.startswith("https://www.b2b-center.ru/"))
        self.assertEqual([i.title for i in items if tm.evaluate(i, MATCHER, REGIONS, STRICT)],
                         ["ЗАЧИСТКА РЕЗЕРВУАРОВ ХРАНЕНИЯ МАЗУТА КОТЕЛЬНОЙ"])


class GenericPage(unittest.TestCase):
    def test_page_without_links(self):
        url = "https://www.gt-ksk.com/about/tenders/"
        items = tm.collect(source({"name": "КСК", "type": "html", "url": url}), FakeHttp({url: "ksk.html"}))
        titles = [i.title for i in items]
        self.assertIn("№18 Зачистка резервуаров дизельного топлива на территории терминала", titles)
        self.assertEqual(len(items), 3)
        self.assertEqual([i.title for i in items if tm.evaluate(i, MATCHER, REGIONS, STRICT)],
                         ["№18 Зачистка резервуаров дизельного топлива на территории терминала"])
        # приложенные файлы и заголовок страницы — не тендеры
        self.assertFalse(any(t.endswith((".jpg", ".gsfx", ".docx")) or t == "Тендеры и закупки" for t in titles))

    def test_javascript_page_is_an_error(self):
        url = "https://goldenseed.ru/tenders"
        src = source({"name": "Юг Руси", "type": "html", "url": url})
        info: dict = {}
        with self.assertRaises(tm.FetchError) as ctx:
            tm.collect(src, FakeHttp({url: "js_app.html"}), info)
        self.assertIn("JavaScript", tm.probe_hint(src, info, str(ctx.exception)))


class Robots(unittest.TestCase):
    def test_rostender_wildcards(self):
        r = tm.Robots("User-agent: *\nAllow: /*?page=*\nDisallow: /*?*\nDisallow: /search?*\n")
        base = "https://rostender.info"
        self.assertTrue(r.allowed(base + "/category/tendery-na-zachistku-rezervuarov"))
        self.assertTrue(r.allowed(base + "/category/tendery-na-zachistku-rezervuarov?page=2"))
        self.assertFalse(r.allowed(base + "/category/tendery-na-zachistku-rezervuarov?active_filter=1&kladr23=on"))
        self.assertTrue(r.allowed(base + "/rss-category-839.xml"))

    def test_groups_and_anchors(self):
        r = tm.Robots("User-agent: *\nDisallow: /market/*\n\nUser-agent: TenderMonitor\nDisallow: /private/\n")
        self.assertTrue(r.allowed("https://x.ru/market/tender-1/"))  # своя группа заменяет «*»
        self.assertFalse(r.allowed("https://x.ru/private/list"))
        self.assertTrue(tm.Robots("User-agent: *\nDisallow:\n").allowed("https://x.ru/any"))
        pdf = tm.Robots("User-agent: *\nDisallow: /*.pdf$\n")
        self.assertFalse(pdf.allowed("https://x.ru/a.pdf"))
        self.assertTrue(pdf.allowed("https://x.ru/a.pdf?x=1"))


class Words(unittest.TestCase):
    POSITIVE = [
        "Оказание услуг по зачистке резервуаров",
        "Выполнение работ позачистке и демонтажу подземных железобетонных резервуаров",
        "Зачистка газовых емкостей из-под сжиженного газа",
        "Оказание услуг по сбору, зачистке нефтешлама и ила",
        "Выполнение работ по размыву донных отложений резервуаров хранения сырой нефти",
        "Оказание услуг на чистку емкостей под дизельное топливо",
        "Пропарка и дегазация автоцистерн",
        "Выполнение работ по техническому диагностированию и зачистке технических средств службы горючего",
        "Очистка резервуаров чистой воды",
        "Зачистка мазутных емкостей и утилизация нефтешлама",
    ]
    NEGATIVE = [
        "Оказание услуг по комплексной гидродинамической очистке технологических емкостей очистных сооружений "
        "канализации и канализационных станций",
        "Поставка краски для резервуарного парка",
        "Вывоз септических отходов",
        "Поставка ГСМ",
        "Закупка пропана",
        "Покрытие антикоррозионное для резервуара",
    ]

    def test_positive(self):
        for text in self.POSITIVE:
            with self.subTest(text=text):
                self.assertTrue(MATCHER.match([text]))

    def test_negative(self):
        for text in self.NEGATIVE:
            with self.subTest(text=text):
                self.assertFalse(MATCHER.match([text]))

    def test_tidy_title(self):
        self.assertEqual(tm.tidy_title("АО Агрохолдинг «СТЕПЬ» объявляет о проведении открытого запроса предложений "
                                       "на: «Корень мыльный (кг)»"), "Корень мыльный (кг)")
        self.assertEqual(tm.tidy_title("Зачистка резервуаров, осталось 3 дня"), "Зачистка резервуаров,")


class Schedule(unittest.TestCase):
    def test_every_minutes(self):
        store = tm.Store(":memory:")
        src = source({"name": "РТ стр.", "type": "html", "url": "https://rostender.info/category/x",
                      "every_minutes": 120})
        self.assertTrue(store.due(src, store.source_state(src)))  # новый источник читается сразу
        store.source_ok(src, 1)
        store.touch(src)
        self.assertFalse(store.due(src, store.source_state(src)))
        old = (tm.now_utc() - timedelta(minutes=119)).isoformat()  # 2 минуты запаса
        store.db.execute("UPDATE sources SET last_check = ?", (old,))
        self.assertTrue(store.due(src, store.source_state(src)))

    def test_page_urls(self):
        src = source({"name": "Л", "type": "html", "url": "https://x.ru/list",
                      "page_url": "https://x.ru/list?take=10&skip={offset}", "pages": 3})
        self.assertEqual(src.page_urls(), ["https://x.ru/list", "https://x.ru/list?take=10&skip=10",
                                           "https://x.ru/list?take=10&skip=20"])


class Probe(unittest.TestCase):
    def test_report(self):
        cfg = dict(CFG)
        cfg["sources"] = [
            {"name": "РТ лента", "type": "rss", "url": "https://rostender.info/rss-category-839.xml"},
            {"name": "Юг Руси сайт", "type": "html", "url": "https://goldenseed.ru/tenders"},
            {"name": "КСК сайт", "type": "html", "url": "https://www.gt-ksk.com/about/tenders/"},
        ]
        routes = {"https://rostender.info/rss-category-839.xml": "rostender_rss.xml",
                  "https://goldenseed.ru/tenders": "js_app.html",
                  "https://www.gt-ksk.com/about/tenders/": "ksk.html"}
        original_http, original_path, original_pages = tm.Http, tm.PROBE_REPORT_PATH, tm.PROBE_PAGES_DIR
        with tempfile.TemporaryDirectory() as tmp:
            tm.Http = lambda cfg: FakeHttp(routes)
            tm.PROBE_REPORT_PATH = Path(tmp) / "probe_report.txt"
            tm.PROBE_PAGES_DIR = Path(tmp) / "probe_pages"
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    code = tm.cmd_probe(cfg)
                report = tm.PROBE_REPORT_PATH.read_text(encoding="utf-8")
                pages = sorted(p.name for p in tm.PROBE_PAGES_DIR.iterdir())
            finally:
                tm.Http, tm.PROBE_REPORT_PATH, tm.PROBE_PAGES_DIR = original_http, original_path, original_pages
        self.assertEqual(code, 1)
        self.assertIn("✓ РТ лента: записей 4", report)
        self.assertIn("Оказание услуг по зачистке ёмкостей (резервуаров) для хранения ГСМ", report)
        self.assertIn("✗ Юг Руси сайт", report)
        self.assertIn("JavaScript", report)
        self.assertIn("страница сохранена: probe_pages/КСК_сайт.html", report)
        self.assertEqual(pages, ["КСК_сайт.html"])


class EdgeCases(unittest.TestCase):
    """Случаи, найденные при проверке кода."""

    LUKOIL = "https://lukoil.ru/Company/Tendersandauctions/Tenders/TendersofLukoilgroup"

    def test_single_tender_on_last_page_keeps_its_title(self):
        src = source({"name": "ЛУКОЙЛ", "type": "html", "url": self.LUKOIL,
                      "page_url": self.LUKOIL + "?take=10&skip={offset}", "pages": 200})
        http = FakeHttp({self.LUKOIL: "lukoil_page1.html", self.LUKOIL + "?take=10&skip=10": "lukoil_last_page.html"})
        items = tm.collect(src, http)
        last = items[-1]
        self.assertEqual(last.uid, "html:lukoil.ru:2001-26")
        self.assertEqual(last.title, "Оказание услуг по зачистке резервуаров нефтебазы ООО «ЛУКОЙЛ-Югнефтепродукт» "
                                     "в г. Армавир")
        self.assertNotIn("Страна-регион", last.context)  # фильтр страницы не попал в строку тендера

    def test_single_tender_page_alone(self):
        # даже без подсказки с первой страницы блок не поднимается до заголовка страницы
        items = tm.collect(source({"name": "Л", "type": "html", "url": self.LUKOIL}),
                           FakeHttp({self.LUKOIL: "lukoil_last_page.html"}))
        self.assertEqual(items[0].title, "Оказание услуг по зачистке резервуаров нефтебазы ООО «ЛУКОЙЛ-Югнефтепродукт» "
                                         "в г. Армавир")

    def test_tls_drop_is_not_a_certificate_problem(self):
        import requests
        http = tm.Http(CFG)

        def boom(*args, **kwargs):
            raise requests.exceptions.SSLError("EOF occurred in violation of protocol (_ssl.c:1006)")

        http.session.get = boom
        original_sleep = tm.time.sleep
        tm.time.sleep = lambda s: None
        try:
            with self.assertRaises(tm.FetchError) as ctx:
                http.get("https://zakupki.gov.ru/epz/order/extendedsearch/rss.html")
        finally:
            tm.time.sleep = original_sleep
        self.assertIn("оборвал соединение", str(ctx.exception))
        self.assertNotIn("сертификат", str(ctx.exception))

    def test_certificate_error_message(self):
        import requests
        http = tm.Http(CFG)
        http.bundle = None

        def boom(*args, **kwargs):
            raise requests.exceptions.SSLError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
                                               "unable to get local issuer certificate")

        def no_site(*args, **kwargs):
            raise OSError("нет сети")

        http.session.get = boom
        with patched(tm.ssl, "get_server_certificate", no_site):
            with self.assertRaises(tm.FetchError) as ctx:
                http.get("https://aston.ru/tenders/current-purchases/")
        self.assertIn("--setup-cert", str(ctx.exception))

    def test_hostname_mismatch_is_not_a_mincifry_problem(self):
        import requests
        http = tm.Http(CFG)

        def boom(*args, **kwargs):
            raise requests.exceptions.SSLError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
                                               "Hostname mismatch, certificate is not valid for 'gas.crimea.ru'.")

        http.session.get = boom
        with self.assertRaises(tm.FetchError) as ctx:
            http.get("https://gas.crimea.ru/gosudarstvennye-zakupki")
        self.assertIn("выписан на другой адрес", str(ctx.exception))
        src = source({"name": "Ч", "type": "html", "url": "https://gas.crimea.ru/gosudarstvennye-zakupki"})
        self.assertNotIn("--setup-cert", tm.probe_hint(src, {}, str(ctx.exception)))

    def test_config_typo_raises_config_error(self):
        original = tm.CONFIG_PATH
        with tempfile.TemporaryDirectory() as tmp:
            tm.CONFIG_PATH = Path(tmp) / "config.toml"
            tm.CONFIG_PATH.write_text('[[sources]]\nitem_link = "tender(\\d+)"\n', encoding="utf-8")
            try:
                with self.assertRaises(tm.ConfigError) as ctx:
                    tm.load_config()
            finally:
                tm.CONFIG_PATH = original
        self.assertIn("одинарных кавычках", str(ctx.exception))

    def test_rostender_slug_beats_words(self):
        item = tm.Item(uid="rostender:1", title="Зачистка резервуаров АЗС на Волгоградском проспекте",
                       link="https://rostender.info/region/moskva-gorod/1234567-tender-x", place="г. Москва")
        self.assertEqual(REGIONS.classify(item), "other")

    def test_bad_link_does_not_break_page(self):
        html = ('<html><body><table>'
                '<tr><td><a href="http://[object Object]/x">Битая ссылка на тендер с длинным названием</a></td></tr>'
                '<tr><td><a href="/tenders/tender77">Зачистка резервуаров ГСМ на МТФ-3</a></td></tr>'
                '</table></body></html>').encode("utf-8")
        resp = FakeResponse("https://www.ahstep.ru/tender", html, "text/html; charset=utf-8")
        items = tm.items_from_html(source({"name": "С", "type": "html", "url": "https://www.ahstep.ru/tender"}), resp)
        self.assertEqual([i.uid for i in items], ["html:www.ahstep.ru:77"])
        generic = tm.items_from_html(source({"name": "G", "type": "html", "url": "https://x.ru/list"}), resp)
        self.assertTrue(generic)

    def test_announce_without_colon_is_kept(self):
        title = "ПАО «НМТП» объявляет запрос предложений на выполнение работ по зачистке резервуаров на «Шесхарис»"
        self.assertEqual(tm.tidy_title(title), title)
        self.assertTrue(MATCHER.match([tm.tidy_title(title)]))

    def test_trimmed_title_keeps_full_text_for_matching(self):
        title, full = tm._title_pair("ООО «Юг» объявляет о проведении запроса предложений на: «Работы на нефтебазе»")
        self.assertEqual(title, "Работы на нефтебазе")
        self.assertIn("объявляет", full)

    def test_page_limit_is_reported(self):
        url = "https://www.ahstep.ru/tender"
        src = source({"name": "Степь", "type": "html", "url": url, "page_url": url + "?page={page}", "pages": 2})
        info: dict = {}
        tm.collect(src, FakeHttp({url: "ahstep_page1.html", url + "?page=2": "ahstep_page2.html"}), info)
        self.assertTrue(any("увеличьте pages" in n for n in info.get("notes", [])))

    def test_robots_with_bom(self):
        r = tm.Robots("﻿User-agent: *\nAllow: /*?page=*\nDisallow: /*?*\n")
        self.assertFalse(r.allowed("https://rostender.info/x?filter=1"))

    def test_require_for_rss_and_non_text_values(self):
        src = source({"name": "РТ", "type": "rss", "url": "https://rostender.info/rss-category-839.xml",
                      "require": ["ростов", 2026]})
        info: dict = {}
        http = FakeHttp({src.url: "rostender_rss.xml"})
        items = tm.collect(src, http, info)
        self.assertEqual([i.uid for i in items], ["rostender:95224663"])
        self.assertEqual(info["required"], (4, 1))
        tm.Matcher({"rules": [{"name": "x", "actions": [123], "objects": ["бак"], "words": [7]}],
                    "exclude": {"words": [1]}})

    def test_big_page_is_fast(self):
        import time
        rows = "".join(
            f'<tr><td>{n}</td><td><a href="/tenders/tender{n}">Поставка запасных частей номер {n} для техники</a>'
            f'</td><td>01.10.2026 16:00</td></tr>' for n in range(600))
        marker_rows = "".join(
            f'<div class="t"><h3>Тендер номер {n} на поставку оборудования</h3><p>№: A-{n}</p>'
            f'<p>Прием заявок до: 01.10.2026</p></div>' for n in range(600))
        html = f"<html><body><h1>Список</h1><table>{rows}</table></body></html>".encode()
        mhtml = f"<html><body><h1>Список</h1><div>{marker_rows}</div></body></html>".encode()
        started = time.monotonic()
        links = tm.items_from_html(source({"name": "С", "type": "html", "url": "https://www.ahstep.ru/tender"}),
                                   FakeResponse("https://www.ahstep.ru/tender", html, "text/html; charset=utf-8"))
        marks = tm.items_from_html(source({"name": "Л", "type": "html", "url": self.LUKOIL}),
                                   FakeResponse(self.LUKOIL, mhtml, "text/html; charset=utf-8"))
        self.assertEqual((len(links), len(marks)), (600, 600))
        self.assertLess(time.monotonic() - started, 15)

    def test_title_change_in_same_source_is_rechecked_once(self):
        store = tm.Store(":memory:")
        first = tm.Item(uid="html:lukoil.ru:2001-26", title="Тендеры Группы «ЛУКОЙЛ»", link="", source="ЛУКОЙЛ")
        store.mark_seen(first)
        fixed = tm.Item(uid=first.uid, title="Зачистка резервуаров нефтебазы", link="", source="ЛУКОЙЛ")
        other_source = tm.Item(uid=first.uid, title="Другое название", link="", source="РосТендер")
        self.assertTrue(store.needs_check(fixed))
        self.assertFalse(store.needs_check(other_source))
        store.mark_seen(fixed)
        store.mark_sent("fp", fixed.uid)
        self.assertFalse(store.needs_check(tm.Item(uid=first.uid, title="Третье название", link="", source="ЛУКОЙЛ")))


class MissingIntermediate(unittest.TestCase):
    """Сайт отдаёт только свой сертификат, без промежуточного (как aston.ru и oteko.ru в сентябре 2026).

    Сертификаты в fixtures/certs тестовые: корневой «Test Root CA», промежуточный «Test Intermediate CA»
    и сертификат сайта localhost со ссылкой на промежуточный http://pki.test/int.crt.
    """
    CERTS = FIX / "certs"

    def setUp(self):
        self.site_pem = (self.CERTS / "site.pem").read_text(encoding="ascii")
        self.site = tm.ssl.PEM_cert_to_DER_cert(self.site_pem)
        self.intermediate = (self.CERTS / "intermediate.der").read_bytes()
        self.root = (self.CERTS / "root.der").read_bytes()
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.enterContext(patched(tm, "ISSUERS_DIR", Path(self.dir.name) / "issuer_certs"))
        self.enterContext(patched(tm.ssl, "get_server_certificate", lambda addr, timeout=None: self.site_pem))

    def http(self, served: dict[str, bytes]):
        import requests
        http = tm.Http(CFG)
        http.calls = []

        def get(url, **kwargs):
            http.calls.append(url)
            resp = requests.Response()
            resp.status_code = 200 if url in served else 404
            resp._content = served.get(url, b"")
            return resp

        http.session.get = get
        return http

    def test_der_parsing(self):
        self.assertEqual(tm.ca_issuer_urls(self.site), ["http://pki.test/int.crt"])  # OCSP не берём
        self.assertEqual(tm.ca_issuer_urls(self.intermediate), ["http://pki.test/root.crt"])
        issuer, subject = tm.cert_names(self.root)
        self.assertEqual(issuer, subject)
        issuer, subject = tm.cert_names(self.intermediate)
        self.assertNotEqual(issuer, subject)
        self.assertEqual(tm.cert_der(tm.ssl.DER_cert_to_PEM_cert(self.intermediate).encode()), self.intermediate)
        self.assertIsNone(tm.cert_der(b"<html>404</html>"))
        self.assertIsNone(tm.cert_der(b"\x30\x03\x02\x01\x01"))

    def test_intermediate_is_saved_root_is_not(self):
        http = self.http({"http://pki.test/int.crt": self.intermediate, "http://pki.test/root.crt": self.root})
        self.assertTrue(http.fetch_missing_issuer("https://aston.ru/tenders/"))
        saved = (tm.ISSUERS_DIR / "aston.ru.crt").read_text(encoding="ascii")
        self.assertEqual(saved.count("BEGIN CERTIFICATE"), 1)
        self.assertEqual(tm.ssl.PEM_cert_to_DER_cert(saved), self.intermediate)
        # сертификат применяется только к своему сайту
        bundle = http.verify_for("https://aston.ru/tenders/")
        self.assertIsInstance(bundle, str)
        self.assertIn(saved.strip(), Path(bundle).read_text(encoding="ascii"))
        self.assertNotIn(bundle, (http.verify_for("https://www.oteko.ru/x"), http.verify_for("https://example.com/")))
        # второй раз за проверку сайт не опрашивается
        self.assertFalse(http.fetch_missing_issuer("https://aston.ru/other"))

    def test_self_signed_answer_is_rejected(self):
        http = self.http({"http://pki.test/int.crt": self.root})  # вместо промежуточного подсунули корневой
        self.assertFalse(http.fetch_missing_issuer("https://aston.ru/tenders/"))
        self.assertFalse(tm.ISSUERS_DIR.exists())

    def test_get_retries_with_downloaded_intermediate(self):
        import requests
        http = self.http({"http://pki.test/int.crt": self.intermediate})
        fetch_get = http.session.get
        verified_with = []

        def get(url, **kwargs):
            if "pki.test" in url:
                return fetch_get(url, **kwargs)
            verified_with.append(kwargs.get("verify"))
            if kwargs.get("verify") is True:
                raise requests.exceptions.SSLError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
                                                   "unable to get local issuer certificate (_ssl.c:1081)")
            return FakeResponse(url, b"<html></html>", "text/html; charset=utf-8")

        http.session.get = get
        with patched(tm.time, "sleep", lambda s: None):
            http.get("https://aston.example/tenders/")
        self.assertEqual(verified_with[0], True)
        self.assertTrue(str(verified_with[1]).endswith("aston.example.bundle.pem"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
