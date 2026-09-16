# -*- coding: utf-8 -*-
# ============================================================
#  КРИПТО-РАДАР v1.2 — исследовательский бот (НЕ торгует!)
#  © 2026 Чибисов Сергей. Все права защищены.
#  Код является интеллектуальной собственностью автора.
#  Копирование, передача и использование третьими лицами
#  запрещены. Контакты: @Sergey201079
# ------------------------------------------------------------
#  Методология: СКАН → РАЗБОР → ИЗУЧЕНИЕ → РИСК → ПЛАН
#  Гейт подписки: строгий режим (отписался — доступ закрыт
#  мгновенно, через события канала; без кнопок подтверждения)
#  Источники: CoinGecko, Binance/Bybit (публичные данные),
#  alternative.me, DefiLlama, GoPlus, Stooq
# ============================================================
import os, re, io, time, sqlite3, statistics, asyncio, datetime, requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton,
                           InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile)
# ====== ПАТЧ СОВМЕСТИМОСТИ (не удалять!) ======
def _allow_positional(cls, *field_names):
    _orig = cls.__init__
    def _init(self, *args, **kwargs):
        kwargs.update(dict(zip(field_names, args)))
        return _orig(self, **kwargs)
    cls.__init__ = _init

_allow_positional(KeyboardButton, "text")
_allow_positional(InlineKeyboardButton, "text")
_allow_positional(ReplyKeyboardMarkup, "keyboard")
_allow_positional(InlineKeyboardMarkup, "inline_keyboard")
# ====== КОНЕЦ ПАТЧА ======

from config import *

from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
bot = Bot(TG_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

menu = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton("🔎 СКАН"), KeyboardButton("⭐ Мои монеты")],
    [KeyboardButton("📊 Статистика"), KeyboardButton("⚙️ Настройки")],
    [KeyboardButton("ℹ️ О боте")]], resize_keyboard=True)

_heavy = {}; HEAVY_CD = 30          # лимит тяжёлых операций: не чаще раза в 30 сек
_sub_cache = {}                     # подписки: user_id -> время последней подтверждённой подписки

# ============================================================
#  1. БАЗА ДАННЫХ
# ============================================================
def _db():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    c.executescript("""
    CREATE TABLE IF NOT EXISTS settings(user_id INTEGER PRIMARY KEY,
        balance REAL DEFAULT 10000, risk_pct REAL DEFAULT 1.0, style TEXT DEFAULT 'свинг');
    CREATE TABLE IF NOT EXISTS watch(user_id INTEGER, symbol TEXT,
        alert_pct REAL DEFAULT 5.0, funding_on INTEGER DEFAULT 1,
        PRIMARY KEY(user_id, symbol));
    CREATE TABLE IF NOT EXISTS alerts_log(user_id INTEGER, symbol TEXT,
        rule TEXT, ts INTEGER, PRIMARY KEY(user_id, symbol, rule));
    CREATE TABLE IF NOT EXISTS candidates(id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, symbol TEXT, ts INTEGER, price REAL, ret REAL, reviewed INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS reminders(id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, symbol TEXT, due_ts INTEGER, note TEXT, sent INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS pulse_history(symbol TEXT, day TEXT, pct INTEGER,
        PRIMARY KEY(symbol, day));""")
    return c

def q(sql, *args, one=False):
    c = _db()
    r = (c.execute(sql, args).fetchone() if one else c.execute(sql, args).fetchall())
    c.commit(); c.close(); return r

def meta_get(k, d=None):
    r = q("SELECT value FROM meta WHERE key=?", k, one=True); return r["value"] if r else d
def meta_set(k, v): q("INSERT OR REPLACE INTO meta VALUES(?,?)", k, str(v))

_KL = str.maketrans("АВСЕНКМОРТУХ", "ABCEHKMOPTYX")

def safe_sym(raw: str) -> str:
    """Санитайзер тикера: латиница/цифры, 1-12 символов, авто-перевод русской раскладки."""
    s = raw.strip().upper().replace("USDT", "")
    s = s.translate(_KL)                      # ВТС -> BTC, СОР -> SOP и т.п.
    return s if s.isascii() and s.isalnum() and 1 <= len(s) <= 12 else ""

def heavy_ok(uid, kind):
    now = time.time()
    if now - _heavy.get((uid, kind), 0) < HEAVY_CD: return False
    _heavy[(uid, kind)] = now; return True

# ============================================================
#  2. ИСТОЧНИКИ ДАННЫХ (Binance с авто-переходом на Bybit
#     для регионов с гео-блоком; все данные публичные,
#     ключи бирж НЕ используются — и никогда не будут)
# ============================================================
_cg    = {"ts": 0, "rows": []}
_fund  = {"ts": 0, "d": {}}
_fng   = {"ts": 0, "v": None}
_glob  = {"ts": 0, "v": None}
_llama = {"ts": 0, "d": {}}
_src   = {"binance": None, "checked": 0}

def binance_alive(force=False):
    if force or _src["binance"] is None or time.time() - _src["checked"] > 600:
        try:
            requests.get("https://api.binance.com/api/v3/ping", timeout=5)
            _src["binance"] = True
        except Exception:
            _src["binance"] = False
        _src["checked"] = time.time()
    return _src["binance"]

def market(max_age=600):
    if time.time() - _cg["ts"] > max_age:
        try:
            _cg["rows"] = requests.get("https://api.coingecko.com/api/v3/coins/markets", params={
                "vs_currency": "usd", "order": "market_cap_desc", "per_page": 100,
                "page": 1, "price_change_percentage": "24h,7d"}, timeout=20).json()
            _cg["ts"] = time.time()
        except Exception: pass
    return _cg["rows"]

def find_coin(sym):
    for m in market():
        if m["symbol"].upper() == sym: return m
    return None

def klines(sym, limit=100):
    """Дневные свечи, старые первыми: (open, high, low, close, volume)."""
    if binance_alive():
        try:
            r = requests.get("https://api.binance.com/api/v3/klines",
                params={"symbol": sym + "USDT", "interval": "1d", "limit": limit}, timeout=10).json()
            if isinstance(r, list) and r:
                return [(float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])) for x in r]
        except Exception: pass
    try:  # Bybit отдаёт свечи новыми первыми — разворачиваем
        r = requests.get("https://api.bybit.com/v5/market/kline",
            params={"category": "spot", "symbol": sym + "USDT", "interval": "D", "limit": limit}, timeout=10).json()
        lst = (r.get("result") or {}).get("list") or []
        return list(reversed([(float(b[1]), float(b[2]), float(b[3]), float(b[4]), float(b[5])) for b in lst]))
    except Exception:
        return []

def ticker24(sym):
    if binance_alive():
        try:
            return requests.get("https://api.binance.com/api/v3/ticker/24hr",
                                params={"symbol": sym + "USDT"}, timeout=10).json()
        except Exception: pass
    try:
        r = requests.get("https://api.bybit.com/v5/market/tickers",
                         params={"category": "spot", "symbol": sym + "USDT"}, timeout=10).json()
        t = (r.get("result") or {}).get("list", [{}])[0]
        return {"lastPrice": t.get("lastPrice"),
                "priceChangePercent": float(t.get("price24hPcnt") or 0) * 100,
                "quoteVolume": t.get("turnover24h")}
    except Exception:
        return {}

def funding_map():
    if time.time() - _fund["ts"] > 600:
        d = {}
        if binance_alive():
            try:
                r = requests.get("https://fapi.binance.com/fapi/v1/premiumIndex", timeout=10).json()
                d = {x["symbol"]: float(x.get("lastFundingRate") or 0) for x in r}
            except Exception: d = {}
        if not d:
            try:
                r = requests.get("https://api.bybit.com/v5/market/tickers",
                                 params={"category": "linear"}, timeout=15).json()
                d = {t["symbol"]: float(t.get("fundingRate") or 0)
                     for t in (r.get("result") or {}).get("list", [])}
            except Exception: d = {}
        _fund["d"], _fund["ts"] = d, time.time()
    return _fund["d"]

def oi_info(sym):
    """Open Interest: объём открытых позиций и изменение за 7 дней."""
    if binance_alive():
        try:
            r = requests.get("https://fapi.binance.com/futures/data/openInterestHist",
                params={"symbol": sym + "USDT", "period": "1d", "limit": 8}, timeout=10).json()
            if isinstance(r, list) and len(r) >= 2:
                now, ago = float(r[-1]["sumOpenInterest"]), float(r[0]["sumOpenInterest"])
                return {"chg7d": (now / ago - 1) * 100 if ago else 0,
                        "value_usd": float(r[-1]["sumOpenInterestValue"])}
        except Exception: pass
    try:
        r = requests.get("https://api.bybit.com/v5/market/open-interest",
            params={"category": "linear", "symbol": sym + "USDT", "intervalTime": "D", "limit": 8}, timeout=10).json()
        lst = (r.get("result") or {}).get("list") or []
        if len(lst) >= 2:
            now_o, ago_o = float(lst[0]["openInterest"]), float(lst[-1]["openInterest"])
            m = find_coin(sym); px = float(m["current_price"]) if m else 0
            return {"chg7d": (now_o / ago_o - 1) * 100 if ago_o else 0, "value_usd": now_o * px}
    except Exception: pass
    return None

