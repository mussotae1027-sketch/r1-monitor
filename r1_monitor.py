#!/usr/bin/env python3
"""야마하 R1 (2015~2019년식) 매물 감시 → 디스코드 알림 (GitHub Actions에서 10분마다 1회 실행)"""
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# ═══════════════════ 설정 (여기만 바꾸면 됨) ═══════════════════
KEYWORDS = ["야마하 R1", "YZF-R1"]
YEAR_MIN, YEAR_MAX = 2015, 2019
BUDGET_A = 10_000_000            # 🔥 표시 기준 (1,000만)
BUDGET_B = 15_000_000            # 최대 예산 (1,500만)
NOTIFY_UNKNOWN_YEAR = False      # True: 연식 안 적힌 매물도 '연식 미확인'으로 알림
SKIP_RESERVED = True             # True: 예약중 매물도 제외 / False: 예약중은 알림 받기
PASSO_ENABLED = True             # False: 파쏘 감시 끄기
PASSO_LIST_URL = "http://bike.passo.co.kr/bike/"
LIGHTWAGEN_SEARCH_URL = "https://www.reitwagen.co.kr/products/home/used?u=models%255B0%255D%3DYZF%2520R1%26modelIds%255B0%255D%3D10895%26brands%255B0%255D%3D%25EC%2595%25BC%25EB%25A7%2588%25ED%2595%2598%26brandIds%255B0%255D%3D5&s=models%255B0%255D%3DYZF%2520R1%26modelIds%255B0%255D%3D10895%26brands%255B0%255D%3D%25EC%2595%25BC%25EB%25A7%2588%25ED%2595%2598%26brandIds%255B0%255D%3D5&b=models%255B0%255D%3DYZF%2520R1%26modelIds%255B0%255D%3D10895%26brands%255B0%255D%3D%25EC%2595%25BC%25EB%25A7%2588%25ED%2595%2598%26brandIds%255B0%255D%3D5&p=models%255B0%255D%3DYZF%2520R1%26modelIds%255B0%255D%3D10895%26brands%255B0%255D%3D%25EC%2595%25BC%25EB%25A7%2588%25ED%2595%2598%26brandIds%255B0%255D%3D5&e=keyword%255B0%255D%3DYZF%2520R1"
# ═══════════════════════════════════════════════════════════════

REAL_MIN, REAL_MAX = 5_000_000, 30_000_000      # R1 실제 시세로 인정하는 범위
SEEN_FILE = "seen.json"
WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
KST = timezone(timedelta(hours=9))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")

R1_RE = re.compile(r"(?<![a-z0-9])(?:yzf[\s\-]?)?r\s?1(?:m|s)?(?![0-9a-z\-])|알원", re.I)
EXCLUDE_RE = re.compile(r"mt\s?-?\s?10|r1\s?-?\s?z", re.I)
WANTED_RE = re.compile(r"삽니다|구합니다|구매합니다|구해요|매입합니다")
BIKE_SIGNAL_RE = re.compile(r"[\d만천]\s*(?:km|키로|킬로)|주행|연식|년식|년형|무사고|명의|서류", re.I)

# 판매완료/예약중 판별
SOLD_TEXT_RE = re.compile(r"판매\s*완료|거래\s*완료|sold\s*out" + (r"|예약\s*중" if SKIP_RESERVED else ""), re.I)
SOLD_STATUS_RE = re.compile(r"sold|complete|판매\s*완료|거래\s*완료" + (r"|reserv|예약" if SKIP_RESERVED else ""), re.I)

