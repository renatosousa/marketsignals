"""Posicao em aberto das opcoes de BOVA11 (coberto/travado/descoberto, titulares/lancadores) direto
da B3 -> SQLite, substituindo a colagem manual (opcoes_manual.py) como fonte do painel de posicao.

Fontes (portal publico do BDI - Boletim Diario do Mercado, https://arquivos.b3.com.br/bdi/):
  OpenPositionsEquities       posicoes em aberto por serie (D-1). Baixada pela exportacao CSV do
                              portal (POST /bdi/table/export/csv), que traz a tabela inteira de uma
                              vez (~45 mil linhas, ~3 min) -- mas o gateway corta em 240 s, e em
                              07/10/2026 todas as tentativas morreram em 499/504; nesse caso o
                              script cai sozinho para a paginacao JSON ordenada (?sort=TckrSymb),
                              que vem completa e so le as paginas BOVA* (--metodo paginas forca).
  InstrumentsEquities         cadastro: strike (ExrcPric), vencimento (XprtnDt), call/put.
  ConsolidatedTradesEquities  fechamento/negocios/volume das series e do BOVA11 (opcional: sem ele
                              nao ha spot, IV nem delta, mas a posicao em aberto e gravada igual).
Endpoints NAO documentados (descobertos no JS do portal): podem mudar sem aviso.
Obs.: a paginacao JSON SEM ordenacao repete/omite linhas (era o problema do posicoes_b3.py).

Grava em tabelas proprias (b3_posicoes_series / b3_posicoes_resumo), com as mesmas colunas das
opcoes_manual_* + sessao/ts_ms; o resumo por vencimento usa opcoes_manual.resumo() (mesmas metricas),
calculado so com strikes a +-25% do spot (--faixa): a B3 traz todas as series e puts muito fora do
dinheiro com grande posicao descoberta (strike 100 com spot 200) dominariam suporte/ima.
A tabela de series guarda todas as series de todos os vencimentos.
Idempotente: regravar a mesma sessao apaga e reinsere as linhas dela numa transacao curta.
ts_ms = data do pregao 00:00 em hora de Brasilia codificada como epoch "UTC" (convencao do banco);
capturado_em = hora local (Brasilia) do download, ISO.

IV/delta: Black-Scholes sobre o preco de FECHAMENTO da serie (so series com >= --min-negocios
negocios no dia), spot = fechamento do BOVA11, taxa = Selic meta do dia (BCB SGS 432), prazo em
dias uteis/252 sem feriados (mesmo criterio do coletor_opcoes.py). E aproximado: o fechamento da
opcao e o do BOVA11 nao sao simultaneos.

Uso: python posicoes_b3_csv.py [AAAA-MM-DD] [--db dados/book.db] [--subjacente BOVA11]
                               [--metodo csv|paginas] [--sem-fallback] [--faixa 0.25]
                               [--log dados/posicoes_b3_csv.log]
     sem data: ultimo pregao concluido (consulta o calendario do proprio BDI).
Saida: 0 ok | 1 erro | 2 dados da sessao ainda nao publicados (tentar mais tarde).
"""
import argparse
import csv
import io
import json
import logging
import math
import sqlite3
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

import banco
import opcoes_manual as om

AQUI = Path(__file__).parent
BDI = "https://arquivos.b3.com.br/bdi"
UA = {"User-Agent": "Mozilla/5.0 (posicoes_b3_csv; uso pessoal)", "Accept": "application/json, text/csv, */*"}
TIMEOUT_CSV = (30, 480)    # (conexao, leitura) - a exportacao completa leva ~3 min
TIMEOUT_JSON = (30, 120)
TENTATIVAS = 4
TENTATIVAS_CSV = 1         # a exportacao leva 3-4 min e o gateway da B3 corta em 240 s (499/504);
                           # em vez de repetir, cai para a paginacao ordenada (que tem retry/backoff)
