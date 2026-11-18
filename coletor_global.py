"""Coletor global (madrugada, pre-mercado e pregao): cotacoes do Yahoo Finance -> global_cotacoes/global_barras.

Fontes (API chart publica do Yahoo, sem chave):
  GET /v8/finance/chart/<simbolo>?interval=5m&range=1d&includePrePost=true
  GET /v8/finance/chart/<simbolo>?interval=60m&range=730d
  GET /v8/finance/chart/TIO=F?interval=1d&range=5d          (minerio: so diario, marcado como lento)

Convencao de tempo do banco: ts_ms = hora de Brasilia codificada como epoch UTC. O Yahoo devolve o epoch
UTC real da barra; a conversao e ts_ms = (epoch_s - 3*3600) * 1000 (o Brasil nao tem horario de verao desde
2019). ts_ms e sempre a hora da ULTIMA BARRA negociada, nunca a hora da consulta.

Sessoes (minutos do dia BRT): ASIA < 07:00, EUROPA 07:00-08:59, US_PRE 09:00-10:29, PREGAO_B3 10:30-18:29,
POS >= 18:30. E so um rotulo de contexto; os retornos de pregao usam a grade propria de cada modulo.

Tabelas:
  global_cotacoes (ts_ms, ativo, ultimo, variacao_pct_ref_b3, sessao, fonte; PK ts_ms, ativo)
  global_barras   (ts_ms, ativo, intervalo, open, high, low, close; PK ts_ms, ativo, intervalo)

variacao_pct_ref_b3 = variacao contra o ultimo preco do ativo ate as 18:00 BRT do pregao B3 anterior
(calendario_b3.pregao_anterior). Sem referencia no banco, fica NULL.

Servico no padrao do supervisor:
  python coletor_global.py GLOBAL <segundos> [--db dados/book.db]
  - consulta a cada 60 s antes das 09:00 e a cada 180 s depois;
  - erro de rede de um ativo nao derruba os outros (loga uma vez por ativo por hora);
  - na partida completa o backfill de 5m dos ultimos 5 dias e atualiza o calendario macro 1x.
"""
import argparse
import sqlite3
import time
from datetime import date, datetime, timedelta
from urllib.parse import quote, urlparse

import requests

import banco
import calendario_b3
import desempenho

D = 86_400_000
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
BASE = "https://query1.finance.yahoo.com/v8/finance/chart/"

# nome curto estavel -> simbolo Yahoo
MAPA = {
    "N225": "^N225", "HSI": "^HSI", "CSI300": "000300.SS",
    "STOXX50": "^STOXX50E", "DAX": "^GDAXI", "FTSE": "^FTSE",
    "ES": "ES=F", "NQ": "NQ=F", "US10Y": "^TNX", "DXY": "DX-Y.NYB",
    "VIX9D": "^VIX9D", "VIX": "^VIX", "VIX3M": "^VIX3M",
    "OURO": "GC=F", "BRENT": "BZ=F", "WTI": "CL=F", "COBRE": "HG=F",
    "USDMXN": "MXN=X", "USDZAR": "ZAR=X", "EWZ": "EWZ",
    "MINERIO": "TIO=F",
}
LENTOS = {"MINERIO"}  # sem barras intradiarias: meta regularMarketPrice/chartPreviousClose, so diario
TOLERANCIA_META = 7 * 86_400_000  # meta com horario muito antigo (TIO=F veio com 2021) nao e usavel

SCHEMA = """
CREATE TABLE IF NOT EXISTS global_cotacoes (
    ts_ms INTEGER NOT NULL,
    ativo TEXT NOT NULL,
    ultimo REAL NOT NULL,
    variacao_pct_ref_b3 REAL,
    sessao TEXT,
    fonte TEXT,
    PRIMARY KEY (ts_ms, ativo));
CREATE TABLE IF NOT EXISTS global_barras (
    ts_ms INTEGER NOT NULL,
    ativo TEXT NOT NULL,
    intervalo TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    PRIMARY KEY (ts_ms, ativo, intervalo));
CREATE INDEX IF NOT EXISTS ix_global_barras_ativo ON global_barras (ativo, ts_ms);
"""