# 연식 패턴
Y2 = r"(9[89]|[0-2]\d)"
Y4 = r"(199[89]|20[0-2]\d)"
STRONG_YEAR = [   # 제목·본문 모두에서 사용 (확실한 연식 표기)
    re.compile(rf"(?<!\d){Y4}\s*년\s*(?:식|형|도|모델)"),
    re.compile(rf"(?<![\d,.]){Y2}\s*년\s*(?:식|형|도|모델)"),
    re.compile(rf"(?<![\d,.]){Y4}\s*(?:식|형)(?![가-힣])"),
    re.compile(rf"(?<![\d,.]){Y2}\s*(?:식|형)(?![가-힣])"),
    re.compile(rf"연식\s*[:：]?\s*{Y4}"),
    re.compile(rf"연식\s*[:：]?\s*{Y2}(?!\d)"),
    re.compile(rf"(?<![a-z])my\s?'?{Y2}(?!\d)", re.I),
    re.compile(rf"(?<![\d,.]){Y2}\s*년\s*(?:\d{{1,2}}\s*월\s*)?(?:등록|출고|최초)"),
]
TITLE_YEAR = [    # 제목에서만 사용 (느슨한 표기: '2017', '17년', ''17')
    re.compile(rf"(?<![\d,.]){Y4}(?![\d,.]|\s*(?:만|원|km|cc|키로))", re.I),
    re.compile(rf"(?<![\d,.]){Y2}\s*년(?!\s*(?:동안|간|째|전|이상|넘|정도|탄|탔))"),
    re.compile(rf"['’]{Y2}(?!\d)"),
]

# 가격 패턴
HAN = dict(zip("일이삼사오육칠팔구", range(1, 10)))
D = "[1-9일이삼사오육칠팔구]"
P_KEYWORD = re.compile(r"(?:가격|판매가|금액|희망가)\s*[:：]?\s*(\d{1,2},\d{3}|\d{3,4})(?![\d,])")
P_WON = re.compile(r"(?<![\d,])(\d{1,2},\d{3},\d{3}|\d{7,8})\s*원")
P_MAN = re.compile(r"(?<![\d.,])(\d{1,2},\d{3}|\d{3,4})\s*만(?!\s*(?:k|키로|킬로))", re.I)
P_SELL = re.compile(r"(?<![\d.,])(\d{3,4})\s*에\s*(?:판매|팝니다|팔아|드립|넘깁|정리)")
P_KOR = re.compile(rf"(?<![가-힣])({D}?)\s*천\s*(?:({D})\s*백)?\s*(?:({D})\s*십)?\s*만")
P_KOR2 = re.compile(rf"(?<![가-힣\d])({D})\s*천\s*(?:({D})\s*백)?(?!\s*(?:만|km|키로|킬로|cc|원|rpm))", re.I)


@dataclass
class Listing:
    platform: str
    item_id: str
    title: str
    url: str
    price: int | None = None
    content: str = ""
    posted_at: str = ""
    sold: bool = False

    @property
    def uid(self):
        return f"{self.platform}:{self.item_id}"


def status_sold(d: dict) -> bool:
    """JSON 데이터 안의 state/status 값으로 판매완료 여부 확인"""
    for k, v in d.items():
        if isinstance(v, (str, int)) and re.search(r"state|status|availability", k, re.I):
            if SOLD_STATUS_RE.search(str(v)):
                return True
    return False


# ───────────── 가격/연식 해석 ─────────────
def to_int(v):
    try:
        n = int(float(str(v).replace(",", "").strip()))
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def won(v):
    if not v:
        return "없음"
    return f"{v:,}원" if v < 10_000 else f"{v / 10_000:,.0f}만원"


def plausible(p):
    return bool(p) and REAL_MIN <= p <= REAL_MAX


def kd(c):
    return 1 if not c else (int(c) if c.isdigit() else HAN[c])


def text_price(text):
    text = text or ""
    rules = [(P_KEYWORD, lambda s: int(s.replace(",", "")) * 10_000),
             (P_WON, lambda s: int(s.replace(",", ""))),
             (P_MAN, lambda s: int(s.replace(",", "")) * 10_000),
             (P_SELL, lambda s: int(s) * 10_000)]
    for rx, conv in rules:
        for m in rx.finditer(text):
            v = conv(m.group(1))
            if plausible(v):
                return v
    for rx in (P_KOR, P_KOR2):
        for m in rx.finditer(text):
            v = kd(m.group(1)) * 1000 + (kd(m.group(2)) * 100 if m.group(2) else 0)
            if rx is P_KOR and m.group(3):
                v += kd(m.group(3)) * 10
            v *= 10_000
            if plausible(v):
                return v
    return None


