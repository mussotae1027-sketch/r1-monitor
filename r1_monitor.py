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

# ───────────── 조건 ─────────────
KEYWORDS = ["야마하 R1", "YZF-R1"]
YEAR_MIN, YEAR_MAX = 2015, 2019
BUDGET_A, BUDGET_B = 10_000_000, 15_000_000
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
YEAR_PATTERNS = [
    re.compile(r"(?<!\d)(20[0-2]\d|[0-2]\d)\s*년\s*(?:식|형)"),
    re.compile(r"연식\s*[:：]?\s*(20[0-2]\d|[0-2]\d)(?!\d)"),
    re.compile(r"(?<![a-z])my\s?(\d{2})(?!\d)", re.I),
]
HAN = dict(zip("일이삼사오육칠팔구", range(1, 10)))
D = "[1-9일이삼사오육칠팔구]"
P_KEYWORD = re.compile(r"(?:가격|판매가|금액|희망가)\s*[:：]?\s*(\d{1,2},\d{3}|\d{3,4})(?![\d,])")
P_WON = re.compile(r"(?<![\d,])(\d{1,2},\d{3},\d{3}|\d{7,8})\s*원")
P_MAN = re.compile(r"(?<![\d.,])(\d{1,2},\d{3}|\d{3,4})\s*만(?!\s*(?:k|키로|킬로))", re.I)
P_SELL = re.compile(r"(?<![\d.,])(\d{3,4})\s*에\s*(?:판매|팝니다|팔아|드립|넘깁|정리)")
P_KOR = re.compile(rf"(?<![가-힣])({D}?)\s*천\s*(?:({D})\s*백)?\s*(?:({D})\s*십)?\s*만")


@dataclass
class Listing:
    platform: str
    item_id: str
    title: str
    url: str
    price: int | None = None
    content: str = ""
    posted_at: str = ""

    @property
    def uid(self):
        return f"{self.platform}:{self.item_id}"


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


def kd(c):
    return 1 if not c else (int(c) if c.isdigit() else HAN[c])


def text_price(text):
    """제목/본문에서 진짜 가격 찾기"""
    text = text or ""
    rules = [(P_KEYWORD, lambda s: int(s.replace(",", "")) * 10_000),
             (P_WON, lambda s: int(s.replace(",", ""))),
             (P_MAN, lambda s: int(s.replace(",", "")) * 10_000),
             (P_SELL, lambda s: int(s) * 10_000)]
    for rx, conv in rules:
        for m in rx.finditer(text):
            v = conv(m.group(1))
            if REAL_MIN <= v <= REAL_MAX:
                return v
    for m in P_KOR.finditer(text):
        v = (kd(m.group(1)) * 1000 + (kd(m.group(2)) * 100 if m.group(2) else 0)
             + (kd(m.group(3)) * 10 if m.group(3) else 0)) * 10_000
        if REAL_MIN <= v <= REAL_MAX:
            return v
    return None


def resolve_price(listed, text, allow_scale):
    """등록가가 말이 안 되면(24원, 90만원 등) 본문 가격 → 배수 추정 순으로 진짜 가격 계산"""
    if listed and REAL_MIN <= listed <= REAL_MAX:
        return listed, ""
    t = text_price(text)
    if t:
        return t, f"등록가 {won(listed)} → 본문 기준 {won(t)}"
    if listed and allow_scale:
        for k in (10, 100, 1_000, 10_000, 100_000, 1_000_000):
            if REAL_MIN <= listed * k <= REAL_MAX:
                return listed * k, f"등록가 {won(listed)} → 추정 {won(listed * k)}"
    return None, "가격 미확인"


def extract_year(text):
    for rx in YEAR_PATTERNS:
        m = rx.search(text or "")
        if m:
            y = int(m.group(1))
            return y + 2000 if y < 100 else y
    return None


