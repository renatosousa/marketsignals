"""Motor espacial de regioes operacionais do WIN$, WDO$ e BOVA11 (ESPEC_V3.md 6.2 e 6.3).

HEURISTICA AINDA NAO VALIDADA. Os pesos de confluencia (20 a 30 por fonte), a largura de agrupamento
(0,12 x ATR diario), o piso de forca (35) e os multiplicadores do stop (0,25 x ATR de 15 min) foram
escolhidos a partir da espec, nao estimados em dados. A validacao por MFE/MAE e expectativa liquida de
custo e o estudo_regioes.py (outra tarefa). Ate la, trate a regua como hipotese de leitura, nao como
estatistica comprovada.

O que o modulo faz
------------------
1. Coleta os niveis candidatos de cada ativo, cada um {preco, fonte, peso, rotulo, tag}:
     D-1 (POC, VAH, VAL e ultimo do fechamento_diario)                     fonte "d1"               peso 20
     perfil composto de 5 e de 20 pregoes (POC/VAH/VAL/HVN, barras_1m)      fonte "composto"         peso 25
     maxima/minima de D-1 e da semana anterior                              fonte "extremos"         peso 15
     VWAP do dia, da semana e do mes                                        fonte "vwap"             peso 20
     put/call wall do vencimento semanal e mensal (GEX + posicao B3)        fonte "walls"            peso 25
     gamma flip (GEX total)                                                 fonte "gamma_flip"       peso 30
     quantis do dia q15/q85 (WIN: modelo_regime_v3; senao gaussiana)        fonte "quantis"          peso 15
     quantis extremos q05/q95                                               fonte "quantis_extremos" peso 25
     preco medio do Estrangeiro/Institucional no dia e no D-1 (Tryd)        fonte "premed"           peso 20
   Para o WIN as paredes vem das opcoes de BOVA11 convertidas por k_win_bova; para o WDO, das opcoes de
   dolar (b3_dolar_series) convertidas por fator_wdo; para o BOVA11, direto.
2. Agrupa os niveis por preco, delta = 0,12 x ATR diario de 14 pregoes. Guloso por densidade: escolhe a janela
   de largura <= 2 x delta com a maior soma de pesos de fontes distintas, forma a zona, tira esses niveis e repete.
   (O single linkage da ESPEC encadeava 15 niveis numa zona de 2.200 pts no WIN de 17/11/2026: inutil como regiao.)
   A forca da zona e min(100, soma dos pesos das fontes DISTINTAS do grupo); a faixa e [min, max] dos niveis com
   pelo menos +-0,25 x delta em volta do centro. Zona que contem o preco aparece com `contem_preco`, e o lado
   (acima/abaixo) e decidido pelo centro da zona.
3. Devolve no maximo 3 zonas acima e 3 abaixo do preco atual com forca >= 35, ja classificadas pelo vies
   (parametro de -100 a +100, que vem do motor_cenario.py):
     vies <= -25: acima = VENDA a favor da tendencia; abaixo = COMPRA contra a tendencia somente com
                  forca >= 60 e se a zona contiver q05 E (put wall OU HVN de 20 pregoes); as demais abaixo
                  = ALVO (onde o vendedor realiza).
     vies >= +25: espelho (abaixo COMPRA a favor; acima VENDA contra so com forca >= 60, q95 E
                  (call wall OU HVN de 20 pregoes)).
     |vies| < 25: zonas nas pontas (com q15/q85 ou VAH/VAL) = CONTRA_TENDENCIA (reversao na extremidade);
                  as demais = NEUTRA.
   Toda zona operavel ganha plano: entrada = faixa da zona, stop = borda externa +- 0,25 x ATR de 15 min,
   alvo_1 = a zona seguinte na direcao da operacao ou o POC/VWAP do dia (o mais perto), alvo_2 = o
   seguinte; R:R liquido de 2 x custo por lado (5 pts no WIN, 0,5 pt no WDO, R$ 0,01 no BOVA11).

Causalidade: toda consulta le apenas ts_ms <= agora_ms (inclusive o regime_live, o modelo de regime e as
fotos do Tryd). O modelo_regime_v3, quando o modulo e dados/modelo_regime_v3.json existem, devolve os
quantis do fechamento; senao o substituto e a faixa gaussiana do regime_live (rv_dia_prev para o dia,
faixa_1h_pts para a hora) e a origem sai marcada no diagnostico.

Convencoes do projeto: ts_ms = hora de Brasilia codificada como epoch UTC; dia_idx = ts_ms // 86400000;
nao ha escrita no banco real (a unica escrita e atualizar(db), que grava em regioes_operacionais).
"""
import json
import math
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

import banco
import calendario_b3 as cal
import conversor_paridade as conv
import grafico_quant as gq
import players
import relogio

DIA_MS = 86_400_000
H = lambda hh, mm=0: hh * 60 + mm          # minuto do dia
SESSAO_FUTUROS = (H(9), H(18, 30))
SESSAO_ACOES = (H(10), H(17))

ATIVOS = {
    "WIN$": dict(symbol="WIN$", tabela="snapshots", livro=True, sessao=SESSAO_FUTUROS, passo=5.0, passo_comp=50.0,
                 tick=5.0, custo_lado=5.0, opcoes="BOVA11", mult=0.20, casas=0),
    "WDO$": dict(symbol="WDO$", tabela="macro_cotacoes", livro=False, sessao=SESSAO_FUTUROS, passo=0.5, passo_comp=5.0,
                 tick=0.5, custo_lado=0.5, opcoes="DOL", mult=10.0, casas=1),
    "BOVA11": dict(symbol="BOVA11", tabela="snapshots", livro=True, sessao=SESSAO_ACOES, passo=0.01, passo_comp=0.05,
                   tick=0.01, custo_lado=0.01, opcoes="BOVA11", mult=1.0, casas=2),
}
ALIAS = {"WIN": "WIN$", "WDO": "WDO$", "WIN$": "WIN$", "WDO$": "WDO$", "BOVA11": "BOVA11"}

PESOS = {"d1": 20.0, "composto": 25.0, "extremos": 15.0, "vwap": 20.0, "walls": 25.0,
         "gamma_flip": 30.0, "quantis": 15.0, "quantis_extremos": 25.0, "premed": 20.0}
