"""Sector Pulse: andamento dei settori USA, notizie e sintesi di Claude.

Dati: Yahoo Finance (libreria non ufficiale yfinance, uso personale).
Sintesi: API di Anthropic (modello Haiku, costo di pochi centesimi al mese).
"""
import html
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
import yfinance as yf

# ----------------------------------------------------------------------------
# Configurazione
# ----------------------------------------------------------------------------
MODEL = "claude-haiku-5-5"          # per risultati più ricchi: "claude-sonnet-5-5"
NEWS_PER_TICKER = 20              # notizie richieste a Yahoo per ogni ticker
MAX_ARTICLES = 80                 # massimo di notizie inviate a Claude
NEWS_PER_QUERY = 30               # notizie per ogni ricerca di settore/industria
TOP_INDUSTRIES = 3                # quante industrie del settore allargano la ricerca (0 = disattiva)
EXTRA_PER_INDUSTRY = 4            # aziende in più per industria (0 = disattiva)
CACHE_SECONDS = 600                 # prezzi e notizie restano in cache 10 minuti
TZ = ZoneInfo("Europe/Rome")

# chiave Yahoo del settore -> (nome, ETF settoriale usato come proxy)
SECTORS = {
    "technology": ("Technology", "XLK"),
    "healthcare": ("Healthcare", "XLV"),
    "financial-services": ("Financial Services", "XLF"),
    "industrials": ("Industrials", "XLI"),
    "consumer-cyclical": ("Consumer Cyclical", "XLY"),
    "consumer-defensive": ("Consumer Defensive", "XLP"),
    "communication-services": ("Communication Services", "XLC"),
    "energy": ("Energy", "XLE"),
    "utilities": ("Utilities", "XLU"),
    "real-estate": ("Real Estate", "XLRE"),
    "basic-materials": ("Basic Materials", "XLB"),
}
PERIODS = {"1g": "1 giorno", "1s": "1 settimana", "1m": "1 mese", "ytd": "YTD"}

st.set_page_config(page_title="Sector Pulse", page_icon="📈", layout="centered")


# ----------------------------------------------------------------------------
# Utilità
# ----------------------------------------------------------------------------
def esc(text, quote=True):
    """Escape HTML e il simbolo $ (Streamlit lo leggerebbe come formula)."""
    return html.escape(str(text), quote=quote).replace("$", "&#36;")


def publisher_txt(n):
    return f"{n} testat" + ("a" if n == 1 else "e")


def secret(name, default=""):
    try:
        return st.secrets.get(name, default)
    except Exception:
        return default


def fmt_pct(p, with_arrow=True):
    if p is None or pd.isna(p):
        return "n.d."
    txt = f"{p * 100:+.1f}%".replace(".", ",").replace("-", "−")
    if not with_arrow:
        return txt
    return ("▲ " if p >= 0 else "▼ ") + txt


def pct_color(p):
    if p is None or pd.isna(p):
        return "#9AA4B2"
    return "#34D399" if p >= 0 else "#FB7C5B"


def tile_bg(p):
    if p is None or pd.isna(p):
        return "#FFFFFF"
    strength = min(abs(p) * 100 / 2.0, 1.0) * 0.30 + 0.07
    rgb = "6,95,59" if p >= 0 else "194,65,12"
    return f"rgba({rgb},{strength:.2f})"


def abs_time(ts):
    if ts is None or pd.isna(ts):
        return "n.d."
    return ts.tz_convert(TZ).strftime("%d/%m %H:%M")


