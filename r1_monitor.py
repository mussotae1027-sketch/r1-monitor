#!/usr/bin/env python3
"""야마하 R1 (2015~2019년식) 매물 감시 → 디스코드 알림 (GitHub Actions에서 10분마다 1회 실행)
- 가격은 사이트 등록가 그대로 표시
- 새 매물/가격 인하가 있을 때 '전체 목록'을 보내고, 신규는 🆕 · 가격 인하는 🔻 표시
"""
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
BUDGET_B = 15_000_000            # 최대 예산 (등록가가 정상 범위인데 이걸 넘으면 제외)
NOTIFY_UNKNOWN_YEAR = False      # True: 연식 안 적힌 매물도 '연식 미확인'으로 표시
SKIP_RESERVED = True             # True: 예약중 매물도 제외 / False: 예약중은 표시
PASSO_ENABLED = True             # False: 파쏘 감시 끄기
PASSO_LIST_URL = "http://bike.passo.co.kr/bike/"
LIGHTWAGEN_SEARCH_URL = "https://www.reitwagen.co.kr/products/home/used?u=models%255B0%255D%3DYZF%2520R1%26modelIds%255B0%255D%3D10895%26brands%255B0%255D%3D%25EC%2595%25BC%25EB%25A7%2588%25ED%2595%2598%26brandIds%255B0%255D%3D5&s=models%255B0%255D%3DYZF%2520R1%26modelIds%255B0%255D%3D10895%26brands%255B0%255D%3D%25EC%2595%25BC%25EB%25A7%2588%25ED%2595%2598%26brandIds%255B0%255D%3D5&b=models%255B0%255D%3DYZF%2520R1%26modelIds%255B0%255D%3D10895%26brands%255B0%255D%3D%25EC%2595%25BC%25EB%25A7%2588%25ED%2595%2598%26brandIds%255B0%255D%3D5&p=models%255B0%255D%3DYZF%2520R1%26modelIds%255B0%255D%3D10895%26brands%255B0%255D%3D%25EC%2595%25BC%25EB%25A7%2588%25ED%2595%2598%26brandIds%255B0%255D%3D5&e=keyword%255B0%255D%3DYZF%2520R1"
# ═══════════════════════════════════════════════════════════════

REAL_MIN, REAL_MAX = 5_000_000, 30_000_000      # '정상 등록가'로 보는 범위
SEEN_FILE = "seen.json"
WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
KST = timezone(timedelta(hours=9))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")

R1_RE = re.compile(r"(?<![a-z0-9])(?:yzf[\s\-]?)?r\s?1(?:m|s)?(?![0-9a-z\-])|알원", re.I)
EXCLUDE_RE = re.compile(r"mt\s?-?\s?10|r1\s?-?\s?z", re.I)
WANTED_RE = re.compile(r"삽니다|구합니다|구매합니다|구해요|매입합니다")

# 부품·용품 판별 (제목 기준)
COMPAT_RE = re.compile(
    r"호환|전용|(?:r\s?1|알원)\s*용(?![가-힣])"
    r"|(?<!\d)(?:20)?[0-2]\d\s*(?:년식?|년형)?\s*[~\-]\s*(?:20)?[0-2]\d(?!\d)", re.I)
PARTS_RE = re.compile(
    r"백스텝|레귤레이터|정류기|머플러|배기|슬립온|카울|윙렛|스크린|휠|시트|브레이크|캘리퍼|디스크|패드|레버"
    r"|핸들|미러|체인|스프라[켓킷]|타이어|라디에이터|쇼바|서스|포크|ecu|퀵\s*[시쉬]프터|헤드\s*라이트|테일"
    r"|깜빡이|윙커|번호판|커버|가드|슬라이더|휀다|펜더|리스킷|스탠드|매뉴얼|모형|피규어|다이캐스트|프라모델"
    r"|키링|스티커|데칼|헬멧|자켓|장갑|부츠|배터리|오일|필터|플러그|가스켓|클러치|기어|스위치|계기판|클러스터"
    r"|키박스|탱크|인젝터|스로틀|부품|파츠|용품|순정품|중고\s*파츠", re.I)

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

# 목록 카드 글자에서 가격 읽기용 (라이트바겐·파쏘)
P_MAN = re.compile(r"(?<![\d.,])(\d{1,2},\d{3}|\d{2,4})\s*만\s*원", re.I)
P_WON = re.compile(r"(?<![\d,])(\d{1,3}(?:,\d{3})+|\d+)\s*원")


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
    page_price: int | None = None     # 매물 페이지에 적힌 공식 등록가 (있으면 이걸로 덮어씀)

    @property
    def uid(self):
        return f"{self.platform}:{self.item_id}"


# ───────────── 유틸 ─────────────
def to_int(v):
    try:
        n = int(float(str(v).replace(",", "").strip()))
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def won(v):
    if not v:
        return "가격 없음"
    if v < 10_000:
        return f"{v:,}원"
    return f"{v / 10_000:,.0f}만원" if v % 10_000 == 0 else f"{v:,}원"


