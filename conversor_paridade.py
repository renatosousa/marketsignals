"""Conversao de escala entre ativos relacionados: BOVA11 -> WIN$ e DOL -> WDO$ (ESPEC_V3.md 6.1).

O grafico e a regua do WIN$ usam pontos do indice; as paredes de opcoes (call/put wall, gamma flip) sao
publicadas em reais do BOVA11. A razao nao e fixa: o WIN/BOVA muda com o tempo (dias de vencimento, fluxo
de caixa) e usar a razao do fechamento no meio do pregao desloca os strikes. Aqui:

  k_win_bova(con, agora_ms)   mediana dos ultimos 5 min de mid(WIN$)/mid(BOVA11) nos snapshots com spread > 0,
                              casando cada segundo do WIN com o BOVA mais proximo ate 2 s. Antes das 10:05
                              (o BOVA11 so negocia a partir das 10:00) ou sem dado ao vivo, cai no
                              fechamento do pregao anterior: ultimo_liquido(WIN$)/ultimo_liquido(BOVA11)
                              multiplicado por (1 + DI1F28/100)^(1/252), um dia de carrego da taxa DI.

  strike_em_pontos(k, k_win)  strike do BOVA11 -> pontos do WIN, arredondado ao multiplo de 5 pontos (tick
                              do WIN), que e o mesmo arredondamento de grafico_quant.em_pontos.

  fator_wdo(con, dia, venc)   forward do DOL do vencimento / forward do DOL mais negociado. O fator e o que o
                              posicoes_b3_dolar.py grava na coluna `fator` de b3_dolar_series (F do vencimento
                              / F do futuro de referencia, o que o WDO$ acompanha); se a serie ainda nao existir,
                              o fator e recalculado dos ajustes da ultima sessao publicada.

  strike_wdo(k, fator)        strike do DOL -> pontos do WDO$ da frente (k / fator), arredondado a 0,5 ponto.

Somente leitura. A sessao da B3 e a de `fechamento_diario`: futuros 09:00-18:30, acoes/ETFs 10:00-17:00.
Antes do fechamento a posicao em aberto da B3 ainda nao foi publicada, entao fator_wdo usa a ultima sessao
ESTRITAMENTE anterior ao dia pedido (D-1), como as opcoes em grafico_quant.posicoes.
"""
import sqlite3
from datetime import date, timedelta

import pandas as pd

import calendario_b3 as cal

DIA_MS = 86_400_000
JANELA_K_MS = 5 * 60_000
TOLERANCIA_K_MS = 2_000
ABERTURA_BOVA_MIN = 10 * 60 + 5     # antes disso so ha preco de ontem no BOVA11
TICK_WIN = 5.0
TICK_WDO = 0.5


def _dia_idx(agora_ms):
    return int(agora_ms) // DIA_MS


def _iso(dia_idx):
    return (date(1970, 1, 1) + timedelta(days=int(dia_idx))).isoformat()


def _pregao_anterior(dia_idx):
    """Indice do ultimo pregao estritamente anterior ao dia (calendario ANBIMA)."""
    return (cal.pregao_anterior(_iso(dia_idx)) - date(1970, 1, 1)).days


def _mid_win_bova(con, ini_ms, fim_ms):
    """Snapshots de WIN$ e BOVA11 na janela, com mid valido (spread > 0)."""
    sql = ("SELECT ts_ms, mid FROM snapshots WHERE symbol=? AND ts_ms BETWEEN ? AND ? "
           "AND spread > 0 AND mid > 0 ORDER BY ts_ms")
    try:
        win = pd.read_sql(sql, con, params=("WIN$", int(ini_ms), int(fim_ms)))
        bova = pd.read_sql(sql, con, params=("BOVA11", int(ini_ms), int(fim_ms)))
    except sqlite3.OperationalError:      # tabela ausente
        return None
    if win.empty or bova.empty:
        return None
    return win, bova


def _k_ao_vivo(con, agora_ms):
    """Mediana de mid(WIN$)/mid(BOVA11) dos ultimos 5 min; None sem os dois lados."""
    par = _mid_win_bova(con, int(agora_ms) - JANELA_K_MS, int(agora_ms))
    if par is None:
        return None
    win, bova = par
    m = pd.merge_asof(win.sort_values("ts_ms"), bova.rename(columns={"mid": "mid_bova"}).sort_values("ts_ms"),
                      on="ts_ms", tolerance=TOLERANCIA_K_MS, direction="nearest")
    m = m.dropna(subset=["mid_bova"])
    m = m[(m.mid > 0) & (m.mid_bova > 0)]
    if m.empty:
        return None
    k = float((m.mid / m.mid_bova).median())
    return k if k > 0 else None