def time_ago(ts):
    if ts is None or pd.isna(ts):
        return ""
    delta = datetime.now(ZoneInfo("UTC")) - ts.to_pydatetime()
    mins = int(delta.total_seconds() // 60)
    if mins < 60:
        return f"{max(mins, 1)} min fa"
    if mins < 60 * 24:
        return f"{mins // 60} h fa"
    return f"{mins // (60 * 24)} g fa"


# ----------------------------------------------------------------------------
# Accesso (opzionale): se in secrets c'è APP_PASSWORD, chiede la password
# ----------------------------------------------------------------------------
def password_gate():
    pwd = secret("APP_PASSWORD")
    if not pwd or st.session_state.get("auth_ok"):
        return
    st.title("Sector Pulse")
    typed = st.text_input("Password", type="password")
    if typed and typed == pwd:
        st.session_state["auth_ok"] = True
        st.rerun()
    elif typed:
        st.error("Password errata.")
    st.stop()


# ----------------------------------------------------------------------------
# Dati: prezzi (Yahoo Finance)
# ----------------------------------------------------------------------------
def compute_perf(s: pd.Series) -> dict:
    last = float(s.iloc[-1])

    def back(n):
        return last / float(s.iloc[-1 - n]) - 1 if len(s) > n else None

    year_start = pd.Timestamp(datetime.now(TZ).year, 1, 1)
    prev_year = s[s.index < year_start]
    ytd = last / float(prev_year.iloc[-1]) - 1 if len(prev_year) else None
    hist = [round(float(x), 4) for x in s.tail(260).tolist()]
    ytd_n = int((s.index >= year_start).sum())
    return {"1g": back(1), "1s": back(5), "1m": back(21), "ytd": ytd, "last": last,
            "hist": hist, "ytd_n": ytd_n}


@st.cache_data(ttl=CACHE_SECONDS, show_spinner=False)
def get_prices(tickers: tuple):
    df = yf.download(list(tickers), period="1y", auto_adjust=True,
                     progress=False, threads=True)
    close = df["Close"]
    if isinstance(close, pd.Series):
        close = close.to_frame(tickers[0])
    out = {}
    for t in close.columns:
        s = close[t].dropna()
        if len(s) < 3:
            continue
        s.index = pd.DatetimeIndex(s.index).tz_localize(None)
        out[t] = compute_perf(s)
    return out, datetime.now(TZ).strftime("%H:%M")


# ----------------------------------------------------------------------------
# Dati: settore, industrie, aziende (Yahoo Finance)
# ----------------------------------------------------------------------------
@st.cache_data(ttl=3600, show_spinner=False)
def get_sector_info(key: str) -> dict:
    sec = yf.Sector(key)
    industries, companies = [], []
    try:
        inds = sec.industries
        if inds is not None and len(inds):
            for k, r in inds.iterrows():
                industries.append({
                    "key": str(k),
                    "name": str(r.get("name", k)),
                    "weight": r.get("market weight"),
                })
    except Exception:
        pass
    try:
        comps = sec.top_companies
        if comps is not None and len(comps):
            for sym, r in comps.head(8).iterrows():
                companies.append({"symbol": str(sym), "name": str(r.get("name", sym))})
    except Exception:
        pass
    if not industries and not companies:
        raise RuntimeError("Yahoo non ha restituito industrie né aziende")
    return {"industries": industries, "companies": companies}


@st.cache_data(ttl=3600, show_spinner=False)
def get_industry_top(key: str):
    df = yf.Industry(key).top_performing_companies
    return None if df is None else df.reset_index()


@st.cache_data(ttl=3600, show_spinner=False)
def get_industry_symbols(key: str) -> list:
    df = yf.Industry(key).top_companies
    if df is None or not len(df):
        raise RuntimeError("nessuna azienda")
    return [str(x) for x in df.head(EXTRA_PER_INDUSTRY).index]


# ----------------------------------------------------------------------------
# Dati: notizie (Yahoo Finance)
# ----------------------------------------------------------------------------
def parse_news(item: dict, source_ticker: str):
    c = item.get("content") or item
    title = c.get("title")
    if not title:
        return None
    link = ((c.get("canonicalUrl") or {}).get("url")
            or (c.get("clickThroughUrl") or {}).get("url")
            or item.get("link") or c.get("link"))
    publisher = (c.get("provider") or {}).get("displayName") or item.get("publisher") or ""
    ts = None
    if c.get("pubDate"):
        ts = pd.to_datetime(c["pubDate"], utc=True, errors="coerce")
    elif item.get("providerPublishTime"):
        ts = pd.to_datetime(item["providerPublishTime"], unit="s", utc=True, errors="coerce")
    return {"title": title, "link": link, "publisher": publisher,
            "ts": ts, "ticker": source_ticker,
            "summary": c.get("summary") or c.get("description") or ""}


def fetch_raw_news(ticker: str):
    tk = yf.Ticker(ticker)
    for kwargs in ({"count": NEWS_PER_TICKER, "tab": "all"}, {"count": NEWS_PER_TICKER}):
        try:
            got = tk.get_news(**kwargs)
            if got:
                return got
        except Exception:
            continue
    try:
        return tk.news or []
    except Exception:
        return []


def fetch_search_news(query: str):
    """Notizie di settore cercate per parola chiave, non legate a un singolo titolo."""
    try:
        return yf.Search(query, max_results=1, news_count=NEWS_PER_QUERY,
                         include_cb=False, raise_errors=False).news or []
    except Exception:
        return []


@st.cache_data(ttl=CACHE_SECONDS, show_spinner=False)
def get_news(tickers: tuple, queries: tuple = ()):
    """Raccoglie notizie di ETF, aziende e ricerche di settore; toglie i doppioni e tiene
    le ultime 24 ore (se sono poche allarga a 48)."""
    jobs = [("t", t) for t in tickers] + [("q", q) for q in queries]

    def run(job):
        kind, value = job
        return fetch_raw_news(value) if kind == "t" else fetch_search_news(value)

    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(run, jobs))

    by_key, fetched = {}, 0
    for (kind, value), raws in zip(jobs, results):
        label = value if kind == "t" else "settore"
        for raw in raws:
            n = parse_news(raw, label)
            if not n:
                continue
            fetched += 1
            related = [str(x) for x in (raw.get("relatedTickers") or [])][:3] if kind == "q" else []
            tags = related or [label]
            k = re.sub(r"\W+", " ", n["title"].lower()).strip()
            if k in by_key:
                cur = by_key[k]
                cur["reach"] += 1
                for tg in tags:
                    if tg not in cur["tickers"]:
                        cur["tickers"].append(tg)
            else:
                n["tickers"] = list(tags)
                n["reach"] = 1
                by_key[k] = n
    items = sorted(by_key.values(),
                   key=lambda n: n["ts"] if n["ts"] is not None and not pd.isna(n["ts"])
                   else pd.Timestamp(0, tz="UTC"), reverse=True)
    now = pd.Timestamp.now(tz="UTC")

    def within(hours):
        return [n for n in items if n["ts"] is not None and not pd.isna(n["ts"])
                and now - n["ts"] <= pd.Timedelta(hours=hours)]

    window, chosen = 24, within(24)
    if len(chosen) < 8:
        window, chosen = 48, within(48)
    if len(chosen) < 4:
        window, chosen = None, items
    if len(chosen) > MAX_ARTICLES:   # se sono troppe, tieni le più riprese e recenti
        keep = sorted(chosen, key=lambda n: (n["reach"], n["ts"] if n["ts"] is not None
                                             and not pd.isna(n["ts"]) else pd.Timestamp(0, tz="UTC")),
                      reverse=True)[:MAX_ARTICLES]
        chosen = sorted(keep, key=lambda n: n["ts"] if n["ts"] is not None and not pd.isna(n["ts"])
                        else pd.Timestamp(0, tz="UTC"), reverse=True)
    return {"items": chosen, "window": window, "fetched": fetched, "unique": len(items),
            "n_tickers": len(tickers), "n_queries": len(queries)}