def longshort(sym):
    """Доля лонг/шорт аккаунтов (розница)."""
    if binance_alive():
        try:
            r = requests.get("https://fapi.binance.com/futures/data/globalLongShortAccountRatio",
                params={"symbol": sym + "USDT", "period": "1d", "limit": 1}, timeout=10).json()
            d = r[0]; return {"ratio": float(d["longShortRatio"]), "long_pct": float(d["longAccount"]) * 100}
        except Exception: pass
    try:
        r = requests.get("https://api.bybit.com/v5/market/account-ratio",
            params={"category": "linear", "symbol": sym + "USDT", "period": "1d", "limit": 1}, timeout=10).json()
        d = (r.get("result") or {}).get("list", [{}])[0]
        b, s = float(d.get("buyRatio") or 0), float(d.get("sellRatio") or 0)
        if b + s: return {"ratio": b / s, "long_pct": b * 100}
    except Exception: pass
    return None

def taker(sym):
    """Агрессия тейкеров buy/sell за сутки. Есть только на Binance."""
    if binance_alive():
        try:
            r = requests.get("https://fapi.binance.com/futures/data/takerlongshortRatio",
                params={"symbol": sym + "USDT", "period": "1d", "limit": 1}, timeout=10).json()
            return float(r[0]["buySellRatio"])
        except Exception: pass
    return None

def basis(sym):
    """Премия фьючерса к споту, %."""
    m = find_coin(sym)
    if not m: return None
    spot = float(m["current_price"])
    if binance_alive():
        try:
            r = requests.get("https://fapi.binance.com/fapi/v1/premiumIndex",
                             params={"symbol": sym + "USDT"}, timeout=10).json()
            return (float(r["markPrice"]) / spot - 1) * 100
        except Exception: pass
    try:
        r = requests.get("https://api.bybit.com/v5/market/tickers",
                         params={"category": "linear", "symbol": sym + "USDT"}, timeout=10).json()
        t = (r.get("result") or {}).get("list", [{}])[0]
        return (float(t["markPrice"]) / spot - 1) * 100
    except Exception:
        return None

def vol_split(sym):
    """Доля фьючерсного объёма от общего (оценка)."""
    s = float((ticker24(sym).get("quoteVolume")) or 0); f = 0.0
    if binance_alive():
        try:
            f = float(requests.get("https://fapi.binance.com/fapi/v1/ticker/24hr",
                params={"symbol": sym + "USDT"}, timeout=10).json().get("quoteVolume") or 0)
        except Exception: f = 0.0
    if not f:
        try:
            r = requests.get("https://api.bybit.com/v5/market/tickers",
                             params={"category": "linear", "symbol": sym + "USDT"}, timeout=10).json()
            f = float((r.get("result") or {}).get("list", [{}])[0].get("turnover24h") or 0)
        except Exception: f = 0.0
    return {"perp_share": f / (s + f) * 100} if s + f else None

def fear_greed():
    if time.time() - _fng["ts"] > 3600:
        try:
            d = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10).json()["data"][0]
            _fng["v"] = (int(d["value"]), d["value_classification"])
        except Exception: _fng["v"] = None
        _fng["ts"] = time.time()
    return _fng["v"]

def global_ctx():
    if time.time() - _glob["ts"] > 3600:
        try:
            g = requests.get("https://api.coingecko.com/api/v3/global", timeout=10).json()["data"]
            _glob["v"] = {"mcap_chg": g["market_cap_change_percentage_24h_usd"],
                          "btc_dom": g["market_cap_percentage"]["btc"]}
        except Exception: _glob["v"] = None
        _glob["ts"] = time.time()
    return _glob["v"]

def llama_protocols():
    if time.time() - _llama["ts"] > 3600:
        try:
            r = requests.get("https://api.llama.fi/protocols", timeout=20).json()
            _llama["d"] = {p["symbol"].upper(): p for p in r if p.get("symbol")}
            _llama["ts"] = time.time()
        except Exception: pass
    return _llama["d"]

def tvl_info(sym):
    p = llama_protocols().get(sym)
    if not p: return None
    return {"tvl": p.get("tvl") or 0, "chg7d": p.get("change_7d")}

def goplus_check(sym):
    """Скам-чек ERC-20 через GoPlus."""
    try:
        m = find_coin(sym)
        if not m: return None
        pl = requests.get(f"https://api.coingecko.com/api/v3/coins/{m['id']}", timeout=15).json().get("platforms", {})
        addr = pl.get("ethereum")
        if not addr: return {"skip": "ERC-20 контракта нет — скам-чек неприменим (нативная монета)."}
        d = requests.get("https://api.gopluslabs.com/api/v1/token_security/1",
                         params={"contract_addresses": addr}, timeout=15).json()
        t = (d.get("result") or {}).get(addr.lower())
        if not t: return {"skip": "GoPlus не имеет данных по контракту — проверь вручную."}
        flags = []
        for k, txt in (("is_honeypot", "HONEYPOT — продать невозможно!"),
                       ("is_open_source", "код не открыт"),
                       ("is_blacklisted", "чёрный список адресов"),
                       ("is_mintable", "эмитент может ДОПЕЧАТАТЬ токены")):
            if str(t.get(k)) == "1": flags.append(txt)
        if float(t.get("owner_percent") or 0) > 10:
            flags.append(f"у владельца {float(t['owner_percent']):.0f}% токенов")
        if float(t.get("lp_holder_percent") or 0) > 30:
            flags.append("ликвидность в одних руках")
        return {"addr": addr, "flags": flags, "holders": t.get("holder_count"),
                "buy_tax": float(t.get("buy_tax") or 0) * 100,
                "sell_tax": float(t.get("sell_tax") or 0) * 100}
    except Exception:
        return None

def macro_line():
    """Золото и EURUSD — макро-фон. Источник: Stooq (бесплатно)."""
    out = []
    for name, s in (("XAUUSD", "xauusd"), ("EURUSD", "eurusd")):
        try:
            row = requests.get(f"https://stooq.com/q/l/?s={s}&f=sd2t2ohlcv&h&e=csv",
                               timeout=10).text.splitlines()[1].split(",")
            px, op = float(row[6]), float(row[2])
            out.append(f"{name} {px:,.4g} ({(px / op - 1) * 100:+.1f}% к открытию)")
        except Exception: pass
    return " · ".join(out)

def stable_flows():
    """Приток/отток стейблов USDT+USDC за 7 дней (по данным, накопленным ботом)."""
    rows = market()
    sm = sum((m.get("market_cap") or 0) for m in rows if m["symbol"].upper() in {"USDT", "USDC"})
    today = time.strftime("%Y%m%d", time.gmtime())
    if not meta_get("sm_" + today): meta_set("sm_" + today, sm)
    for d in range(6, 9):
        past = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=d)).strftime("%Y%m%d")
        v = meta_get("sm_" + past)
        if v: return {"now": sm, "chg7d": (sm / float(v) - 1) * 100}
    return {"now": sm, "chg7d": None}

# ============================================================
#  3. ИНДИКАТОРЫ
# ============================================================
def ema_series(v, p):
    if len(v) < p: return []
    k = 2 / (p + 1); e = [sum(v[:p]) / p]
    for x in v[p:]: e.append(x * k + e[-1] * (1 - k))
    return e

def rsi(c, p=14):
    if len(c) < p + 1: return None
    g = sum(max(c[i] - c[i-1], 0) for i in range(1, p + 1)) / p
    l = sum(max(c[i-1] - c[i], 0) for i in range(1, p + 1)) / p
    for i in range(p + 1, len(c)):
        d = c[i] - c[i-1]; g = (g * (p - 1) + max(d, 0)) / p; l = (l * (p - 1) + max(-d, 0)) / p
    return 100.0 if l == 0 else 100 - 100 / (1 + g / l)

def atr(bars, p=14):
    if len(bars) < p + 1: return None
    trs = []
    for i in range(1, len(bars)):
        h, l, c = bars[i][1], bars[i][2], bars[i][3]; pc = bars[i-1][3]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-p:]) / p