ESPERA = (10, 30, 90)      # backoff entre tentativas (s)
PAGINA = 1000
log = logging.getLogger("posicoes_b3_csv")

SCHEMA = """
CREATE TABLE IF NOT EXISTS b3_posicoes_series (
    sessao TEXT NOT NULL, ts_ms INTEGER NOT NULL, capturado_em TEXT NOT NULL, fonte TEXT NOT NULL,
    subjacente TEXT NOT NULL, symbol TEXT NOT NULL, isin TEXT,
    vencimento TEXT, dias_uteis INTEGER, tipo TEXT, strike REAL, situacao TEXT, dist_pct REAL,
    ultimo REAL, var_pct REAL, num_neg INTEGER, vol_financeiro REAL, vol_impl_pct REAL,
    delta REAL, gamma REAL, theta_cifra REAL, theta_pct REAL, vega REAL,
    coberto REAL, travado REAL, descoberto REAL, titulares REAL, lancadores REAL,
    total_pos REAL, estilo TEXT,
    PRIMARY KEY (sessao, subjacente, symbol)
);
CREATE INDEX IF NOT EXISTS ix_b3pos_series_ts ON b3_posicoes_series (subjacente, ts_ms);
CREATE TABLE IF NOT EXISTS b3_posicoes_resumo (
    sessao TEXT NOT NULL, ts_ms INTEGER NOT NULL, capturado_em TEXT NOT NULL, fonte TEXT NOT NULL,
    subjacente TEXT NOT NULL, vencimento TEXT NOT NULL, spot REAL, n_series INTEGER,
    oi_call REAL, oi_put REAL,
    atm_iv REAL, rr25 REAL, strike_ima REAL, concentracao_ima REAL, suporte REAL, resistencia REAL,
    PRIMARY KEY (sessao, subjacente, vencimento)
);
CREATE INDEX IF NOT EXISTS ix_b3pos_resumo_ts ON b3_posicoes_resumo (subjacente, ts_ms);
"""

# colunas da OpenPositionsEquities, na ordem do portal (nome interno -> nomes aceitos no cabecalho CSV)
COLS_OI = [
    ("symbol", "TckrSymb", "Ticker symbol", "Instrumento financeiro"),
    ("isin", "ISIN", "ISIN code", "Código ISIN"),
    ("ativo", "Asst", "Asset", "Ativo"),
    ("xprtn_cd", "XprtnCd", "Expiration code", "Código de expiração"),
    ("segmento", "SgmtNm", "Segment", "Segmento"),
    ("opn_intrst", "OpnIntrst", "Open interest", "Contratos em aberto"),
    ("var_opn_intrst", "VartnOpnIntrst", "Variation open interest", "Variação de contratos em aberto"),
    ("distribuicao_id", "DstrbtnId", "Distribution identification", "Identificador da distribuição"),
    ("coberto", "CvrdQty", "Covered quantity", "Quantidade coberta"),
    ("travado", "TtlBlckdPos", "Total blocked position", "Total de posições bloqueadas"),
    ("descoberto", "UcvrdQty", "Uncovered quantity", "Quantidade descoberta"),
    ("total_pos", "TtlPos", "Total position", "Total de posições"),
    ("titulares", "BrrwrQty", "Borrower quantity", "Quantidade de tomadores"),
    ("lancadores", "LndrQty", "Lender quantity", "Quantidade de doadores"),
    ("cur_qty", "CurQty", "Current quantity", "Quantidade atual"),
    ("locked_qty", "LockedQty", "Commodities locked qty", "Contratos travados"),
    ("unlocked_qty", "UnlockedQty", "Unlocked qty by transfer", "Contratos baixados por transferência"),
    ("fwd_pric", "FwdPric", "Forward price", "Preço a termo"),
]
NUM_OI = {"opn_intrst", "var_opn_intrst", "coberto", "travado", "descoberto", "total_pos", "titulares",
          "lancadores", "cur_qty", "locked_qty", "unlocked_qty", "fwd_pric"}