# ----------------------------------------------------------------------------
# Sintesi con Claude (cache: stessa notizia = nessuna nuova chiamata)
# ----------------------------------------------------------------------------
SYSTEM_PROMPT = """Sei un analista che prepara un briefing sul settore azionario USA per un investitore privato italiano.
Ricevi i dati di prezzo e un elenco numerato di notizie delle ultime ore (titolo e, se c'è, un breve riassunto).
Usa SOLO il materiale fornito: non inventare fatti, cifre, cause o citazioni, e non usare conoscenze esterne sugli eventi di oggi. Se le notizie non spiegano un movimento di prezzo, dillo.
Non dare consigli di investimento.

Lavora così:
1. Raggruppa le notizie che parlano della stessa vicenda in un unico tema.
2. Assegna a ogni tema una priorità (alta, media, bassa) in base a questi criteri:
   - TIPO DI EVENTO: risultati trimestrali e guidance, fusioni e acquisizioni, grandi accordi industriali, regolamentazione e cause legali, decisioni macro o di banche centrali, annunci di prodotto rilevanti pesano di più. Analisi di mercato e giudizi degli analisti pesano in modo medio. Articoli di opinione, liste "titoli da comprare", rendimenti ipotetici e contenuti promozionali pesano poco: ignorali o mettili a priorità bassa.
   - AMPIEZZA: più testate indipendenti sullo stesso tema aumentano il peso.
   - IMPATTO SUL PREZZO: se il titolo o l'ETF collegato si muove molto oggi, il peso aumenta.
   - FRESCHEZZA: a parità di altri fattori contano di più le notizie più recenti.
3. Scrivi prima un quadro generale che sintetizza TUTTE le notizie, poi vai nel dettaglio sui temi, dal più al meno importante.

Rispondi esclusivamente con un oggetto JSON, senza testo prima o dopo e senza blocchi di codice, con queste chiavi:
"sentiment": uno tra "positivo", "neutro", "negativo", "misto";
"quadro_generale": macro-sintesi in italiano di 5-7 frasi su ciò che emerge nel complesso e su come si collega ai movimenti di prezzo;
"temi": lista ordinata per priorità, massimo 6 elementi, ciascuno con:
   "titolo" (breve), "priorita" ("alta"|"media"|"bassa"), "perche" (una frase sul motivo della priorità),
   "dettaglio" (alta: 4-6 frasi con fatti e cifre presenti nelle notizie; media: 2-3 frasi; bassa: 1 frase),
   "ticker" (lista dei ticker coinvolti), "articoli" (lista dei numeri delle notizie a supporto);
"non_spiegato": una frase su ciò che le notizie non chiariscono (stringa vuota se tutto è chiaro)."""


