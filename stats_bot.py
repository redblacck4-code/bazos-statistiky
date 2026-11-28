#!/usr/bin/env python3
"""
Bazoš štatistiky – samostatný bot (NIE je prepojený s Bazoš strážcom).

Každé ~2 hodiny prejde Bazoš (celé Slovensko) pre modely, ktoré sledujeme, a zapamätá si:
  kedy sa inzerát objavil, za koľko, či zlacnel a kedy zmizol (predaný alebo zmazaný).
Raz týždenne pošle do Telegramu prehľad: čo zmizne najrýchlejšie (najväčší záujem),
priemerné ceny, počet nových inzerátov a trend cien.

Použitie:
  python stats_bot.py scan      – jeden prechod Bazošom (spúšťa GitHub Actions každé 2 h)
  python stats_bot.py report    – pošle týždenný prehľad do Telegramu (nedeľa večer)
  python stats_bot.py flips     – spracuje správy o kúpe/predaji z Telegramu (každých 20 min)
  python stats_bot.py show      – vypíše prehľad len na obrazovku (nič neposiela)
Premenné prostredia: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
"""
import csv, json, os, random, re, statistics, sys, time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

import modely as M

DATA = Path(__file__).with_name("data")
LISTINGS = DATA / "listings.json"     # všetky sledované inzeráty
HISTORY = DATA / "history.json"       # týždenné mediány cien (pre trend)
META = DATA / "meta.json"             # počítadlo neúspešných behov a pod.

MAX_PAGES = int(os.getenv("MAX_PAGES", "8"))        # strán po 20 na jedno hľadanie
MAX_DETAIL = int(os.getenv("MAX_DETAIL", "60"))     # koľko "zmiznutých" overiť v jednom behu
KEEP_DAYS = 90                                      # staršie záznamy sa zmažú
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
      "Accept-Language": "sk-SK,sk;q=0.9"}

# (kľúč, sekcia Bazoša, hľadaný text, cena od) – celé Slovensko
QUERIES = [(f"iphone{n}", "mobil", f"iphone {n}", 80) for n in range(12, 19)]
QUERIES += [("iphoneair", "mobil", "iphone air", 200)]
QUERIES += [(q.replace(" ", ""), "mobil", q, 80) for q in
            ("galaxy s23", "galaxy s24", "galaxy s25", "galaxy s26", "galaxy z fold", "galaxy z flip")]
QUERIES += [("applewatch", "mobil", "apple watch", 50), ("garmin", "mobil", "garmin", 80)]
QUERIES += [(q.replace(" ", ""), "pc", q, 150) for q in ("macbook air", "macbook pro", "mac mini", "imac")]
QUERIES += [(q.replace(" ", ""), "pc", q, 120) for q in
            ("ps5", "playstation 5", "xbox series x")]
SKIP_MODELS = ("Nintendo", "Steam Deck")      # nesledujeme – takmer sa nepredávajú

CATEGORY = [("iPhone", "iPhone"), ("MacBook", "MacBook"), ("Mac mini", "Mac"), ("iMac", "Mac"),
            ("Galaxy", "Samsung"), ("Apple Watch", "Apple Watch"), ("AirPods", "Ostatné"), ("iPad", "Ostatné"),
            ("Garmin", "Garmin"), ("PS5", "Konzoly"), ("Xbox", "Konzoly"), ("Steam Deck", "Konzoly"),
            ("Nintendo", "Konzoly"), ("DualSense", "Konzoly")]


class Blocked(Exception):
    pass


_http = requests.Session()


def get(url, tries=3):
    for attempt in range(tries):
        time.sleep(2.5 + 1.5 * random.random())      # pomalšie tempo – Bazoš inak po ~150 požiadavkách brzdí
        try:
            r = _http.get(url, headers=UA, timeout=25, allow_redirects=True)
        except requests.RequestException:
            if attempt == tries - 1:
                raise
            time.sleep(15)
            continue
        if r.status_code in (403, 429, 503):
            if attempt == tries - 1:
                raise Blocked(f"HTTP {r.status_code}")
            time.sleep(30 * (attempt + 1))            # krátke pribrzdenie Bazoša – počkaj a skús znova
            continue
        return r


# ---------------------------------------------------------------- rozpoznanie modelu
def normalize(s):
    for rx, rep in M._NORM:
        s = rx.sub(rep, s)
    return s


def classify(title, snippet=""):
    """(model, kapacita alebo None, orientačná cena) alebo None – len funkčné kusy, bez príslušenstva."""
    title, snippet = normalize(title), normalize(snippet or "")
    full = f"{title} {snippet}"
    if M.OTHER_BRAND.search(title) and not M.OUR_BRAND.search(title):
        return None
    if (M.HARD_EXCLUDE.search(full) or M.PHOTO_SCAM.search(full) or M.TITLE_PHOTO.search(title)
            or M.TITLE_ACCESSORY.search(title)):
        return None
    c = M.console_estimate(title)
    if c:
        if c[0].startswith(("Ovládač", "DualSense") + SKIP_MODELS):
            return None                                   # ovládače nesledujeme
        return c[0], None, c[1]
    t = full.lower()
    m = M.MODEL_RE.search(t) or ("iphone" in t and M._IPH_FALLBACK.search(t))
    if m:
        if m.group(1):
            model = m.group(1) + (" " + re.sub(r"\s+", " ", m.group(2).lower()) if m.group(2) else "")
            model = model.replace("16 e", "16e").replace("17 e", "17e")
        else:
            model = "air"
        prices = M.IPHONE.get(model)
        if not prices:
            return None
        g = M.GB_RE.search(t)
        gb = (1024 if g.group(2) else int(g.group(1))) if g else None
        if gb is None:
            g2 = re.search(r"\b(128|256|512)\b(?!\s*(€|eur|,-|e\b|%|mah|hz|km|x))", t)
            gb = int(g2.group(1)) if g2 else None
        if gb is not None and gb < min(prices):
            return None                                   # 64 GB a menej nesledujeme
        pretty = model.replace("pro max", "Pro Max").replace("pro", "Pro").replace("plus", "Plus").replace("air", "Air")
        ref = prices.get(gb) or min(prices.values())
        return f"iPhone {pretty}", gb_label(gb) if gb in prices else None, ref
    for rx, name, value in M.OTHER:
        if re.search(rx, t, re.I):
            if M.ACCESSORY.search(title) and any(k in name for k in ("Mac mini", "iMac", "Apple Watch", "Garmin")):
                return None
            return name, variant(name, t), value
    return None