DELTA_ATR = 0.12
FORCA_MIN = 35.0
MAX_POR_LADO = 3
MULT_STOP_ATR15 = 0.25
ATR_DIAS = 14
DIAS_COMPOSTO = (5, 20)
COMPOSTO_MAX_HVN = 4
VOL_NORMAL_DIAS = 20
Z = {"q05": 1.6449, "q15": 1.0364, "q85": 1.0364, "q95": 1.6449}
ARQ_MODELO = "dados/modelo_regime_v3.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS regioes_operacionais (
    ts_ms INTEGER NOT NULL,
    ativo TEXT NOT NULL,
    horizonte TEXT NOT NULL,
    id_zona TEXT NOT NULL,
    preco_min REAL NOT NULL,
    preco_centro REAL NOT NULL,
    preco_max REAL NOT NULL,
    forca_score REAL NOT NULL,
    alinhamento TEXT NOT NULL,
    direcao_operacao TEXT NOT NULL,
    componentes_json TEXT NOT NULL,
    stop_sugerido REAL,
    alvo_1 REAL,
    alvo_2 REAL,
    status_gatilho TEXT,
    PRIMARY KEY (ts_ms, ativo, horizonte, id_zona));
"""


# ------------------------------------------------------------------ utilidades

def normalizar(ativo):
    """'WIN', 'WIN$', 'WDO', 'WDO$' ou 'BOVA11' -> chave de ATIVOS (ValueError se desconhecido)."""
    chave = ALIAS.get(str(ativo).upper())
    if chave is None:
        raise ValueError(f"ativo desconhecido: {ativo}")
    return chave


def _dia_idx(ts_ms):
    return int(ts_ms) // DIA_MS


def _iso(dia_idx):
    return (date(1970, 1, 1) + timedelta(days=int(dia_idx))).isoformat()


def _dia_anterior(dia_idx):
    return (cal.pregao_anterior(_iso(dia_idx)) - date(1970, 1, 1)).days


def _seg(ts_ms):
    return int(ts_ms) / 1000.0


def _finito(x):
    return x is not None and isinstance(x, (int, float, np.floating)) and math.isfinite(float(x))


def _semana_anterior(dia_idx):
    """(segunda, sexta) da semana de calendario anterior ao dia."""
    d = date(1970, 1, 1) + timedelta(days=int(dia_idx))
    segunda = d - timedelta(days=d.weekday() + 7)
    return segunda, segunda + timedelta(days=4)


# ------------------------------------------------------------------ leitura de precos

def preco_atual(con, ativo, agora_ms):
    """Ultimo preco valido ate agora_ms (snapshots com spread > 0 ou macro com livro valido)."""
    ativo = normalizar(ativo)
    cfg = ATIVOS[ativo]
    dia = _dia_idx(agora_ms)
    ini = dia * DIA_MS + cfg["sessao"][0] * 60_000
    fim = int(agora_ms)
    if fim < ini:
        ini = dia * DIA_MS
    if cfg["tabela"] == "snapshots":
        sql = ("SELECT last FROM snapshots WHERE symbol=? AND ts_ms BETWEEN ? AND ? AND spread > 0 AND last > 0 "
               "ORDER BY ts_ms DESC LIMIT 1")
    else:
        sql = ("SELECT last FROM macro_cotacoes WHERE symbol=? AND ts_ms BETWEEN ? AND ? "
               "AND bid > 0 AND ask > bid AND last > 0 ORDER BY ts_ms DESC LIMIT 1")
    try:
        r = con.execute(sql, (cfg["symbol"], ini, fim)).fetchone()
    except sqlite3.OperationalError:
        return None
    return float(r[0]) if r and r[0] else None


def _serie_precos(con, ativo, dia, fim_ms=None):
    """(ts_ms, last) do dia ate o fim pedido; a fonte e a mesma de preco_atual. Vazio se nao houver."""
    cfg = ATIVOS[normalizar(ativo)]
    ini = int(dia) * DIA_MS + cfg["sessao"][0] * 60_000
    fim = int(dia) * DIA_MS + cfg["sessao"][1] * 60_000
    if fim_ms is not None:
        fim = min(fim, int(fim_ms))
    if ini >= fim:
        return pd.DataFrame(columns=["ts_ms", "last"])
    if cfg["tabela"] == "snapshots":
        sql = ("SELECT ts_ms, last FROM snapshots WHERE symbol=? AND ts_ms BETWEEN ? AND ? "
               "AND spread > 0 AND last > 0 ORDER BY ts_ms")
    else:
        sql = ("SELECT ts_ms, last FROM macro_cotacoes WHERE symbol=? AND ts_ms BETWEEN ? AND ? "
               "AND bid > 0 AND ask > bid AND last > 0 ORDER BY ts_ms")
    try:
        return pd.read_sql(sql, con, params=(cfg["symbol"], ini, fim))
    except sqlite3.OperationalError:
        return pd.DataFrame(columns=["ts_ms", "last"])


def _dados_dia(con, ativo, dia, fim_ms=None, cache=None):
    """Perfil de preco/volume do dia: DataFrame[ts_ms, last, vc, vv].

    WIN/WDO: barras_1m (last = close, volume inteiro em vc); sem as barras do dia (hoje, antes do backfill),
    snapshots (WIN/BOVA) ou agressao_1min (WDO) agregados por minuto. Dias fechados ficam em `cache`.
    """
    ativo = normalizar(ativo)
    cfg = ATIVOS[ativo]
    chave = None
    if fim_ms is None and cache is not None:
        chave = (ativo, int(dia))
        if chave in cache:
            return cache[chave]
    ini = int(dia) * DIA_MS + cfg["sessao"][0] * 60_000
    fim = int(dia) * DIA_MS + cfg["sessao"][1] * 60_000
    if fim_ms is not None:
        fim = min(fim, int(fim_ms))
    df = pd.DataFrame(columns=["ts_ms", "last", "vc", "vv"])
    if ini < fim:
        if cfg["symbol"] in ("WIN$", "WDO$"):
            try:
                df = pd.read_sql("SELECT ts_ms, close last, coalesce(volume, 0) vc FROM barras_1m "
                                 "WHERE symbol=? AND ts_ms BETWEEN ? AND ? AND close > 0 ORDER BY ts_ms",
                                 con, params=(cfg["symbol"], ini, fim))
                df["vv"] = 0.0
            except sqlite3.OperationalError:
                df = pd.DataFrame(columns=["ts_ms", "last", "vc", "vv"])
        if df.empty and cfg["livro"]:
            # uma linha por minuto: o volume do minuto ao preco medio dele (perfil estavel e barato)
            try:
                df = pd.read_sql("SELECT (ts_ms / 60000) * 60000 ts_ms, avg(last) last, "
                                 "sum(coalesce(vol_compra_agr, 0)) vc, sum(coalesce(vol_venda_agr, 0)) vv "
                                 "FROM snapshots WHERE symbol=? AND ts_ms BETWEEN ? AND ? AND spread > 0 "
                                 "AND last > 0 GROUP BY ts_ms / 60000 ORDER BY ts_ms",
                                 con, params=(cfg["symbol"], ini, fim))
            except sqlite3.OperationalError:
                pass
        elif df.empty:
            try:
                df = pd.read_sql("SELECT ts_ms, preco last, coalesce(vol_compra, 0) vc, coalesce(vol_venda, 0) vv "
                                 "FROM agressao_1min WHERE symbol=? AND ts_ms BETWEEN ? AND ? AND preco > 0 "
                                 "ORDER BY ts_ms", con, params=(cfg["symbol"], ini, fim))
            except sqlite3.OperationalError:
                pass
    if df.empty:
        df = pd.DataFrame(columns=["ts_ms", "last", "vc", "vv"])
    df = df.astype({"ts_ms": "int64", "last": float, "vc": float, "vv": float})
    if chave is not None:
        cache[chave] = df
    return df


def _perfil_dia(con, ativo, dia, fim_ms=None, cache=None):
    """Perfil de volume do dia (POC/VAH/VAL) -> dict do grafico_quant.perfil ou None."""
    cfg = ATIVOS[normalizar(ativo)]
    df = _dados_dia(con, ativo, dia, fim_ms, cache)
    if df.empty or (df.vc + df.vv).sum() <= 0:
        return None
    return gq.perfil(df, cfg["passo"])


def _vwap_frame(df):
    """(vwap, volume) de um DataFrame com last, vc e vv; sem volume, media simples do preco."""
    if df is None or df.empty:
        return None, 0.0
    v = (df.vc + df.vv).to_numpy(float)
    tot = float(v.sum())
    if tot > 0:
        return float((df.last.to_numpy(float) * v).sum() / tot), tot
    return float(df.last.mean()), 0.0


def _tem_dados(con, ativo, dia):
    """True se o dia tem algum preco na fonte principal (barras_1m, senao snapshots/macro/agressao)."""
    ativo = normalizar(ativo)
    cfg = ATIVOS[ativo]
    ini = int(dia) * DIA_MS + cfg["sessao"][0] * 60_000
    fim = int(dia) * DIA_MS + cfg["sessao"][1] * 60_000
    consultas = []
    if cfg["symbol"] in ("WIN$", "WDO$"):
        consultas.append(("SELECT 1 FROM barras_1m WHERE symbol=? AND ts_ms BETWEEN ? AND ? LIMIT 1",
                          (cfg["symbol"], ini, fim)))
    if cfg["livro"]:
        consultas.append(("SELECT 1 FROM snapshots WHERE symbol=? AND ts_ms BETWEEN ? AND ? AND spread > 0 LIMIT 1",
                          (cfg["symbol"], ini, fim)))
    else:
        consultas.append(("SELECT 1 FROM macro_cotacoes WHERE symbol=? AND ts_ms BETWEEN ? AND ? AND bid > 0 LIMIT 1",
                          (cfg["symbol"], ini, fim)))
        consultas.append(("SELECT 1 FROM agressao_1min WHERE symbol=? AND ts_ms BETWEEN ? AND ? LIMIT 1",
                          (cfg["symbol"], ini, fim)))
    for sql, p in consultas:
        try:
            if con.execute(sql, p).fetchone():
                return True
        except sqlite3.OperationalError:
            continue
    return False


def dias_com_dados(con, ativo, dia_idx, n):
    """Ate n pregoes com dados ANTES de dia_idx (indice do dia), do mais recente para o mais antigo."""
    out, d = [], int(dia_idx)
    for _ in range(int(n) * 3 + 10):
        d = _dia_anterior(d)
        if _tem_dados(con, ativo, d):
            out.append(d)
            if len(out) >= n:
                break
    return out


def _extremos_dia(con, ativo, dia, cache=None):
    """(maxima, minima, ultimo) do dia: fechamento_diario e, sem ele, a fonte bruta."""
    ativo = normalizar(ativo)
    try:
        r = con.execute("SELECT maxima, minima, ultimo_liquido FROM fechamento_diario WHERE dia_idx=? AND ativo=?",
                        (int(dia), ATIVOS[ativo]["symbol"])).fetchone()
    except sqlite3.OperationalError:
        r = None
    if r and _finito(r[0]) and _finito(r[1]):
        return float(r[0]), float(r[1]), float(r[2]) if _finito(r[2]) else None
    df = _dados_dia(con, ativo, dia, cache=cache)
    if df.empty:
        return None
    return float(df.last.max()), float(df.last.min()), float(df.last.iloc[-1])


def atr_diario(con, ativo, dia_idx, n=ATR_DIAS, cache=None):
    """ATR (true range medio) dos ultimos n pregoes fechados ANTES de dia_idx; None sem 2 dias."""
    ativo = normalizar(ativo)
    dias = dias_com_dados(con, ativo, dia_idx, n)
    ext = [_extremos_dia(con, ativo, d, cache) for d in reversed(dias)]
    ext = [e for e in ext if e and _finito(e[0]) and _finito(e[1])]
    if len(ext) < 2:
        return None
    trs, anterior = [], None
    for alta, baixa, ultimo in ext:
        tr = alta - baixa
        if anterior is not None:
            tr = max(tr, abs(alta - anterior), abs(baixa - anterior))
        trs.append(tr)
        anterior = ultimo if _finito(ultimo) else anterior
    return float(np.mean(trs)) if trs else None


def atr_15m(con, ativo, agora_ms, cache=None):
    """ATR das barras de 15 min do dia ate agora (alinhadas a abertura da sessao).

    Sem pelo menos 3 barras, usa 0,25 x ATR diario como aproximacao (o mesmo 0,25 que a ESPEC usa no stop),
    para o stop nao ficar degenerado no comeco do pregao.
    """
    ativo = normalizar(ativo)
    cfg = ATIVOS[ativo]
    dia = _dia_idx(agora_ms)
    s = _serie_precos(con, ativo, dia, agora_ms)
    atr = None
    if len(s) >= 10:
        ini = dia * DIA_MS + cfg["sessao"][0] * 60_000
        passo = 15 * 60_000
        b = s.assign(k=((s.ts_ms - ini) // passo)).groupby("k").agg(o=("last", "first"), h=("last", "max"),
                                                                   l=("last", "min"), c=("last", "last"),
                                                                   fim=("ts_ms", "max"))
        b = b[(ini + (b.index + 1) * passo) <= int(agora_ms)]      # so barras fechadas
        if len(b) >= 3:
            pc = b.c.shift(1)
            tr = pd.concat([b.h - b.l, (b.h - pc).abs(), (b.l - pc).abs()], axis=1).max(axis=1)
            atr = float(tr.iloc[1:].mean())
    if atr and atr > 0:
        return atr
    diario = atr_diario(con, ativo, dia, cache=cache)
    return 0.25 * diario if diario else None


def _vol_normal(con, ativo, dia_idx, n=VOL_NORMAL_DIAS, cache=None):
    """Vol realizada diaria normal (mediana de n sessoes, em % do dia) pela fonte bruta."""
    vols = []
    for d in dias_com_dados(con, ativo, dia_idx, n):
        df = _dados_dia(con, ativo, d, cache=cache)
        if len(df) < 10:
            continue
        p = df.last.to_numpy(float)
        r = np.diff(np.log(p[p > 0]))
        if len(r):
            vols.append(float(np.sqrt((r * r).sum()) * 100))
    return float(np.median(vols)) if vols else None


# ------------------------------------------------------------------ fontes de niveis

def _add(levels, preco, fonte, rotulo, tag=None):
    if _finito(preco) and float(preco) > 0:
        levels.append(dict(preco=float(preco), fonte=fonte, peso=PESOS[fonte], rotulo=rotulo, tag=tag))


def _niveis_d1(con, ativo, dia, cache):
    """POC/VAH/VAL e ultimo do D-1: as colunas do fechamento_diario e, quando vazias, o perfil do dia."""
    out = []
    d1 = _dia_anterior(dia)
    simb = ATIVOS[normalizar(ativo)]["symbol"]
    try:
        r = con.execute("SELECT poc_dia, vah_dia, val_dia, ultimo_liquido FROM fechamento_diario "
                        "WHERE dia_idx=? AND ativo=?", (int(d1), simb)).fetchone()
    except sqlite3.OperationalError:
        r = None
    poc, vah, val = (r[0], r[1], r[2]) if r else (None, None, None)
    if not (_finito(poc) and _finito(vah) and _finito(val)):
        perfil = _perfil_dia(con, ativo, d1, cache=cache)
        if perfil:
            poc, vah, val = perfil["poc"], perfil["vah"], perfil["val"]
    _add(out, poc, "d1", "POC D-1")
    _add(out, vah, "d1", "VAH D-1", "vah")
    _add(out, val, "d1", "VAL D-1", "val")
    ext = _extremos_dia(con, ativo, d1, cache)
    if ext:
        _add(out, ext[2], "d1", "Último D-1")
    return out


def _niveis_composto(con, ativo, dia, cache):
    """POC/VAH/VAL e HVN do perfil composto de 5 e de 20 pregoes anteriores."""
    cfg = ATIVOS[normalizar(ativo)]
    out = []
    for n in DIAS_COMPOSTO:
        dias = dias_com_dados(con, ativo, dia, n)
        if not dias:
            continue
        partes = [_dados_dia(con, ativo, d, cache=cache) for d in dias]
        partes = [p for p in partes if not p.empty]
        if not partes:
            continue
        df = pd.concat(partes, ignore_index=True)
        if (df.vc + df.vv).sum() <= 0:
            continue
        pf = gq.perfil(df, cfg["passo_comp"])
        if not pf:
            continue
        _add(out, pf["poc"], "composto", f"POC {n}d")
        _add(out, pf["vah"], "composto", f"VAH {n}d")
        _add(out, pf["val"], "composto", f"VAL {n}d")
        for hvn in gq.zonas_hvn(pf)[:COMPOSTO_MAX_HVN]:
            _add(out, hvn["pico"], "composto", f"HVN {n}d", "hvn20" if n >= 20 else "hvn5")
    return out


def _niveis_extremos(con, ativo, dia, cache):
    out = []
    d1 = _dia_anterior(dia)
    ext = _extremos_dia(con, ativo, d1, cache)
    if ext:
        _add(out, ext[0], "extremos", "Máx. D-1")
        _add(out, ext[1], "extremos", "Mín. D-1")
    seg, sex = _semana_anterior(dia)
    altas, baixas = [], []
    for d in cal.pregoes(seg, sex):
        idx = (d - date(1970, 1, 1)).days
        e = _extremos_dia(con, ativo, idx, cache)
        if e:
            altas.append(e[0])
            baixas.append(e[1])
    if altas:
        _add(out, max(altas), "extremos", "Máx. semana anterior")
        _add(out, min(baixas), "extremos", "Mín. semana anterior")
    return out


def _niveis_vwap(con, ativo, agora_ms, cache):
    """VWAP do dia, da semana (segunda ate agora) e do mes (dia 1 ate agora)."""
    ativo = normalizar(ativo)
    dia = _dia_idx(agora_ms)
    d = date(1970, 1, 1) + timedelta(days=dia)
    out = []

    def acumular(dias):
        soma_pv = soma_v = 0.0
        simples = []
        for k, dd in enumerate(dias):
            fim = agora_ms if dd == dia else None
            v, vol = _vwap_frame(_dados_dia(con, ativo, dd, fim, cache))
            if v is None:
                continue
            if vol > 0:
                soma_pv += v * vol
                soma_v += vol
            else:
                simples.append(v)
        if soma_v > 0:
            return soma_pv / soma_v
        return float(np.mean(simples)) if simples else None

    dia_v = acumular([dia])
    _add(out, dia_v, "vwap", "VWAP dia")
    semana = [dd for dd in cal.pregoes(d - timedelta(days=d.weekday()), d)]
    _add(out, acumular([(x - date(1970, 1, 1)).days for x in semana]), "vwap", "VWAP semana")
    mes = [dd for dd in cal.pregoes(d.replace(day=1), d)]
    _add(out, acumular([(x - date(1970, 1, 1)).days for x in mes]), "vwap", "VWAP mês")
    return out, dia_v


def _terceira_sexta(iso):
    d = date.fromisoformat(str(iso)[:10])
    return d.weekday() == 4 and 15 <= d.day <= 21


def _venc_semanal_mensal(vencs, dia_iso):
    """Vencimento semanal (o mais proximo) e mensal (a 3a sexta seguinte; sem ela, o segundo)."""
    vencs = sorted(v for v in set(vencs) if v and str(v)[:10] >= dia_iso)
    if not vencs:
        return None, None
    semanal = vencs[0]
    mensal = next((v for v in vencs if _terceira_sexta(v)), None)
    if mensal is None or mensal == semanal:
        mensal = vencs[1] if len(vencs) > 1 else semanal
    return semanal, mensal


def _spot_opcoes(con, ativo, agora_ms, k_win):
    ativo = normalizar(ativo)
    if ativo == "WDO$":
        return preco_atual(con, "WDO$", agora_ms)
    bova = preco_atual(con, "BOVA11", agora_ms)
    if bova:
        return bova
    if ativo == "WIN$" and k_win:      # antes das 10:00: BOVA implicito pelo WIN ao vivo
        win = preco_atual(con, "WIN$", agora_ms)
        return win / k_win if win else None
    d1 = _dia_anterior(_dia_idx(agora_ms))
    e = _extremos_dia(con, "BOVA11", d1)
    return e[2] if e else None


def _taxa_opcoes(con, agora_ms):
    try:
        r = con.execute("SELECT taxa FROM opcoes_resumo WHERE subjacente='BOVA11' AND ts_ms <= ? AND taxa > 0 "
                        "ORDER BY ts_ms DESC LIMIT 1", (int(agora_ms),)).fetchone()
    except sqlite3.OperationalError:
        r = None
    return float(r[0]) if r else gq.TAXA_PADRAO


def _ivs(con, agora_ms):
    """Sorriso causal: ultima IV de cada serie nos 10 min antes de agora (opcoes_cotacoes); None sem dado."""
    ini = int(agora_ms) - 10 * 60_000
    try:
        vencs = [v for (v,) in con.execute("SELECT DISTINCT vencimento FROM opcoes_resumo WHERE subjacente='BOVA11' "
                                           "AND ts_ms BETWEEN ? AND ?", (ini, int(agora_ms)))]
        partes = [pd.read_sql("SELECT ts_ms, vencimento venc, tipo, strike, iv FROM opcoes_cotacoes "
                              "WHERE subjacente='BOVA11' AND vencimento=? AND ts_ms BETWEEN ? AND ? AND iv > 0 "
                              "ORDER BY ts_ms", con, params=(v, ini, int(agora_ms))) for v in vencs]
    except sqlite3.OperationalError:
        return None
    partes = [p for p in partes if not p.empty]
    if not partes:
        return None
    x = pd.concat(partes, ignore_index=True).sort_values("ts_ms")
    x = x.groupby(["venc", "tipo", "strike"]).agg(iv=("iv", "last"), ts_ms=("ts_ms", "last")).reset_index()
    return gq.ivs_por_strike(x)


def _converter(k, ativo, k_win, fator):
    if k is None:
        return None
    if ativo == "WIN$":
        return conv.strike_em_pontos(k, k_win) if k_win else None
    if ativo == "WDO$":
        return conv.strike_wdo(k, fator) if fator else None
    return float(round(float(k), 2))


def _niveis_opcoes(con, ativo, agora_ms):
    """Put/call walls (semanal e mensal) e gamma flip convertidos para pontos do ativo."""
    ativo = normalizar(ativo)
    dia = _dia_idx(agora_ms)
    dia_iso = _iso(dia)
    k_win = conv.k_win_bova(con, agora_ms)["k"] if ativo == "WIN$" else None
    if ativo == "WIN$" and not k_win:
        return [], "sem razao WIN/BOVA11"
    spot = _spot_opcoes(con, ativo, agora_ms, k_win)
    if not spot:
        return [], "sem spot do subjacente das opcoes"
    try:
        pos = gq.posicoes_dolar(con, dia_iso) if ativo == "WDO$" else gq.posicoes(con, dia_iso)
    except sqlite3.OperationalError:
        pos = None
    if not pos or pos["series"].empty:
        fonte = "b3_dolar_series" if ativo == "WDO$" else "b3_posicoes_series/opcoes_manual_series"
        return [], f"sem posicao em aberto ({fonte})"
    serie = pos["series"]
    semanal, mensal = _venc_semanal_mensal(serie.venc.dropna().unique().tolist(), dia_iso)
    if not semanal:
        return [], "sem vencimento de opcao valido"
    taxa = _taxa_opcoes(con, agora_ms)
    ivs = _ivs(con, agora_ms)
    out, fator_semanal = [], 1.0
    for rotulo, venc in (("semanal", semanal), ("mensal", mensal)):
        if not venc:
            continue
        sub = dict(pos, series=serie[serie.venc == venc])
        if sub["series"].empty:
            continue
        gx = gq.gex(sub, spot, dia_iso, taxa, ivs, None, n_wall=1)
        if not gx:
            continue
        fator = float(sub["series"].fator.dropna().iloc[0]) if sub["series"].fator.notna().any() else 1.0
        if rotulo == "semanal":
            fator_semanal = fator
        for tipo, chave, tag in (("Put", "put_walls", "put_wall"), ("Call", "call_walls", "call_wall")):
            if gx.get(chave):
                _add(out, _converter(gx[chave][0]["k"], ativo, k_win, fator), "walls", f"{tipo} wall {rotulo}", tag)
    gx = gq.gex(pos, spot, dia_iso, taxa, ivs, None)
    if gx and gx.get("flip"):
        _add(out, _converter(gx["flip"], ativo, k_win, fator_semanal), "gamma_flip", "Gamma flip")
    if not out:
        return [], "GEX sem walls nem flip na faixa de strikes"
    return out, None


def _modelo_v3():
    """Modelo do modelo_regime_v3 (dados/modelo_regime_v3.json) ou None se o modulo/json nao existirem."""
    arq = Path(__file__).parent / ARQ_MODELO
    if not arq.exists():
        return None
    try:
        import modelo_regime_v3 as m3
        return m3, json.loads(arq.read_text(encoding="utf-8"))
    except Exception:
        return None


def _regime(con, agora_ms):
    """Ultima linha do regime_live ate agora -> (rv_dia_prev, faixa_1h_pts, wdo_rv_rel) ou None."""
    dia = _dia_idx(agora_ms)
    try:
        r = con.execute("SELECT rv_dia_prev, faixa_1h_pts, wdo_rv_rel FROM regime_live WHERE ts_ms BETWEEN ? AND ? "
                        "ORDER BY ts_ms DESC LIMIT 1", (dia * DIA_MS, int(agora_ms))).fetchone()
    except sqlite3.OperationalError:
        r = None
    return r


def _niveis_quantis(con, ativo, agora_ms, preco, cache):
    """q15/q85 (fonte quantis) e q05/q95 (quantis_extremos) do fechamento; None sem referencia de vol."""
    ativo = normalizar(ativo)
    dia = _dia_idx(agora_ms)
    base, q, origem = preco, None, None
    if not preco:
        return [], "sem preco atual"
    if ativo == "WIN$":
        mod = _modelo_v3()
        if mod:
            m3, modelo = mod
            try:
                prev = m3.prever(con, agora_ms, modelo)
            except Exception:
                prev = None
            if prev and prev.get("fechamento"):
                f = prev["fechamento"]
                base = float(f.get("preco") or prev.get("preco") or preco)
                if all(k in f for k in Z):
                    q = {k: base + float(f[k]) for k in Z}
                    origem = "modelo_regime_v3"
    if q is None:
        reg = _regime(con, agora_ms)
        if not reg or not _finito(reg[0]):
            return [], "sem modelo_regime_v3 e sem regime_live"
        if ativo == "WDO$":
            normal = _vol_normal(con, ativo, dia, cache=cache)
            rel = float(reg[2]) if _finito(reg[2]) else None
            if not normal or not rel:
                return [], "sem vol normal do WDO para os quantis"
            sigma = base * normal * rel / 100.0
            origem = "gaussiana_wdo (vol normal x wdo_rv_rel)"
        else:
            sigma = base * float(reg[0]) / 100.0
            origem = "gaussiana_regime_live (rv_dia_prev)"
        if not sigma or sigma <= 0:
            return [], "regime_live sem vol valida"
        q = {k: base + sinal * z * sigma for k, (sinal, z) in
             {"q05": (-1, Z["q05"]), "q15": (-1, Z["q15"]), "q85": (1, Z["q85"]), "q95": (1, Z["q95"])}.items()}
    out = []
    for k in ("q15", "q85"):
        _add(out, q[k], "quantis", f"{k} do dia", k)
    for k in ("q05", "q95"):
        _add(out, q[k], "quantis_extremos", f"{k} do dia", k)
    return out, origem


def _contrato_tryd(con, ativo):
    """Contrato futuro do ativo na tryd_saldo (WINZ26, WDOZ26...); None para acoes."""
    pref = {"WIN$": "WIN", "WDO$": "WDO"}.get(normalizar(ativo))
    if not pref:
        return None
    try:
        r = con.execute("SELECT ativo FROM tryd_saldo WHERE ativo LIKE ? ORDER BY ts_ms DESC LIMIT 1",
                        (pref + "%",)).fetchone()
    except sqlite3.OperationalError:
        return None
    return r[0] if r else None


def _foto_tryd(con, contrato, dia, agora_ms=None):
    """Ultima foto do Tryd do dia ate agora_ms (None = dia inteiro, para D-1)."""
    fim = (int(dia) + 1) * DIA_MS if agora_ms is None else min(int(agora_ms), (int(dia) + 1) * DIA_MS)
    try:
        r = con.execute("SELECT max(ts_ms) FROM tryd_saldo WHERE ativo=? AND ts_ms BETWEEN ? AND ?",
                        (contrato, int(dia) * DIA_MS, fim)).fetchone()
    except sqlite3.OperationalError:
        return None
    return int(r[0]) if r and r[0] else None


def _premed(con, ativo, agora_ms, preco):
    """Preco medio ponderado por |qtd| do Estrangeiro e do Institucional, no dia e no D-1 (so futuros)."""
    ativo = normalizar(ativo)
    if ativo == "BOVA11":
        return [], "premed so de futuros (WIN$/WDO$)"
    contrato = _contrato_tryd(con, ativo)
    if not contrato:
        return [], "sem contrato do ativo em tryd_saldo"
    dia = _dia_idx(agora_ms)
    out = []
    multi = ATIVOS[ativo]["mult"]
    for rotulo, d in (("dia", dia), ("D-1", _dia_anterior(dia))):
        foto = _foto_tryd(con, contrato, d, agora_ms if d == dia else None)
        if foto is None:
            continue
        try:
            rows = con.execute("SELECT brk, qtd, premed FROM tryd_saldo WHERE ts_ms=? AND ativo=? AND qtd != 0 "
                               "AND premed > 0", (foto, contrato)).fetchall()
        except sqlite3.OperationalError:
            continue
        por_grupo = {}
        for brk, qtd, prem in rows:
            g = players.grupo(brk)
            if g not in ("Estrangeiro", "Institucional"):
                continue
            p = float(prem) / multi
            if preco and not (0.7 * preco <= p <= 1.3 * preco):   # celula suja do DDE
                continue
            por_grupo.setdefault(g, []).append((p, abs(float(qtd))))
        for g, vals in por_grupo.items():
            peso = sum(q for _p, q in vals)
            if peso <= 0:
                continue
            medio = sum(p * q for p, q in vals) / peso
            _add(out, medio, "premed", f"Preço médio {g} {rotulo}")
    if not out:
        ult = None
        try:
            ult = con.execute("SELECT max(ts_ms) FROM tryd_saldo WHERE ativo=?", (contrato,)).fetchone()[0]
        except sqlite3.OperationalError:
            pass
        motivo = "sem saldo do Tryd no dia nem no D-1"
        if ult:
            motivo += f" (ultima foto {pd.Timestamp(int(ult), unit='ms').strftime('%d/%m/%Y %H:%M')})"
        return [], motivo
    return out, None


def _coletar(con, ativo, agora_ms, cache=None):
    """Niveis candidatos + diagnostico das fontes que faltaram. -> (levels, faltando, extras)."""
    ativo = normalizar(ativo)
    cache = {} if cache is None else cache
    dia = _dia_idx(agora_ms)
    preco = preco_atual(con, ativo, agora_ms)
    levels, faltando, extras = [], [], {}

    grupos = [
        ("d1", lambda: _niveis_d1(con, ativo, dia, cache), "fechamento_diario/perfil de D-1 indisponivel"),
        ("composto", lambda: _niveis_composto(con, ativo, dia, cache), "sem barras_1m com volume dos pregoes anteriores"),
        ("extremos", lambda: _niveis_extremos(con, ativo, dia, cache), "sem maxima/minima de D-1 e da semana anterior"),
    ]
    for nome, fn, motivo in grupos:
        novos = fn()
        levels.extend(novos)
        if not novos:
            faltando.append(f"{nome}: {motivo}")

    vwap, vwap_dia = _niveis_vwap(con, ativo, agora_ms, cache)
    levels.extend(vwap)
    extras["vwap_dia"] = vwap_dia
    if not vwap:
        faltando.append("vwap: sem volume nem preco no dia/semana/mes")

    opcoes, motivo = _niveis_opcoes(con, ativo, agora_ms)
    levels.extend(opcoes)
    if not opcoes:
        faltando.append(f"walls/gamma_flip: {motivo}")
    elif not any(n["fonte"] == "gamma_flip" for n in opcoes):
        faltando.append("gamma_flip: o GEX total nao troca de sinal na faixa varrida")

    quantis, origem = _niveis_quantis(con, ativo, agora_ms, preco, cache)
    levels.extend(quantis)
    extras["origem_quantis"] = origem
    if not quantis:
        faltando.append(f"quantis: {origem}")

    prem, motivo = _premed(con, ativo, agora_ms, preco)
    levels.extend(prem)
    if not prem:
        faltando.append(f"premed: {motivo}")

    extras["preco"] = preco
    return levels, faltando, extras


def niveis_candidatos(con, ativo, agora_ms):
    """Lista de {preco, fonte, peso, rotulo, tag} do ativo no instante (somente leitura)."""
    return _coletar(con, ativo, agora_ms)[0]


def diagnostico(con, ativo, agora_ms):
    """Contagem por fonte e o motivo das que faltaram (para a aceitacao e para a tela de saude)."""
    levels, faltando, extras = _coletar(con, ativo, agora_ms)
    contagem = {}
    for n in levels:
        contagem[n["fonte"]] = contagem.get(n["fonte"], 0) + 1
    return dict(ativo=normalizar(ativo), niveis=len(levels), por_fonte=contagem, faltando=faltando,
                origem_quantis=extras.get("origem_quantis"), preco=extras.get("preco"),
                vwap_dia=extras.get("vwap_dia"))


# ------------------------------------------------------------------ clusterizacao e classificacao

LARGURA_MAX_DELTAS = 2.0   # largura maxima de uma zona, em multiplos de delta


def clusterizar(niveis, delta):
    """Agrupamento guloso por densidade: repete "janela de largura <= LARGURA_MAX_DELTAS x delta com a maior soma de
    pesos de fontes distintas vira zona; seus niveis saem da lista" ate acabar.

    -> lista de dict(preco_min, preco_centro, preco_max, forca, componentes, tags, fontes), em ordem de preco.
    """
    resto = sorted((n for n in niveis if _finito(n.get("preco"))), key=lambda n: n["preco"])
    largura = LARGURA_MAX_DELTAS * delta
    grupos = []
    while resto:
        melhor = None
        j = 0
        for i in range(len(resto)):
            j = max(j, i)
            while j + 1 < len(resto) and resto[j + 1]["preco"] - resto[i]["preco"] <= largura:
                j += 1
            peso = sum({n["fonte"]: n["peso"] for n in resto[i:j + 1]}.values())
            if melhor is None or peso > melhor[0]:
                melhor = (peso, i, j)
        _, i, j = melhor
        grupos.append(resto[i:j + 1])
        resto = resto[:i] + resto[j + 1:]
    grupos.sort(key=lambda g: g[0]["preco"])
    zonas = []
    for g in grupos:
        precos = [n["preco"] for n in g]
        centro = float(np.mean(precos))
        lo, hi = min(precos), max(precos)
        lo = min(lo, centro - 0.25 * delta)
        hi = max(hi, centro + 0.25 * delta)
        fontes = {}
        vistos, componentes = set(), []
        for n in g:
            fontes[n["fonte"]] = n["peso"]
            if n["rotulo"] not in vistos:
                vistos.add(n["rotulo"])
                componentes.append(n["rotulo"])
        zonas.append(dict(preco_min=float(lo), preco_centro=centro, preco_max=float(hi),
                          forca=float(min(100.0, sum(fontes.values()))), componentes=componentes,
                          tags={n.get("tag") for n in g if n.get("tag")}, fontes=fontes))
    return zonas


def _classificar(zona, preco, vies):
    """(alinhamento, direcao) da zona pelo vies do motor de cenario."""
    acima = zona["preco_centro"] > preco
    tags, forca = zona["tags"], zona["forca"]
    if vies <= -25:
        if acima:
            return "A_FAVOR_TENDENCIA", "VENDA"
        if forca >= 60 and "q05" in tags and ({"put_wall", "hvn20"} & tags):
            return "CONTRA_TENDENCIA", "COMPRA"
        return "ALVO", "VENDA"
    if vies >= 25:
        if not acima:
            return "A_FAVOR_TENDENCIA", "COMPRA"
        if forca >= 60 and "q95" in tags and ({"call_wall", "hvn20"} & tags):
            return "CONTRA_TENDENCIA", "VENDA"
        return "ALVO", "COMPRA"
    if tags & {"q15", "q85", "vah", "val"}:
        return "CONTRA_TENDENCIA", "VENDA" if acima else "COMPRA"
    return "NEUTRA", "VENDA" if acima else "COMPRA"


def _alvos(zonas, zona, direcao, extras):
    """Alvo 1 e 2: a proxima zona na direcao da operacao ou o POC/VWAP do dia, o mais perto primeiro."""
    if direcao == "VENDA":
        base = zona["preco_min"]
        cand = [z["preco_centro"] for z in zonas
                if z is not zona and z["forca"] >= FORCA_MIN and z["preco_centro"] < base]
        cand += [v for v in (extras.get("poc_dia"), extras.get("vwap_dia")) if _finito(v) and v < base]
        cand = sorted(set(round(float(v), 4) for v in cand), reverse=True)
    else:
        base = zona["preco_max"]
        cand = [z["preco_centro"] for z in zonas
                if z is not zona and z["forca"] >= FORCA_MIN and z["preco_centro"] > base]
        cand += [v for v in (extras.get("poc_dia"), extras.get("vwap_dia")) if _finito(v) and v > base]
        cand = sorted(set(round(float(v), 4) for v in cand))
    return (cand[0] if cand else None), (cand[1] if len(cand) > 1 else None)


def _plano(zona, direcao, atr15, zonas, extras, custo_lado):
    """Stop/alvos e R:R liquido de 2 x custo por lado; entrada = centro da faixa."""
    entrada = zona["preco_centro"]
    if not atr15:
        return dict(entrada_min=zona["preco_min"], entrada_max=zona["preco_max"], stop=None,
                    alvo_1=None, alvo_2=None, rr=None)
    stop = zona["preco_max"] + MULT_STOP_ATR15 * atr15 if direcao == "VENDA" \
        else zona["preco_min"] - MULT_STOP_ATR15 * atr15
    a1, a2 = _alvos(zonas, zona, direcao, extras)
    rr = None
    if a1 is not None:
        custo = 2 * custo_lado
        if direcao == "VENDA" and stop > entrada:
            rr = ((entrada - a1) - custo) / ((stop - entrada) + custo)
        elif direcao == "COMPRA" and entrada > stop:
            rr = ((a1 - entrada) - custo) / ((entrada - stop) + custo)
    return dict(entrada_min=zona["preco_min"], entrada_max=zona["preco_max"], stop=float(stop),
                alvo_1=a1, alvo_2=a2, rr=float(rr) if rr is not None else None)


def _id_zona(lado, k):
    return f"{'R' if lado == 'ACIMA' else 'S'}{k}"


def regioes_detalhado(con, ativo, agora_ms, vies=0.0):
    """Zonas do ativo mais o contexto da regua (preco, VWAP do dia, gamma flip e fontes que faltaram).

    Faz uma unica coleta de niveis, reaproveitada pelo cockpit: pedir `regioes` e `diagnostico`
    separados custava duas varreduras completas (GEX, perfil composto, Tryd). -> dict com
    {ativo, preco, vwap_dia, gamma_flip, origem_quantis, faltando, zonas}.
    """
    ativo = normalizar(ativo)
    cfg = ATIVOS[ativo]
    agora_ms = int(agora_ms)
    cache = {}
    levels, faltando, extras = _coletar(con, ativo, agora_ms, cache)
    preco = extras.get("preco")
    ctx = dict(ativo=ativo, preco=preco, vwap_dia=extras.get("vwap_dia"),
               gamma_flip=next((n["preco"] for n in levels if n["fonte"] == "gamma_flip"), None),
               origem_quantis=extras.get("origem_quantis"), faltando=faltando, zonas=[])
    if not preco or not levels:
        return ctx
    dia = _dia_idx(agora_ms)
    atr_d = atr_diario(con, ativo, dia, cache=cache)
    if not atr_d or atr_d <= 0:
        ctx["faltando"] = faltando + ["ATR diario: sem 2 pregoes fechados com maxima e minima"]
        return ctx
    delta = DELTA_ATR * atr_d
    perfil = _perfil_dia(con, ativo, dia, fim_ms=agora_ms)
    extras["poc_dia"] = perfil["poc"] if perfil else None
    atr15 = atr_15m(con, ativo, agora_ms, cache=cache)
    zonas = clusterizar(levels, delta)

    acima = [z for z in zonas if z["preco_centro"] > preco and z["forca"] >= FORCA_MIN]
    abaixo = [z for z in zonas if z["preco_centro"] <= preco and z["forca"] >= FORCA_MIN]
    acima.sort(key=lambda z: (-z["forca"], z["preco_centro"] - preco))
    abaixo.sort(key=lambda z: (-z["forca"], preco - z["preco_centro"]))
    selecionadas = [(z, "ACIMA") for z in acima[:MAX_POR_LADO]] + [(z, "ABAIXO") for z in abaixo[:MAX_POR_LADO]]
    selecionadas.sort(key=lambda par: -par[0]["preco_centro"])

    out, contagem = [], {"ACIMA": 0, "ABAIXO": 0}
    for zona, lado in selecionadas:
        contagem[lado] += 1
        alinhamento, direcao = _classificar(zona, preco, vies)
        plano = _plano(zona, direcao, atr15, zonas, extras, cfg["custo_lado"])
        out.append(dict(id_zona=_id_zona(lado, contagem[lado]), lado=lado, preco_min=zona["preco_min"],
                        preco_centro=zona["preco_centro"], preco_max=zona["preco_max"], forca=zona["forca"],
                        componentes=zona["componentes"], alinhamento=alinhamento, direcao=direcao,
                        contem_preco=bool(zona["preco_min"] <= preco <= zona["preco_max"]),
                        distancia_pts=float(zona["preco_centro"] - preco),
                        distancia_pct=float((zona["preco_centro"] - preco) / preco * 100),
                        **plano))
    ctx["zonas"] = out
    ctx["atr15"] = atr15
    return ctx


def regioes(con, ativo, agora_ms, vies=0.0):
    """Zonas operacionais do ativo no instante (no maximo 3 acima e 3 abaixo do preco, forca >= 35).

    Cada zona: id_zona, lado, preco_min, preco_centro, preco_max, forca, componentes, alinhamento,
    direcao, distancia_pts, distancia_pct, entrada_min/entrada_max, stop, alvo_1, alvo_2, rr.
    """
    return regioes_detalhado(con, ativo, agora_ms, vies)["zonas"]


# ------------------------------------------------------------------ gravacao

def _vies_do_banco(con, agora_ms):
    """score_estrutural do cenario_scores mais recente ate agora; 0.0 sem a tabela (motor_cenario futuro)."""
    try:
        r = con.execute("SELECT score_estrutural FROM cenario_scores WHERE ts_ms <= ? ORDER BY ts_ms DESC LIMIT 1",
                        (int(agora_ms),)).fetchone()
    except sqlite3.OperationalError:
        return 0.0
    return float(r[0]) if r and _finito(r[0]) else 0.0


def _status_leve(zona, preco, atr15):
    """DISTANTE/PROXIMA/EM_TESTE pela distancia a faixa (o gatilho fino e o gatilhos_execucao.py)."""
    if preco is None or not atr15:
        return None
    if zona["preco_min"] <= preco <= zona["preco_max"]:
        return "EM_TESTE"
    dist = zona["preco_min"] - preco if preco < zona["preco_min"] else preco - zona["preco_max"]
    return "PROXIMA" if dist <= atr15 else "DISTANTE"


def atualizar(db, agora_ms=None):
    """Calcula e grava as zonas do instante em regioes_operacionais (horizonte DAYTRADE).

    O vies sai do cenario_scores mais recente (0.0 enquanto o motor_cenario.py nao existir). O
    status_gatilho e a classificacao leve por ATR; o gatilho fino (absorcao/OFI/Wyckoff) e o
    gatilhos_execucao.estado_zona. Um ativo que falhe nao impede os outros (o erro fica de fora da
    gravacao). -> numero de linhas gravadas.
    """
    agora_ms = int(agora_ms if agora_ms is not None else relogio.local_br_ms())
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        vies = _vies_do_banco(con, agora_ms)
        linhas = []
        for ativo in ATIVOS:
            try:
                zonas = regioes(con, ativo, agora_ms, vies)
            except Exception:
                continue
            preco = preco_atual(con, ativo, agora_ms)
            atr15 = atr_15m(con, ativo, agora_ms)
            for z in zonas:
                linhas.append((agora_ms, ativo, "DAYTRADE", z["id_zona"], z["preco_min"], z["preco_centro"],
                               z["preco_max"], z["forca"], z["alinhamento"], z["direcao"],
                               json.dumps(z["componentes"], ensure_ascii=False), z.get("stop"),
                               z.get("alvo_1"), z.get("alvo_2"), _status_leve(z, preco, atr15)))
    finally:
        con.close()
    if not linhas:
        return 0
    dbw = banco.abrir(str(db), SCHEMA)
    try:
        dbw.executemany("INSERT OR REPLACE INTO regioes_operacionais (ts_ms, ativo, horizonte, id_zona, preco_min, "
                        "preco_centro, preco_max, forca_score, alinhamento, direcao_operacao, componentes_json, "
                        "stop_sugerido, alvo_1, alvo_2, status_gatilho) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        linhas)
        dbw.commit()
    finally:
        dbw.close()
    return len(linhas)
