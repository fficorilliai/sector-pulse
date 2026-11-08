# Sector Pulse

App Streamlit che mostra l'andamento degli 11 settori USA (via ETF SPDR), le aziende principali,
le ultime notizie di Yahoo Finance e una sintesi scritta da Claude.

Avvio in locale:
    pip install -r requirements.txt
    cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # poi inserisci la chiave
    streamlit run app.py

Dati: Yahoo Finance tramite yfinance (non ufficiale, uso personale). Non è consulenza finanziaria.