def macd(c):
    if len(c) < 35: return None
    e12, e26 = ema_series(c, 12), ema_series(c, 26)
    line = [a - b for a, b in zip(e12[len(e12) - len(e26):], e26)]
    sig = ema_series(line, 9)
    if len(sig) < 2: return None
    return line[-1], sig[-1], line[-1] - sig[-1], line[-2] - sig[-2]

def adx(h, l, c, p=14):
    n = len(c)
    if n < 2 * p + 1: return None
    tr = [h[0] - l[0]]; pdm = [0.0]; ndm = [0.0]
    for i in range(1, n):
        up, dn = h[i] - h[i-1], l[i-1] - l[i]
        pdm.append(up if up > dn and up > 0 else 0.0)
        ndm.append(dn if dn > up and dn > 0 else 0.0)
        tr.append(max(h[i] - l[i], abs(h[i] - c[i-1]), abs(l[i] - c[i-1])))
    a, ap, an = sum(tr[1:p+1]), sum(pdm[1:p+1]), sum(ndm[1:p+1]); dxs = []
    for i in range(p + 1, n):
        a += tr[i] - a / p; ap += pdm[i] - ap / p; an += ndm[i] - an / p
        pdi, ndi = (100 * ap / a if a else 0), (100 * an / a if a else 0)
        dxs.append(100 * abs(pdi - ndi) / (pdi + ndi) if pdi + ndi else 0)
    return sum(dxs[-p:]) / p

def mfi(bars, p=14):
    if len(bars) < p + 1: return None
    tp = [(b[1] + b[2] + b[3]) / 3 for b in bars]; pos = neg = 0.0
    for i in range(len(bars) - p, len(bars)):
        mf = tp[i] * bars[i][4]
        if tp[i] > tp[i-1]: pos += mf
        elif tp[i] < tp[i-1]: neg += mf
    return 100.0 if neg == 0 else 100 - 100 / (1 + pos / neg)

def bb_squeeze(c, p=20, k=2):
    if len(c) < p + 20: return None
    bws = []
    for i in range(p, len(c) + 1):
        w = c[i-p:i]; m = sum(w) / p
        if m: bws.append(2 * k * statistics.pstdev(w) / m)
    cur = bws[-1]; return sum(1 for b in bws if b < cur) / len(bws)

# ============================================================
#  4. СКАН v2 (базовые правила + техника + потоки)
# ============================================================
def scan_v2():
    rows = [m for m in market() if m["symbol"].upper() not in EXCLUDE]
    to = [m["total_volume"] / m["market_cap"] for m in rows if m.get("market_cap") and m.get("total_volume")]
    med = statistics.median(to) if to else 0
    fg, glob = fear_greed(), global_ctx()
    greed = bool(fg and fg[0] >= 75); fear = bool(fg and fg[0] <= 25)
    mkt7 = statistics.mean([m.get("price_change_percentage_7d_in_currency") or 0 for m in rows]) if rows else 0
    overheat = 20 if greed else 30
    fm = funding_map()
    stage = []
    for m in rows:
        sym, why, ag, s = m["symbol"].upper(), [], [], 0
        vol, cap = m.get("total_volume") or 0, m.get("market_cap") or 0
        c24 = m.get("price_change_percentage_24h_in_currency") or 0.0
        c7d = m.get("price_change_percentage_7d_in_currency") or 0.0
        ath = m.get("ath_change_percentage") or 0.0
        if med and cap and vol / cap >= 1.5 * med: s += 1; why.append(f"оборачиваемость {vol/cap:.0%} ≥ 1,5× медианы")
        if abs(c24) >= 4: s += 1; why.append(f"импульс 24ч {c24:+.1f}%")
        if c7d and c24 and abs(c24) >= abs(c7d): s += 1; why.append("движение свежее (24ч ≥ 7д)")
        if -70 <= ath <= -40: s += 1; why.append(f"{ath:+.0f}% от ATH")
        if fear and c7d > mkt7: s += 1; why.append("относительная сила на фоне страха рынка")
        if abs(c7d) >= overheat: ag.append(f"перегрев недели {c7d:+.0f}%")
        if cap and vol / cap < 0.02: ag.append("низкая ликвидность")
        fr = fm.get(sym + "USDT")
        if fr is not None and fr >= FUND_HOT: ag.append(f"funding {fr*100:+.3f}%/8ч — перегрев лонгов")
        if s >= 2: stage.append((s, why, ag, m))
    stage.sort(key=lambda x: -x[0])
    out = []
    for s0, why, ag, m in stage[:12]:
        sym = m["symbol"].upper(); bars = klines(sym); tech = 0
        if bars:
            h = [b[1] for b in bars]; l = [b[2] for b in bars]; c = [b[3] for b in bars]
            a = adx(h, l, c); md = macd(c); mf = mfi(bars); sq = bb_squeeze(c); e50 = ema_series(c, 50)
            if a is not None and a >= 25: tech += 1; why.append(f"ADX {a:.0f} — тренд сильный")
            elif a is not None and a < 18: ag.append(f"ADX {a:.0f} — боковик, пробои ложные")
            if md and md[0] > md[1] and md[2] > md[3]: tech += 1; why.append("MACD вверх, гистограмма растёт")
            if mf is not None:
                if 40 <= mf <= 75: tech += 1; why.append(f"MFI {mf:.0f} — объём подтверждает")
                elif mf > 80: ag.append(f"MFI {mf:.0f} — перегрето по объёму")
            if sq is not None and sq < 0.3 and e50 and c[-1] > e50[-1]:
                tech += 1; why.append(f"сжатие BB (ранг {sq:.0%}) над EMA50 — сетап на пробой")
        oi = oi_info(sym); ls = longshort(sym)
        if oi and s0 >= 3 and oi["chg7d"] > 20:
            ag.append(f"OI +{oi['chg7d']:.0f}% за неделю — плечо уже в цене, вход на пике входов")
        if ls and ls["ratio"] >= 3.5:
            ag.append(f"розница {ls['ratio']:.1f}:1 в лонг — толпа с одной стороны")
        out.append((s0 + tech, m, why, ag))
    out.sort(key=lambda x: -x[0])
    return out[:5], (fg, glob)

def scan_text():
    cands, (fg, glob) = scan_v2()
    t = time.strftime("%d.%m %H:%M UTC", time.gmtime())
    head = [f"🔎 <b>СКАН · {t}</b>"]
    if fg:
        zone = "СТРАХ" if fg[0] <= 25 else ("ЖАДНОСТЬ" if fg[0] >= 75 else "нейтрально")
        head.append(f"Страх и жадность: <b>{fg[0]}</b> ({zone}) · alternative.me")
        if fg[0] <= 25: head.append("Режим страха: риск на сделку режь вдвое, ищем относительную силу.")
        if fg[0] >= 75: head.append("Режим жадности: порог перегрева понижен, аккуратнее с альтами.")
    if glob:
        head.append(f"Капитализация {glob['mcap_chg']:+.1f}%/24ч · доминация BTC {glob['btc_dom']:.1f}%"
                    + (" ⚠ альты под давлением" if glob["btc_dom"] > 59 else ""))
    if not cands:
        return "\n".join(head) + "\nКандидатов нет: рынок спокоен по текущим правилам." + DISCLAIMER, None
    head.append("Кандидаты на изучение (не сигналы к покупке):")
    L = ["\n".join(head), ""]
    for i, (s, m, why, ag) in enumerate(cands, 1):
        sym = m["symbol"].upper()
        L += [f"{i}. <b>{sym}</b> · ${m['current_price']:,.6g} · балл {s}"] \
           + [f"   • {w}" for w in why] + [f"   ⚠ {a}" for a in ag] + [""]
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(f"🔍 {cands[0][1]['symbol'].upper()}", callback_data=f"anl:{cands[0][1]['symbol'].upper()}"),
        InlineKeyboardButton(f"⭐ {cands[0][1]['symbol'].upper()}", callback_data=f"wch:{cands[0][1]['symbol'].upper()}")]])
    L += [f"Первый на изучение: <b>{cands[0][1]['symbol'].upper()}</b>",
          f"Источники: CoinGecko, {'Binance' if binance_alive() else 'Bybit'}, alternative.me · {t}"]
    for _, m, _, _ in cands:   # журнал правил: пишем кандидатов для автосверки через 7 дней
        q("INSERT INTO candidates(user_id, symbol, ts, price) VALUES(?,?,?,?)",
          0, m["symbol"].upper(), int(time.time()), m["current_price"])
    return "\n".join(L) + DISCLAIMER, kb

