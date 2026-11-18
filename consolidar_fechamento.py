"""Consolidacao do fechamento diario por ativo -> tabela fechamento_diario (ESPEC_V3 3.4 e 8.1).

Corrige a referencia de fechamento do /api/fechamento: em vez de reler o dia inteiro a cada pedido e cair na
primeira cotacao do dia (que pode ser de antes da abertura), guarda uma linha por (dia, ativo) com:

  abertura       primeiro preco da sessao (open da primeira barra de 1 min ou primeiro mid)
  maxima/minima  extremos da sessao (high/low das barras ou mids)
  ultimo_liquido ultimo preco do pregao continuo: futuros ate 18:30 (sem leilao de fechamento), acoes/ETFs ate 16:55 (antes do leilao
                 de fechamento). E a referencia de variacao do dia seguinte.
  ultimo_dia     ultimo preco da sessao (inclui o leilao de fechamento)
  ajuste_b3      NULL por enquanto (a fonte oficial entra em outra tarefa)
  vwap_dia       so com barras_1m de volume > 0: media dos closes ponderada pelo volume da barra
  poc/vah/val    area de valor de 70% do perfil de volume (grafico_quant.perfil), passo 5 pts no WIN$/IND$,
                 0,5 no WDO$ e 0,01 nas acoes/ETFs
  vol_realizada_dia  % do dia: raiz da soma dos log-retornos de 1 min ao quadrado x 100
  fonte          'barras_1m' (preferida) | 'snapshots' (WIN$/BOVA11, spread > 0) | 'macro_cotacoes'

Sessao (mesma regra do /api/fechamento): futuros (WIN$, WDO$, IND$, DI1*, DAP*) 09:00-18:30; acoes e ETFs
10:00-17:00. Taxas (DI1*, DAP*) ficam com vwap/perfil/vol NULL; o preco e a taxa em % a.a.

Funcoes:
  consolidar_dia(con_ro, dia)          le o banco (somente leitura) e devolve [{...}] do dia
  gravar(con_rw, linhas)               INSERT OR REPLACE em transacao curta
  consolidar_pendentes(db_path, dias)  consolida os pregoes recentes que ainda nao estao na tabela (menos o
                                       dia de hoje antes das 18:30). Chamada na partida do coletor_regime.

Uso: python consolidar_fechamento.py [--db dados/book.db] [--dia AAAA-MM-DD] [--dias 10]
"""
import argparse
import math
import sqlite3
from datetime import date, datetime, timedelta

import pandas as pd

import banco
import calendario_b3
import grafico_quant
import relogio

D = 86_400_000
SESSAO_FUTUROS = (9 * 60, 18 * 60 + 30)
SESSAO_ACOES = (10 * 60, 17 * 60)
LIQUIDO_FUTUROS = 18 * 60 + 30            # WIN/WDO/DI nao tem leilao de fechamento: vale o ultimo da sessao
LIQUIDO_ACOES = 16 * 60 + 55
FIM_SESSAO = 18 * 60 + 30                 # antes disso o dia de hoje ainda esta em formacao
FIXOS = ("WIN$", "WDO$", "IND$")
TAXAS = ("DI1F28", "DI1F29", "DI1F33")
PASSO_PERFIL = {"WIN$": 5.0, "IND$": 5.0, "WDO$": 0.5}
PASSO_PADRAO = 0.01

SCHEMA = """
CREATE TABLE IF NOT EXISTS fechamento_diario (
    dia_idx INTEGER NOT NULL, ativo TEXT NOT NULL,
    abertura REAL, maxima REAL, minima REAL,
    ultimo_liquido REAL NOT NULL,
    ultimo_dia REAL,
    ajuste_b3 REAL,
    vwap_dia REAL, poc_dia REAL, vah_dia REAL, val_dia REAL,
    vol_realizada_dia REAL,
    fonte TEXT,
    ts_consolidacao_ms INTEGER NOT NULL,
    PRIMARY KEY (dia_idx, ativo));
"""
COLS = ("dia_idx", "ativo", "abertura", "maxima", "minima", "ultimo_liquido", "ultimo_dia", "ajuste_b3",
        "vwap_dia", "poc_dia", "vah_dia", "val_dia", "vol_realizada_dia", "fonte", "ts_consolidacao_ms")


# ------------------------------------------------------------------ utilidades

def _dia_idx(d):
    """Indice do dia (ts_ms // 86400000) a partir de 'AAAA-MM-DD', date/datetime ou do proprio indice."""
    if isinstance(d, int):
        return d
    if isinstance(d, str):
        d = date.fromisoformat(d[:10])
    if isinstance(d, datetime):
        d = d.date()
    return (d - date(1970, 1, 1)).days


def _data_iso(dia_idx):
    return (date(1970, 1, 1) + timedelta(days=int(dia_idx))).isoformat()


def _eh_taxa(simbolo):
    """True para DI1*/DAP*: a serie e taxa em % a.a., nao preco."""
    return simbolo.startswith(("DI1", "DAP"))