def evaluate(l: Listing):
    text = f"{l.title}\n{l.content}"
    if WANTED_RE.search(l.content[:150]):
        return None
    year = extract_year(l.title) or extract_year(l.content)
    if year and not (YEAR_MIN <= year <= YEAR_MAX):
        return None
    bike = bool(BIKE_SIGNAL_RE.search(text))
    price, how = resolve_price(l.price, text, allow_scale=bike)
    if price and price > BUDGET_B:
        return None
    if price is None and not bike:
        return None
    notes = [n for n in (how, "" if year else "연식 미확인") if n]
    tier = "?" if price is None else ("A" if price <= BUDGET_A else "B")
    return {"price": price, "year": year, "tier": tier, "notes": notes}


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

    def html(self, url):
        if self._ctx is None:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch()
            self._ctx = self._browser.new_context(user_agent=UA, locale="ko-KR", timezone_id="Asia/Seoul")
        page = self._ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(3000)
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


def offer_price(d):
    o = d.get("offers") or {}
    if isinstance(o, list):
        o = o[0] if o else {}
    return to_int(o.get("price")) if isinstance(o, dict) else None


def links_with_pattern(soup, platform, base, pattern):
    rx, out = re.compile(pattern), {}
    for a in soup.find_all("a", href=True):
        m, text = rx.search(a["href"]), a.get_text(" ", strip=True)
        if m and text:
            out.setdefault(m.group(1), Listing(platform, m.group(1), text[:120], urljoin(base, a["href"])))
    return list(out.values())


def page_detail(f, l: Listing):
    soup = BeautifulSoup(f.html(l.url), "lxml")
    desc, price = "", None
    for d in ld_products(soup):
        desc = desc or str(d.get("description") or "")
        price = price or offer_price(d)
    nd = soup.find("script", id="__NEXT_DATA__")
    if not desc and nd and nd.string:
        try:
            cands = [v for d in walk(json.loads(nd.string)) for k, v in d.items()
                     if k in ("productDescription", "description", "content") and isinstance(v, str)]
            desc = max(cands, key=len) if cands else ""
        except json.JSONDecodeError:
            pass
    if not desc:
        og = soup.find("meta", property="og:description")
        desc = og.get("content", "") if og else soup.get_text(" ", strip=True)[:3000]
    l.content = desc[:5000]
    if l.price is None:
        l.price = price


# ───────────── 플랫폼별 검색 ─────────────
def bunjang_search(f, kw):
    data = f.json(f"https://api.bunjang.co.kr/api/1/find_v2.json?q={quote(kw)}&order=date&page=0&n=60")
    out = []
    for it in data.get("list", []):
        pid = str(it.get("pid") or "")
        if pid:
            ts = to_int(it.get("update_time"))
            out.append(Listing("번개장터", pid, it.get("name", ""), f"https://m.bunjang.co.kr/products/{pid}",
                               to_int(it.get("price")),
                               posted_at=datetime.fromtimestamp(ts, KST).strftime("%m/%d %H:%M") if ts else ""))
    return out


def bunjang_detail(f, l):
    try:
        data = f.json(f"https://api.bunjang.co.kr/api/pms/v3/products-detail/{l.item_id}?viewerUid=-1")
        l.content = ((data.get("data") or {}).get("product") or {}).get("description", "")[:5000]
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
                                            to_int(d.get("price") or d.get("productPrice")))
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
                                      offer_price(d), str(d.get("description") or ""))
    return list(out.values()) or links_with_pattern(soup, "당근마켓", base, r"/buy-sell/([^/?#]+)")