# ============================================================
#  5. КАРТОЧКА МОНЕТЫ
# ============================================================
def coin_card(sym):
    m = find_coin(sym)
    if not m: return None, None
    sym = m["symbol"].upper()
    t = time.strftime("%d.%m %H:%M UTC", time.gmtime())
    fr = funding_map().get(sym + "USDT"); bars = klines(sym)
    trend, r14, a14, rng = "НЕ ПРЕДОСТАВЛЕНО", "—", "—", "НЕ ПРЕДОСТАВЛЕНО"
    if bars:
        c = [b[3] for b in bars]; e20, e50 = ema_series(c, 20), ema_series(c, 50)
        if e20 and e50: trend = "вверх (EMA20>EMA50)" if e20[-1] > e50[-1] else "вниз (EMA20<EMA50)"
        if (r := rsi(c)) is not None: r14 = f"{r:.0f}"
        if (a := atr(bars)) is not None: a14 = f"${a:,.4g} (1,5×ATR = ${a*1.5:,.4g})"
        rng = f"${min(b[2] for b in bars[-30:]):,.4g} — ${max(b[1] for b in bars[-30:]):,.4g}"
    tvl = tvl_info(sym)
    L = [f"🔍 <b>{sym}</b> · ${m['current_price']:,.6g} · {t}",
         f"Δ24ч {(m.get('price_change_percentage_24h_in_currency') or 0):+.1f}% · "
         f"Δ7д {(m.get('price_change_percentage_7d_in_currency') or 0):+.1f}% · "
         f"{(m.get('ath_change_percentage') or 0):+.0f}% от ATH",
         f"Объём/капа: {(m.get('total_volume') or 0)/(m.get('market_cap') or 1):.1%} · "
         f"Funding: {'—' if fr is None else f'{fr*100:+.3f}%/8ч'}",
         f"Тренд: {trend} · RSI: {r14}",
         f"ATR(14): {a14} — ориентир расстояния стопа",
         f"Диапазон 30д: {rng}"]
    if tvl and tvl["tvl"]:
        L.append(f"TVL (DefiLlama): ${tvl['tvl']/1e6:,.0f}M · 7д {tvl['chg7d']:+.1f}%")
    _mm = None
    if bars:
        _lc = bars[-1][3]; _cg = float(m.get("current_price") or 0)
        if _cg and _lc and abs(_cg / _lc - 1) > 0.10:
            _mm = (_cg, _lc)
    if _mm:
        L.insert(1, f"🚨 ТИКЕР-КОЛЛИЗИЯ: цена CoinGecko (${_mm[0]:,.6g}) и свечи биржи (${_mm[1]:,.6g}) расходятся >10%. Тикер {sym} на разных площадках — разные монеты, часть показателей может относиться к другой. Проверь вручную!")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("📈 График", callback_data=f"chart:{sym}"),
         InlineKeyboardButton("🎯 Pulse", callback_data=f"pls:{sym}"),
         InlineKeyboardButton("🌊 Потоки", callback_data=f"flw:{sym}")],
        [InlineKeyboardButton("⭐ В мои", callback_data=f"wch:{sym}"),
         InlineKeyboardButton("🔔 ±5%", callback_data=f"alr:{sym}"),
         InlineKeyboardButton("🛡 Раг-чек", callback_data=f"rug:{sym}")],
        [InlineKeyboardButton("🌐 TradingView", url=f"https://www.tradingview.com/chart/?symbol=BINANCE%3A{sym}USDT"),
         InlineKeyboardButton("🧭 Разбор", callback_data=f"full:{sym}")]])
    return "\n".join(L) + DISCLAIMER, kb

# ============================================================
#  6. ПОТОКИ И ДЕРИВАТИВЫ (/flows)
# ============================================================
def flows_block(sym):
    m = find_coin(sym)
    if not m: return None
    oi, ls, tk, bs, vs = oi_info(sym), longshort(sym), taker(sym), basis(sym), vol_split(sym)
    fr = funding_map().get(sym + "USDT")
    c24 = m.get("price_change_percentage_24h_in_currency") or 0
    L = [f"🌊 <b>ПОТОКИ · {sym}</b> · {time.strftime('%d.%m %H:%M UTC', time.gmtime())}"]
    if oi: L.append(f"Open Interest: ${oi['value_usd']/1e6:,.0f}M · за 7д {oi['chg7d']:+.1f}%")
    if fr is not None: L.append(f"Funding: {fr*100:+.3f}%/8ч")
    if bs is not None: L.append(f"Базис (премия фьюча): {bs:+.3f}%")
    if vs: L.append(f"Доля фьючерсного объёма: {vs['perp_share']:.0f}% (спот {100-vs['perp_share']:.0f}%)")
    if ls: L.append(f"Аккаунты: лонгов {ls['long_pct']:.0f}% (соотношение {ls['ratio']:.2f})")
    L.append(f"Тейкеры buy/sell: {f'{tk:.2f}' if tk is not None else 'НЕ ПРЕДОСТАВЛЕНО (нет в данных Bybit)'}")
    notes = []
    if oi and c24 < -3 and oi["chg7d"] < -10:
        notes.append("цена и OI падают вместе — похоже на каскад ликвидаций лонгов (прокси)")
    if oi and c24 > 3 and oi["chg7d"] < -10:
        notes.append("цена вверх, OI вниз — похоже на шорт-сквиз (прокси)")
    if oi and c24 > 3 and oi["chg7d"] > 15:
        notes.append("цена и OI растут вместе — в рынок заходит плечо, рост хрупкий к откату")
    if ls and ls["ratio"] >= 3:
        notes.append(f"розница перекошена в лонг {ls['ratio']:.1f}:1 — контрарианский флаг осторожности")
    if ls and ls["ratio"] <= 1:
        notes.append("розница перекошена в шорт — контрарианский флаг осторожности")
    if tk is not None and tk >= 1.5: notes.append("агрессивные покупки тейкерами (buy/sell ≥ 1,5)")
    if tk is not None and tk <= 0.7: notes.append("агрессивные продажи тейкерами (buy/sell ≤ 0,7)")
    if bs is not None and bs >= 0.15: notes.append("базис перегрет — фьючерс заметно дороже спота")
    L.append("")
    L += [("⚠ " + n) for n in notes] if notes else ["Заметных перекосов в потоках нет."]
    L.append("Источник: Binance/Bybit public data. Ликвидации — прокси по OI, точных карт нет.")
    return "\n".join(L) + DISCLAIMER

# ============================================================
#  7. MARKET PULSE (/pulse) — агрегированный вердикт
# ============================================================
def pulse(sym):
    bars = klines(sym)
    if len(bars) < 60: return None
    h = [b[1] for b in bars]; l = [b[2] for b in bars]; c = [b[3] for b in bars]
    comp = []
    md = macd(c)
    if md:
        if md[0] > md[1] and md[2] > md[3]: comp.append(("🟢", "MACD: линия выше сигнальной, гистограмма растёт", 1))
        elif md[0] < md[1] and md[2] < md[3]: comp.append(("🔴", "MACD: линия ниже сигнальной, гистограмма падает", -1))
        else: comp.append(("🟡", "MACD: без направления", 0))
    e20, e50 = ema_series(c, 20), ema_series(c, 50); a = adx(h, l, c)
    if e20 and e50 and a is not None:
        if a >= 25 and e20[-1] > e50[-1]: comp.append(("🟢", f"ADX {a:.0f}: тренд сильный, направлен вверх", 1))
        elif a >= 25 and e20[-1] < e50[-1]: comp.append(("🔴", f"ADX {a:.0f}: тренд сильный, направлен вниз", -1))
        else: comp.append(("🟡", f"ADX {a:.0f}: тренда нет (боковик), пробои часто ложные", 0))
    r = rsi(c)
    if r is not None:
        if r >= 70: comp.append(("🟡", f"RSI {r:.0f}: импульс есть, но фаза поздняя", 0))
        elif r >= 50: comp.append(("🟢", f"RSI {r:.0f}: моментум вверх", 1))
        elif r >= 30: comp.append(("🔴", f"RSI {r:.0f}: моментум вниз", -1))
        else: comp.append(("🟡", f"RSI {r:.0f}: перепродан, ждём подтверждения", 0))
    mf = mfi(bars)
    if mf is not None:
        if mf > 80: comp.append(("🟡", f"MFI {mf:.0f}: объёмный перегрев", 0))
        elif mf >= 40: comp.append(("🟢", f"MFI {mf:.0f}: покупки подтверждены объёмом", 1))
        elif mf >= 25: comp.append(("🔴", f"MFI {mf:.0f}: объём на стороне продавцов", -1))
        else: comp.append(("🟡", f"MFI {mf:.0f}: объём иссяк", 0))
    price = c[-1]; mid = sum(c[-20:]) / 20; sq = bb_squeeze(c)
    if sq is not None and sq < 0.3:
        comp.append(("🟡", f"BB: сжатие (ранг {sq:.0%}) — волатильность копится, направления нет", 0))
    elif price > mid: comp.append(("🟢", "BB: цена выше середины полос — структура поддерживает лонг", 1))
    else: comp.append(("🔴", "BB: цена ниже середины полос — структура давит", -1))
    fr = funding_map().get(sym + "USDT")
    if fr is not None:
        if fr >= FUND_HOT: comp.append(("🔴", f"Funding {fr*100:+.3f}%/8ч: лонги перегружены", -1))
        elif fr <= FUND_COLD: comp.append(("🟢", f"Funding {fr*100:+.3f}%/8ч: шорты перегружены — топливо для сквиза", 1))
        else: comp.append(("🟡", "Funding нейтрален", 0))
    n = len(comp); score = sum(x[2] for x in comp)
    pct = round((score + n) / (2 * n) * 100)
    return comp, pct, price, bars