def resolve_price(listed, text, allow_scale):
    """
    가격 결정 순서: ① 등록가가 정상 범위면 그대로 ② 본문/제목에 적힌 가격
    ③ 두 자리 이상 숫자면 배수 추정(24원 → 2,400만, 표시는 '추정') ④ 그래도 없으면 미확인
    반환: (가격, 설명, 추정여부)
    """
    if plausible(listed):
        return listed, "", False
    t = text_price(text)
    if t:
        return t, f"등록가 {won(listed)} → 본문 기준 {won(t)}", False
    if listed and listed >= 10 and allow_scale:
        for k in (10, 100, 1_000, 10_000, 100_000, 1_000_000):
            if plausible(listed * k):
                return listed * k, f"등록가 {won(listed)} → 추정 {won(listed * k)}? (본문에 가격 없음, 확인 필요)", True
    return None, f"가격 미확인 (등록가 {won(listed)})", False


def to_year(s):
    y = int(s)
    if len(s) == 4:
        return y
    return 1900 + y if y >= 90 else 2000 + y


def find_year(text, patterns):
    best = None
    for rx in patterns:
        m = rx.search(text or "")
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), m.group(1))
    return to_year(best[1]) if best else None


def extract_year(title, content):
    return find_year(title, STRONG_YEAR) or find_year(title, TITLE_YEAR) or find_year(content, STRONG_YEAR)


def evaluate(l: Listing):
    text = f"{l.title}\n{l.content}"
    if WANTED_RE.search(l.content[:150]):
        return None
    year = extract_year(l.title, l.content)
    if year is None and not NOTIFY_UNKNOWN_YEAR:
        return None
    if year and not (YEAR_MIN <= year <= YEAR_MAX):
        return None
    bike = bool(BIKE_SIGNAL_RE.search(text))
    price, how, scaled = resolve_price(l.price, text, allow_scale=bike)
    if price and price > BUDGET_B:
        return None
    if price is None and not bike:
        return None
    notes = [n for n in (how, "" if year else "연식 미확인") if n]
    if price is None or scaled:
        tier = "?"             # 가격이 확실하지 않으면 🔥 대신 ❔
    else:
        tier = "A" if price <= BUDGET_A else "B"
    return {"price": price, "year": year, "tier": tier, "notes": notes, "scaled": scaled}


# ───────────── 페이지 가져오기 ─────────────
class Fetcher:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"})
        self._pw = self._browser = self._ctx = None

    def json(self, url, **kw):
        r = self.s.get(url, timeout=20, **kw)
        r.raise_for_status()
        return r.json()

    def html(self, url, wait=3000, timeout=30_000):
        if self._ctx is None:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch()
            self._ctx = self._browser.new_context(user_agent=UA, locale="ko-KR", timezone_id="Asia/Seoul")
        page = self._ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            page.wait_for_timeout(wait)
            try:   # 아래로 스크롤해서 늦게 뜨는 매물까지 불러오기
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(1500)
            except Exception:
                pass
            return page.content()
        finally:
            page.close()

    def close(self):
        for obj, fn in ((self._ctx, "close"), (self._browser, "close"), (self._pw, "stop")):
            try:
                if obj:
                    getattr(obj, fn)()
            except Exception:
                pass


def nap():
    time.sleep(random.uniform(1.5, 3.5))


def walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)


def ld_products(soup):
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except json.JSONDecodeError:
            continue
        for d in walk(data):
            if d.get("@type") == "Product":
                yield d


def script_jsons(soup):
    """페이지 안에 숨어 있는 데이터(JSON) 전부 꺼내기"""
    for tag in soup.find_all("script"):
        s = tag.string or ""
        if not s.strip():
            continue
        if tag.get("id") == "__NEXT_DATA__" or tag.get("type") == "application/json":
            try:
                yield json.loads(s)
            except json.JSONDecodeError:
                pass
        elif "__remixContext" in s:
            m = re.search(r"__remixContext\s*=\s*(\{.*\})\s*;?\s*$", s, re.S)
            if m:
                try:
                    yield json.loads(m.group(1))
                except json.JSONDecodeError:
                    pass