def gb_label(gb):
    return None if gb is None else ("1 TB" if gb == 1024 else f"{gb} GB")


def variant(name, t):
    """MacBook/Mac: 'RAM/SSD' (napr. 16/512), Samsung: kapacita. Inak None."""
    if name.startswith(("MacBook", "Mac mini", "iMac")):
        ram = re.search(r"\b(8|16|18|24|32|36|48|64)\s?gb\s*(ram|rám|unified|/|,|\s*a\b)|\b(8|16|18|24|32|36|48|64)\s*/\s*(256|512|1)", t)
        ssd = re.search(r"\b(256|512)\s?gb\s*(ssd|disk|úložisk|ulozisk)?|\b([12])\s?tb", t)
        r = (ram.group(1) or ram.group(3)) if ram else None
        d = None
        if ssd:
            d = ssd.group(1) or (ssd.group(3) + "TB")
        elif ram and ram.group(4):
            d = ram.group(4) if ram.group(4) != "1" else "1TB"
        if r and d:
            return f"{r}/{d}"
        return f"{r} GB RAM" if r else (f"SSD {d}" if d else None)
    if name.startswith("Galaxy"):
        g = re.search(r"\b(128|256|512)\s?gb|\b(1)\s?tb", t)
        return gb_label(1024 if g and g.group(2) else int(g.group(1))) if g else None
    return None


def category(model):
    for prefix, cat in CATEGORY:
        if model.startswith(prefix):
            return cat
    return "Ostatné"


# ---------------------------------------------------------------- Bazoš
def parse_list(html, base):
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for box in soup.select("div.inzeraty"):
        a = box.select_one("h2.nadpis a") or box.select_one("h2 a")
        if not a:
            continue
        href = a.get("href", "")
        m = re.search(r"/inzerat/(\d+)/", href)
        if not m:
            continue
        pr = box.select_one(".inzeratycena")
        price = int(re.sub(r"[^\d]", "", pr.get_text(" ", strip=True)) or 0) if pr else 0
        loc = box.select_one(".inzeratylok")
        popis = box.select_one(".popis")
        out.append({"id": m.group(1), "url": href if href.startswith("http") else base + href,
                    "title": a.get_text(strip=True), "price": price,
                    "loc": re.sub(r"\s+", " ", loc.get_text(" ", strip=True)) if loc else "",
                    "snippet": popis.get_text(" ", strip=True) if popis else ""})
    return out


DETAIL_CODES = {}      # diagnostika: aké odpovede vracia Bazoš pri overovaní zmiznutých


def is_gone(url):
    """True = inzerát už neexistuje, False = stále je, None = nevedno (chyba siete)."""
    try:
        r = get(url, tries=2)
    except Blocked:
        DETAIL_CODES["blokované"] = DETAIL_CODES.get("blokované", 0) + 1
        raise
    except requests.RequestException:
        DETAIL_CODES["sieť"] = DETAIL_CODES.get("sieť", 0) + 1
        return None
    DETAIL_CODES[str(r.status_code)] = DETAIL_CODES.get(str(r.status_code), 0) + 1
    if r.status_code in (404, 410):
        return True
    if r.status_code != 200:
        return None
    if "/inzerat/" not in r.url:
        return True                                         # presmerovanie preč z inzerátu
    soup = BeautifulSoup(r.text, "html.parser")
    if soup.select_one(".popisdetail"):
        return False
    if re.search(r"neexistuje|vymazan|zmazan|odstránen|nebol nájden", r.text, re.I):
        return True
    return None


BATCH_GAP = 250        # inzeráty s ID bližšie ako toto pridal pravdepodobne ten istý predajca naraz
BATCH_MIN = 3


def mark_batches(db, now):
    """Predajca (bazár) zmaže/obnoví naraz viac inzerátov – to nie sú predaje. Ak zmizne 3+ inzerátov,
    ktoré boli pridané tesne po sebe (blízke ID), označí ich ako hromadné zmazanie (b=1) a nepočítajú sa."""
    recent = sorted((int(i), r) for i, r in db.items() if r.get("g") and r["g"] >= now - 6 * 3600)
    marked, group = 0, []
    for iid, r in recent + [(None, None)]:
        if group and (iid is None or iid - group[-1][0] > BATCH_GAP):
            if len(group) >= BATCH_MIN:
                for _, gr in group:
                    if not gr.get("b"):
                        gr["b"] = 1
                        marked += 1
            group = []
        if iid is not None:
            group.append((iid, r))
    return marked


def sold(r):
    """Zmizol a nejde o hromadné zmazanie ani o znovu pridaný inzerát = berieme ako predaný."""
    return bool(r.get("g")) and not r.get("b") and not r.get("re")


def _norm(t):
    t = re.sub(r"[^a-z0-9á-ž ]", " ", (t or "").lower())
    return re.sub(r"\s+", " ", t).strip()


def fingerprint(it):
    """Odtlačok inzerátu: nadpis + lokalita + začiatok popisu. Rovnaký odtlačok = ten istý kus znovu pridaný."""
    return f"{_norm(it['title'])}|{_norm(it.get('loc'))}|{_norm(it.get('snippet'))[:80]}"


# ---- stav kusu (z nadpisu a začiatku popisu, bez otvárania inzerátu)
_NEG_ST = re.compile(r"(\bnie\b|\bnikdy\b|\bnebol\w*|\bbez\b|\bnemá\b|\bžiadn\w*|\bziadn\w*)[^.,;!?]{0,20}$", re.I)
ST_BROKEN = re.compile(r"(displej|obrazovk|lcd|sklo|zadn\w* sk|chrbt)[^.]{0,30}(rozbit|prask|pukn|poškod|čiar|nefunk)|"
                       r"(rozbit|prask|pukn)\w*[^.]{0,25}(displej|obrazovk|sklo|zadn)|face\s*id\s*(nefung|nejde|nefunk)|"
                       r"nefunguje\s*(face|kamer|nabíjan|nabijan|reproduk|mikrof)|okrem\b|na\s*opravu|poškoden|rozbit", re.I)
ST_NEW = re.compile(r"\bnov[ýáé]\b|nerozbal|zabalen|zapečaten|zapecaten|sealed|\bnepoužit|\bnepouzit", re.I)
ST_TOP = re.compile(r"záruk|zaruk|ako\s*nov|top\s*stav|perfekt|10\s*/\s*10|bez\s*(škrab|skrab|vád|vad)|krabic|komplet", re.I)
_BAT = re.compile(r"(\d{2,3})\s?%")
STAVY = {"new": "🆕 nový", "top": "⭐ top / záruka", "ok": "✅ bežný", "bat": "🔋 batéria pod 85 %", "broken": "🔧 poškodený"}


