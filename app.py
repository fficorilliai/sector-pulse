"""Sector Pulse: andamento dei settori USA, notizie e sintesi di Claude.

Dati: Yahoo Finance (libreria non ufficiale yfinance, uso personale).
Sintesi: API di Anthropic (modello Haiku, costo di pochi centesimi al mese).
"""
import html
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
import yfinance as yf

# ----------------------------------------------------------------------------
# Configurazione
# ----------------------------------------------------------------------------
MODEL = "claude-haiku-5-5"          # per risultati più ricchi: "claude-sonnet-5-5"
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

st.set_page_config(page_title="Sector Pulse", page_icon="📈", layout="wide")


# ----------------------------------------------------------------------------
# Utilità
# ----------------------------------------------------------------------------
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
        return "#4B5263"
    return "#065F3B" if p >= 0 else "#9A3412"


def tile_bg(p):
    if p is None or pd.isna(p):
        return "#FFFFFF"
    strength = min(abs(p) * 100 / 2.0, 1.0) * 0.30 + 0.07
    rgb = "6,95,59" if p >= 0 else "194,65,12"
    return f"rgba({rgb},{strength:.2f})"


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
    return {"1g": back(1), "1s": back(5), "1m": back(21), "ytd": ytd, "last": last}


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
    return {"industries": industries, "companies": companies}


@st.cache_data(ttl=3600, show_spinner=False)
def get_industry_top(key: str):
    df = yf.Industry(key).top_performing_companies
    return None if df is None else df.reset_index()


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


@st.cache_data(ttl=CACHE_SECONDS, show_spinner=False)
def get_news(tickers: tuple, limit: int = 8):
    seen, items = set(), []
    for t in tickers:
        try:
            raw = yf.Ticker(t).news or []
        except Exception:
            continue
        for it in raw[:6]:
            n = parse_news(it, t)
            if n and n["title"] not in seen:
                seen.add(n["title"])
                items.append(n)
    items.sort(key=lambda n: n["ts"] if n["ts"] is not None and not pd.isna(n["ts"])
               else pd.Timestamp(0, tz="UTC"), reverse=True)
    return items[:limit]


# ----------------------------------------------------------------------------
# Sintesi con Claude (cache: stessa notizia = nessuna nuova chiamata)
# ----------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "Sei un assistente che riassume l'andamento di un settore azionario USA per un "
    "investitore privato italiano. Usa SOLO i dati e i titoli forniti, non inventare "
    "fatti, cifre o motivazioni che non compaiono nel materiale. Se i titoli non "
    "spiegano il movimento, dillo. Non dare consigli di investimento. "
    "Rispondi esclusivamente con un oggetto JSON con queste chiavi: "
    '"sentiment" (uno tra "positivo", "neutro", "negativo", "misto"), '
    '"sintesi" (3-4 frasi in italiano), '
    '"temi" (lista di 2-3 brevi temi o rischi emersi, in italiano).'
)