def pulse_text(sym_raw):
    sym = safe_sym(sym_raw)
    if not sym: return "Некорректный тикер."
    r = pulse(sym)
    if not r: return f"🎯 {sym}: данных мало (нет спот-пары или истории <60 дней)."
    comp, pct, price, bars = r
    if pct >= 65: verdict = "🟩 <b>ЛОНГ-КОНТЕКСТ ПОДТВЕРЖДЁН</b> — изучай по 5 шагам, проверь стоп и R:R"
    elif pct <= 35: verdict = "🟥 <b>ЛОНГ-КОНТЕКСТ НЕ ПОДТВЕРЖДЁН</b> — покупки методологией не поддерживаются. Это не запрет: решение твоё"
    else: verdict = "🟨 <b>НЕЙТРАЛЬНО</b> — контекст не определён, лучше ждать ясности"
    day = time.strftime("%Y-%m-%d", time.gmtime())
    if not q("SELECT 1 FROM pulse_history WHERE symbol=? AND day=?", sym, day, one=True):
        q("INSERT INTO pulse_history VALUES(?,?,?)", sym, day, pct)
    hist = q("SELECT pct FROM pulse_history WHERE symbol=? ORDER BY day DESC LIMIT 14", sym)
    strip = "".join("🟩" if x["pct"] >= 65 else ("🟥" if x["pct"] <= 35 else "🟨") for x in reversed(hist))
    t_hi, t_lo = bars[-1][1], bars[-1][2]; y_hi, y_lo = bars[-2][1], bars[-2][2]
    L = [f"🎯 <b>MARKET PULSE · {sym}</b> · ${price:,.6g} · {time.strftime('%d.%m %H:%M UTC', time.gmtime())}",
         f"Скор лонг-контекста: <b>{pct}%</b> из 100",
         verdict, "", "Компоненты (каждый можно пересчитать руками):"]
    L += [f"  {e} {txt}" for e, txt, _ in comp]
    L += ["", f"HI/LO сегодня: ${t_hi:,.6g} / ${t_lo:,.6g}",
          f"HI/LO вчера: ${y_hi:,.6g} / ${y_lo:,.6g}",
          "", f"История вердиктов (14 дней, фиксируется раз в день, не перерисовывается): {strip}"]
    return "\n".join(L) + DISCLAIMER

# ============================================================
#  8. ГРАФИК (PNG)
# ============================================================
def chart_png(sym):
    bars = klines(sym, 90)
    if len(bars) < 25: return None
    o = [b[0] for b in bars]; h = [b[1] for b in bars]; l = [b[2] for b in bars]; c = [b[3] for b in bars]
    fig, ax = plt.subplots(figsize=(9, 4.5), dpi=110)
    for i in range(len(bars)):
        col = "#2e7d32" if c[i] >= o[i] else "#c62828"
        ax.plot([i, i], [l[i], h[i]], color=col, lw=0.8)
        ax.add_patch(plt.Rectangle((i - 0.35, min(o[i], c[i])), 0.7, abs(c[i] - o[i]) + 1e-12,
                                   facecolor=col, edgecolor=col))
    for series, p, col in ((ema_series(c, 20), 20, "#ff9800"), (ema_series(c, 50), 50, "#3f51b5")):
        if series: ax.plot(range(len(c) - len(series), len(c)), series, lw=1.2, color=col, label=f"EMA{p}")
    ax.legend(); ax.grid(alpha=0.2)
    ax.set_title(f"{sym}USDT · дневной · 90д · источник: {'Binance' if binance_alive() else 'Bybit'}")
    ax.set_xticks(range(0, len(bars), 15))
    buf = io.BytesIO(); fig.tight_layout(); fig.savefig(buf, format="png"); plt.close(fig)
    buf.seek(0); return buf

# ============================================================
#  9. РАСЧЁТ / РАГ-ЧЕК / TVL / LLM-РАЗБОР
# ============================================================
def calc_text(uid, sym, entry, stop, target):
    s = q("SELECT * FROM settings WHERE user_id=?", uid, one=True)
    bal, rp = (s["balance"], s["risk_pct"]) if s else (10000, 1.0)
    if stop >= entry: return "❌ Стоп должен быть НИЖЕ входа (лонг-логика). Поменяй местами."
    per = entry - stop
    risk_usd = bal * rp / 100; qty = risk_usd / per; rr = (target - entry) / per
    verdict = "✅ СТОИТ изучать дальше" if rr >= 2 else "❌ НЕ СТОИТ: R:R < 2:1 — план говорит «нет»"
    return (f"🧮 <b>РАСЧЁТ · {sym}</b>\nВход ${entry:,.6g} · Стоп ${stop:,.6g} · Цель ${target:,.6g}\n"
            f"Риск на 1 монету: ${per:,.6g}\nСчёт ${bal:,.0f} × {rp}% = <b>${risk_usd:,.2f}</b> под риском\n"
            f"Объём: <b>{qty:,.4g} {sym}</b> ≈ ${qty*entry:,.0f} ({qty*entry/bal:.0%} счёта)\n"
            f"Риск к прибыли: <b>{rr:.1f} : 1</b>\n{verdict}\n"
            f"Формула: (счёт × риск%) ÷ (вход − стоп). Решение — твоё." + DISCLAIMER)

def rug_text(sym):
    r = goplus_check(sym)
    if r is None: return f"🛡 {sym}: сервис недоступен — проверь вручную." + DISCLAIMER
    if "skip" in r: return f"🛡 <b>{sym}</b>: {r['skip']}"
    out = [f"🛡 <b>РАГ-ЧЕК · {sym}</b> (GoPlus, контракт {r['addr'][:10]}…)"]
    out.append(f"Холдеров: {r['holders'] if r['holders'] else 'НЕ ПРЕДОСТАВЛЕНО'} · "
               f"налоги: buy {r['buy_tax']:.0f}% / sell {r['sell_tax']:.0f}%")
    if r["flags"]:
        out += ["🚨 " + f for f in r["flags"]]
        out.append("ВЫВОД: есть красные флаги — в ватчлист НЕ добавляй до ручной проверки.")
    else:
        out.append("Вывод: красных флагов GoPlus не нашёл. Это НЕ гарантия — проверь вручную.")
    return "\n".join(out) + DISCLAIMER

def tvl_text(sym):
    tvl = tvl_info(sym)
    if not tvl: return (f"💧 {sym}: в DefiLlama по тикеру не найден — либо не DeFi, "
                        f"либо проверь вручную.") + DISCLAIMER
    return (f"💧 <b>TVL · {sym}</b>\nСейчас: ${tvl['tvl']/1e6:,.0f}M\n"
            f"7 дней: {tvl['chg7d']:+.1f}% (растёт TVL — деньги заходят в протокол; падает — выходят)\n"
            f"Источник: DefiLlama · {time.strftime('%d.%m %H:%M UTC', time.gmtime())}" + DISCLAIMER)

def llm_enabled(): return bool(OPENAI_API_KEY)