_ultimo_acesso = {}


def _throttle(host, minimo=1.0):
    """Respeita no maximo 1 requisicao por segundo por host."""
    agora = time.monotonic()
    espera = minimo - (agora - _ultimo_acesso.get(host, 0.0))
    if espera > 0:
        time.sleep(espera)
    _ultimo_acesso[host] = time.monotonic()


def _get_json(sessao_http, url):
    """GET JSON com User-Agent de navegador e limite de 1 req/s por host.

    Se query1 responder 429/5xx, tenta o mesmo caminho em query2 (host alternativo autorizado)."""
    alternativo = (url.replace("query1.", "query2.") if "query1." in url
                   else url.replace("query2.", "query1."))
    for i, alvo in enumerate((url, alternativo)):
        _throttle(urlparse(alvo).netloc)
        r = sessao_http.get(alvo, headers=UA, timeout=(5, 25))
        if i == 0 and r.status_code in (429, 500, 502, 503, 504):
            time.sleep(1.0)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("query1/query2 indisponiveis")


def ts_de_epoch(epoch_s):
    """Epoch UTC real (segundos) -> ts_ms na convencao do banco (BRT como epoch UTC)."""
    return (int(epoch_s) - 3 * 3600) * 1000


def sessao(ts_ms):
    """Rotulo de contexto pela hora BRT: ASIA, EUROPA, US_PRE, PREGAO_B3 ou POS."""
    m = (int(ts_ms) % D) // 60_000
    if m < 7 * 60:
        return "ASIA"
    if m < 9 * 60:
        return "EUROPA"
    if m < 10 * 60 + 30:
        return "US_PRE"
    if m < 18 * 60 + 30:
        return "PREGAO_B3"
    return "POS"


def extrair_barras(payload):
    """(meta, [(ts_ms, open, high, low, close, volume)]) de uma resposta chart do Yahoo.

    Barras sem close sao descartadas (o includePrePost traz linhas vazias). open/high/low ausentes
    repetem o close. Nao usa rede: e o parser testado com as fixtures gravadas."""
    chart = (payload or {}).get("chart", {})
    resultados = chart.get("result") or []
    if not resultados:
        erro = chart.get("error") or {}
        raise ValueError(f"Yahoo sem resultado: {erro.get('description') or erro or 'desconhecido'}")
    r = resultados[0]
    meta = r.get("meta", {})
    stamps = r.get("timestamp") or []
    quote_ = ((r.get("indicators") or {}).get("quote") or [{}])[0]
    colunas = {k: quote_.get(k) or [] for k in ("open", "high", "low", "close", "volume")}
    barras = []
    for i, epoch_s in enumerate(stamps):
        close = colunas["close"][i] if i < len(colunas["close"]) else None
        if epoch_s is None or close is None:
            continue
        def _v(nome):
            v = colunas[nome][i] if i < len(colunas[nome]) else None
            return float(v) if v is not None else float(close)
        volume = colunas["volume"][i] if i < len(colunas["volume"]) else None
        barras.append((ts_de_epoch(epoch_s), _v("open"), _v("high"), _v("low"), float(close),
                       float(volume) if volume is not None else None))
    return meta, barras


def baixar(http, simbolo, intervalo="5m", range_="1d"):
    """Baixa e converte uma serie do Yahoo -> (meta, barras). Faz uma requisicao."""
    url = f"{BASE}{quote(simbolo, safe='')}"
    params = f"?interval={intervalo}&range={range_}"
    if intervalo != "1d":
        params += "&includePrePost=true"
    return extrair_barras(_get_json(http, url + params))


