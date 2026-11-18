"""Saldo por grupo de corretora reconstruido pela fita do Tryd: o fallback de quando a janela de "Saldo por
corretora" para de exportar (ESPEC_V3.md secao 3.3).

De 13/11/2026 18:29 a pelo menos 17/11 as 7 janelas de saldo do Tryd pararam de exportar (o DDE perde a
exportacao quando a janela e reaberta; ver coletor_tryd.py) enquanto a fita de negocios seguiu em dia. O
painel mostrava "Tryd saldo e fita: ok" porque usava o mais recente das duas fontes, entao ninguem foi avisado
por 2 pregoes. Aqui a fita, que tem corretora compradora, vendedora e agressor de cada negocio, reconstroi a
serie de saldo e de agressao por grupo.

Regra (por negocio do dia/ativo, ordem por `num`; o grupo sai de players.grupo):
  saldo  grupo do comprador += qtd; grupo do vendedor -= qtd. No RLP a corretora e a mesma nas duas pontas
         (varejo cruzado com o provedor de liquidez da propria casa), entao soma zero, como no saldo real.
  agr    agressor "Comprador": grupo do comprador += qtd; "Vendedor": grupo do vendedor -= qtd. RLP, Direto e
         Leilao nao entram: nao tem agressor.
  captura  a janela de negocios do Tryd guarda os N ultimos negocios e se redesenha ~4x/s, entao parte se
         perde. O `num` anda de 10 em 10: por minuto (campo `hora`), captura = negocios lidos /
         ((max(num) - min(num)) / 10 + 1). O fluxo do minuto e dividido pela captura (minimo 0,2: abaixo
         disso o ajuste explode e o minuto fica com o piso). A serie sem ajuste tambem e devolvida, para
         comparacao: o ajuste corrige o volume esperado de cada minuto, nao o instante em que cada grupo
         entrou. Como o saldo e uma quase-cancelacao (compras contra vendas), o ajuste amplia o ruido da
         amostra: no dia 13/11/2026 a correlacao minuto a minuto com o saldo real ficou entre -0,28 e 0,93
         por grupo e o nivel final se afastou em varios grupos. Use a serie como forma, nao como nivel exato.

Limites: so existe fita do WIN e do WDO; a fita e uma amostra (45 a 75% dos negocios, ver players.py), entao
o resultado e uma ESTIMATIVA marcada como FITA_FALLBACK - a fonte exata e a janela de Saldo do Tryd. O
agrupamento das corretoras e o prior de players.MAPA, nao um dado.

Convencoes do projeto: ts_ms = hora de Brasilia codificada como epoch UTC; dia = ts_ms // 86_400_000; as
series saem com t em segundos e grade de 1 minuto, no mesmo formato de players.saldo_do_dia.
"""
import sqlite3
from datetime import date, timedelta

import numpy as np

import calendario_b3 as cal
import players

D = 86_400_000
CAPTURA_MINIMA = 0.2          # divisor minimo do ajuste (captura de 20%)
INICIO_COBRANCA_MIN = 9 * 60 + 5
FIM_PREGAO_MIN = 18 * 60 + 25
# minuto do dia a partir do campo `hora` ("hh:mm:ss.mmm"); sem hora valida, usa o ts da captura
_MINUTO = ("CASE WHEN hora GLOB '[0-9][0-9]:[0-9][0-9]*' "
           "THEN CAST(substr(hora, 1, 2) AS INTEGER) * 60 + CAST(substr(hora, 4, 2) AS INTEGER) "
           "ELSE (ts_ms / 60000) % 1440 END")


def _data(dia):
    """date do indice do dia (dias desde 1970-01-01)."""
    return date(1970, 1, 1) + timedelta(days=int(dia))


def captura(negocios, num_min, num_max):
    """Parte dos negocios que a fita capturou no intervalo: `num` anda de 10 em 10 (players.cobertura_fita)."""
    if not negocios or num_min is None or num_max is None:
        return 0.0
    esperado = (int(num_max) - int(num_min)) // 10 + 1
    return float(negocios) / esperado if esperado > 0 else 0.0