class NaoPublicado(Exception):
    """A B3 ainda nao publicou a tabela para a sessao pedida."""


# ------------------------------------------------------------------ utilitarios
def agora_br():
    """Hora de Brasilia (UTC-3, sem horario de verao desde 2019), independente do fuso do PC."""
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=-3))).replace(tzinfo=None)


def ms_br(dt):
    """datetime 'ingenuo' em hora de Brasilia -> epoch ms 'UTC' (convencao do banco)."""
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


def num_br(v):
    """Numero do CSV da B3: '8.000.000' -> 8e6; '203,61' -> 203.61; '1.234,5' -> 1234.5;
    '-', '', None -> NaN. Aceita tambem formato en ('1,234.5') e numeros ja tipados (JSON)."""
    if v is None:
        return np.nan
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("R$", "").replace("\u00a0", "").replace(" ", "")
    if s in ("", "-", "—", "–", "null", "None"):
        return np.nan
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):          # pt: 1.234,56
            s = s.replace(".", "").replace(",", ".")
        else:                                    # en: 1,234.56
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")                  # pt: 203,61
    elif s.count(".") > 1 or (s.count(".") == 1 and len(s.split(".")[1]) == 3 and s.split(".")[0].lstrip("-").isdigit()):
        s = s.replace(".", "")                   # pt milhar: 3.300 / 8.000.000 (quantidades sao inteiras)
    try:
        return float(s)
    except ValueError:
        return np.nan


def _norm(s):
    return om._norm(s or "")


RETENTAVEIS = {408, 425, 429, 499}  # 499: o gateway da B3 corta requisicoes longas (~240 s)


def _requisicao(metodo, url, timeout, tentativas=TENTATIVAS, **kw):
    """HTTP com tentativas e backoff. Erros 4xx definitivos (404, 400...) nao sao repetidos."""
    ultimo = None
    for i in range(tentativas):
        try:
            r = requests.request(metodo, url, headers=UA, timeout=timeout, **kw)
            if r.status_code < 400:
                return r
            r.raise_for_status()
        except requests.HTTPError as e:
            cod = e.response.status_code if e.response is not None else 0
            if 400 <= cod < 500 and cod not in RETENTAVEIS:
                raise
            ultimo = e
        except (requests.ConnectionError, requests.Timeout) as e:
            ultimo = e
        if i < tentativas - 1:
            espera = ESPERA[min(i, len(ESPERA) - 1)]
            log.warning("falha em %s (%s: %s); nova tentativa em %ss", url.split("?")[0], ultimo.__class__.__name__,
                        ultimo, espera)
            time.sleep(espera)
    raise ultimo


# ------------------------------------------------------------------ sessao
def ultima_sessao():
    """Ultimo pregao concluido segundo o calendario do BDI (considera feriados da B3).
    Ex.: consultado em 07/10 -> 06/10; numa segunda apos feriado -> a sexta anterior.
    Sem o portal: dia util anterior (seg-sex), sem feriados."""
    hoje = agora_br().date()
    try:
        r = _requisicao("GET", f"{BDI}/table/workday?date={hoje:%Y-%m-%d}", TIMEOUT_JSON)
        d = date.fromisoformat(r.json()[:10])
        return d if d < hoje else d - timedelta(days=1 if d.weekday() else 3)
    except Exception as e:
        log.warning("calendario do BDI indisponivel (%s); usando o dia util anterior", e)
        d = hoje - timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        return d


# ------------------------------------------------------------------ OpenPositionsEquities
def _decodificar(raw):
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