def _hit(rx, t):
    return any(not _NEG_ST.search(t[max(0, m.start() - 25):m.start()]) for m in rx.finditer(t))


def condition(title, snippet=""):
    t = f"{title} {snippet}"
    if _hit(ST_BROKEN, t):
        return "broken"
    bats = [int(b) for b in _BAT.findall(t) if 50 <= int(b) <= 100]
    if _hit(ST_NEW, title) and not bats or re.search(r"nerozbal|zabalen|zapečaten|zapecaten|sealed", t, re.I):
        return "new"
    if bats and bats[0] < 85:
        return "bat"
    if (bats and bats[0] >= 90) or _hit(ST_TOP, t):
        return "top"
    return "ok"


MAX_MEM = int(os.getenv("MAX_MEM", "20"))


def mem_missing(r):
    if r.get("mv") or r.get("g"):
        return False
    gb = r.get("gb") or ""
    if r["m"].startswith(("iPhone", "Galaxy")):
        return not gb
    if r["m"].startswith("MacBook"):
        return "/" not in gb
    return False


def detail_text(url):
    try:
        r = get(url, tries=2)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    d = BeautifulSoup(r.text, "html.parser").select_one(".popisdetail")
    return re.sub(r"\s+", " ", d.get_text(" ", strip=True)) if d else None


def fill_memory(db):
    """Kapacitu / RAM a SSD hľadá v celom popise – iba pri inzerátoch, kde chýba v nadpise (pár za beh)."""
    todo = sorted((r for r in db.values() if mem_missing(r)), key=lambda r: -r["f"])[:MAX_MEM]
    done = 0
    for r in todo:
        try:
            txt = detail_text(r["u"])
        except Blocked:
            break                                          # Bazoš brzdí – zvyšok nabudúce
        if txt is None:
            continue
        r["mv"] = 1                                        # overené, znova neotvárať
        c = classify(r["t"], txt[:600])
        if c and c[0] == r["m"] and c[1]:
            r["gb"] = c[1]
            done += 1
        r["st"] = condition(r["t"], txt[:400])             # stav podľa celého popisu (batéria, poškodenie)
    return done


def cond(r):
    return r.get("st") or condition(r.get("t", ""))


RELIST_DAYS = 14


def link_relists(db, now):
    """Predajca zmaže inzerát a pridá ho znova (rovnaký nadpis, lokalita, popis). Starý sa neráta ako predaný
    a nový pokračuje s pôvodným dátumom a cenou – dni v predaji sa počítajú od prvého pridania."""
    gone = {}
    for i, r in db.items():
        if r.get("g") and not r.get("re") and r["g"] >= now - RELIST_DAYS * 86400:
            gone.setdefault((r["m"], r.get("gb")), []).append((i, r))
    linked = 0
    for i, r in db.items():
        if r.get("lnk") or r.get("x") or r["f"] < now - RELIST_DAYS * 86400:
            continue
        for j, o in gone.get((r["m"], r.get("gb")), []):
            if j == i or o.get("re") or o["f"] >= r["f"]:
                continue
            same = (r.get("h") and o.get("h") == r["h"]) or (
                not o.get("h") and _norm(o["t"]) == _norm(r["t"]) and abs(o["p"] - r["p"]) <= 0.15 * o["p"])
            if same:
                o["re"] = 1                              # starý: nie predaný, len znovu pridaný
                r["lnk"] = j
                r["f"], r["p0"], r["x"] = o["f"], o["p0"], o.get("x", 0)
                r["d"] = o.get("d", 0) + (1 if r["p"] < o["p"] else 0)
                r["n"] = o.get("n", 0) + 1               # koľkokrát bol znovu pridaný
                linked += 1
                break
    return linked