def _fechamento_ativo(con, ativo, dia_idx):
    """ultimo_liquido do ativo no fechamento_diario; sem a linha, o ultimo preco da sessao na fonte bruta."""
    try:
        r = con.execute("SELECT ultimo_liquido FROM fechamento_diario WHERE dia_idx=? AND ativo=?",
                        (int(dia_idx), ativo)).fetchone()
    except sqlite3.OperationalError:
        r = None
    if r and r[0]:
        return float(r[0])
    ini, fim = int(dia_idx) * DIA_MS, (int(dia_idx) + 1) * DIA_MS
    if ativo in ("WIN$", "BOVA11"):
        sql = ("SELECT last FROM snapshots WHERE symbol=? AND ts_ms BETWEEN ? AND ? "
               "AND spread > 0 AND last > 0 ORDER BY ts_ms DESC LIMIT 1")
    else:
        sql = ("SELECT last FROM macro_cotacoes WHERE symbol=? AND ts_ms BETWEEN ? AND ? "
               "AND bid > 0 AND ask > bid AND last > 0 ORDER BY ts_ms DESC LIMIT 1")
    r = con.execute(sql, (ativo, ini, fim)).fetchone()
    return float(r[0]) if r and r[0] else None


def _di1(con, dia_idx):
    """DI1F28 (% a.a.) do fechamento do dia; None sem a taxa."""
    try:
        r = con.execute("SELECT ultimo_liquido FROM fechamento_diario WHERE dia_idx=? AND ativo='DI1F28'",
                        (int(dia_idx),)).fetchone()
    except sqlite3.OperationalError:
        r = None
    if r and r[0]:
        return float(r[0])
    r = con.execute("SELECT last FROM macro_cotacoes WHERE symbol='DI1F28' AND ts_ms BETWEEN ? AND ? "
                    "AND last > 0 ORDER BY ts_ms DESC LIMIT 1",
                    (int(dia_idx) * DIA_MS, (int(dia_idx) + 1) * DIA_MS)).fetchone()
    return float(r[0]) if r and r[0] else None


def k_win_bova(con, agora_ms):
    """Razao WIN$/BOVA11 valida no instante -> {"k": float, "origem": "ao_vivo"|"fechamento"}.

    Antes das 10:05 nao ha BOVA11 ao vivo (o ETF abre as 10:00) e a razao dos snapshots seria a de ontem
    contra um WIN que ja se moveu; nesse caso usa o fechamento de D-1 com um dia de carrego do DI1F28.
    Sem nenhuma das duas fontes devolve k=None (o chamador decide o que mostrar).
    """
    agora_ms = int(agora_ms)
    dia = _dia_idx(agora_ms)
    min_dia = (agora_ms % DIA_MS) // 60_000
    if min_dia >= ABERTURA_BOVA_MIN:
        k = _k_ao_vivo(con, agora_ms)
        if k:
            return {"k": k, "origem": "ao_vivo"}
    d1 = _pregao_anterior(dia)
    win = _fechamento_ativo(con, "WIN$", d1)
    bova = _fechamento_ativo(con, "BOVA11", d1)
    if not win or not bova:
        return {"k": None, "origem": "fechamento"}
    taxa = _di1(con, d1)
    carrego = (1 + taxa / 100) ** (1 / 252) if taxa else 1.0
    return {"k": float(win / bova * carrego), "origem": "fechamento"}


def strike_em_pontos(k_strike, k, tick=TICK_WIN):
    """Strike do BOVA11 -> pontos do WIN no tick (5 pontos); None sem a razao."""
    if k_strike is None or not k:
        return None
    return float(round(float(k_strike) * float(k) / tick) * tick)


def fator_wdo(con, dia, vencimento):
    """Forward do DOL do vencimento / forward do contrato mais negociado (o que o WDO$ acompanha).

    `dia` aceita indice do dia, date ou 'AAAA-MM-DD'. Usa o `fator` de b3_dolar_series da ultima sessao
    publicada ANTES do dia (a posicao de D-1 so sai a noite) e, sem ele, recalcula dos ajustes da mesma
    sessao pelo `num_neg` de cada vencimento. -> float ou None.
    """
    if isinstance(dia, int):
        dia_iso = _iso(dia)
    else:
        dia_iso = str(dia)[:10]
    venc = str(vencimento)[:10] if vencimento else None
    if not venc:
        return None
    try:
        sessao = con.execute("SELECT max(sessao) FROM b3_dolar_series WHERE sessao < ?", (dia_iso,)).fetchone()[0]
    except sqlite3.OperationalError:
        return None
    if not sessao:
        return None
    r = con.execute("SELECT fator FROM b3_dolar_series WHERE sessao=? AND vencimento=? AND fator IS NOT NULL "
                    "AND fator > 0 LIMIT 1", (sessao, venc)).fetchone()
    if r:
        return float(r[0])
    # sem o fator gravado: forward de cada vencimento / forward do vencimento mais negociado
    try:
        fut = con.execute("SELECT vencimento, max(futuro), sum(coalesce(num_neg, 0)) FROM b3_dolar_series "
                          "WHERE sessao=? AND futuro > 0 GROUP BY vencimento", (sessao,)).fetchall()
    except sqlite3.OperationalError:
        return None
    fut = [f for f in fut if f[1]]
    if not fut:
        return None
    alvo = next((f for f in fut if f[0] == venc), None)
    if not alvo:
        return None
    ref = max(fut, key=lambda f: f[2] or 0)[1]
    return float(alvo[1] / ref) if ref else None


def strike_wdo(k, fator):
    """Strike do DOL -> pontos do WDO$ da frente (k / fator), arredondado a 0,5 ponto; None sem o fator."""
    if k is None or not fator:
        return None
    return float(round(float(k) / float(fator) / TICK_WDO) * TICK_WDO)