def parsear_csv_posicoes(texto):
    """CSV exportado pelo BDI -> DataFrame com as colunas internas de COLS_OI.
    O arquivo traz linhas de descricao antes do cabecalho e 'Nenhum resultado' quando vazio."""
    if "nenhum resultado" in texto[:1000].lower() or not texto.strip():
        return pd.DataFrame(columns=[c[0] for c in COLS_OI])
    linhas = texto.splitlines()
    conhecidos = {_norm(n) for c in COLS_OI for n in c[1:]}
    i_cab = None
    for i, l in enumerate(linhas[:30]):
        partes = [_norm(p) for p in (l.split(";") if l.count(";") >= l.count(",") else l.split(","))]
        if len(partes) >= 10 and sum(p in conhecidos for p in partes) >= 8:
            i_cab = i
            break
    if i_cab is None:
        raise ValueError(f"cabecalho do CSV nao reconhecido; inicio do arquivo: {texto[:300]!r}")
    sep = ";" if linhas[i_cab].count(";") >= linhas[i_cab].count(",") else ","
    corpo = "\n".join(linhas[i_cab:])
    leitor = csv.reader(io.StringIO(corpo), delimiter=sep, quotechar='"')
    cab = next(leitor)
    alvo = {}
    for j, h in enumerate(cab):
        n = _norm(h)
        for c in COLS_OI:
            if n in {_norm(x) for x in c[1:]}:
                alvo[j] = c[0]
                break
    if "symbol" not in alvo.values() or "descoberto" not in alvo.values():
        if len(cab) == len(COLS_OI):  # nomes mudaram mas a ordem e a do portal
            log.warning("cabecalho com nomes desconhecidos; mapeando por posicao: %s", cab)
            alvo = {j: c[0] for j, c in enumerate(COLS_OI)}
        else:
            raise ValueError(f"colunas essenciais ausentes no CSV: {cab}")
    regs = []
    for cel in leitor:
        if not cel or (len(cel) == 1 and not cel[0].strip()):
            continue
        if len(cel) < len(alvo) // 2:  # 'Nenhum resultado', rodape etc.
            continue
        regs.append({alvo[j]: v.strip() for j, v in enumerate(cel) if j in alvo})
    df = pd.DataFrame(regs, columns=[c[0] for c in COLS_OI])
    for c in NUM_OI:
        df[c] = df[c].map(num_br)
    return df


def posicoes_csv(sessao):
    payload = {"Name": "OpenPositionsEquities", "Date": f"{sessao:%Y-%m-%d}", "FinalDate": f"{sessao:%Y-%m-%d}",
               "ClientId": "", "Filters": {}}
    t0 = time.time()
    r = _requisicao("POST", f"{BDI}/table/export/csv?lang=pt", TIMEOUT_CSV, tentativas=TENTATIVAS_CSV, json=payload)
    texto = _decodificar(r.content)
    df = parsear_csv_posicoes(texto)
    log.info("CSV OpenPositionsEquities %s: %d bytes, %d linhas, %.0fs", sessao, len(r.content), len(df),
             time.time() - t0)
    return df


def _pagina(tabela, sessao, p, tam=PAGINA):
    d = f"{sessao:%Y-%m-%d}"
    r = _requisicao("POST", f"{BDI}/table/{tabela}/{d}/{d}/{p}/{tam}?sort=TckrSymb", TIMEOUT_JSON, json={})
    if r.status_code == 204 or not r.content:
        return {"pageCount": 0, "values": [], "columns": []}
    j = r.json()
    t = j["table"]
    t["_atualizado"] = j.get("lastUpdateDate")  # hora (Brasilia) em que a B3 publicou/atualizou a tabela
    return t


def _df_tabela(t, linhas):
    nomes = [c["name"] for c in t["columns"]]
    return pd.DataFrame([l[:len(nomes)] for l in linhas], columns=nomes)