def full_analysis(sym):
    m = find_coin(sym)
    if not m: return "Не нашёл монету в топ-100."
    card, _ = coin_card(sym)
    tvl = tvl_info(sym)
    ctx = (card or "") + ("\nTVL: НЕ ПРЕДОСТАВЛЕНО" if not tvl else f"\nTVL: ${tvl['tvl']/1e6:,.0f}M, 7д {tvl['chg7d']:+.1f}%")
    bars = klines(sym); risk_block = "РИСК: НЕ ПРЕДОСТАВЛЕНО"
    if bars:  # РИСК-блок считает КОД, а не LLM (правило методологии)
        c = [b[3] for b in bars]; last = c[-1]
        sup = min(b[2] for b in bars[-30:]); res = max(b[1] for b in bars[-30:])
        stop = sup * 0.99
        if last > stop:
            bal, rp = 10000, 1.0
            per = last - stop; qty = (bal * rp / 100) / per; rr = (res - last) / per
            risk_block = (f"РИСК (посчитано кодом): вход ~${last:,.6g}, стоп-кандидат ${stop:,.6g} "
                          f"(под 30д-минимумом), цель-кандидат ${res:,.6g} (30д-максимум). "
                          f"Объём при счёте ${bal}×{rp}%: {qty:,.4g}. R:R {rr:.1f}:1 "
                          + ("— ОК." if rr >= 2 else "— НИЖЕ 2:1, план говорит «нет».")
                          + " Три условия отмены сформулируй; самое слабое допущение укажи.")
    prompt = (f"Ты мой ИССЛЕДОВАТЕЛЬСКИЙ ОТДЕЛ ПО КРИПТОРЫНКУ. По [{sym}] пройди шаги по порядку: "
              f"СКАН → РАЗБОР → ИЗУЧЕНИЕ → РИСК → ПЛАН.\nМОИ ДАННЫЕ (с датами и источниками):\n{ctx}\n"
              f"{risk_block}\nНОВОСТИ: НЕ ПРЕДОСТАВЛЕНО — проверь вручную.\n"
              f"СКАН: подтверди одним абзацем, стоит ли исследовать сейчас. РАЗБОР: тренд, импульс, "
              f"уровни, поведение цены — только из данных. МНЕНИЕ отдельно. ИЗУЧЕНИЕ: таблица ФАКТОВ "
              f"(показатель|значение|дата|источник) из моих данных; драйверы пометь как требующие "
              f"ручной проверки. ПЛАН: заполни шаблон (идея/направление и срок/вход/стоп/цели/объём/"
              f"R:R/отмена 3 пункта/уверенность/числа на проверку). В конце: СТАТУС: ЖДЁТ ПРОВЕРКИ ЧЕЛОВЕКОМ.\n"
              f"ПРАВИЛА: не выдумывай числа — если данных нет, пиши «НЕ ПРЕДОСТАВЛЕНО — проверь вручную». "
              f"ФАКТЫ отдельно от МНЕНИЯ. Заканчивай строкой УВЕРЕННОСТЬ: низкая/средняя/высокая. "
              f"Никаких обещаний прибыли и указаний покупать/продавать.")
    try:
        from openai import OpenAI
        cl = OpenAI(api_key=OPENAI_API_KEY, base_url="https://generativelanguage.googleapis.com/v1beta/openai/")
        ans = cl.chat.completions.create(model="gemini-2.0-flash", messages=[{"role": "user", "content": prompt}], max_tokens=1200, timeout=90)
        return (ans.choices[0].message.content.strip()[:3800]
                + "\n\n🧮 Риск-блок посчитан кодом, текст — LLM." + DISCLAIMER)
    except Exception as e:
        return f"LLM-разбор не удался ({e}). Проверь OPENAI_API_KEY в config.py или попробуй позже."
# ============================================================
#  10. ГЕЙТ ПОДПИСКИ — СТРОГИЙ РЕЖИМ
#  Вступил в канал — бот сам видит событие и открывает доступ.
#  Вышел из канала — доступ закрывается мгновенно.
#  Пользователь не нажимает никаких кнопок подтверждения.
# ============================================================
async def check_sub(uid: int, force: bool = False) -> bool:
    if uid in WHITELIST: return True
    if GATE_MODE == "off": return True
    if GATE_MODE == "whitelist": return False
    if SUB_TTL > 0 and not force:                      # кэш работает только если TTL > 0
        ts = _sub_cache.get(uid)
        if ts and time.time() - ts < SUB_TTL: return True
    try:
        m = await bot.get_chat_member(chat_id=CHANNEL, user_id=uid)
        if m.status in ("creator", "administrator", "member", "restricted"):
            _sub_cache[uid] = time.time()
            return True
        _sub_cache.pop(uid, None)
        return False                                   # отписался — доступ закрыт сразу
    except Exception as e:
        # Telegram недоступен: кто недавно проходил проверку — отсрочка 10 минут,
        # остальным fail-closed (безопаснее заблокировать, чем пустить)
        print("check_sub:", e)
        ts = _sub_cache.get(uid)
        return bool(ts and time.time() - ts < 600)

@dp.chat_member()
async def on_member_change(event):
    """Мгновенные события канала: вступил — доступ открыт, вышел — закрыт."""
    uid = event.new_chat_member.user.id
    st = event.new_chat_member.status
    if st in ("member", "administrator", "creator", "restricted"):
        _sub_cache[uid] = time.time()
        if JOIN_NOTICE:
            try:
                await bot.send_message(uid,
                    "✅ Подписка на канал подтверждена — доступ к боту открыт.\n"
                    "Жми «🔎 СКАН» или пришли тикер.", reply_markup=menu)
            except Exception:
                pass    # человек ещё не жал Start боту — покажем гейт при первом обращении
    else:
        _sub_cache.pop(uid, None)                      # вышел из канала — тут же лишаем кэша

class Gate(BaseMiddleware):
    async def __call__(self, handler, event, data):
        fu = getattr(event, "from_user", None)
        if fu is None:                                   # посты канала и системные — пропускаем
            return await handler(event, data)
        if isinstance(event, CallbackQuery) and event.data == "chksub":
            return await handler(event, data)
        ok = await check_sub(fu.id)
        if ok:
            return await handler(event, data)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton("📰 Подписаться на канал", url=CHANNEL_URL)],
            [InlineKeyboardButton("✅ Я подписался — проверить", callback_data="chksub")]])
        txt = ("🔒 Бот работает только для подписчиков канала.\n"
               "Подпишись — и доступ откроется автоматически.")
        if isinstance(event, CallbackQuery):
            await event.answer("Нужна подписка на канал", show_alert=True)
            try: await event.message.answer(txt, reply_markup=kb)
            except Exception: pass
        else:
            await event.answer(txt, reply_markup=kb, disable_web_page_preview=True)

dp.message.middleware(Gate())
dp.callback_query.middleware(Gate())

# ============================================================
#  11. ФОНОВЫЕ ЦИКЛЫ (алерты, журнал, напоминания, дайджест)
# ============================================================
def cooled(uid, sym, rule):
    r = q("SELECT ts FROM alerts_log WHERE user_id=? AND symbol=? AND rule=?", uid, sym, rule, one=True)
    if r and time.time() - r["ts"] < ALERT_COOLDOWN: return False
    q("INSERT OR REPLACE INTO alerts_log VALUES(?,?,?,?)", uid, sym, rule, int(time.time()))
    return True

def digest_text(uid):
    fg, glob = fear_greed(), global_ctx()
    L = [f"☀️ <b>ДАЙДЖЕСТ · {time.strftime('%d.%m', time.gmtime())}</b>"]
    if fg: L.append(f"Страх и жадность: <b>{fg[0]}</b> ({fg[1]})")
    if glob: L.append(f"Капа {glob['mcap_chg']:+.1f}%/24ч · BTC-доминация {glob['btc_dom']:.1f}%")
    ml = macro_line()
    if ml: L.append(f"🌍 Макро: {ml}")
    sf = stable_flows()
    if sf and sf["chg7d"] is not None:
        L.append(f"Приток стейблов (USDT+USDC): {sf['chg7d']:+.1f}% за 7д"
                 + (" — сухой порох растёт" if sf["chg7d"] > 0 else " — деньги уходят"))
    wl = q("SELECT * FROM watch WHERE user_id=?", uid)
    if wl:
        L.append("\n⭐ Ватчлист за ночь:")
        for r in wl:
            t = ticker24(r["symbol"])
            L.append(f"• {r['symbol']}: {float(t.get('priceChangePercent') or 0):+.1f}%/24ч")
    cands, _ = scan_v2()
    if cands:
        L.append("\n🔎 Свежий скан (топ-3):")
        for s, m, why, _ in cands[:3]:
            L.append(f"• {m['symbol'].upper()} — балл {s}: {why[0] if why else ''}")
    rows = q("SELECT ret FROM candidates WHERE reviewed=1")
    if rows:
        w = sum(1 for r in rows if r["ret"] >= REVIEW_WIN)
        l = sum(1 for r in rows if r["ret"] <= REVIEW_LOSS)
        L.append(f"\n📊 Журнал правил: {len(rows)} кандидатов проверено · win {w} / flat {len(rows)-w-l} / loss {l}")
    L.append("\nПолный СКАН — кнопка «🔎 СКАН». Учебное исследование, не совет.")
    return "\n".join(L), None