def site_search(f, name, tpl, kw):
    """라이트바겐·파쏘처럼 주소만 알면 되는 사이트용 범용 검색"""
    if not tpl:
        return None
    url = tpl.replace("{kw}", quote(kw))
    soup = BeautifulSoup(f.html(url), "lxml")
    host, out = urlparse(url).netloc, []
    for a in soup.find_all("a", href=True):
        full, text = urljoin(url, a["href"]), a.get_text(" ", strip=True)
        if urlparse(full).netloc == host and full != url and text and R1_RE.search(text):
            out.append(Listing(name, urlparse(full).path, text[:120], full))
    return out


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
        out.append(Listing("바튜매", m.group(1) if m else link, strip(it.get("title")), link,
                           None, strip(it.get("description"))))
    return out


PLATFORMS = [
    ("번개장터", bunjang_search, bunjang_detail),
    ("중고나라", joongna_search, page_detail),
    ("당근마켓", daangn_search, page_detail),
    ("라이트바겐", lambda f, kw: site_search(f, "라이트바겐", os.getenv("LIGHTWAGEN_SEARCH_URL", ""), kw), page_detail),
    ("파쏘", lambda f, kw: site_search(f, "파쏘", os.getenv("PASSO_SEARCH_URL", ""), kw), page_detail),
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


TIER = {"A": "🔥 1,000만 이하", "B": "1,500만 이하", "?": "가격 직접 확인"}


def send_alert(l: Listing, res, extra_note=""):
    notes = ([extra_note] if extra_note else []) + res["notes"]
    embed = {
        "title": l.title[:250], "url": l.url,
        "color": 0xE5484D if res["tier"] == "A" else 0x3E63DD,
        "description": ("".join(f"⚠️ {n}\n" for n in notes) + "\n" + l.content[:300]).strip() or "-",
        "fields": [
            {"name": "가격", "value": won(res["price"]) if res["price"] else "미확인", "inline": True},
            {"name": "연식", "value": f"{res['year']}년식" if res["year"] else "미확인", "inline": True},
            {"name": "플랫폼", "value": l.platform, "inline": True},
            {"name": "조건", "value": TIER[res["tier"]], "inline": True},
        ],
        "footer": {"text": f"작성 {l.posted_at}" if l.posted_at else "R1 매물 알림"},
    }
    payload = {"embeds": [embed]}
    if res["tier"] == "A":
        payload["content"] = "@here 1천 이하 R1 떴다"
    try:
        return discord(payload)
    except Exception as e:
        print("디스코드 발송 실패:", e)
        return False


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
    seen, today, report = load_seen(), datetime.now(KST).strftime("%Y-%m-%d"), []
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

            r1 = sent = 0
            for l in found.values():
                if not R1_RE.search(l.title) or WANTED_RE.search(l.title) or EXCLUDE_RE.search(l.title):
                    continue
                r1 += 1
                if l.price and REAL_MIN <= l.price <= REAL_MAX and l.price > BUDGET_B:
                    continue
                old = seen.get(l.uid)
                if old and old.get("p") == l.price:
                    continue
                if detail and not l.content:
                    try:
                        detail(f, l)
                    except Exception:
                        pass
                    nap()
                res = evaluate(l)
                rp = res["price"] if res else None
                was_match = bool(old and old.get("m"))
                cheaper = bool(was_match and rp and old.get("rp") and rp < old["rp"])
                if res and (not was_match or cheaper):
                    note = f"가격 인하 {won(old['rp'])} → {won(rp)}" if cheaper else ""
                    if not send_alert(l, res, note):
                        continue
                    sent += 1
                seen[l.uid] = {"p": l.price, "rp": rp, "m": bool(res), "t": l.title[:60], "at": today}
            icon = "⚠️" if (err and not found) else "✅"
            report.append(f"{icon} {name}: 검색 {len(found)}건 / R1 {r1}건 / 알림 {sent}건"
                          + (f"\n   오류: {err}" if err and not found else ""))
    finally:
        f.close()
        save_seen(seen)

    print("\n".join(report))
    if os.getenv("RUN_MODE") == "workflow_dispatch":
        discord({"content": "📋 **R1 감시 수동 실행 결과**\n" + "\n".join(report)})


if __name__ == "__main__":
    main()