def _sessao(simbolo):
    """(min_ini, min_fim) da sessao em minutos do dia, inclusive."""
    if simbolo.endswith("$") or _eh_taxa(simbolo):
        return SESSAO_FUTUROS
    return SESSAO_ACOES


def _limite_liquido(simbolo):
    """Minuto do dia do ultimo preco do pregao continuo (fora do leilao de fechamento)."""
    return LIQUIDO_FUTUROS if (simbolo.endswith("$") or _eh_taxa(simbolo)) else LIQUIDO_ACOES


def _passo(simbolo):
    return PASSO_PERFIL.get(simbolo, PASSO_PADRAO)


def ativos():
    """Ativos consolidados: WIN$, WDO$, IND$, os vertices de DI, BOVA11 e as acoes/ETFs de
    dashboard.MACRO_GRAFICOS sem '$' (lista importada, sem duplicar)."""
    from dashboard import MACRO_GRAFICOS
    out = list(FIXOS) + list(TAXAS) + ["BOVA11"]
    for syms in MACRO_GRAFICOS.values():
        out.extend(s for s in syms if "$" not in s)
    vistos, unicos = set(), []
    for s in out:
        if s not in vistos:
            vistos.add(s)
            unicos.append(s)
    return unicos


def _dentro(simbolo, ts):
    ini, fim = _sessao(simbolo)
    return ini <= (ts % D) // 60_000 <= fim


def _barras(con, simbolo, dia):
    """Barra de 1 min do simbolo no dia, so dentro da sessao: [(ts, open, high, low, close, volume)]."""
    try:
        rows = con.execute("SELECT ts_ms, open, high, low, close, volume FROM barras_1m WHERE symbol=? "
                           "AND ts_ms>=? AND ts_ms<? ORDER BY ts_ms",
                           (simbolo, dia * D, (dia + 1) * D)).fetchall()
    except sqlite3.OperationalError:
        return []
    return [r for r in rows if r[4] is not None and _dentro(simbolo, r[0])]


def _mids(con, simbolo, dia):
    """Mids do dia dentro da sessao. -> ([(ts, mid)], fonte); fonte = 'snapshots' ou 'macro_cotacoes'."""
    if simbolo in ("WIN$", "BOVA11"):
        sql = ("SELECT ts_ms, mid FROM snapshots WHERE symbol=? AND spread>0 AND ts_ms>=? AND ts_ms<? "
               "ORDER BY ts_ms")
        fonte = "snapshots"
    else:
        sql = ("SELECT ts_ms, mid FROM macro_cotacoes WHERE symbol=? AND ts_ms>=? AND ts_ms<? ORDER BY ts_ms")
        fonte = "macro_cotacoes"
    try:
        rows = con.execute(sql, (simbolo, dia * D, (dia + 1) * D)).fetchall()
    except sqlite3.OperationalError:
        return [], fonte
    return [(ts, m) for ts, m in rows if m is not None and _dentro(simbolo, ts)], fonte