@st.cache_data(ttl=3600, show_spinner=False)
def get_summary(context: str) -> dict:
    import anthropic

    client = anthropic.Anthropic(api_key=secret("ANTHROPIC_API_KEY"))
    # Haiku 5.5 "ragiona" prima di rispondere e quel ragionamento conta nel limite di token:
    # un limite basso lascia la risposta vuota. Per questo il tetto è alto.
    msg = client.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": context}],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    reason = getattr(msg, "stop_reason", None)
    if reason == "refusal":
        raise RuntimeError("Claude ha rifiutato la richiesta (filtro di sicurezza)")
    if not text.strip():
        raise RuntimeError(f"risposta vuota (motivo: {reason})")
    return parse_summary(text)


def parse_summary(text: str) -> dict:
    """Legge il JSON della risposta; se non è valido mostra comunque il testo."""
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(text[start:end + 1])
            if isinstance(data, dict) and (data.get("quadro_generale") or data.get("sintesi")):
                data.setdefault("quadro_generale", data.get("sintesi", ""))
                temi = data.get("temi")
                data["temi"] = [t for t in temi if isinstance(t, dict)] if isinstance(temi, list) else []
                return data
        except json.JSONDecodeError:
            pass
    clean = text.replace("```json", "").replace("```", "").strip()
    return {"sentiment": "neutro", "quadro_generale": clean[:1200], "temi": [], "non_spiegato": ""}


def build_context(label, etf, perf, moves, news):
    lines = [f"Settore: {label} (ETF proxy {etf})",
             "Performance dell'ETF: " + ", ".join(
                 f"{PERIODS[k]} {fmt_pct(perf.get(k), False)}" for k in PERIODS)]
    mv = [f"{sym} {fmt_pct(v, False)}" for sym, v in moves.items() if v is not None]
    if mv:
        lines.append("Variazione di oggi dei titoli collegati: " + ", ".join(mv))
    lines.append(f"Notizie ({len(news)}), dalla più recente:")
    for i, n in enumerate(news, 1):
        extra = f" — {n['summary'][:180]}" if n["summary"] else ""
        lines.append(f"[{i}] ({', '.join(n['tickers'][:4])}; {n['publisher']}; {abs_time(n['ts'])}) {n['title']}{extra}")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# Stile (layout A: tema scuro, lista con mini-grafici)