def plausible(p):
    return bool(p) and REAL_MIN <= p <= REAL_MAX


def card_price(text):
    """목록 카드 글자에서 표시된 가격 그대로 읽기"""
    m = P_MAN.search(text or "")
    if m:
        return to_int(m.group(1)) * 10_000 if to_int(m.group(1)) else None
    m = P_WON.search(text or "")
    return to_int(m.group(1)) if m else None


def status_sold(d: dict) -> bool:
    for k, v in d.items():
        if isinstance(v, (str, int)) and re.search(r"state|status|availability", k, re.I):
            if SOLD_STATUS_RE.search(str(v)):
                return True
    return False


def is_parts(l: "Listing") -> bool:
    """부품·용품 글: 호환/연식범위 표시, 또는 부품 단어 + 정상 범위가 아닌 등록가"""
    if COMPAT_RE.search(l.title):
        return True
    return bool(PARTS_RE.search(l.title)) and not plausible(l.price)


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
    """조건에 맞으면 {'year': ...} 반환, 아니면 None"""
    if WANTED_RE.search(l.content[:150]):
        return None
    year = extract_year(l.title, l.content)
    if year is None and not NOTIFY_UNKNOWN_YEAR:
        return None
    if year and not (YEAR_MIN <= year <= YEAR_MAX):
        return None
    if plausible(l.price) and l.price > BUDGET_B:
        return None
    return {"year": year}


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
            try:
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
                                      card_price(text), sold=bool(SOLD_TEXT_RE.search(text)))
    return list(out.values())


def norm(s):
    return re.sub(r"\s+", "", s or "")


def page_detail(f, l: Listing):
    """매물 페이지에서 '이 매물의' 본문 + 공식 등록가 + 판매상태 읽기"""
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
    cands.append(og_desc.get("content", "") if og_desc else "")
    l.content = max(cands, key=len, default="")[:5000]
    l.page_price = price


# ───────────── 플랫폼별 검색 ─────────────
def bunjang_search(f, kw):
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
        nums = [n for n in (to_int(s) for s in parts if re.fullmatch(r"[\d,]+", s)) if n]
        price = max(nums) if nums else None     # 찜 개수 같은 작은 숫자 무시 (상세에서 공식 가격으로 다시 확인)
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
        l.page_price = to_int(prod.get("price"))
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
    url = LIGHTWAGEN_SEARCH_URL or os.getenv("LIGHTWAGEN_SEARCH_URL", "")
    if not url:
        return None
    if kw != KEYWORDS[0]:
        return []
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
                                  card_price(text), sold=bool(SOLD_TEXT_RE.search(text)))
    if not out:
        for pid in dict.fromkeys(re.findall(r"/products/detail/(\d+)", html)):
            out[pid] = Listing("라이트바겐", pid, "야마하 YZF R1", f"{base}/products/detail/{pid}")
    return list(out.values())


def reitwagen_detail(f, l):
    soup = BeautifulSoup(f.html(l.url, wait=2500), "lxml")
    page_title = soup.title.get_text(strip=True).split("|")[0].strip() if soup.title else ""
    if page_title:
        l.title = page_title       # 예: 야마하 YZF R1 2022년식 개인중고 2,200만원
    body = soup.get_text(" ", strip=True)
    m = re.search(r"상세 정보(.*?)(?:튜닝이력|정비이력|모델 정보)", body)
    l.content = (m.group(1) if m else "")[:5000]
    l.page_price = card_price(page_title)
    if SOLD_TEXT_RE.search(page_title) or re.search(r"판매\s*완료|거래\s*완료", body):
        l.sold = True


def site_search(f, name, tpl, kw, wait=5000, timeout=30_000):
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
                               card_price(text), sold=bool(SOLD_TEXT_RE.search(text))))
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
                           card_price(title), strip(it.get("description")),
                           sold=bool(SOLD_TEXT_RE.search(title))))
    return out


PLATFORMS = [
    ("번개장터", bunjang_search, bunjang_detail),
    ("중고나라", joongna_search, page_detail),
    ("당근마켓", daangn_search, page_detail),
    ("라이트바겐", reitwagen_search, reitwagen_detail),
    ("파쏘", passo_search, page_detail),
    ("바튜매", batume_search, None),
]


# ───────────── 디스코드 ─────────────
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


def format_item(l, info, tag):
    mark = {"new": "🆕 ", "drop": "🔻 "}.get(tag, "")
    p = info["price"]
    fire = "🔥" if plausible(p) and p <= BUDGET_A else ""
    year = f"{info['year']}년식" if info["year"] else "연식 미확인"
    lines = [f"{mark}{fire}**{won(p)}** · {year} · {l.platform}", f"[{clean(info.get('title') or l.title)}]({l.url})"]
    if info.get("note"):
        lines.append("⚠️ " + info["note"])
    return "\n".join(lines)