def saldo_da_fita(con, dia, ativo, ajustar=True):
    """Saldo e agressao por grupo reconstruidos pela fita do dia/ativo, no formato de players.saldo_do_dia.

    -> {"t": [seg], "series": {grupo: {"saldo": [...], "agr": [...]}}, "fonte_saldo": "FITA_FALLBACK",
    "captura_media": fracao (0 a 1), "series_sem_ajuste": {grupo: {"saldo": [...], "agr": [...]}} quando
    ajustar=True}; None sem fita no dia. A grade e de 1 minuto, do primeiro ao ultimo minuto com negocio, e
    os valores sao acumulados no dia. `ajustar=False` devolve a serie crua (fluxo do minuto sem dividir pela
    captura). As consultas agregam no SQLite por minuto/corretoras: ler 1,7 milhao de negocios no Python
    levaria segundos a cada pedido."""
    dia = int(dia)
    try:
        faixas = con.execute(f"SELECT {_MINUTO} AS m, count(*), min(num), max(num) FROM tryd_fita "
                             "WHERE dia=? AND ativo=? GROUP BY m ORDER BY m", (dia, ativo)).fetchall()
        fluxos = con.execute(f"SELECT {_MINUTO} AS m, comprador, vendedor, agressor, coalesce(sum(qtd), 0) "
                             "FROM tryd_fita WHERE dia=? AND ativo=? GROUP BY m, comprador, vendedor, agressor",
                             (dia, ativo)).fetchall()
    except sqlite3.OperationalError:      # tabela ausente: o fallback simplesmente nao existe
        return None
    faixas = [f for f in faixas if f[0] is not None]
    if not faixas:
        return None
    minutos = [int(m) for m, _n, _lo, _hi in faixas]
    caps = {int(m): captura(n, lo, hi) for m, n, lo, hi in faixas}
    m0, m1 = min(minutos), max(minutos)
    k = m1 - m0 + 1
    fluxo_saldo = {g: np.zeros(k) for g in players.GRUPOS}
    fluxo_agr = {g: np.zeros(k) for g in players.GRUPOS}
    for m, comp, vend, ag, q in fluxos:
        if m is None:
            continue
        i, q = int(m) - m0, float(q or 0)
        g_comp, g_vend = players.grupo(comp), players.grupo(vend)
        fluxo_saldo[g_comp][i] += q
        fluxo_saldo[g_vend][i] -= q          # RLP (comprador = vendedor) soma zero
        if ag == "Comprador":
            fluxo_agr[g_comp][i] += q
        elif ag == "Vendedor":
            fluxo_agr[g_vend][i] -= q
    serie_cap = np.array([caps.get(m, 1.0) for m in range(m0, m1 + 1)], dtype=float)
    fator = 1.0 / np.maximum(serie_cap, CAPTURA_MINIMA) if ajustar else np.ones(k)

    def series(fator):
        out = {}
        for g in players.GRUPOS:
            if np.any(fluxo_saldo[g]) or np.any(fluxo_agr[g]):
                out[g] = {"saldo": np.round(np.cumsum(fluxo_saldo[g] * fator), 6).tolist(),
                          "agr": np.round(np.cumsum(fluxo_agr[g] * fator), 6).tolist()}
        return out

    resposta = {"t": [dia * 86400 + m * 60 for m in range(m0, m1 + 1)], "series": series(fator),
                "fonte_saldo": "FITA_FALLBACK",
                "captura_media": float(np.mean([caps[m] for m in caps])) if caps else 0.0}
    if ajustar:
        resposta["series_sem_ajuste"] = series(np.ones(k))
    return resposta


def saldo_atrasado(con, ativo, agora_ms, limite_s=60):
    """True quando estamos no pregao (calendario_b3.eh_pregao e 09:05-18:25) e o ultimo tryd_saldo do ativo
    tem mais de `limite_s` (ou nao existe foto hoje). Fora do pregao nao cobra: o saldo so chega das 09:00 as
    18:25 e a janela fechada a noite nao e falha."""
    agora_ms = int(agora_ms)
    dia = agora_ms // D
    min_agora = (agora_ms % D) // 60_000
    if not (cal.eh_pregao(_data(dia)) and INICIO_COBRANCA_MIN <= min_agora <= FIM_PREGAO_MIN):
        return False
    try:
        r = con.execute("SELECT max(ts_ms) FROM tryd_saldo WHERE ativo=? AND ts_ms>=? AND ts_ms<?",
                        (ativo, dia * D, (dia + 1) * D)).fetchone()
    except sqlite3.OperationalError:
        return True
    ultimo = r[0] if r else None
    return ultimo is None or agora_ms - ultimo > limite_s * 1000