def paginas_prefixo(tabela, sessao, prefixo):
    """Linhas de `tabela` cujo TckrSymb comeca com `prefixo`, via paginacao ordenada por TckrSymb:
    busca binaria da 1a pagina que alcanca o prefixo e leitura sequencial ate passar dele.
    -> DataFrame com os nomes de coluna do portal (vazio se a tabela da sessao nao existe)."""
    cache = {}

    def pag(p):
        if p not in cache:
            cache[p] = _pagina(tabela, sessao, p)
        return cache[p]

    t1 = pag(1)
    n = t1["pageCount"]
    if not n:
        return pd.DataFrame()
    log.info("%s %s: %d paginas, publicada pela B3 em %s", tabela, sessao, n, t1.get("_atualizado"))
    i_sym = [c["name"] for c in t1["columns"]].index("TckrSymb")

    def simb(v):
        return str(v[i_sym] or "")

    lo, hi = 1, n
    while lo < hi:  # 1a pagina cujo ultimo simbolo >= prefixo
        mid = (lo + hi) // 2
        if simb(pag(mid)["values"][-1]) < prefixo:
            lo = mid + 1
        else:
            hi = mid
    linhas = []
    for p in range(max(1, lo - 1), n + 1):  # 1 pagina de folga p/ diferencas de collation
        vals = pag(p)["values"]
        linhas += [v for v in vals if simb(v).startswith(prefixo)]
        if vals and simb(vals[-1]) > prefixo and not simb(vals[-1]).startswith(prefixo):
            break
    return _df_tabela(t1, linhas).drop_duplicates(subset=["TckrSymb"])


def posicoes_paginas(sessao, prefixo):
    t0 = time.time()
    df = paginas_prefixo("OpenPositionsEquities", sessao, prefixo)
    if df.empty:
        return pd.DataFrame(columns=[c[0] for c in COLS_OI])
    df = df.rename(columns={c[1]: c[0] for c in COLS_OI})[[c[0] for c in COLS_OI]]
    for c in NUM_OI:
        df[c] = df[c].map(num_br)
    log.info("paginas OpenPositionsEquities %s (prefixo %s): %d linhas, %.0fs", sessao, prefixo, len(df),
             time.time() - t0)
    return df


# ------------------------------------------------------------------ cadastro, negocios, taxa
def cadastro(sessao, subjacente, prefixo):
    df = paginas_prefixo("InstrumentsEquities", sessao, prefixo)
    if df.empty:
        raise NaoPublicado(f"cadastro de instrumentos de {sessao} vazio")
    df = df[(df["Asst"] == subjacente) & (df["SctyCtgyNm"] == "OPTION ON EQUITIES")]
    out = pd.DataFrame({
        "symbol": df["TckrSymb"],
        "vencimento": df["XprtnDt"].astype(str).str[:10],
        "tipo": df["OptnTp"].astype(str).str[0].str.upper().map({"C": "C", "P": "P"}),
        "strike": df["ExrcPric"].map(num_br),
        "estilo": df["OptnStyle"],
    })
    log.info("cadastro %s: %d series de opcoes de %s", sessao, len(out), subjacente)
    return out.drop_duplicates("symbol")


def negocios(sessao, prefixo):
    try:
        df = paginas_prefixo("ConsolidatedTradesEquities", sessao, prefixo)
    except Exception as e:
        log.warning("negocios consolidados indisponiveis (%s); seguindo sem spot/IV", e)
        return pd.DataFrame(columns=["symbol", "ultimo", "num_neg", "vol_financeiro", "osc"])
    if df.empty:
        log.warning("negocios consolidados de %s vazios; seguindo sem spot/IV", sessao)
        return pd.DataFrame(columns=["symbol", "ultimo", "num_neg", "vol_financeiro", "osc"])
    return pd.DataFrame({"symbol": df["TckrSymb"], "ultimo": df["LastPric"].map(num_br),
                         "num_neg": df["TradQty"].map(num_br), "vol_financeiro": df["NtlFinVol"].map(num_br),
                         "osc": df["Osc"].map(num_br)}).drop_duplicates("symbol")