def first_offer(d):
    o = d.get("offers") or {}
    if isinstance(o, list):
        o = o[0] if o else {}
    return o if isinstance(o, dict) else {}


def offer_price(d):
    return to_int(first_offer(d).get("price"))


def offer_sold(d):
    avail = str(first_offer(d).get("availability", ""))
    return bool(re.search(r"SoldOut|OutOfStock|Discontinued", avail, re.I))


def links_with_pattern(soup, platform, base, pattern):
    rx, out = re.compile(pattern), {}
    for a in soup.find_all("a", href=True):
        m, text = rx.search(a["href"]), a.get_text(" ", strip=True)
        if m and text and m.group(1) not in out:
            out[m.group(1)] = Listing(platform, m.group(1), text[:120], urljoin(base, a["href"]),
                                      sold=bool(SOLD_TEXT_RE.search(text)))
    return list(out.values())


def norm(s):
    return re.sub(r"\s+", "", s or "")


def page_detail(f, l: Listing):
    """매물 페이지에서 '이 매물의' 본문만 골라 읽기 + 판매상태 확인"""
    soup = BeautifulSoup(f.html(l.url), "lxml")
    cands, price = [], None

    for d in ld_products(soup):
        cands.append(str(d.get("description") or ""))
        price = price or offer_price(d)
        l.sold = l.sold or offer_sold(d)

    key = norm(l.title)[:15]
    for blob in script_jsons(soup):
        for d in walk(blob):
            t = d.get("title") or d.get("name") or d.get("productTitle") or d.get("subject")
            is_this = isinstance(t, str) and key and key in norm(t)
            for k in ("productDescription", "content", "description", "body"):
                v = d.get(k)
                if isinstance(v, str) and v and (is_this or k == "productDescription"):
                    cands.append(v)
            if is_this:
                l.sold = l.sold or status_sold(d)
                price = price or to_int(d.get("price"))

    og_desc = soup.find("meta", property="og:description")
    og_title = soup.find("meta", property="og:title")
    cands.append(og_desc.get("content", "") if og_desc else "")
    head = " ".join(filter(None, [og_title.get("content", "") if og_title else "",
                                  soup.title.get_text(strip=True) if soup.title else ""]))
    l.content = (head + "\n" + max(cands, key=len, default=""))[:5000]
    if not plausible(l.price) and price:
        l.price = price if plausible(price) else l.price


# ───────────── 플랫폼별 검색 ─────────────
def bunjang_search(f, kw):
    """번개장터 웹 검색 화면을 열어서 매물 카드 읽기"""
    base = "https://m.bunjang.co.kr"
    soup = BeautifulSoup(f.html(f"{base}/search/products?q={quote(kw)}&order=date", wait=4000), "lxml")
    out = {}
    for a in soup.find_all("a", href=True):
        m = re.search(r"/products/(\d+)", a["href"])
        if not m or m.group(1) in out:
            continue
        parts = [s.strip() for s in a.stripped_strings if s.strip()]
        if not parts:
            continue
        price = next((to_int(s) for s in parts if re.fullmatch(r"[\d,]+", s)), None)
        texts = [s for s in parts if not re.fullmatch(r"[\d,]+", s) and s not in ("AD", "광고")]
        if not texts:
            continue
        title = next((s for s in texts if R1_RE.search(s)), texts[0])
        out[m.group(1)] = Listing("번개장터", m.group(1), title, f"{base}/products/{m.group(1)}", price,
                                  sold=bool(SOLD_TEXT_RE.search(" ".join(parts))))
    return list(out.values())


def bunjang_detail(f, l):
    try:
        data = f.json(f"https://api.bunjang.co.kr/api/pms/v3/products-detail/{l.item_id}?viewerUid=-1")
        prod = (data.get("data") or {}).get("product") or {}
        l.content = prod.get("description", "")[:5000]
        l.sold = l.sold or status_sold(prod)
        if not l.content:
            raise ValueError("empty")
    except Exception:
        page_detail(f, l)