def consultar_ativo(http, ativo):
    """Cotacao atual de um ativo -> dict com ativo, ts_ms, ultimo, intervalo, barras e meta.

    Intraday: ultima barra de 5m. MINERIO (lento): usa o meta diario regularMarketPrice quando o
    regularMarketTime for coerente com a ultima barra; se o meta estiver velho (TIO=F ja veio com
    2021), vale a ultima barra diaria negociada."""
    simbolo = MAPA[ativo]
    if ativo in LENTOS:
        meta, barras = baixar(http, simbolo, "1d", "5d")
        if not barras and meta.get("regularMarketPrice") is None:
            raise ValueError("minerio sem barra nem regularMarketPrice")
        ts_bar = barras[-1][0] if barras else None
        close_bar = barras[-1][4] if barras else None
        ts_meta = ts_de_epoch(meta["regularMarketTime"]) if meta.get("regularMarketTime") is not None else None
        preco_meta = meta.get("regularMarketPrice")
        # o meta do TIO=F ja veio com horario de 2021 e preco de outro contrato: so usa o meta se o
        # horario for coerente com a ultima barra diaria; caso contrario vale a barra negociada.
        meta_ok = (ts_meta is not None and preco_meta is not None
                   and (ts_bar is None or abs(int(ts_meta) - int(ts_bar)) <= TOLERANCIA_META))
        if meta_ok:
            ts_ms, ultimo = int(ts_meta), float(preco_meta)
        elif ts_bar is not None and close_bar is not None:
            ts_ms, ultimo = int(ts_bar), float(close_bar)
        elif ts_meta is not None and preco_meta is not None:
            ts_ms, ultimo = int(ts_meta), float(preco_meta)
        else:
            raise ValueError("minerio sem horario/preco utilizavel")
        return {"ativo": ativo, "ts_ms": int(ts_ms), "ultimo": float(ultimo), "intervalo": "1d",
                "barras": barras, "meta": meta}
    meta, barras = baixar(http, simbolo, "5m", "1d")
    if not barras:
        preco = meta.get("regularMarketPrice")
        if preco is None:
            raise ValueError("sem barra de 5m")
        ts_ms = ts_de_epoch(meta.get("regularMarketTime") or time.time())
        return {"ativo": ativo, "ts_ms": int(ts_ms), "ultimo": float(preco), "intervalo": "5m",
                "barras": [], "meta": meta}
    ts_ms, _o, _h, _l, close, _v = barras[-1]
    return {"ativo": ativo, "ts_ms": int(ts_ms), "ultimo": float(close), "intervalo": "5m",
            "barras": barras, "meta": meta}


def _dia_idx(d):
    if isinstance(d, int):
        return d
    if isinstance(d, datetime):
        d = d.date()
    if isinstance(d, str):
        d = date.fromisoformat(d[:10])
    return (d - date(1970, 1, 1)).days


def referencia_pregao_anterior(con, ativo, dia_idx):
    """Ultimo close do ativo ate as 18:00 BRT do pregao B3 anterior a `dia_idx` (ou None)."""
    try:
        prev = calendario_b3.pregao_anterior(date(1970, 1, 1) + timedelta(days=int(dia_idx)))
    except Exception:
        prev = date(1970, 1, 1) + timedelta(days=int(dia_idx) - 1)
    corte = _dia_idx(prev) * D + 18 * 3_600_000
    try:
        row = con.execute("SELECT close FROM global_barras WHERE ativo=? AND close IS NOT NULL AND ts_ms<=? "
                          "ORDER BY ts_ms DESC LIMIT 1", (ativo, corte)).fetchone()
    except sqlite3.OperationalError:
        return None
    return float(row[0]) if row and row[0] is not None and row[0] > 0 else None


def variacao(ultimo, referencia):
    """Variacao percentual contra a referencia; None sem referencia valida."""
    if referencia is None or referencia <= 0:
        return None
    return (float(ultimo) / referencia - 1.0) * 100.0


def gravar(db, cot, ref=None):
    """Grava a cotacao (so quando muda: mesma PK ts_ms/ativo e ignorada) e as barras do dia.

    -> True se a linha de global_cotacoes entrou agora."""
    ts_ms, ativo = int(cot["ts_ms"]), cot["ativo"]
    existe = db.execute("SELECT 1 FROM global_cotacoes WHERE ts_ms=? AND ativo=?", (ts_ms, ativo)).fetchone()
    if not existe:
        db.execute("INSERT OR REPLACE INTO global_cotacoes VALUES (?,?,?,?,?,?)",
                   (ts_ms, ativo, float(cot["ultimo"]), variacao(cot["ultimo"], ref), sessao(ts_ms),
                    "yahoo:" + cot["intervalo"]))
    for b in cot.get("barras") or []:
        db.execute("INSERT OR IGNORE INTO global_barras VALUES (?,?,?,?,?,?,?)",
                   (int(b[0]), ativo, cot["intervalo"], b[1], b[2], b[3], b[4]))
    db.commit()
    return not existe