async def bg_loop():
    last_hour = -1; last_alert = 0
    while True:
        now = time.time()
        if now - last_alert >= 300:                      # алерты каждые 5 минут
            last_alert = now
            try:
                fm = funding_map()
                for r in q("SELECT * FROM watch"):
                    sym = r["symbol"]
                    t = ticker24(sym); ch = float(t.get("priceChangePercent") or 0)
                    if r["alert_pct"] and abs(ch) >= r["alert_pct"] and \
                       cooled(r["user_id"], sym, "move_up" if ch > 0 else "move_dn"):
                        fr = fm.get(sym + "USDT")
                        await bot.send_message(r["user_id"],
                            f"🔔 <b>{sym}USDT</b>: Δ24ч {ch:+.1f}% (порог ±{r['alert_pct']:.0f}%)\n"
                            f"Цена ${float(t.get('lastPrice') or 0):,.6g}"
                            + (f" · funding {fr*100:+.3f}%/8ч" if fr is not None else "")
                            + "\nПравило: |Δ24ч| ≥ порога. Наблюдение, не призыв покупать."
                            + f"\nИсточник: Binance/Bybit · {time.strftime('%d.%m %H:%M UTC', time.gmtime())}")
                    fr = fm.get(sym + "USDT")
                    if fr is not None and r["funding_on"] and (fr >= FUND_HOT or fr <= FUND_COLD):
                        rule = "fund_hot" if fr >= FUND_HOT else "fund_cold"
                        if cooled(r["user_id"], sym, rule):
                            await bot.send_message(r["user_id"],
                                f"🔔 {sym}: funding {fr*100:+.3f}%/8ч — "
                                f"{'лонги перегружены' if fr >= FUND_HOT else 'шорты перегружены'}. Источник: Binance/Bybit.")
            except Exception as e: print("alerts:", e)
        hour = time.gmtime().tm_hour
        if hour != last_hour:                            # раз в час
            last_hour = hour
            try:                                         # автосверка журнала правил
                for cnd in q("SELECT * FROM candidates WHERE reviewed=0 AND ts < ?",
                             int(now - REVIEW_AFTER_DAYS * 86400)):
                    m = find_coin(cnd["symbol"])
                    if not m: continue
                    ret = (m["current_price"] / cnd["price"] - 1) * 100
                    q("UPDATE candidates SET ret=?, reviewed=1 WHERE id=?", ret, cnd["id"])
            except Exception as e: print("review:", e)
            try:                                         # напоминания
                for r in q("SELECT * FROM reminders WHERE sent=0 AND due_ts < ?", int(now)):
                    q("UPDATE reminders SET sent=1 WHERE id=?", r["id"])
                    await bot.send_message(r["user_id"],
                        f"⏰ Напоминание: план по <b>{r['symbol']}</b> — ещё актуален? "
                        f"Проверь условия отмены и числа." + DISCLAIMER)
            except Exception as e: print("remind:", e)
            if hour == DIGEST_HOUR_UTC:                  # утренний дайджест
                try:
                    today = time.strftime("%Y%m%d", time.gmtime())
                    if not meta_get("digest_" + today):
                        meta_set("digest_" + today, "1")
                        for s in q("SELECT user_id FROM settings"):
                            txt, _ = digest_text(s["user_id"])
                            await bot.send_message(s["user_id"], txt, disable_web_page_preview=True)
                except Exception as e: print("digest:", e)
        await asyncio.sleep(60)

# ============================================================
#  12. ХЕНДЛЕРЫ (порядок важен — не переставляй!)
# ============================================================
class Setup(StatesGroup):
    balance = State(); risk = State()

@dp.message(CommandStart())
async def start(m: Message):
    q("INSERT OR IGNORE INTO settings(user_id) VALUES(?)", m.from_user.id)
    await m.answer("Привет! Это <b>КРИПТО-РАДАР</b> — исследователь по методологии "
                   "СКАН → РАЗБОР → ИЗУЧЕНИЕ → РИСК → ПЛАН.\n\n"
                   "Пришли тикер (SOL, BTCUSDT) или жми кнопки. Команды:\n"
                   "/calc SOL 143 138 152 — расчёт сделки\n/pulse BTC — вердикт контекста\n"
                   "/flows SOL — потоки и деривативы\n/rug SOL — скам-чек\n"
                   "/tvl SOL — TVL протокола\n/remind SOL 3 — напомнить о плане\n"
                   "/stats — статистика правил\n/digest — дайджест сейчас\n"
                   "/ping — проверка источников", reply_markup=menu)

@dp.message(Command("help"))
async def help_cmd(m: Message): await start(m)

@dp.message(Command("ping"))
async def ping_cmd(m: Message):
    ok = binance_alive(force=True)
    L = ["🩺 <b>ПИНГ ИСТОЧНИКОВ</b>",
         f"Binance: {'✅ доступен' if ok else '⛔ гео-блок → работаем через Bybit'}",
         f"Bybit (публичные данные): {'✅' if klines('BTC', 5) else '⛔'}",
         f"CoinGecko markets: {'✅ ' + str(len(market())) + ' монет' if market() else '⛔'}",
         f"CoinGecko global: {'✅' if global_ctx() else '⛔'}",
         f"Fear&Greed: {'✅ ' + str(fear_greed()[0]) if fear_greed() else '⛔'}",
         f"DefiLlama: {'✅' if llama_protocols() else '⛔'}",
         f"Гейт подписки: {GATE_MODE} · канал {CHANNEL} · режим "
         f"{'строгий (мгновенный)' if SUB_TTL == 0 else f'кэш {SUB_TTL//60} мин'}"]
    await m.answer("\n".join(L) + DISCLAIMER)

@dp.message(Command("digest"))
async def digest_cmd(m: Message):
    txt, _ = digest_text(m.from_user.id)
    await m.answer(txt, disable_web_page_preview=True)

@dp.message(Command("stats"))
async def stats_cmd(m: Message):
    rows = q("SELECT ret FROM candidates WHERE reviewed=1")
    if not rows:
        return await m.answer("Журнал пока пуст: кандидаты из СКАНа проверяются автоматически "
                              f"через {REVIEW_AFTER_DAYS} дней. Первые цифры — через неделю работы бота.")
    rets = [r["ret"] for r in rows]
    w = sum(1 for x in rets if x >= REVIEW_WIN); l = sum(1 for x in rets if x <= REVIEW_LOSS)
    await m.answer(f"📊 <b>ЖУРНАЛ ПРАВИЛ СКАНЕРА</b>\nКандидатов проверено: {len(rows)}\n"
                   f"+{REVIEW_WIN}% за {REVIEW_AFTER_DAYS}д (win): <b>{w}</b> · "
                   f"flat: {len(rows)-w-l} · {REVIEW_LOSS}% (loss): <b>{l}</b>\n"
                   f"Winrate: <b>{w/len(rows):.0%}</b> · средняя доходность кандидата: "
                   f"<b>{statistics.mean(rets):+.1f}%</b>\nМедиана: {statistics.median(rets):+.1f}%\n\n"
                   f"Это метрика твоих правил, не обещание прибыли." + DISCLAIMER)

@dp.message(Command("calc"))
async def calc_cmd(m: Message):
    mt = re.match(r"^/calc\s+(\S+)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)$", m.text.strip())
    if not mt: return await m.answer("Формат: /calc SOL 143 138 152 (тикер вход стоп цель)")
    sym = safe_sym(mt.group(1))
    if not sym: return await m.answer("Некорректный тикер.")
    try: nums = [float(x.replace(",", ".")) for x in mt.groups()[1:]]
    except ValueError: return await m.answer("Числа указаны неверно. Пример: /calc SOL 143 138 152")
    await m.answer(calc_text(m.from_user.id, sym, *nums))

@dp.message(Command("rug"))
async def rug_cmd(m: Message):
    p = m.text.split()
    if len(p) < 2: return await m.answer("Формат: /rug SOL")
    sym = safe_sym(p[1])
    if not sym: return await m.answer("Некорректный тикер.")
    await m.answer(rug_text(sym))

@dp.message(Command("tvl"))
async def tvl_cmd(m: Message):
    p = m.text.split()
    if len(p) < 2: return await m.answer("Формат: /tvl SOL")
    sym = safe_sym(p[1])
    if not sym: return await m.answer("Некорректный тикер.")
    await m.answer(tvl_text(sym))

@dp.message(Command("flows"))
async def flows_cmd(m: Message):
    p = m.text.split()
    if len(p) < 2: return await m.answer("Формат: /flows SOL")
    sym = safe_sym(p[1])
    if not sym: return await m.answer("Некорректный тикер.")
    await m.answer(flows_block(sym) or "Данных нет.")

@dp.message(Command("pulse"))
async def pulse_cmd(m: Message):
    p = m.text.split()
    if len(p) < 2: return await m.answer("Формат: /pulse BTC")
    await m.answer(pulse_text(p[1]))

@dp.message(Command("remind"))
async def remind_cmd(m: Message):
    p = m.text.split()
    if len(p) < 3 or not p[2].isdigit():
        return await m.answer("Формат: /remind SOL 3 (через сколько дней)")
    sym = safe_sym(p[1])
    if not sym: return await m.answer("Некорректный тикер.")
    q("INSERT INTO reminders(user_id, symbol, due_ts, note) VALUES(?,?,?,?)",
      m.from_user.id, sym, int(time.time() + int(p[2]) * 86400), "проверка плана")
    await m.answer(f"⏰ Ок, через {p[2]} дн. спрошу про план по {sym}.")