def joongna_search(f, kw):
    base = "https://web.joongna.com"
    soup = BeautifulSoup(f.html(f"{base}/search/{quote(kw)}?sort=RECENT_SORT"), "lxml")
    out = {}
    nd = soup.find("script", id="__NEXT_DATA__")
    if nd and nd.string:
        try:
            for d in walk(json.loads(nd.string)):
                seq, title = d.get("seq") or d.get("productSeq"), d.get("title") or d.get("productTitle")
                if seq and title and ("price" in d or "productPrice" in d):
                    out[str(seq)] = Listing("중고나라", str(seq), str(title), f"{base}/product/{seq}",
                                            to_int(d.get("price") or d.get("productPrice")),
                                            sold=status_sold(d))
        except json.JSONDecodeError:
            pass
    return list(out.values()) or links_with_pattern(soup, "중고나라", base, r"/product/(\d+)")


def daangn_search(f, kw):
    base = "https://www.daangn.com"
    soup = BeautifulSoup(f.html(f"{base}/kr/buy-sell/?search={quote(kw)}"), "lxml")
    out = {}
    for d in ld_products(soup):
        url = urljoin(base, str(d.get("url") or ""))
        m = re.search(r"/buy-sell/([^/?#]+)", url)
        if m:
            out[m.group(1)] = Listing("당근마켓", m.group(1), str(d.get("name", "")), url,
                                      offer_price(d), str(d.get("description") or ""),
                                      sold=offer_sold(d))
    return list(out.values()) or links_with_pattern(soup, "당근마켓", base, r"/buy-sell/([^/?#]+)")


def reitwagen_search(f, kw):
    """라이트바겐: R1 필터된 목록 페이지에서 /products/detail/번호 링크 수집"""
    url = LIGHTWAGEN_SEARCH_URL or os.getenv("LIGHTWAGEN_SEARCH_URL", "")
    if not url:
        return None
    if kw != KEYWORDS[0]:
        return []          # 목록 페이지는 한 번만 열기
    base = "https://www.reitwagen.co.kr"
    html = f.html(url, wait=5000)
    soup = BeautifulSoup(html, "lxml")
    out = {}
    for a in soup.find_all("a", href=True):
        m = re.search(r"/products/detail/(\d+)", a["href"])
        if not m or m.group(1) in out:
            continue
        text = a.get_text(" ", strip=True)
        title = text[:120] if R1_RE.search(text) else f"야마하 YZF R1 {text[:100]}".strip()
        out[m.group(1)] = Listing("라이트바겐", m.group(1), title, f"{base}/products/detail/{m.group(1)}",
                                  text_price(text), sold=bool(SOLD_TEXT_RE.search(text)))
    if not out:   # 링크 태그가 없으면 페이지 소스에서 매물 번호 직접 찾기
        for pid in dict.fromkeys(re.findall(r"/products/detail/(\d+)", html)):
            out[pid] = Listing("라이트바겐", pid, "야마하 YZF R1", f"{base}/products/detail/{pid}")
    return list(out.values())


def reitwagen_detail(f, l):
    """라이트바겐 매물 페이지: 제목(모델·연식·가격) + 상세 설명 + 판매상태"""
    soup = BeautifulSoup(f.html(l.url, wait=2500), "lxml")
    page_title = soup.title.get_text(strip=True).split("|")[0].strip() if soup.title else ""
    if page_title:
        l.title = page_title       # 예: 야마하 YZF R1 2022년식 개인중고 2,200만원
    body = soup.get_text(" ", strip=True)
    m = re.search(r"상세 정보(.*?)(?:튜닝이력|정비이력|모델 정보)", body)
    l.content = (m.group(1) if m else "")[:5000]
    if not plausible(l.price):
        l.price = text_price(page_title) or l.price
    if SOLD_TEXT_RE.search(page_title) or re.search(r"판매\s*완료|거래\s*완료", body):
        l.sold = True