def backfill(db, intervalo="60m", range_="730d", fonte=None):
    """Preenche global_barras com o historico do Yahoo; idempotente (INSERT OR IGNORE).

    `fonte(ativo, intervalo, range_) -> [(ts_ms, o, h, l, c, v)]` existe para o teste rodar sem rede.
    ATIVOS lentos (minerio) so entram no intervalo diario. -> numero de barras novas."""
    if fonte is None:
        http = requests.Session()

        def fonte(ativo, inter, rng):
            return baixar(http, MAPA[ativo], inter, rng)[1]
    novos = 0
    for ativo in MAPA:
        if ativo in LENTOS and intervalo != "1d":
            continue
        if ativo not in LENTOS and intervalo == "1d":
            continue
        try:
            barras = fonte(ativo, intervalo, range_)
        except Exception:
            continue
        antes = db.total_changes
        db.executemany("INSERT OR IGNORE INTO global_barras VALUES (?,?,?,?,?,?,?)",
                       [(int(b[0]), ativo, intervalo, b[1], b[2], b[3], b[4]) for b in barras])
        db.commit()
        novos += db.total_changes - antes
    return novos


def _intervalo_servico(agora=None):
    agora = agora or datetime.now()
    return 60.0 if agora.hour * 60 + agora.minute < 9 * 60 else 180.0


def main():
    ap = argparse.ArgumentParser(description="Coletor global Yahoo Finance -> global_cotacoes/global_barras")
    ap.add_argument("grupo", nargs="?", default="GLOBAL")
    ap.add_argument("segundos", nargs="?", type=float, default=60.0)
    ap.add_argument("--db", default="dados/book.db")
    args = ap.parse_args()

    desempenho.sem_modo_eficiencia()
    db = banco.abrir(args.db, SCHEMA)
    print(f"Global ({len(MAPA)} ativos) -> {args.db}. Ctrl+C para parar.", flush=True)

    try:  # calendario macro: fontes (a) e (b) 1x por dia na partida, sem derrubar o coletor
        import calendario_macro
        calendario_macro.atualizar_fontes(args.db)
    except Exception as e:
        print(f"aviso: calendario macro nao atualizado ({e.__class__.__name__}: {e})", flush=True)

    try:
        print(f"backfill 5m ultimos 5 dias: {backfill(db, '5m', '5d')} barras novas", flush=True)
    except Exception as e:
        print(f"aviso: backfill 5m falhou ({e.__class__.__name__}: {e})", flush=True)

    http = requests.Session()
    fim = time.time() + args.segundos if args.segundos > 0 else float("inf")
    erros = {}
    n = 0
    while time.time() < fim:
        t0 = time.time()
        for ativo in MAPA:
            try:
                cot = consultar_ativo(http, ativo)
                dia = cot["ts_ms"] // D
                ref = referencia_pregao_anterior(db, ativo, dia)
                if gravar(db, cot, ref):
                    n += 1
                    print(f"[{datetime.now():%H:%M:%S}] {ativo:8s} {cot['ultimo']:.4g} "
                          f"({sessao(cot['ts_ms'])})", flush=True)
            except sqlite3.OperationalError as e:  # banco ocupado: tenta no proximo ciclo
                db.rollback()
                print(f"aviso: gravacao adiada ({e})", flush=True)
            except Exception as e:
                agora = time.time()
                if agora - erros.get(ativo, 0) >= 3600:  # 1 aviso por ativo por hora
                    erros[ativo] = agora
                    print(f"aviso: {ativo} nao atualizado ({e.__class__.__name__}: {e})", flush=True)
        espera = _intervalo_servico() - (time.time() - t0)
        time.sleep(max(5.0, espera))
    db.close()
    print(f"Linhas gravadas: {n}", flush=True)


if __name__ == "__main__":
    main()