def taxa_selic(sessao, padrao=0.15):
    """Selic meta (BCB SGS 432) vigente na sessao, como taxa continua."""
    d = f"{sessao:%d/%m/%Y}"
    url = f"https://api.bcb.gov.br/dados/serie/bcdata.sgs.432/dados?formato=json&dataInicial={d}&dataFinal={d}"
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            selic = float(json.load(r)[-1]["valor"].replace(",", ".")) / 100
        return math.log(1 + selic), selic
    except Exception as e:
        log.warning("Selic do BCB indisponivel (%s); usando %.2f%%", e.__class__.__name__, padrao * 100)
        return math.log(1 + padrao), padrao


def _ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs(tipo, s, k, t, r, vol):
    d1 = (math.log(s / k) + (r + 0.5 * vol * vol) * t) / (vol * math.sqrt(t))
    d2 = d1 - vol * math.sqrt(t)
    if tipo == "C":
        return s * _ncdf(d1) - k * math.exp(-r * t) * _ncdf(d2), _ncdf(d1)
    return k * math.exp(-r * t) * _ncdf(-d2) - s * _ncdf(-d1), _ncdf(d1) - 1.0


def iv_delta(tipo, preco, s, k, t, r):
    """Bissecao em [1%, 300%] (mesmo metodo do coletor_opcoes). -> (iv, delta) ou (None, None)."""
    if not all(x and x > 0 for x in (preco, s, k, t)):
        return None, None
    intr = max(0.0, s - k * math.exp(-r * t)) if tipo == "C" else max(0.0, k * math.exp(-r * t) - s)
    if preco <= intr + 1e-6:
        return None, None
    lo, hi = 0.01, 3.0
    if not (_bs(tipo, s, k, t, r, lo)[0] <= preco <= _bs(tipo, s, k, t, r, hi)[0]):
        return None, None
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if _bs(tipo, s, k, t, r, mid)[0] > preco:
            hi = mid
        else:
            lo = mid
    iv = 0.5 * (lo + hi)
    return iv, _bs(tipo, s, k, t, r, iv)[1]


# ------------------------------------------------------------------ montagem
def montar(sessao, subjacente, metodo, min_negocios, fallback=True):
    prefixo = subjacente[:4]
    usado = metodo
    if metodo == "csv":
        try:
            oi = posicoes_csv(sessao)
        except Exception as e:
            if not fallback:
                raise
            log.warning("exportacao CSV falhou (%s: %s); usando a paginacao ordenada", e.__class__.__name__, e)
            oi, usado = posicoes_paginas(sessao, prefixo), "paginas"
    else:
        oi = posicoes_paginas(sessao, prefixo)
    if oi.empty:
        raise NaoPublicado(f"OpenPositionsEquities de {sessao} vazia (ainda nao publicada?)")
    oi = oi[(oi["ativo"].str.upper() == prefixo) & oi["segmento"].str.upper().isin(["EQUITY CALL", "EQUITY PUT"])]
    oi = oi.drop_duplicates("symbol").copy()
    qtd = ["coberto", "travado", "descoberto", "total_pos", "titulares", "lancadores"]
    oi[qtd] = oi[qtd].fillna(0.0)  # CSV traz vazio onde o JSON traz 0
    if oi.empty:
        raise ValueError(f"nenhuma serie de opcao de {prefixo} na posicao em aberto de {sessao}")
    cad = cadastro(sessao, subjacente, prefixo)
    neg = negocios(sessao, prefixo)
    df = oi.merge(cad, on="symbol", how="left").merge(neg, on="symbol", how="left")
    sem_cad = df["strike"].isna().sum()
    if sem_cad:
        log.warning("%d series sem cadastro (sem strike/vencimento): %s", sem_cad,
                    ", ".join(df.loc[df["strike"].isna(), "symbol"].head(10)))
    tipo_seg = df["segmento"].str.upper().map({"EQUITY CALL": "C", "EQUITY PUT": "P"})
    df["tipo"] = df["tipo"].fillna(tipo_seg)

    spot = neg.loc[neg["symbol"] == subjacente, "ultimo"]
    spot = float(spot.iloc[0]) if len(spot) and pd.notna(spot.iloc[0]) else None
    r, selic = taxa_selic(sessao)
    venc = pd.to_datetime(df["vencimento"], errors="coerce").dt.date
    df["dias_uteis"] = [int(np.busday_count(sessao + timedelta(days=1), v + timedelta(days=1)))
                        if isinstance(v, date) and v > sessao else 0 for v in venc]
    if spot:
        df["dist_pct"] = df["strike"] / spot - 1
        itm = np.where(df["tipo"] == "C", df["strike"] < spot, df["strike"] > spot)
        df["situacao"] = np.where(df["dist_pct"].abs() <= 0.01, "ATM", np.where(itm, "ITM", "OTM"))
    else:
        df["dist_pct"], df["situacao"] = np.nan, None
    ivs, deltas = [], []
    for x in df.itertuples():
        ok = spot and x.dias_uteis > 0 and pd.notna(x.ultimo) and pd.notna(x.num_neg) and x.num_neg >= min_negocios
        iv, de = iv_delta(x.tipo, float(x.ultimo), spot, float(x.strike), x.dias_uteis / 252, r) if ok else (None, None)
        ivs.append(iv)
        deltas.append(de)
    df["vol_impl_pct"] = pd.Series(ivs, index=df.index, dtype=float)
    df["delta"] = pd.Series(deltas, index=df.index, dtype=float)
    df["var_pct"] = df["osc"] / 100 if "osc" in df else np.nan
    df["subjacente"] = subjacente
    info = dict(metodo=usado, spot=spot, selic=selic, n_iv=int(df["vol_impl_pct"].notna().sum()))
    return df, info