def site_search(f, name, tpl, kw, wait=5000, timeout=30_000):
    """주소만 알면 되는 범용 검색: 페이지에서 제목에 R1이 들어간 링크를 골라냄"""
    if not tpl:
        return None
    if "{kw}" not in tpl and kw != KEYWORDS[0]:
        return []
    url = tpl.replace("{kw}", quote(kw))
    soup = BeautifulSoup(f.html(url, wait=wait, timeout=timeout), "lxml")
    host, out = urlparse(url).netloc, []
    for a in soup.find_all("a", href=True):
        full, text = urljoin(url, a["href"]), a.get_text(" ", strip=True)
        if urlparse(full).netloc == host and full != url and text and R1_RE.search(text):
            p = urlparse(full)
            out.append(Listing(name, p.path + ("?" + p.query if p.query else ""), text[:120], full,
                               text_price(text), sold=bool(SOLD_TEXT_RE.search(text))))
    return out


def passo_search(f, kw):
    if not PASSO_ENABLED:
        return None
    return site_search(f, "파쏘", PASSO_LIST_URL, kw, wait=3000, timeout=60_000)


def batume_search(f, kw):
    cid, sec = os.getenv("NAVER_CLIENT_ID"), os.getenv("NAVER_CLIENT_SECRET")
    if not (cid and sec):
        return None
    data = f.json("https://openapi.naver.com/v1/search/cafearticle.json",
                  params={"query": kw, "display": 100, "sort": "date"},
                  headers={"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": sec})
    out = []
    for it in data.get("items", []):
        if "바이크튜닝매니아" not in it.get("cafename", ""):
            continue
        strip = lambda s: BeautifulSoup(s or "", "lxml").get_text(" ", strip=True)
        link = it.get("link", "")
        m = re.search(r"/(\d+)(?:\?|$)", link)
        title = strip(it.get("title"))
        out.append(Listing("바튜매", m.group(1) if m else link, title, link,
                           None, strip(it.get("description")), sold=bool(SOLD_TEXT_RE.search(title))))
    return out


PLATFORMS = [
    ("번개장터", bunjang_search, bunjang_detail),
    ("중고나라", joongna_search, page_detail),
    ("당근마켓", daangn_search, page_detail),
    ("라이트바겐", reitwagen_search, reitwagen_detail),
    ("파쏘", passo_search, page_detail),
    ("바튜매", batume_search, None),
]


# ───────────── 디스코드 (한 번에 모아서 보내기) ─────────────
def discord(payload):
    for _ in range(3):
        r = requests.post(WEBHOOK, json=payload, timeout=15)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
            continue
        r.raise_for_status()
        time.sleep(1.2)
        return True
    return False


def clean(s, n=70):
    s = re.sub(r"\s+", " ", s or "").strip().replace("[", "(").replace("]", ")")
    return s[:n] + ("…" if len(s) > n else "")


def format_item(l, res, note):
    icon = {"A": "🔥", "B": "🔵"}.get(res["tier"], "❔")
    if not res["price"]:
        price = "가격 미확인"
    elif res.get("scaled"):
        price = f"~{won(res['price'])}?"
    else:
        price = won(res["price"])
    year = f"{res['year']}년식" if res["year"] else "연식 미확인"
    lines = [f"{icon} **{price}** · {year} · {l.platform}", f"[{clean(l.title)}]({l.url})"]
    notes = ([note] if note else []) + [n for n in res["notes"] if n != "연식 미확인"]
    if notes:
        lines.append("⚠️ " + " / ".join(notes))
    return "\n".join(lines)


def send_digest(items):
    """찾은 매물을 메시지 하나로 모아서 발송 (🔥 → 🔵 → ❔, 각각 가격 낮은 순)"""
    order = {"A": 0, "B": 1, "?": 2}
    items.sort(key=lambda x: (order[x[1]["tier"]], x[1]["price"] or 10**10))
    chunks, cur = [], ""
    for l, res, note in items:
        block = format_item(l, res, note)
        if cur and len(cur) + len(block) > 3800:
            chunks.append(cur)
            cur = ""
        cur += ("\n\n" if cur else "") + block
    if cur:
        chunks.append(cur)

    n_a = sum(1 for _, r, _ in items if r["tier"] == "A")
    ok = True
    for i, chunk in enumerate(chunks):
        title = f"🏍️ R1 새 매물 {len(items)}건" + (f" ({i + 1}/{len(chunks)})" if len(chunks) > 1 else "")
        payload = {"embeds": [{"title": title, "description": chunk,
                               "color": 0xE5484D if n_a else 0x3E63DD}]}
        if i == 0 and n_a:
            payload["content"] = f"@here 🔥 1천 이하 {n_a}건 포함"
        try:
            ok = discord(payload) and ok
        except Exception as e:
            print("디스코드 발송 실패:", e)
            ok = False
    return ok