# ----------------------------------------------------------------------------
BASE_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=Space+Grotesk:wght@500;600;700&display=swap');
html, body, [class*="css"], .stMarkdown, button { font-family: 'Inter', system-ui, sans-serif; }
.stApp { background:#0E1116; color:#E8ECF2; }
.block-container { max-width:640px; padding:1.4rem 1rem 4rem; }
header[data-testid="stHeader"] { background:transparent; }
h1, h2, h3 { font-family:'Space Grotesk', sans-serif !important; color:#E8ECF2; letter-spacing:-0.01em; }
a { color:#7DB4FF; }

.a-sub { color:#9AA4B2; font-size:13px; margin-top:-0.5rem; }
.a-small { font-size:12px; color:#9AA4B2; }

/* intestazione con pulsante Aggiorna in alto a destra */
.st-key-hdr { position:relative; gap:0 !important; }
.st-key-refresh { position:absolute !important; top:4px; right:0; width:auto !important; }
.st-key-refresh button { width:48px; height:48px; min-height:48px; padding:0; border:0; border-radius:12px;
  background:#E8ECF2; color:#0E1116; font-size:22px; }
.st-key-refresh button:hover { background:#FFFFFF; color:#0E1116; }
.st-key-refresh button p { font-size:22px; line-height:1; }

/* lista dei settori */
.st-key-list { gap:0 !important; background:#171B22; border:1px solid #262C36; border-radius:16px; overflow:hidden; }
[class*="st-key-row_"] { position:relative; gap:0 !important; }
[class*="st-key-hit_"] { position:absolute !important; inset:0; z-index:5; width:100% !important; height:100% !important; margin:0 !important; }
[class*="st-key-hit_"] > div, [class*="st-key-hit_"] [data-testid="stButton"] { height:100%; width:100%; }
[class*="st-key-hit_"] button { width:100%; height:100%; min-height:0; opacity:0; cursor:pointer; padding:0; }
.a-row { display:grid; grid-template-columns:minmax(0,1fr) 84px 82px; gap:12px; align-items:center;
  padding:14px; border-top:1px solid #232A33; }
[class*="st-key-row_"]:hover .a-row { background:#1B212B; }
.a-row.sel { background:#1E2530; border-left:3px solid #E8ECF2; padding-left:11px; }
.a-row .nm { font-size:15px; font-weight:500; }
.a-row.sel .nm { font-weight:600; }
.a-row .sb { font-size:12px; color:#9AA4B2; }
.a-row .val { text-align:right; font-family:'Space Grotesk', sans-serif; font-size:17px; font-weight:700; white-space:nowrap; }

/* dettaglio del settore selezionato */
.a-detail { background:#12161C; padding:16px 14px 6px; border-top:1px solid #262C36; }
.a-detail h3 { margin:0; font-size:16px; font-weight:600; }
.a-pill { display:inline-block; padding:4px 10px; border-radius:999px; font-size:12px; font-weight:600; }
.a-meta { font-size:12px; color:#9AA4B2; margin:4px 0 10px; }
.a-macro { font-size:15px; line-height:1.65; margin:0 0 6px; }
.a-theme { padding:14px 0; border-top:1px solid #262C36; }
.a-theme .tt { font-size:15px; font-weight:600; line-height:1.35; }
.a-theme .why { font-size:12px; color:#9AA4B2; margin:4px 0 0; }
.a-theme .det { font-size:14px; line-height:1.6; color:#D5DBE5; margin:8px 0 0; }
.a-theme .src { font-size:12px; color:#9AA4B2; margin-top:8px; display:flex; gap:6px; flex-wrap:wrap; align-items:center; }
.a-theme .src a { color:#7DB4FF; text-decoration:none; }
.a-prio { display:inline-block; padding:2px 8px; border-radius:6px; font-size:11px; font-weight:700; letter-spacing:.04em; text-transform:uppercase; margin-right:8px; }
.a-news .item { padding:12px 0; border-top:1px solid #262C36; }
.a-news a { text-decoration:none; color:#E8ECF2; }
.a-news .t { font-size:14px; font-weight:600; line-height:1.4; }
.a-news .m { font-size:12px; color:#9AA4B2; margin-top:5px; display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
.a-chip { display:inline-block; padding:2px 7px; border-radius:6px; background:#263349; color:#A9C8FF; font-weight:600; font-size:12px; }
.a-co { display:flex; justify-content:space-between; align-items:center; padding:11px 0; border-top:1px solid #262C36; font-size:14px; }
</style>
"""


def sparkline(vals, color, w=84, h=28):
    vals = [v for v in (vals or []) if v is not None]
    if len(vals) < 2:
        return f'<svg width="{w}" height="{h}"></svg>'
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1.0
    pts = " ".join(f"{i * (w - 2) / (len(vals) - 1):.1f},{(h - 4) - (v - lo) / rng * (h - 8) + 2:.1f}"
                   for i, v in enumerate(vals))
    return (f'<svg aria-hidden="true" width="{w}" height="{h}" viewBox="0 0 {w} {h}" fill="none" '
            f'stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            f'<polyline points="{pts}"/></svg>')


def spark_values(p, period):
    h = p.get("hist") or []
    n = {"1g": 5, "1s": 10, "1m": 22}.get(period) or p.get("ytd_n") or 22
    return h[-max(int(n), 2):]


# ----------------------------------------------------------------------------
# App
# ----------------------------------------------------------------------------
password_gate()
st.markdown(BASE_CSS, unsafe_allow_html=True)

if "sector" not in st.session_state:
    st.session_state["sector"] = "technology"
if "period" not in st.session_state:
    st.session_state["period"] = "1g"

# Intestazione
with st.container(key="hdr"):
    st.title("Sector Pulse")
    st.markdown('<div class="a-sub">Mercato USA · dati Yahoo Finance con ritardo fino a 15 minuti</div>',
                unsafe_allow_html=True)
    if st.button("↻", key="refresh", help="Aggiorna prezzi e notizie"):
        get_prices.clear()
        get_news.clear()
        st.rerun()

# Prezzi dei 11 ETF proxy
etfs = tuple(v[1] for v in SECTORS.values())
try:
    perfs, updated_at = get_prices(etfs)
except Exception as e:
    st.error("Non riesco a scaricare i prezzi da Yahoo Finance. Succede quando Yahoo "
             "limita le richieste: riprova tra qualche minuto.")
    st.caption(f"Dettaglio tecnico: {e}")
    st.stop()

st.markdown(f'<div class="a-small">Aggiornato alle {updated_at} · cache {CACHE_SECONDS // 60} min</div>',
            unsafe_allow_html=True)

# Periodo
period_key = st.segmented_control("Periodo", list(PERIODS), format_func=PERIODS.get,
                                  key="period", label_visibility="collapsed") or "1g"

# Settori ordinati per performance del periodo scelto
def sort_key(k):
    p = (perfs.get(SECTORS[k][1]) or {}).get(period_key)
    return -(p if p is not None else -9)


order = sorted(SECTORS, key=sort_key)
selected = st.session_state["sector"]

sent_colors = {"positivo": ("rgba(52,211,153,0.16)", "#6EE7B7"),
               "negativo": ("rgba(251,124,91,0.16)", "#FDBA9F"),
               "neutro": ("#263349", "#C5CDD8"),
               "misto": ("rgba(250,204,21,0.16)", "#FDE68A")}


PRIO_STYLE = {"alta": ("rgba(250,204,21,0.18)", "#FDE68A"),
              "media": ("#263349", "#A9C8FF"),
              "bassa": ("#232A33", "#9AA4B2")}


def render_detail(key):
    label, etf = SECTORS[key]
    sector_err = None
    try:
        info = get_sector_info(key)
    except Exception as e:
        info = {"industries": [], "companies": []}
        sector_err = f"{type(e).__name__}: {str(e)[:160]}"
    companies = info["companies"][:8]
    extra = []
    if EXTRA_PER_INDUSTRY > 0:
        for ind in info["industries"][:TOP_INDUSTRIES]:
            try:
                extra += get_industry_symbols(ind["key"])
            except Exception:
                pass
    base = [c["symbol"] for c in companies]
    all_syms = tuple(dict.fromkeys(base + extra))
    queries = tuple(dict.fromkeys(
        [f"{label} stocks"] + [re.sub(r"\s*-\s*", " ", ind["name"]) + " stocks"
                               for ind in info["industries"][:TOP_INDUSTRIES]]))
    comp_perf = {}
    if all_syms:
        try:
            comp_perf, _ = get_prices(all_syms)
        except Exception:
            comp_perf = {}

    data = get_news((etf,) + all_syms, queries)
    news = data["items"]
    moves = {etf: (perfs.get(etf) or {}).get("1g")}
    for sym in all_syms:
        moves[sym] = (comp_perf.get(sym) or {}).get("1g")

    # Briefing di Claude
    summary, err = None, None
    if not secret("ANTHROPIC_API_KEY"):
        err = "Chiave API di Anthropic non configurata: la sintesi è disattivata."
    elif not news:
        err = "Nessuna notizia recente trovata su Yahoo Finance per questo settore."
    else:
        try:
            ctx = build_context(label, etf, perfs.get(etf) or {}, moves, news)
            with st.spinner(f"Claude sta leggendo {len(news)} notizie…"):
                summary = get_summary(ctx)
        except Exception as e:
            err = f"Non sono riuscito a generare la sintesi: {type(e).__name__}: {str(e)[:200]}"

    # riga informativa su quante notizie sono state lette
    publishers = len({n["publisher"] for n in news if n["publisher"]})
    if data["window"] == 24:
        arco = "delle ultime 24 ore"
    elif data["window"] == 48:
        arco = "delle ultime 48 ore (nelle ultime 24 ore ce n'erano poche)"
    else:
        arco = "più recenti disponibili"
    meta = (f'<div class="a-meta">Letture: {len(news)} notizie {arco}, da {publisher_txt(publishers)}. '
            f'Fonti: ETF {esc(etf)}, {max(len(all_syms), 0)} aziende e '
            f'{data["n_queries"]} ricerche di settore su Yahoo Finance.</div>') if news else ""

    if summary:
        s_ = str(summary.get("sentiment", "neutro")).lower()
        bg, fg = sent_colors.get(s_, sent_colors["neutro"])
        pill = f'<span class="a-pill" style="background:{bg};color:{fg}">Sentiment: {esc(s_)}</span>'
        body = f'<p class="a-macro">{esc(summary.get("quadro_generale", ""))}</p>'
        temi_html = ""
        for t in summary.get("temi", []):
            prio = str(t.get("priorita", "media")).lower()
            pbg, pfg = PRIO_STYLE.get(prio, PRIO_STYLE["media"])
            det = str(t.get("dettaglio", "")).strip()
            why = str(t.get("perche", "")).strip()
            chips = "".join(f'<span class="a-chip">{esc(x)}</span>' for x in (t.get("ticker") or [])[:6])
            links = ""
            for num in (t.get("articoli") or [])[:5]:
                try:
                    a = news[int(num) - 1]
                except (ValueError, IndexError, TypeError):
                    continue
                href = esc(a["link"] or "#", quote=True)
                label_a = esc(a["publisher"] or "fonte")
                links += f'<a href="{href}" target="_blank" rel="noopener">{label_a}</a>'
            temi_html += (
                f'<div class="a-theme"><div class="tt"><span class="a-prio" style="background:{pbg};color:{pfg}">'
                f'{esc(prio)}</span>{esc(t.get("titolo", ""))}</div>'
                + (f'<div class="why">{esc(why)}</div>' if why else "")
                + (f'<div class="det">{esc(det)}</div>' if det else "")
                + (f'<div class="src">{chips}{links}</div>' if (chips or links) else "")
                + '</div>')
        non_sp = str(summary.get("non_spiegato", "")).strip()
        extra = (f'<div class="a-small" style="padding:10px 0 4px"><b>Non spiegato dalle notizie:</b> {esc(non_sp)}</div>'
                 if non_sp else "")
        body += temi_html + extra
    else:
        pill = ""
        body = f'<p class="a-small" style="margin:10px 0">{esc(err or "")}</p>'

    cos = ""
    for c in companies[:5]:
        d = (comp_perf.get(c["symbol"]) or {}).get("1g")
        cos += (f'<div class="a-co"><span><b>{esc(c["symbol"])}</b> '
                f'<span class="a-small">{esc(c["name"])}</span></span>'
                f'<b style="color:{pct_color(d)}">{fmt_pct(d)}</b></div>')

    st.markdown(
        f'<div class="a-detail">'
        f'<div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap">'
        f'<h3>Cosa si dice</h3>{pill}</div>{meta}{body}'
        f'<div class="a-small" style="margin:8px 0 6px">Generato da Claude dai titoli e dai brevi riassunti di '
        f'Yahoo Finance, non dagli articoli completi. Non è consulenza finanziaria.</div>'
        + (f'<h3 style="margin-top:16px">Aziende principali</h3><div style="margin-top:6px">{cos}</div>'
           f'<div class="a-small" style="padding:6px 0 10px">Variazione di oggi</div>' if cos else "")
        + '</div>',
        unsafe_allow_html=True)

    if news:
        with st.expander(f"Tutte le notizie analizzate ({len(news)})"):
            items = ""
            for n in news:
                link = esc(n["link"] or "#", quote=True)
                chips = "".join(f'<span class="a-chip">{esc(x)}</span>' for x in n["tickers"][:3])
                items += (f'<div class="item"><a href="{link}" target="_blank" rel="noopener">'
                          f'<div class="t">{esc(n["title"])}</div>'
                          f'<div class="m">{chips}{esc(n["publisher"])} · {time_ago(n["ts"])}</div></a></div>')
            st.markdown(f'<div class="a-news">{items}</div>', unsafe_allow_html=True)

    if sector_err:
        st.caption("Aziende e sottosettori non disponibili: Yahoo non ha risposto "
                   f"({sector_err}). Riprova più tardi con ↻.")
    if info["industries"]:
        with st.expander("Sottosettori"):
            rows = ""
            for ind in info["industries"][:10]:
                w = ind["weight"]
                w_txt = (f"{w * 100:.1f}%".replace(".", ",")
                         if isinstance(w, (int, float)) and not pd.isna(w) else "")
                rows += (f'<div class="a-co"><span>{esc(ind["name"])}</span>'
                         f'<span class="a-small">{w_txt}</span></div>')
            st.markdown(rows + '<div class="a-small" style="padding-top:6px">Peso dell\'industria nel settore</div>',
                        unsafe_allow_html=True)
            names = {i["name"]: i["key"] for i in info["industries"][:10]}
            pick = st.selectbox("Approfondisci un sottosettore", ["—"] + list(names))
            if pick != "—":
                try:
                    df = get_industry_top(names[pick])
                    if df is not None and len(df):
                        st.dataframe(df, hide_index=True, use_container_width=True)
                    else:
                        st.caption("Nessun dato disponibile per questo sottosettore.")
                except Exception:
                    st.caption("Dati del sottosettore non disponibili in questo momento.")


# Lista dei settori (riga cliccabile = bottone invisibile sopra l'HTML)
with st.container(key="list"):
    for key in order:
        label, etf = SECTORS[key]
        p = perfs.get(etf) or {}
        val = p.get(period_key)
        color = pct_color(val)
        spark = sparkline(spark_values(p, period_key), color)
        sel = " sel" if key == selected else ""
        with st.container(key=f"row_{key}"):
            st.markdown(
                f'<div class="a-row{sel}"><div><div class="nm">{label}</div>'
                f'<div class="sb">{etf} · YTD {fmt_pct(p.get("ytd"), False)}</div></div>'
                f'{spark}<div class="val" style="color:{color}">{fmt_pct(val)}</div></div>',
                unsafe_allow_html=True)
            if st.button(f"Apri {label}", key=f"hit_{key}"):
                st.session_state["sector"] = key
                st.rerun()
        if key == selected:
            render_detail(key)

st.markdown('<div class="a-small" style="margin-top:16px;line-height:1.5">Fonte dati: Yahoo Finance tramite '
            'libreria non ufficiale, uso personale. Le informazioni mostrate non costituiscono '
            'consulenza finanziaria.</div>', unsafe_allow_html=True)