def gravar(db_path, sessao, subjacente, df, resumos, spot, fonte):
    capturado = agora_br().isoformat(timespec="seconds")
    ts = ms_br(datetime(sessao.year, sessao.month, sessao.day))
    cols = ["symbol", "isin", "vencimento", "dias_uteis", "tipo", "strike", "situacao", "dist_pct", "ultimo",
            "var_pct", "num_neg", "vol_financeiro", "vol_impl_pct", "delta", "gamma", "theta_cifra", "theta_pct",
            "vega", "coberto", "travado", "descoberto", "titulares", "lancadores", "total_pos", "estilo"]

    def limpo(v):
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return None
        return v.item() if isinstance(v, np.generic) else v

    linhas = [(f"{sessao}", ts, capturado, fonte, subjacente, *(limpo(rec.get(c)) for c in cols))
              for rec in df.to_dict("records")]
    res = []
    for venc, r in resumos.items():
        g = df[df["vencimento"] == venc]
        oi = g[["coberto", "travado", "descoberto"]].sum(axis=1, min_count=1)
        res.append((f"{sessao}", ts, capturado, fonte, subjacente, venc, spot, len(g),
                    limpo(float(oi[g["tipo"] == "C"].sum())), limpo(float(oi[g["tipo"] == "P"].sum())),
                    r["atm_iv"], r["rr25"], r["strike_ima"], r["concentracao_ima"], r["suporte"], r["resistencia"]))

    db = banco.abrir(str(db_path), SCHEMA)  # WAL + cria tabelas (IF NOT EXISTS); timeout 30 s
    try:
        db.execute("PRAGMA busy_timeout=30000")
        for i in range(10):  # transacao curta; se outro coletor estiver gravando, espera e tenta de novo
            try:
                db.execute("BEGIN IMMEDIATE")
                db.execute("DELETE FROM b3_posicoes_series WHERE sessao=? AND subjacente=?", (f"{sessao}", subjacente))
                db.execute("DELETE FROM b3_posicoes_resumo WHERE sessao=? AND subjacente=?", (f"{sessao}", subjacente))
                db.executemany(f"INSERT INTO b3_posicoes_series (sessao, ts_ms, capturado_em, fonte, subjacente, "
                               f"{', '.join(cols)}) VALUES ({','.join('?' * (5 + len(cols)))})", linhas)
                db.executemany("INSERT INTO b3_posicoes_resumo (sessao, ts_ms, capturado_em, fonte, subjacente, "
                               "vencimento, spot, n_series, oi_call, oi_put, atm_iv, rr25, strike_ima, "
                               "concentracao_ima, suporte, resistencia) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", res)
                db.commit()
                break
            except sqlite3.OperationalError as e:
                db.rollback()
                if "locked" not in str(e) and "busy" not in str(e) or i == 9:
                    raise
                log.warning("banco ocupado (%s); tentando de novo", e)
                time.sleep(2 + i)
    finally:
        db.close()
    return len(linhas), len(res), capturado