# ───────────── 메인 ─────────────
def load_seen():
    try:
        with open(SEEN_FILE, encoding="utf-8") as fp:
            return json.load(fp)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_seen(seen):
    cutoff = (datetime.now(KST) - timedelta(days=90)).strftime("%Y-%m-%d")
    seen = {k: v for k, v in seen.items() if v.get("at", "9999") >= cutoff}
    with open(SEEN_FILE, "w", encoding="utf-8") as fp:
        json.dump(seen, fp, ensure_ascii=False, indent=0)


def main():
    if not WEBHOOK:
        print("DISCORD_WEBHOOK_URL이 비어 있어요. Settings > Secrets에 등록했는지 확인하세요.")
        sys.exit(1)
    seen, today, report, pending = load_seen(), datetime.now(KST).strftime("%Y-%m-%d"), [], []
    f = Fetcher()
    try:
        for name, search, detail in PLATFORMS:
            found, err, skipped = {}, "", False
            for kw in KEYWORDS:
                try:
                    res = search(f, kw)
                    if res is None:
                        skipped = True
                        break
                    for l in res:
                        found[l.uid] = l
                except Exception as e:
                    err = str(e)[:100]
                nap()
            if skipped:
                report.append(f"⏸️ {name}: 설정 안 됨 (건너뜀)")
                continue

            r1 = new = sold = 0
            for l in found.values():
                if not R1_RE.search(l.title) or WANTED_RE.search(l.title) or EXCLUDE_RE.search(l.title):
                    continue
                r1 += 1
                if l.sold or SOLD_TEXT_RE.search(l.title):
                    sold += 1
                    continue
                if plausible(l.price) and l.price > BUDGET_B:
                    continue
                old = seen.get(l.uid)
                if old and old.get("p") == l.price:
                    continue
                listed = l.price
                # 본문이 없거나, 등록가가 이상하면(8원, 90만원 등) 매물 페이지를 열어 본문 전체 확인
                if detail and (not l.content or not plausible(l.price)):
                    try:
                        detail(f, l)
                    except Exception:
                        pass
                    nap()
                entry = {"p": listed, "rp": None, "m": False, "t": l.title[:60], "at": today}
                if l.sold or not R1_RE.search(l.title):      # 상세에서 판매완료/R1 아님 확인
                    sold += int(l.sold)
                    seen[l.uid] = entry
                    continue
                res = evaluate(l)
                rp = res["price"] if res else None
                was_match = bool(old and old.get("m"))
                cheaper = bool(was_match and rp and old.get("rp") and rp < old["rp"])
                entry.update(rp=rp, m=bool(res))
                if res and (not was_match or cheaper):
                    note = f"가격 인하 {won(old['rp'])} → {won(rp)}" if cheaper else ""
                    pending.append((l, res, note, entry))
                    new += 1
                else:
                    seen[l.uid] = entry
            icon = "⚠️" if (err and not found) else "✅"
            report.append(f"{icon} {name}: 검색 {len(found)}건 / R1 {r1}건 / 판매완료 {sold}건 / 새 매물 {new}건"
                          + (f"\n   오류: {err}" if err and not found else ""))

        if pending and send_digest([(l, r, n) for l, r, n, _ in pending]):
            for l, _, _, entry in pending:
                seen[l.uid] = entry       # 발송 성공한 것만 '봤음'으로 저장 (실패 시 다음에 재시도)
    finally:
        f.close()
        save_seen(seen)

    print("\n".join(report))
    if os.getenv("RUN_MODE") == "workflow_dispatch":
        discord({"content": "📋 **R1 감시 수동 실행 결과**\n" + "\n".join(report)})


if __name__ == "__main__":
    main()