def send_list(items):
    """현재 조건에 맞는 매물 전체를 보냄. 🆕/🔻 먼저, 그다음 가격순"""
    def sort_key(x):
        _, info, tag = x
        p = info["price"]
        return (0 if tag else 1, 0 if plausible(p) else 1, p or 10**12)
    items = sorted(items, key=sort_key)
    n_new = sum(1 for _, _, t in items if t == "new")
    n_drop = sum(1 for _, _, t in items if t == "drop")

    chunks, cur = [], ""
    for l, info, tag in items:
        block = format_item(l, info, tag)
        if cur and len(cur) + len(block) > 3800:
            chunks.append(cur)
            cur = ""
        cur += ("\n\n" if cur else "") + block
    if cur:
        chunks.append(cur)

    head = f"🏍️ R1 매물 현황 {len(items)}건"
    extra = [s for s in (f"🆕 신규 {n_new}건" if n_new else "", f"🔻 가격인하 {n_drop}건" if n_drop else "") if s]
    if extra:
        head += " (" + ", ".join(extra) + ")"
    ok = True
    for i, chunk in enumerate(chunks):
        title = head + (f" [{i + 1}/{len(chunks)}]" if len(chunks) > 1 else "")
        payload = {"embeds": [{"title": title, "description": chunk,
                               "color": 0xE5484D if (n_new or n_drop) else 0x3E63DD}]}
        if i == 0 and (n_new or n_drop):
            payload["content"] = "@here 새 R1 매물 있음"
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
    manual = os.getenv("RUN_MODE") == "workflow_dispatch"
    seen, today, report, current = load_seen(), datetime.now(KST).strftime("%Y-%m-%d"), [], []
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

            shown = new = sold = parts = 0
            for l in found.values():
                if not R1_RE.search(l.title) or WANTED_RE.search(l.title) or EXCLUDE_RE.search(l.title):
                    continue
                if is_parts(l):
                    parts += 1
                    continue
                if l.sold or SOLD_TEXT_RE.search(l.title):
                    sold += 1
                    continue
                if plausible(l.price) and l.price > BUDGET_B:
                    continue

                old = seen.get(l.uid)
                v2 = bool(old and old.get("v") == 2)

                # ① 이미 확인한 매물이고 가격 그대로면 → 저장된 결과 재사용 (페이지 다시 안 열음)
                if old and old.get("p") == l.price and (v2 or not old.get("m")):
                    old["at"] = today
                    if v2 and old.get("m"):
                        info = {"price": old.get("price"), "year": old.get("y"), "title": old.get("t")}
                        current.append((l, info, "" if old.get("n") else "new"))
                        shown += 1
                        new += int(not old.get("n"))
                    continue

                # ② 처음 보는 매물, 가격이 바뀐 매물, 예전 버전 기록 → 새로 확인
                listed = l.price
                if detail and (not l.content or not plausible(l.price)):
                    try:
                        detail(f, l)
                    except Exception:
                        pass
                    nap()
                if l.page_price:
                    l.price = l.page_price      # 매물 페이지의 공식 등록가 우선
                entry = {"v": 2, "p": listed, "price": l.price, "m": False, "y": None,
                         "t": l.title[:80], "n": False, "at": today}
                if l.sold or not R1_RE.search(l.title) or is_parts(l):
                    sold += int(l.sold)
                    seen[l.uid] = entry
                    continue
                res = evaluate(l)
                if res:
                    entry.update(m=True, y=res["year"])
                    info = {"price": l.price, "year": res["year"], "title": l.title}
                    if v2 and old.get("m"):                      # 알던 매물인데 가격이 바뀜
                        entry["n"] = old.get("n", False)
                        if old.get("price") and l.price and l.price < old["price"]:
                            tag = "drop"
                            info["note"] = f"가격 변경 {won(old['price'])} → {won(l.price)}"
                        else:
                            tag = "" if entry["n"] else "new"
                    elif old and not v2 and old.get("m"):        # 예전 버전에서 이미 알림 갔던 매물
                        entry["n"] = True
                        tag = ""
                    else:
                        tag = "new"
                    current.append((l, info, tag))
                    shown += 1
                    new += int(tag in ("new", "drop"))
                seen[l.uid] = entry

            icon = "⚠️" if (err and not found) else "✅"
            report.append(f"{icon} {name}: 검색 {len(found)}건 / 목록 {shown}건 (신규 {new}) / "
                          f"부품 제외 {parts} / 판매완료 {sold}"
                          + (f"\n   오류: {err}" if err and not found else ""))

        has_event = any(tag in ("new", "drop") for _, _, tag in current)
        if current and (has_event or manual):
            if send_list(current):
                for l, _, _ in current:
                    if l.uid in seen:
                        seen[l.uid]["n"] = True      # 보낸 매물은 다음부터 🆕 없이 표시
        elif manual:
            discord({"content": "🏍️ 현재 조건에 맞는 R1 매물이 없어요."})
    finally:
        f.close()
        save_seen(seen)

    print("\n".join(report))
    if manual:
        discord({"content": "📋 **R1 감시 수동 실행 결과**\n" + "\n".join(report)})


if __name__ == "__main__":
    main()