# ---------------------------------------------------------------- dáta
def load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save(path, obj):
    DATA.mkdir(exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


def telegram(text):
    tok = (os.getenv("TELEGRAM_TOKEN") or "").strip("'\" ")
    chat = (os.getenv("TELEGRAM_CHAT_ID") or "").strip("'\" ")
    if not tok or not chat:
        print(text)
        return
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) > 3500:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    chunks.append(cur)
    for c in chunks:
        if c.strip():
            requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                          data={"chat_id": chat, "text": c, "parse_mode": "HTML",
                                "disable_web_page_preview": "true"}, timeout=20)


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------- scan
def scan():
    db = load(LISTINGS, {})
    meta = load(META, {"fails": 0, "warned": False})
    seeding = not db                     # úplne prvý beh: inzeráty už existovali, čas predaja sa nepočíta
    now = time.time()
    seen, thresholds, new, changed = set(), {}, 0, 0
    blocked_q, streak_q = [], 0
    try:
        for key, sec, q, cena_od in QUERIES:
            base = f"https://{sec}.bazos.sk"
            last_ids = []
            if streak_q >= 2:                      # Bazoš brzdí – zvyšné hľadania nechaj na ďalší beh
                blocked_q.append(key)
                continue
            for p in range(MAX_PAGES):
                off = f"{p * 20}/" if p else ""
                url = f"{base}/{off}?hledat={requests.utils.quote(q)}&hlokalita=&humkreis=&cenaod={cena_od}&cenado="
                try:
                    r = get(url)
                except Blocked as e:
                    print(f"[!] {q}: {e} – preskakujem, pauza 2 min", file=sys.stderr)
                    blocked_q.append(key)
                    streak_q += 1
                    last_ids = None
                    time.sleep(120)
                    break
                streak_q = 0
                if r.status_code != 200:
                    break
                items = parse_list(r.text, base)
                for it in items:
                    seen.add(it["id"])
                    rec = db.get(it["id"])
                    if rec:
                        rec["l"] = now
                        rec.setdefault("k", [])
                        if key not in rec["k"]:
                            rec["k"].append(key)
                        if it["price"] and it["price"] != rec["p"]:
                            if it["price"] < rec["p"]:
                                rec["d"] = rec.get("d", 0) + 1
                            rec["p"] = it["price"]
                            changed += 1
                        if rec.get("g"):           # objavil sa znova (napr. obnovený inzerát)
                            rec["g"] = None
                            rec.pop("b", None)
                        continue
                    c = classify(it["title"], it["snippet"])
                    if not c or it["price"] <= 0:
                        continue
                    model, gb, ref = c
                    if not (0.35 * ref <= it["price"] <= 2.2 * ref):
                        continue                   # diely, preklepy, nezmysly
                    db[it["id"]] = {"m": model, "gb": gb, "t": it["title"][:80], "u": it["url"],
                                    "p": it["price"], "p0": it["price"], "f": now, "l": now, "g": None,
                                    "k": [key], "x": 1 if seeding else 0, "h": fingerprint(it),
                                    "st": condition(it["title"], it["snippet"])}
                    new += 1
                last_ids = [int(i["id"]) for i in items]
                if len(items) < 20:
                    last_ids = []                  # koniec výsledkov – pokrytie celého hľadania
                    break
            if last_ids is None:
                continue                           # zablokované hľadanie – jeho inzeráty teraz neoverujeme
            thresholds[key] = min(last_ids) if last_ids else 0
        if not seen:
            raise Blocked("žiadne hľadanie neprešlo")

        # pamäť nie je v nadpise -> otvor inzerát (len tie, najviac MAX_MEM za beh)
        filled = fill_memory(db)

        # inzeráty, ktoré mali byť vo výsledkoch, ale neboli -> overiť, či zmizli
        cands = [(i, r) for i, r in db.items()
                 if not r.get("g") and i not in seen
                 and any(k in thresholds and int(i) >= thresholds[k] for k in r.get("k", []))]
        cands.sort(key=lambda x: x[1]["l"])
        meta["fails"], meta["warned"] = 0, False      # zoznamy prešli – zber je v poriadku
        gone, checked, streak = 0, 0, 0
        for i, r in cands[:MAX_DETAIL]:
            try:
                res = is_gone(r["u"])
            except Blocked as e:                       # overovanie detailu zlyhalo – zber dát tým nezahadzuj
                streak += 1
                if streak >= 3:
                    print(f"[!] overovanie zmiznutých zastavené: {e}", file=sys.stderr)
                    break
                time.sleep(20)
                continue
            streak = 0
            checked += 1
            if res is True:
                r["g"] = now
                gone += 1
            elif res is False:
                r["l"] = now
        batch = mark_batches(db, now)
        relist = link_relists(db, now)
        meta["detail"] = {"doplnena_pamat": filled, "hromadne_zmazane": batch, "znovu_pridane": relist, "cas": time.strftime("%d.%m. %H:%M"), "zablokovane_hladania": blocked_q, "kandidatov": len(cands), "overenych": checked,
                          "zmizlo": gone, "odpovede": DETAIL_CODES}
        print(f"{time.strftime('%d.%m. %H:%M')} nové {new}, zmenená cena {changed}, zmizlo {gone}, "
              f"overených {checked}/{len(cands)} {DETAIL_CODES}, sledovaných {sum(1 for r in db.values() if not r.get('g'))}")
    except (Blocked, requests.RequestException) as e:
        meta["fails"] = meta.get("fails", 0) + 1
        print(f"[!] Bazoš: {e} (neúspešných behov po sebe: {meta['fails']})", file=sys.stderr)
        if meta["fails"] >= 6 and not meta.get("warned"):
            telegram("⚠️ <b>Bazoš štatistiky</b>: Bazoš už ~12 hodín nepúšťa GitHub (blokuje). "
                     "Dáta sa momentálne nezbierajú.")
            meta["warned"] = True
    # upratovanie (aj modely, ktoré už nesledujeme)
    limit = now - KEEP_DAYS * 86400
    db = {i: r for i, r in db.items() if (r.get("g") or r["l"]) >= limit and not r["m"].startswith(SKIP_MODELS)}
    save(LISTINGS, db)
    save(META, meta)


# ---------------------------------------------------------------- report
def fmt_days(d):
    if d is None:
        return "–"
    return f"{d * 24:.0f} h" if d < 1 else f"{d:.1f} d"


def stats(db, now):
    week, month = now - 7 * 86400, now - 30 * 86400
    per = {}
    for r in db.values():
        s = per.setdefault(r["m"], {"new7": 0, "gone7": 0, "days": [], "cohort": 0, "fast": 0,
                                    "prices": {}, "drops": 0, "n30": 0, "active": 0})
        if not r.get("g") and now - r["l"] < 3 * 86400:
            s["active"] += 1
        if (r.get("x") or r["f"] >= month) and (not r.get("g") or r["g"] >= month):
            if cond(r) in ("ok", "top", "bat"):
                s["prices"].setdefault(r.get("gb"), []).append(r["p0"])   # ceny aj z inzerátov spred spustenia
        if r["f"] >= week and not r.get("x"):
            s["new7"] += 1
        if sold(r) and r["g"] >= week:
            s["gone7"] += 1
        if sold(r) and r["g"] >= month and not r.get("x"):
            s["days"].append((r["g"] - r["f"]) / 86400)
        if r["f"] >= month and not r.get("x"):
            s["n30"] += 1
            if r.get("d"):
                s["drops"] += 1
            if now - r["f"] >= 3 * 86400:          # dosť starý na posúdenie "zmizol do 3 dní"
                s["cohort"] += 1
                if sold(r) and r["g"] - r["f"] <= 3 * 86400:
                    s["fast"] += 1
    out = {}
    for m, s in per.items():
        gb_lists = {k: v for k, v in s["prices"].items() if v}
        main_gb = max(gb_lists, key=lambda k: len(gb_lists[k])) if gb_lists else None
        price = round(statistics.median(gb_lists[main_gb]) / 5) * 5 if main_gb in gb_lists else None
        out[m] = {"new7": s["new7"], "gone7": s["gone7"], "n30": s["n30"], "active": s["active"],
                  "med_days": statistics.median(s["days"]) if s["days"] else None, "sold30": len(s["days"]),
                  "fast_pct": round(100 * s["fast"] / s["cohort"]) if s["cohort"] else None, "cohort": s["cohort"],
                  "price": price, "gb": main_gb, "drop_pct": round(100 * s["drops"] / s["n30"]) if s["n30"] else None}
    return out


def sold_items(db, now, days=60):
    """Inzeráty, ktoré zmizli (≈ predané) za posledných `days` dní, bez tých, čo existovali pred spustením."""
    lim = now - days * 86400
    return [r for r in db.values() if sold(r) and r["g"] >= lim and not r.get("x")
            and r["g"] - r["f"] <= 45 * 86400]


SOLD_FILE = DATA / "predajne_ceny.json"   # skutočné predajné ceny – číta ich Bazoš strážca (ceny.py)
SOLD_MIN_N = 5