@st.cache_data(ttl=3600, show_spinner=False)
def get_summary(context: str) -> dict:
    import anthropic

    client = anthropic.Anthropic(api_key=secret("ANTHROPIC_API_KEY"))
    msg = client.messages.create(
        model=MODEL,
        max_tokens=500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": context}],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start:end + 1])


def build_context(label, etf, perf, companies, comp_perf, news):
    lines = [f"Settore: {label} (ETF proxy {etf})",
             "Performance ETF: " + ", ".join(
                 f"{PERIODS[k]} {fmt_pct(perf.get(k), False)}" for k in PERIODS)]
    cl = [f"{c['symbol']} {fmt_pct((comp_perf.get(c['symbol']) or {}).get('1g'), False)}"
          for c in companies[:5]]
    if cl:
        lines.append("Aziende principali (oggi): " + ", ".join(cl))
    lines.append("Titoli recenti:")
    for n in news:
        extra = f" - {n['summary'][:200]}" if n["summary"] else ""
        lines.append(f"- [{n['ticker']}] {n['title']}{extra}")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# Stile
# ----------------------------------------------------------------------------
BASE_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=Space+Grotesk:wght@500;600;700&display=swap');
html, body, [class*="css"], .stMarkdown, button { font-family: 'Inter', system-ui, sans-serif; }
.stApp { background: #F4F5F7; color: #14171F; }
.block-container { max-width: 1280px; padding-top: 1.6rem; padding-bottom: 5rem; }
h1, h2, h3 { font-family: 'Space Grotesk', sans-serif !important; letter-spacing: -0.01em; color: #14171F; }
header[data-testid="stHeader"] { background: transparent; }

.sp-sub { color:#4B5263; font-size:14px; margin-top:-0.4rem; }
.sp-card { background:#FFFFFF; border:1px solid #D9DDE6; border-radius:14px; padding:16px 18px; margin-bottom:16px; }
.sp-card h3 { margin:0 0 8px 0; font-size:17px; font-weight:600; }
.sp-row { display:flex; justify-content:space-between; align-items:center; gap:12px; padding:11px 0; border-top:1px solid #ECEEF3; font-size:14px; }
.sp-row:first-of-type { border-top:none; }
.sp-chip { display:inline-block; padding:2px 8px; border-radius:6px; background:#E8ECF4; color:#1E3A8A; font-weight:600; font-size:12px; }
.sp-news a { text-decoration:none; color:#14171F; }
.sp-news .t { font-size:15px; font-weight:600; line-height:1.4; }
.sp-news .m { font-size:12px; color:#4B5263; margin-top:5px; display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
.sp-news .item { padding:12px 0; border-top:1px solid #ECEEF3; }
.sp-news .item:first-of-type { border-top:none; }
.sp-sent { display:inline-block; padding:4px 10px; border-radius:999px; font-size:12px; font-weight:600; }
.sp-small { font-size:12px; color:#4B5263; }

/* Riquadri dei settori */
.st-key-tiles button { width:100%; min-height:98px; border-radius:12px; border:1px solid #CFD5E0;
  justify-content:flex-start; text-align:left; color:#14171F; padding:12px 14px; }
.st-key-tiles button p { text-align:left; font-size:13px; line-height:1.55; }
.st-key-tiles button:hover { border-color:#14171F; color:#14171F; }
@media (max-width: 640px) {
  .st-key-tiles div[data-testid="stColumn"], .st-key-tiles div[data-testid="column"] {
    min-width: calc(50% - 0.5rem) !important; flex: 1 1 calc(50% - 0.5rem) !important; }
}
</style>
"""


def tile_css(order, perfs, period, selected):
    css = []
    for key in order:
        p = (perfs.get(SECTORS[key][1]) or {}).get(period)
        border = "2px solid #14171F" if key == selected else "1px solid #CFD5E0"
        css.append(f".st-key-tile_{key} button {{ background:{tile_bg(p)}; border:{border}; }}")
    return "<style>" + "\n".join(css) + "</style>"


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
c1, c2 = st.columns([5, 1.4], vertical_alignment="center")
with c1:
    st.title("Sector Pulse")
    st.markdown('<div class="sp-sub">Mercato USA · 11 settori · dati Yahoo Finance con ritardo fino a 15 minuti</div>',
                unsafe_allow_html=True)
with c2:
    if st.button("↻ Aggiorna", type="primary", use_container_width=True):
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

st.caption(f"Aggiornato alle {updated_at} · cache {CACHE_SECONDS // 60} min")

# Periodo
period_key = st.radio("Periodo", list(PERIODS), format_func=PERIODS.get,
                      horizontal=True, key="period", label_visibility="collapsed")

# Riquadri ordinati per performance del periodo scelto
order = sorted(SECTORS, key=lambda k: -(((perfs.get(SECTORS[k][1]) or {}).get(period_key))
                                       if (perfs.get(SECTORS[k][1]) or {}).get(period_key) is not None
                                       else -9))
st.markdown(tile_css(order, perfs, period_key, st.session_state["sector"]), unsafe_allow_html=True)
st.subheader("Settori")
with st.container(key="tiles"):
    per_row = 4
    for i in range(0, len(order), per_row):
        cols = st.columns(per_row)
        for col, key in zip(cols, order[i:i + per_row]):
            label, etf = SECTORS[key]
            p = (perfs.get(etf) or {}).get(period_key)
            ytd = (perfs.get(etf) or {}).get("ytd")
            btn = f"**{label}**  \n{etf} · YTD {fmt_pct(ytd, False)}  \n**{fmt_pct(p)}**"
            if col.button(btn, key=f"tile_{key}", use_container_width=True):
                st.session_state["sector"] = key
                st.rerun()

# ---------------------------------------------------------------------------
# Dettaglio del settore selezionato
# ---------------------------------------------------------------------------
sector_key = st.session_state["sector"]
label, etf = SECTORS[sector_key]
st.divider()
st.header(label)
st.markdown(f'<div class="sp-sub">ETF proxy: {etf}</div>', unsafe_allow_html=True)

try:
    info = get_sector_info(sector_key)
except Exception:
    info = {"industries": [], "companies": []}
    st.warning("Non riesco a leggere industrie e aziende principali da Yahoo in questo momento.")

companies = info["companies"][:5]
comp_perf = {}
if companies:
    try:
        comp_perf, _ = get_prices(tuple(c["symbol"] for c in companies))
    except Exception:
        comp_perf = {}

news_tickers = (etf,) + tuple(c["symbol"] for c in companies[:3])
news = get_news(news_tickers)

left, right = st.columns([1, 1.25], gap="large")

with left:
    # Sottosettori
    if info["industries"]:
        rows = ""
        for ind in info["industries"][:10]:
            w = ind["weight"]
            w_txt = f"{w * 100:.1f}%".replace(".", ",") if isinstance(w, (int, float)) and not pd.isna(w) else ""
            rows += (f'<div class="sp-row"><span style="font-weight:500">{html.escape(ind["name"])}</span>'
                     f'<span class="sp-small">{w_txt}</span></div>')
        st.markdown(f'<div class="sp-card"><h3>Sottosettori</h3>{rows}'
                    f'<div class="sp-small" style="padding-top:8px">Peso dell\'industria nel settore</div></div>',
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

    # Aziende principali
    if companies:
        rows = ""
        for c in companies:
            cp = comp_perf.get(c["symbol"]) or {}
            d, y = cp.get("1g"), cp.get("ytd")
            rows += (f'<div class="sp-row"><span><b>{html.escape(c["symbol"])}</b> '
                     f'<span class="sp-small">{html.escape(c["name"])}</span></span>'
                     f'<span style="white-space:nowrap"><b style="color:{pct_color(d)}">{fmt_pct(d)}</b>'
                     f'<span class="sp-small" style="display:inline-block;width:64px;text-align:right">'
                     f'{fmt_pct(y, False)}</span></span></div>')
        st.markdown(f'<div class="sp-card"><h3>Aziende principali</h3>{rows}'
                    f'<div class="sp-small" style="padding-top:8px">Colonne: oggi · YTD</div></div>',
                    unsafe_allow_html=True)

with right:
    # Sintesi di Claude
    sent_colors = {"positivo": ("#D7F0E3", "#065F3B"), "negativo": ("#F8D9C4", "#9A3412"),
                   "neutro": ("#E8ECF4", "#33405A"), "misto": ("#FFF1CC", "#5C4200")}
    summary, err = None, None
    if not secret("ANTHROPIC_API_KEY"):
        err = "Chiave API di Anthropic non configurata: la sintesi è disattivata."
    elif not news:
        err = "Nessuna notizia recente trovata su Yahoo Finance per questo settore."
    else:
        try:
            ctx = build_context(label, etf, perfs.get(etf) or {}, companies, comp_perf, news)
            with st.spinner("Claude sta leggendo le notizie…"):
                summary = get_summary(ctx)
        except Exception as e:
            err = f"Non sono riuscito a generare la sintesi ({type(e).__name__})."

    if summary:
        s = str(summary.get("sentiment", "neutro")).lower()
        bg, fg = sent_colors.get(s, sent_colors["neutro"])
        temi = "".join(f"<li>{html.escape(str(t))}</li>" for t in summary.get("temi", []))
        st.markdown(
            f'<div class="sp-card"><div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap">'
            f'<h3>Cosa si dice sul mercato</h3>'
            f'<span class="sp-sent" style="background:{bg};color:{fg}">Sentiment: {html.escape(s)}</span></div>'
            f'<p style="font-size:15px;line-height:1.6;margin:6px 0 10px">{html.escape(str(summary.get("sintesi", "")))}</p>'
            f'<ul style="margin:0;padding-left:20px;font-size:14px;line-height:1.55;color:#33405A">{temi}</ul>'
            f'<div class="sp-small" style="padding-top:10px">Generato da Claude dai titoli di Yahoo Finance. '
            f'Non è consulenza finanziaria.</div></div>', unsafe_allow_html=True)
    else:
        st.markdown(f'<div class="sp-card"><h3>Cosa si dice sul mercato</h3>'
                    f'<div class="sp-small">{html.escape(err or "")}</div></div>', unsafe_allow_html=True)

    # Notizie
    if news:
        items = ""
        for n in news:
            title = html.escape(n["title"])
            link = html.escape(n["link"] or "#", quote=True)
            items += (f'<div class="item"><a href="{link}" target="_blank" rel="noopener">'
                      f'<div class="t">{title}</div>'
                      f'<div class="m"><span class="sp-chip">{html.escape(n["ticker"])}</span>'
                      f'{html.escape(n["publisher"])} · {time_ago(n["ts"])}</div></a></div>')
        st.markdown(f'<div class="sp-card sp-news"><h3>Ultime notizie</h3>{items}</div>',
                    unsafe_allow_html=True)

st.markdown('<div class="sp-small">Fonte dati: Yahoo Finance tramite libreria non ufficiale, uso personale. '
            'Le informazioni mostrate non costituiscono consulenza finanziaria.</div>',
            unsafe_allow_html=True)