@dp.message(F.text == "🔎 СКАН")
async def do_scan(m: Message):
    if not heavy_ok(m.from_user.id, "scan"):
        return await m.answer("Слишком часто — подожди 30 сек.")
    await m.answer("<i>Сканирую: топ-100 → техника шорт-листа → потоки → Fear&Greed…</i>")
    txt, kb = scan_text()
    await m.answer(txt, reply_markup=kb, disable_web_page_preview=True)

@dp.message(F.text == "⭐ Мои монеты")
async def my_coins(m: Message):
    rows = q("SELECT * FROM watch WHERE user_id=?", m.from_user.id)
    if not rows: return await m.answer("Список пуст. Пришли тикер → «⭐ В мои».")
    ik = [[InlineKeyboardButton(f"✖ {r['symbol']} · 🔔{r['alert_pct']:.0f}%",
                                callback_data=f"rm:{r['symbol']}")] for r in rows]
    await m.answer("⭐ Мои монеты (нажми, чтобы убрать):",
                   reply_markup=InlineKeyboardMarkup(inline_keyboard=ik))

@dp.message(F.text == "📊 Статистика")
async def stats_btn(m: Message): await stats_cmd(m)

@dp.message(F.text == "ℹ️ О боте")
async def about(m: Message):
    await m.answer("Источники: CoinGecko, Binance/Bybit (авто-выбор), alternative.me (F&G), "
                   "DefiLlama (TVL), GoPlus (скам-чек), Stooq (макро).\n"
                   "Методология: 5 шагов, формулы риска в коде, гейт R:R ≥ 2:1, журнал правил.\n"
                   "© 2026 — разработка владельца бота. Не финансовый совет."
                   + ("\nLLM-разбор: включён." if llm_enabled() else "\nLLM-разбор: выключен (нет ключа в config.py).")
                   + DISCLAIMER)

@dp.message(F.text == "⚙️ Настройки")
async def setup(m: Message, state: FSMContext):
    s = q("SELECT * FROM settings WHERE user_id=?", m.from_user.id, one=True)
    await m.answer(f"Сейчас: счёт ${s['balance']:,.0f}, риск {s['risk_pct']}%/сделку.\n"
                   "Пришли новый размер счёта в $ (или «стоп»):")
    await state.set_state(Setup.balance)

@dp.message(Setup.balance)
async def set_bal(m: Message, state: FSMContext):
    if m.text.strip().lower() == "стоп":
        await state.clear(); return await m.answer("Отменено.", reply_markup=menu)
    try:
        v = float(m.text.replace(",", ".").replace(" ", "")); assert v > 0
    except Exception: return await m.answer("Пришли число, например: 10000")
    await state.update_data(b=v)
    await m.answer("Максимальный риск на сделку, % (новичкам 0,5–1):")
    await state.set_state(Setup.risk)

@dp.message(Setup.risk)
async def set_risk(m: Message, state: FSMContext):
    try:
        v = float(m.text.replace(",", ".").replace(" ", "")); assert 0 < v <= 10
    except Exception: return await m.answer("Число от 0,1 до 10, например: 1")
    d = await state.get_data()
    q("UPDATE settings SET balance=?, risk_pct=? WHERE user_id=?", d["b"], v, m.from_user.id)
    await state.clear()
    await m.answer(f"Готово: счёт ${d['b']:,.0f}, риск {v}%/сделку. Эти числа пойдут в шаг РИСК.",
                   reply_markup=menu)

@dp.callback_query(F.data == "chksub")
async def chksub(c: CallbackQuery):
    if await check_sub(c.from_user.id, force=True):
        await c.answer("✅ Подписка подтверждена!")
        await c.message.answer("Доступ открыт. Жми «🔎 СКАН» или пришли тикер.", reply_markup=menu)
    else:
        await c.answer("Пока не вижу подписку. Подпишись и попробуй через минуту.", show_alert=True)

@dp.callback_query(F.data.startswith(("wch:", "rm:", "alr:", "anl:", "chart:",
                                       "rug:", "tvl:", "flw:", "pls:", "full:")))
async def callbacks(c: CallbackQuery):
    act, raw = c.data.split(":", 1)
    uid = c.from_user.id
    sym = safe_sym(raw)
    if not sym: return await c.answer("Некорректный тикер", show_alert=True)
    if act == "wch":
        q("INSERT OR IGNORE INTO watch(user_id, symbol) VALUES(?,?)", uid, sym)
        await c.answer(f"{sym} в списке, алерт ±5%")
    elif act == "rm":
        q("DELETE FROM watch WHERE user_id=? AND symbol=?", uid, sym)
        await c.answer("Убрано")
        try: await c.message.edit_reply_markup(reply_markup=None)
        except Exception: pass
    elif act == "alr":
        cur = q("SELECT alert_pct FROM watch WHERE user_id=? AND symbol=?", uid, sym, one=True)
        cyc = {None: 5, 0.0: 3, 3.0: 5, 5.0: 10, 10.0: 0}
        nxt = cyc.get(cur["alert_pct"] if cur else None, 5)
        q("INSERT OR IGNORE INTO watch(user_id, symbol) VALUES(?,?)", uid, sym)
        q("UPDATE watch SET alert_pct=? WHERE user_id=? AND symbol=?", nxt, uid, sym)
        await c.answer(f"Алерт {sym}: ±{nxt:.0f}%" + (" (выкл)" if nxt == 0 else ""))
    elif act == "anl":
        txt, kb = coin_card(sym)
        if txt: await c.message.answer(txt, reply_markup=kb, disable_web_page_preview=True); await c.answer()
        else: await c.answer("Не нашёл", show_alert=True)
    elif act == "chart":
        if not heavy_ok(uid, "chart"): return await c.answer("Слишком часто — подожди 30 сек", show_alert=True)
        await c.answer("Рисую график…")
        buf = chart_png(sym)
        if buf: await c.message.answer_photo(BufferedInputFile(buf.read(), filename="chart.png"),
                    caption=f"{sym}USDT · дневной. Свечи, EMA20/50. Источник: Binance/Bybit.")
        else: await c.message.answer("График не построился — мало данных.")
    elif act == "rug":
        if not heavy_ok(uid, "rug"): return await c.answer("Слишком часто — подожди 30 сек", show_alert=True)
        await c.answer("Проверяю контракт…"); await c.message.answer(rug_text(sym))
    elif act == "flw":
        if not heavy_ok(uid, "flw"): return await c.answer("Слишком часто — подожди 30 сек", show_alert=True)
        await c.answer("Собираю потоки…")
        await c.message.answer(flows_block(sym) or "Данных нет.")
    elif act == "pls":
        await c.answer("Считаю контекст…"); await c.message.answer(pulse_text(sym))
    elif act == "full":
        if not llm_enabled():
            return await c.answer("Включи OPENAI_API_KEY в config.py — и кнопка заработает.", show_alert=True)
        if not heavy_ok(uid, "full"): return await c.answer("Слишком часто — подожди 30 сек", show_alert=True)
        await c.answer("Собираю данные, LLM пишет разбор…")
        await c.message.answer(full_analysis(sym), disable_web_page_preview=True)

@dp.message(F.text)
async def ticker_in(m: Message):
    sym = safe_sym(m.text)
    if not sym: return
    txt, kb = coin_card(sym)
    if not txt: return await m.answer("Не нашёл в топ-100 CoinGecko. Примеры: BTC, SOL, ENA.")
    await m.answer(txt, reply_markup=kb, disable_web_page_preview=True)

@dp.channel_post()
async def chan_id_debug(p):
    """Одноразовая помощь: покажет ID приватного канала в консоли."""
    print("CHANNEL ID:", p.chat.id)

# ============================================================
#  13. СТАРТ
# ============================================================
async def main():
    if not TG_TOKEN or "ВСТАВЬ" in TG_TOKEN:
        raise SystemExit("Открой config.py и впиши TG_TOKEN от @BotFather.")
    if GATE_MODE == "channel" and "tvoi_kanal" in CHANNEL:
        print("⚠ ВНИМАНИЕ: в config.py не вписан канал — гейт будет блокировать всех, кроме OWNER_ID.")
    asyncio.create_task(bg_loop())
    # подписка на события канала: вступил/вышел — бот узнаёт мгновенно
    updates = dp.resolve_used_update_types() + ["chat_member"]
    await dp.start_polling(bot, allowed_updates=updates)
    print("Бот запущен.")

if __name__ == "__main__":
    asyncio.run(main())