def sold_key(r):
    """Názov modelu rovnako ako v strážcovi: iPhony s kapacitou ('iPhone 15 Pro 256 GB'), ostatné bez."""
    if r["m"].startswith("iPhone"):
        return f"{r['m']} {r['gb']}" if r.get("gb") else None
    return r["m"]


def sold_prices(db, now, days=45):
    """Za koľko sa kusy naozaj predali: posledná cena inzerátu tesne pred zmiznutím.
    Nepočíta inzeráty, ktoré viseli 28+ dní (vypršali, nepredali sa)."""
    lim = now - days * 86400
    groups = {}
    for r in db.values():
        g = r.get("g")
        if not sold(r) or g < lim:
            continue
        if not r.get("x") and g - r["f"] >= 28 * 86400:
            continue
        k = sold_key(r)
        if k and not r["m"].startswith("iPhone") and r.get("gb") and cond(r) in ("ok", "top"):
            groups.setdefault((k, "var:" + r["gb"]), []).append((r["p"], None if r.get("x") else (g - r["f"]) / 86400))
        if k:
            c = cond(r)
            item = (r["p"], None if r.get("x") else (g - r["f"]) / 86400)
            groups.setdefault((k, c), []).append(item)
            if c in ("ok", "top"):
                groups.setdefault(k, []).append(item)     # hlavná cena = funkčný kus (bez nových a poškodených)
    out = {}
    for k, items in groups.items():
        if len(items) < SOLD_MIN_N:
            continue
        ps = sorted(p for p, _ in items)
        cut = len(ps) // 10                                  # orež 10 % extrémov z oboch strán
        core = ps[cut:len(ps) - cut] or ps
        ds = [d for _, d in items if d is not None]
        val = {"cena": int(round(statistics.median(core) / 5) * 5), "n": len(items),
               "dni": round(statistics.median(ds), 1) if ds else None}
        if isinstance(k, tuple) and k[1].startswith("var:"):
            out.setdefault(k[0], {}).setdefault("varianty", {})[k[1][4:]] = val
        elif isinstance(k, tuple):
            out.setdefault(k[0], {}).setdefault("stavy", {})[k[1]] = val
        else:
            out.setdefault(k, {}).update(val)
    return {k: v for k, v in out.items() if "cena" in v or v.get("stavy") or v.get("varianty")}