def configurar_log(caminho):
    Path(caminho).parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    log.setLevel(logging.INFO)
    hs = [logging.FileHandler(caminho, encoding="utf-8")]
    if sys.stdout is not None:  # pythonw.exe (tarefa agendada sem janela) nao tem stdout
        hs.append(logging.StreamHandler(sys.stdout))
    for h in hs:
        h.setFormatter(fmt)
        log.addHandler(h)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("sessao", nargs="?", help="AAAA-MM-DD (padrao: ultimo pregao concluido)")
    ap.add_argument("--db", default=str(AQUI / "dados" / "book.db"))
    ap.add_argument("--subjacente", default="BOVA11")
    ap.add_argument("--metodo", choices=["csv", "paginas"], default="csv",
                    help="csv: exportacao completa (~3 min, padrao); paginas: paginacao ordenada (~1 min)")
    ap.add_argument("--sem-fallback", action="store_true", help="nao cair para a paginacao se o CSV falhar")
    ap.add_argument("--min-negocios", type=int, default=5, help="minimo de negocios no dia p/ calcular IV")
    ap.add_argument("--faixa", type=float, default=0.25,
                    help="resumo usa so strikes a +-faixa do spot (0 = todos); a tabela de series guarda todos")
    ap.add_argument("--log", default=str(AQUI / "dados" / "posicoes_b3_csv.log"))
    args = ap.parse_args(argv)
    configurar_log(args.log)
    t0 = time.time()
    try:
        sessao = date.fromisoformat(args.sessao) if args.sessao else ultima_sessao()
        log.info("inicio: sessao %s, %s, metodo %s, db %s", sessao, args.subjacente, args.metodo, args.db)
        df, info = montar(sessao, args.subjacente, args.metodo, args.min_negocios, not args.sem_fallback)
        base = df[df["vencimento"].notna()]
        if args.faixa > 0 and info["spot"]:  # resumo so com strikes perto do dinheiro (como a grade da corretora)
            base = base[base["dist_pct"].abs() <= args.faixa]
        resumos = om.resumo(base)
        n_s, n_r, cap = gravar(args.db, sessao, args.subjacente, df, resumos, info["spot"], f"b3_bdi_{info['metodo']}")
        oi = df[["coberto", "travado", "descoberto"]].sum(axis=1, min_count=1)
        log.info("ok (%s): sessao %s | %d series (%d calls, %d puts) | %d vencimentos | OI total %.0f "
                 "(coberto %.0f, travado %.0f, descoberto %.0f) | spot %s | Selic %.2f%% | IV em %d series | %.0fs",
                 info["metodo"], sessao, n_s, (df["tipo"] == "C").sum(), (df["tipo"] == "P").sum(), n_r, oi.sum(),
                 df["coberto"].sum(), df["travado"].sum(), df["descoberto"].sum(), info["spot"],
                 info["selic"] * 100, info["n_iv"], time.time() - t0)
        return 0
    except NaoPublicado as e:
        log.error("nao publicado: %s", e)
        return 2
    except Exception as e:
        log.exception("falhou: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