def _vol(precos):
    """Vol realizada do dia em %: raiz da soma dos log-retornos de 1 min ao quadrado x 100. O preco de
    cada minuto e o ultimo da serie. None sem pelo menos dois minutos validos."""
    ultimo = {}
    for ts, p in precos:
        ultimo[ts // 60_000] = p
    vals = [ultimo[k] for k in sorted(ultimo)]
    soma, n = 0.0, 0
    for a, b in zip(vals, vals[1:]):
        if a and b and a > 0 and b > 0:
            soma += math.log(b / a) ** 2
            n += 1
    return math.sqrt(soma) * 100 if n else None


def _perfil(barras, simbolo):
    """(vwap, poc, vah, val) do perfil de volume das barras com volume > 0; None sem volume."""
    validas = [(ts, c, v) for ts, _o, _h, _l, c, v in barras if v and v > 0 and c is not None]
    vol = sum(v for _ts, _c, v in validas)
    if not validas or vol <= 0:
        return None, None, None, None
    vwap = sum(c * v for _ts, c, v in validas) / vol
    # grafico_quant.perfil espera colunas vc/vv (volume comprado/vendido) e last: aqui o volume da barra
    # e do negocio (sem lado), entao entra inteiro como vc
    df = pd.DataFrame([{"last": c, "vc": v, "vv": 0.0} for _ts, c, v in validas])
    pf = grafico_quant.perfil(df, _passo(simbolo))
    if not pf:
        return vwap, None, None, None
    return vwap, pf["poc"], pf["vah"], pf["val"]


def _consolidar_ativo(con, simbolo, dia):
    """Uma linha (sem dia_idx/ativo/ts) do ativo no dia, ou None se a sessao nao tiver dado."""
    taxa = _eh_taxa(simbolo)
    barras = _barras(con, simbolo, dia)
    if barras:
        fonte = "barras_1m"
        abertura = barras[0][1] if barras[0][1] is not None else barras[0][4]
        altas = [h for _ts, _o, h, _l, _c, _v in barras if h is not None]
        baixas = [l for _ts, _o, _h, l, _c, _v in barras if l is not None]
        ultimo_dia = barras[-1][4]
        lim = _limite_liquido(simbolo)
        liquidos = [c for ts, _o, _h, _l, c, _v in barras if (ts % D) // 60_000 <= lim]
        precos = [(ts, c) for ts, _o, _h, _l, c, _v in barras]
        vwap = poc = vah = val = None
        if not taxa:
            vwap, poc, vah, val = _perfil(barras, simbolo)
    else:
        rows, fonte = _mids(con, simbolo, dia)
        if not rows:
            return None
        abertura = rows[0][1]
        altas = baixas = [m for _ts, m in rows]
        ultimo_dia = rows[-1][1]
        lim = _limite_liquido(simbolo)
        liquidos = [m for ts, m in rows if (ts % D) // 60_000 <= lim]
        precos = rows
        vwap = poc = vah = val = None
    return dict(abertura=abertura, maxima=max(altas) if altas else None,
                minima=min(baixas) if baixas else None,
                ultimo_liquido=liquidos[-1] if liquidos else ultimo_dia, ultimo_dia=ultimo_dia,
                ajuste_b3=None, vwap_dia=vwap, poc_dia=poc, vah_dia=vah, val_dia=val,
                vol_realizada_dia=None if taxa else _vol(precos), fonte=fonte)


# ------------------------------------------------------------------ leitura e gravacao

def consolidar_dia(con_ro, dia):
    """Consolida o dia (indice ou 'AAAA-MM-DD') lendo o banco em modo somente leitura. -> [dict] na ordem
    de `ativos()`; dia sem dado em nenhum ativo devolve lista vazia."""
    dia_idx = _dia_idx(dia)
    ts = int(relogio.local_br_ms())
    linhas = []
    for simbolo in ativos():
        r = _consolidar_ativo(con_ro, simbolo, dia_idx)
        if r:
            r.update(dia_idx=dia_idx, ativo=simbolo, ts_consolidacao_ms=ts)
            linhas.append(r)
    return linhas


def gravar(con_rw, linhas):
    """INSERT OR REPLACE das linhas em fechamento_diario (cria a tabela; uma transacao curta). -> n."""
    if not linhas:
        return 0
    con_rw.executescript(SCHEMA)
    sql = (f"INSERT OR REPLACE INTO fechamento_diario ({','.join(COLS)}) "
           f"VALUES ({','.join('?' * len(COLS))})")
    con_rw.executemany(sql, [tuple(l[c] for c in COLS) for l in linhas])
    con_rw.commit()
    return len(linhas)


def _ja_consolidado(con, dia_idx):
    try:
        return con.execute("SELECT 1 FROM fechamento_diario WHERE dia_idx=? LIMIT 1", (dia_idx,)).fetchone() \
            is not None
    except sqlite3.OperationalError:  # tabela ainda nao existe
        return False


def consolidar_pendentes(db_path, dias=10, hoje=None, agora=None):
    """Consolida os pregoes dos ultimos `dias` dias corridos que ainda nao estao em fechamento_diario,
    menos o dia de hoje se ainda for antes das 18:30. -> numero de dias consolidados.

    `hoje`/`agora` existem para o teste; no uso normal sao a data e a hora de Brasilia do PC."""
    hoje = hoje or date.today()
    agora = agora or datetime.now()
    min_agora = agora.hour * 60 + agora.minute
    ro = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rw = None
    n = 0
    try:
        for k in range(int(dias)):
            d = hoje - timedelta(days=k)
            if not calendario_b3.eh_pregao(d):
                continue
            if d == hoje and min_agora < FIM_SESSAO:
                continue
            dia_idx = _dia_idx(d)
            if _ja_consolidado(ro, dia_idx):
                continue
            linhas = consolidar_dia(ro, dia_idx)
            if not linhas:
                continue
            if rw is None:
                rw = banco.abrir(str(db_path), SCHEMA)
            gravar(rw, linhas)
            n += 1
    finally:
        ro.close()
        if rw is not None:
            rw.close()
    return n


def main():
    ap = argparse.ArgumentParser(description="Consolida o fechamento diario por ativo (fechamento_diario)")
    ap.add_argument("--db", default="dados/book.db")
    ap.add_argument("--dia", help="AAAA-MM-DD: consolida so esse dia")
    ap.add_argument("--dias", type=int, default=10, help="janela de dias corridos para os pendentes")
    args = ap.parse_args()
    if args.dia:
        ro = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
        try:
            linhas = consolidar_dia(ro, args.dia)
        finally:
            ro.close()
        rw = banco.abrir(args.db, SCHEMA)
        try:
            n = gravar(rw, linhas)
        finally:
            rw.close()
        print(f"{args.dia}: {n} ativo(s)")
        for l in linhas:
            print(f"  {l['ativo']:8s} fonte={l['fonte']:14s} abertura={l['abertura']} "
                  f"ultimo_liquido={l['ultimo_liquido']} ultimo_dia={l['ultimo_dia']}")
    else:
        print(f"{consolidar_pendentes(args.db, args.dias)} pregao(oes) consolidado(s)")


if __name__ == "__main__":
    main()