def ideal_prices(db, now):
    """Pri akej cene sa model predá rýchlo a kde je strop. Delí predané kusy na 3 cenové pásma."""
    groups = {}
    for r in sold_items(db, now):
        groups.setdefault((r["m"], r.get("gb")), []).append((r["p"], (r["g"] - r["f"]) / 86400))
    out = []
    for (m, v), items in groups.items():
        if len(items) < 9:
            continue
        items.sort()
        n = len(items)
        bands = [items[:n // 3], items[n // 3:2 * n // 3], items[2 * n // 3:]]
        res = []
        for b in bands:
            res.append((b[0][0], b[-1][0], statistics.median(d for _, d in b)))
        out.append((m, v, n, res))
    out.sort(key=lambda x: -x[2])
    return out


def best_flips(db, now):
    """Modely, kde sa dá kúpiť lacno (15. percentil) a rýchlo predať (medián) – skóre = rozdiel × rýchlosť."""
    lim = now - 30 * 86400
    groups = {}
    for r in db.values():
        if r["f"] >= lim and not r.get("x"):
            groups.setdefault((r["m"], r.get("gb")), []).append(r)
    out = []
    for (m, v), rs in groups.items():
        if len(rs) < 8:
            continue
        ps = sorted(r["p0"] for r in rs)
        cheap = ps[max(0, round((len(ps) - 1) * 0.15))]
        med = statistics.median(ps)
        sold_d = [(r["g"] - r["f"]) / 86400 for r in rs if sold(r)]
        old = [r for r in rs if now - r["f"] >= 3 * 86400]
        fast = sum(1 for r in old if sold(r) and r["g"] - r["f"] <= 3 * 86400) / len(old) if len(old) >= 4 else None
        if fast is None or not sold_d:
            continue
        spread = med * 0.93 - cheap                         # predáš ~7 % pod mediánom
        out.append({"m": m, "v": v, "cheap": round(cheap / 5) * 5, "med": round(med / 5) * 5,
                    "spread": round(spread), "fast": round(fast * 100), "days": statistics.median(sold_d),
                    "score": spread * fast, "n": len(rs)})
    out.sort(key=lambda x: -x["score"])
    return out


DAYS_SK = ["pondelok", "utorok", "streda", "štvrtok", "piatok", "sobota", "nedeľa"]


def best_timing(db, now):
    """V ktorý deň / čas pridané inzeráty zmiznú najrýchlejšie (všetky modely spolu)."""
    by_day, by_part = {}, {}
    for r in sold_items(db, now, days=90):
        lt = time.localtime(r["f"])
        d = (r["g"] - r["f"]) / 86400
        by_day.setdefault(lt.tm_wday, []).append(d)
        part = "ráno (5–11)" if 5 <= lt.tm_hour < 11 else "cez deň (11–17)" if 11 <= lt.tm_hour < 17 else \
            "večer (17–22)" if 17 <= lt.tm_hour < 22 else "v noci (22–5)"
        by_part.setdefault(part, []).append(d)
    days = sorted(((DAYS_SK[k], statistics.median(v), len(v)) for k, v in by_day.items() if len(v) >= 5),
                  key=lambda x: x[1])
    parts = sorted(((k, statistics.median(v), len(v)) for k, v in by_part.items() if len(v) >= 5), key=lambda x: x[1])
    return days, parts


def build_report(send):
    db = load(LISTINGS, {})
    hist = load(HISTORY, {})
    now = time.time()
    if not db:
        return "Bazoš štatistiky: zatiaľ žiadne dáta."
    st = stats(db, now)
    first = min(r["f"] for r in db.values())
    days_running = (now - first) / 86400
    prev = hist[max(hist)] if hist else {}
    lines = [f"📊 <b>Bazoš štatistiky – {time.strftime('%d.%m.%Y')}</b>",
             f"Sledované {days_running:.0f} dní · aktívnych inzerátov {sum(1 for r in db.values() if not r.get('g') and now - r['l'] < 3 * 86400)}",
             f"Za 7 dní: nových {sum(s['new7'] for s in st.values())}, zmizlo {sum(s['gone7'] for s in st.values())}"]
    if days_running < 10:
        lines.append("<i>Dát je zatiaľ málo – čísla budú presnejšie po 2–3 týždňoch.</i>")

    ranked = [(m, s) for m, s in st.items() if s["cohort"] >= 4 and s["fast_pct"] is not None]
    ranked.sort(key=lambda x: (-x[1]["fast_pct"], x[1]["med_days"] if x[1]["med_days"] is not None else 99))
    if ranked:
        lines += ["", "🔥 <b>Najväčší záujem</b> (najviac zmizne do 3 dní)"]
        for m, s in ranked[:10]:
            pr = f" · {s['price']} €" if s["price"] else ""
            lines.append(f"• {esc(m)}: <b>{s['fast_pct']} %</b> do 3 dní · medián {fmt_days(s['med_days'])}{pr}")
        slow = ranked[max(10, len(ranked) - 5):][::-1]
        if slow:
            lines += ["", "🐢 <b>Najpomalšie</b>"]
            for m, s in slow:
                lines.append(f"• {esc(m)}: {s['fast_pct']} % do 3 dní · medián {fmt_days(s['med_days'])}")

    flips = best_flips(db, now)
    if flips:
        lines += ["", "💰 <b>Najlepší flip</b> (lacné kusy × rýchly predaj)"]
        for f in flips[:8]:
            v = f" {f['v']}" if f["v"] else ""
            lines.append(f"• {esc(f['m'])}{esc(v)}: lacné od ~{f['cheap']} €, bežne ~{f['med']} € → "
                         f"rozdiel ~{f['spread']} € · {f['fast']} % do 3 dní")

    ideal = ideal_prices(db, now)
    if ideal:
        lines += ["", "🎯 <b>Ideálna predajná cena</b> (koľko dní trvá predaj podľa ceny)"]
        for m, v, n, bands in ideal[:10]:
            vv = f" {v}" if v else ""
            txt = " | ".join(f"{lo}–{hi} €: {fmt_days(d)}" for lo, hi, d in bands)
            lines.append(f"• {esc(m)}{esc(vv)}: {txt}")

    sp = sold_prices(db, now)
    if sp:
        top = sorted(((k, x) for k, x in sp.items() if "cena" in x), key=lambda kv: -kv[1]["n"])[:12]
        lines += ["", "🏷 <b>Skutočné predajné ceny</b> (posledná cena pred predajom, 45 dní)"]
        for k, x in top:
            d = f" · predané za {fmt_days(x['dni'])}" if x["dni"] is not None else ""
            lines.append(f"• {esc(k)}: <b>{x['cena']} €</b> ({x['n']} ks){d}")

    days, parts = best_timing(db, now)
    if days:
        lines += ["", "🗓 <b>Kedy pridať inzerát</b> (najrýchlejšie zmiznú)"]
        lines.append("• dni: " + ", ".join(f"{d} {fmt_days(x)}" for d, x, _ in days[:3])
                     + (f" · najhoršie: {days[-1][0]} {fmt_days(days[-1][1])}" if len(days) > 3 else ""))
        if parts:
            lines.append("• čas: " + ", ".join(f"{p} {fmt_days(x)}" for p, x, _ in parts))

    fl = flip_summary()
    if fl:
        lines += ["", fl]

    by_cat = {}
    for m, s in st.items():
        if s["n30"] or s["gone7"] or s["active"]:
            by_cat.setdefault(category(m), []).append((m, s))
    for cat in sorted(by_cat):
        lines += ["", f"<b>{cat}</b>  (model: cena · nové/7d · zmizlo/7d · medián predaja)"]
        for m, s in sorted(by_cat[cat], key=lambda x: -x[1]["active"]):
            if s["price"]:
                gb = f" {s['gb']}" if s["gb"] else ""
                old = prev.get(m)
                trend = ""
                if old:
                    ch = (s["price"] - old) / old * 100
                    trend = f" {'🔺' if ch >= 3 else '🔻' if ch <= -3 else '='}{ch:+.0f} %"
                price = f"{s['price']} €{gb}{trend}"
            else:
                price = "–"
            lines.append(f"• {esc(m)}: {price} · {s['new7']} / {s['gone7']} · {fmt_days(s['med_days'])}")

    lines += ["", "<i>„Zmizlo“ = inzerát bol zmazaný (väčšinou predaný). Ceny = medián inzerovaných cien za 30 dní.</i>"]
    text = "\n".join(lines)
    if send:
        hist[time.strftime("%Y-%m-%d")] = {m: s["price"] for m, s in st.items() if s["price"]}
        hist = dict(sorted(hist.items())[-26:])            # pol roka histórie
        save(HISTORY, hist)
    return text


FLIPS = DATA / "flips.json"
STATE_TG = DATA / "telegram.json"
_NUMRX = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:€|eur|e\b)?\s*$", re.I)


def tg_api(method, **params):
    tok = (os.getenv("TELEGRAM_TOKEN") or "").strip("'\" ")
    if not tok:
        return None
    try:
        return requests.post(f"https://api.telegram.org/bot{tok}/{method}", data=params, timeout=30).json()
    except Exception:
        return None


def reply(text):
    chat = (os.getenv("TELEGRAM_CHAT_ID") or "").strip("'\" ")
    if chat:
        r = tg_api("sendMessage", chat_id=chat, text=text, parse_mode="HTML", disable_web_page_preview="true")
        if not r or not r.get("ok"):
            print(f"[!] Telegram odpoveď neodišla: {r}", file=sys.stderr)
    else:
        print(text)


def flip_summary(days=30, fl=None):
    fl = load(FLIPS, []) if fl is None else fl
    if not fl:
        return ""
    now = time.time()
    done = [f for f in fl if f.get("sold") and f["sold_t"] >= now - days * 86400]
    open_ = [f for f in fl if not f.get("sold")]
    total = [f for f in fl if f.get("sold")]
    lines = [f"🧾 <b>Moje flipy</b> (za {days} dní)"]
    if done:
        profit = sum(f["sold"] - f["buy"] - f.get("cost", 0) for f in done)
        avg_d = statistics.median((f["sold_t"] - f["buy_t"]) / 86400 for f in done)
        lines.append(f"• predané: {len(done)} ks, zisk <b>{profit:+.0f} €</b>, medián predaja {fmt_days(avg_d)}")
    else:
        lines.append("• za toto obdobie nič predané")
    if open_:
        lines.append(f"• na sklade: {len(open_)} ks za {sum(f['buy'] + f.get('cost', 0) for f in open_):.0f} € – "
                     + ", ".join(f"#{f['id']} {esc(f['name'])} ({(now - f['buy_t']) / 86400:.0f} d)" for f in open_))
    if total:
        lines.append(f"• celkovo: {len(total)} flipov, zisk {sum(f['sold'] - f['buy'] - f.get('cost', 0) for f in total):+.0f} €")
    return "\n".join(lines)


HELP = ("🧾 <b>Evidencia flipov</b> – napíš botovi:\n"
        "• <code>kúpil iPhone 15 Pro 256 450</code> – kúpa (posledné číslo = cena)\n"
        "• <code>náklad 3 35</code> – náklad k flipu #3 (batéria, poštovné…)\n"
        "• <code>predal 3 540</code> – predaj flipu #3 za 540 €\n"
        "• <code>zmaž 3</code> – zmaže záznam #3\n"
        "• <code>flipy</code> – prehľad\n"
        "Bot odpovie do ~20 minút.")


def process_flips():
    """Prečíta nové správy pre bota a zapíše kúpy/predaje. Prijíma iba správy z TELEGRAM_CHAT_ID."""
    chat = (os.getenv("TELEGRAM_CHAT_ID") or "").strip("'\" ")
    st = load(STATE_TG, {"offset": 0})
    bot_id = (os.getenv("TELEGRAM_TOKEN") or "").strip("'\" ").split(":")[0]
    if st.get("bot") != bot_id:          # iný bot (zmenený token) -> jeho správy majú iné čísla, začni od nuly
        st = {"offset": 0, "bot": bot_id}
    me = tg_api("getMe")
    if me and me.get("ok"):
        print(f"Telegram bot: @{me['result'].get('username')}")
    if not chat:
        print("[!] TELEGRAM_CHAT_ID nie je nastavené (GitHub → Settings → Secrets)", file=sys.stderr)
    res = tg_api("getUpdates", offset=st["offset"], timeout=0, allowed_updates='["message"]')
    if not res or not res.get("ok"):
        print(f"[!] Telegram getUpdates zlyhal: {res if res else 'žiadna odpoveď / chýba TELEGRAM_TOKEN'}", file=sys.stderr)
        return
    ids = {str((u.get("message") or {}).get("chat", {}).get("id")) for u in res.get("result", [])}
    print(f"Telegram: nových správ {len(res.get('result', []))}" + (f", z chatov {sorted(ids)}" if ids else "")
          + ("" if not ids or chat in ids else f" – POZOR: TELEGRAM_CHAT_ID je iné ({chat[:3]}…)"))
    fl = load(FLIPS, [])
    changed = False
    for u in res.get("result", []):
        st["offset"] = u["update_id"] + 1
        msg = u.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != chat:
            continue
        text = (msg.get("text") or "").strip()
        low = text.lower().lstrip("/")
        when = msg.get("date", time.time())
        try:
            if re.match(r"(kúpil|kupil|kúpa|kupa|buy)\b", low):
                rest = re.sub(r"^\S+\s*", "", text)
                m = _NUMRX.search(rest)
                if not m:
                    reply("❓ Chýba cena. Príklad: <code>kúpil iPhone 15 Pro 256 450</code>")
                    continue
                name = rest[:m.start()].strip(" -:,") or "?"
                nid = max([f["id"] for f in fl] or [0]) + 1
                fl.append({"id": nid, "name": name, "buy": float(m.group(1).replace(",", ".")), "buy_t": when})
                reply(f"✅ Zapísané #{nid}: {esc(name)} za {fl[-1]['buy']:.0f} €")
                changed = True
            elif re.match(r"(predal|predaj|sold)\b", low):
                nums = re.findall(r"\d+(?:[.,]\d+)?", text)
                f = next((x for x in fl if len(nums) >= 2 and x["id"] == int(float(nums[0]))), None)
                if not f:
                    reply("❓ Napíš číslo flipu a cenu, napr. <code>predal 3 540</code>. Čísla nájdeš cez <code>flipy</code>.")
                    continue
                f["sold"], f["sold_t"] = float(nums[-1].replace(",", ".")), when
                pr = f["sold"] - f["buy"] - f.get("cost", 0)
                reply(f"💰 #{f['id']} {esc(f['name'])}: predané za {f['sold']:.0f} €, zisk <b>{pr:+.0f} €</b> "
                      f"za {(when - f['buy_t']) / 86400:.0f} dní")
                changed = True
            elif re.match(r"(náklad|naklad|cost)\b", low):
                nums = re.findall(r"\d+(?:[.,]\d+)?", text)
                f = next((x for x in fl if len(nums) >= 2 and x["id"] == int(float(nums[0]))), None)
                if not f:
                    reply("❓ Príklad: <code>náklad 3 35</code>")
                    continue
                f["cost"] = f.get("cost", 0) + float(nums[-1].replace(",", "."))
                reply(f"✅ #{f['id']}: náklady spolu {f['cost']:.0f} €")
                changed = True
            elif re.match(r"(zmaž|zmaz|delete)\b", low):
                nums = re.findall(r"\d+", text)
                before = len(fl)
                fl = [x for x in fl if not nums or x["id"] != int(nums[0])]
                reply("🗑 Zmazané." if len(fl) < before else "❓ Taký záznam nie je.")
                changed = True
            elif re.match(r"(flipy|flip|prehľad|prehlad|stav)\b", low):
                reply(flip_summary(30, fl) or "Zatiaľ žiadne flipy. " + HELP)
            elif low in ("help", "pomoc", "start", "?"):
                reply(HELP)
        except Exception as e:
            reply(f"⚠️ Nerozumel som: {esc(text[:60])}")
            print(f"[!] flip: {e}", file=sys.stderr)
    if changed:
        save(FLIPS, fl)
    save(STATE_TG, st)


def web_data(db, now):
    """Kompaktné dáta pre stránku Flip ceny (záložka Trh)."""
    start = min(r["f"] for r in db.values())
    sold_from = min((r["g"] for r in db.values() if r.get("g")), default=now)
    per = {}
    for r in db.values():
        x = per.setdefault(r["m"], {"new": 0, "sold": 0, "act": 0, "drop": 0, "sp": [], "ap": [], "d": [], "st": {}})
        c = cond(r)
        y = x["st"].setdefault(c, {"sp": [], "ap": [], "d": []})
        if not r.get("x"):
            x["new"] += 1
        if r.get("d"):
            x["drop"] += 1
        if sold(r):
            x["sold"] += 1
            y["sp"].append(r["p"])
            if c in ("ok", "top"):
                x["sp"].append(r["p"])
            if not r.get("x"):
                x["d"].append((r["g"] - r["f"]) / 86400)
                y["d"].append((r["g"] - r["f"]) / 86400)
        elif not r.get("g"):
            x["act"] += 1
            y["ap"].append(r["p"])
            if c in ("ok", "top"):
                x["ap"].append(r["p"])
    med = lambda v: round(statistics.median(v) / 5) * 5 if v else None
    models = [{"m": m, "c": category(m), "new": x["new"], "sold": x["sold"], "act": x["act"], "drop": x["drop"],
               "sp": med(x["sp"]), "ap": med(x["ap"]),
               "days": round(statistics.median(x["d"]), 1) if x["d"] else None,
               "st": {c: {"sp": med(y["sp"]), "ns": len(y["sp"]), "ap": med(y["ap"]), "na": len(y["ap"]),
                          "days": round(statistics.median(y["d"]), 1) if y["d"] else None}
                      for c, y in x["st"].items()}}
              for m, x in per.items() if x["act"] + x["sold"] >= 5]
    hist = load(HISTORY, {})
    return {"updated": now, "od": start, "predaje_od": sold_from, "modely": models,
            "historia": {d: h for d, h in sorted(hist.items())[-12:]}}


def export():
    """Tabuľky (CSV) – na GitHube sa zobrazia ako tabuľka, otvoríš ich aj v Exceli / Google Sheets."""
    db = load(LISTINGS, {})
    if not db:
        return
    now = time.time()
    st = stats(db, now)
    day = lambda ts: time.strftime("%d.%m.%Y %H:%M", time.localtime(ts)) if ts else ""
    with open(DATA / "statistiky.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Kategória", "Model", "Medián ceny €", "Variant (najčastejší)", "Aktívnych inzerátov", "Nových za 30 dní",
                    "Nových za 7 dní", "Zmizlo za 7 dní", "Zmizlo za 30 dní", "Medián dní do predaja",
                    "% zmizne do 3 dní", "% zlacnených"])
        for m, x in sorted(st.items(), key=lambda kv: (category(kv[0]), -kv[1]["active"], -kv[1]["n30"])):
            gb = x["gb"] or ""
            w.writerow([category(m), m, x["price"] or "", gb, x["active"], x["n30"], x["new7"], x["gone7"], x["sold30"],
                        f"{x['med_days']:.1f}".replace(".", ",") if x["med_days"] is not None else "",
                        x["fast_pct"] if x["fast_pct"] is not None else "",
                        x["drop_pct"] if x["drop_pct"] is not None else ""])
    with open(DATA / "inzeraty.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Model", "Variant", "Nadpis", "Prvá cena €", "Aktuálna cena €", "Zlacnený",
                    "Objavil sa", "Zmizol", "Dní v predaji", "Odkaz"])
        for r in sorted(db.values(), key=lambda r: -r["f"]):
            end = r.get("g") or now
            w.writerow([r["m"], r.get("gb") or "", r["t"], r["p0"], r["p"], "áno" if r.get("d") else "",
                        "pred začiatkom" if r.get("x") else day(r["f"]),
                        day(r.get("g")) + (" (hromadne zmazané)" if r.get("b") else " (znovu pridaný)" if r.get("re") else ""),
                        "" if r.get("x") else f"{(end - r['f']) / 86400:.1f}".replace(".", ","), r["u"]])
    fl = load(FLIPS, [])
    if fl:
        with open(DATA / "moje_flipy.csv", "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["#", "Vec", "Kúpené €", "Náklady €", "Dátum kúpy", "Predané €", "Dátum predaja", "Zisk €", "Dní"])
            for x in fl:
                pr = x["sold"] - x["buy"] - x.get("cost", 0) if x.get("sold") else ""
                dn = f"{(x['sold_t'] - x['buy_t']) / 86400:.0f}" if x.get("sold") else ""
                w.writerow([x["id"], x["name"], f"{x['buy']:.0f}", f"{x.get('cost', 0):.0f}", day(x["buy_t"]),
                            f"{x['sold']:.0f}" if x.get("sold") else "", day(x.get("sold_t")),
                            f"{pr:.0f}" if pr != "" else "", dn])
    flips = best_flips(db, now)
    if flips:
        with open(DATA / "najlepsi_flip.csv", "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["Model", "Variant", "Lacné kusy od €", "Medián €", "Rozdiel €", "% predaných do 3 dní",
                        "Medián dní", "Inzerátov 30 dní"])
            for x in flips:
                w.writerow([x["m"], x["v"] or "", x["cheap"], x["med"], x["spread"], x["fast"],
                            f"{x['days']:.1f}".replace(".", ","), x["n"]])
    ideal = ideal_prices(db, now)
    if ideal:
        with open(DATA / "idealna_cena.csv", "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["Model", "Variant", "Predaných", "Lacné pásmo €", "Dní", "Stredné pásmo €", "Dní",
                        "Drahé pásmo €", "Dní"])
            for m, v, n, bands in ideal:
                row = [m, v or "", n]
                for lo, hi, d in bands:
                    row += [f"{lo}–{hi}", f"{d:.1f}".replace(".", ",")]
                w.writerow(row)
    sp = sold_prices(db, now)
    save(SOLD_FILE, {"updated": now, "dni": 45, "ceny": sp})
    save(DATA / "web.json", web_data(db, now))
    if sp:
        with open(DATA / "predajne_ceny.csv", "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["Model", "Stav", "Predajná cena €", "Predaných kusov (45 dní)", "Medián dní do predaja"])
            for k, x in sorted(sp.items(), key=lambda kv: (category(kv[0]), kv[0])):
                rows = ([("funkčný (bežný + top)", x)] if "cena" in x else []) + \
                       [(STAVY[c], v) for c, v in x.get("stavy", {}).items()] + \
                       [(f"konfigurácia {g}", v) for g, v in x.get("varianty", {}).items()]
                for lab, v in rows:
                    w.writerow([k, lab, v["cena"], v["n"], f"{v['dni']:.1f}".replace(".", ",") if v["dni"] is not None else ""])
    hist = load(HISTORY, {})
    if hist:
        models = sorted({m for h in hist.values() for m in h})
        with open(DATA / "ceny_po_tyzdnoch.csv", "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["Model"] + list(hist))
            for m in models:
                w.writerow([m] + [hist[d].get(m, "") for d in hist])


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "scan"
    if mode == "scan":
        process_flips()
        scan()
        export()
    elif mode == "report":
        telegram(build_report(send=True))
        export()
    elif mode == "flips":
        process_flips()
        export()
    elif mode == "show":
        print(re.sub(r"</?[bi]>", "", build_report(send=False)))
    else:
        print(__doc__)
