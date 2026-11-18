"""Estruturas de opcoes por cenario da superficie (ESPEC_V3.md 6.3.B).

O motor_regioes.py diz onde o preco pode reagir; aqui se escolhe COMO operar a volatilidade e a direcao
com as series que existem no livro. O modulo cruza tres eixos:

  direcao   vies do motor_cenario (-100 a +100): <= -25 baixa, >= +25 alta, no meio lateral;
  IV        VRP = IV ATM - vol realizada de 20 pregoes e o percentil da IV ATM em 60 pregoes:
            cara quando VRP > 0 e IV no p70+, barata quando VRP <= 0 e IV no p35-, normal no resto;
  skew      RR25 (IV da put 25-delta menos IV da call 25-delta, positivo = puts caras) no p90+ = puts
            caras; no p10- = calls caros.

A tabela da ESPEC 6.3.B tem 4 linhas (queda com IV cara e puts caras, queda com IV barata, lateral com
GEX positivo e IV alta, alta com IV moderada). Os cenarios que a tabela nao cobre recebem uma regra
explicita do modulo (`_plano`), sempre com a nota no resultado.

Para cada estrutura sugerida o modulo MONTA com strikes reais de `opcoes_cotacoes` (ultima cotacao de
cada serie com idade <= 10 min e bid/ask validos): pernas (serie, compra/venda, quantidade, strike,
vencimento, mid, bid, ask), custo/credito no mid e no pior lado (compra no ask, venda no bid),
breakeven(s), ganho e perda maximos no vencimento, retorno/risco e as gregas agregadas por
Black-Scholes com a IV de cada serie (delta em unidades do subjacente por opcao, gama por R$ 1 do
subjacente, vega por 1 ponto percentual de IV). Sem serie liquida que sirva, o campo `motivo` diz o
que faltou; nada e inventado.

Max pain = strike que minimiza o valor intrinseco total das posicoes em aberto (D-1 da B3):
soma(oi_call x max(0, K - k)) + soma(oi_put x max(0, k - K)).

Dolar (opcoes de DOL/WDO, D-1 do posicoes_b3_dolar.py): somente diagnostico (IV ATM, skew, walls, max
pain, gamma flip) porque nao ha livro ao vivo dessas opcoes no banco; a montagem fica de fora e o
motivo vai no resultado.

Causalidade: toda leitura usa ts_ms <= agora_ms. As posicoes em aberto sao D-1 (gq.posicoes e
gq.posicoes_dolar ja exigem sessao < dia). As gregas usam a IV da propria serie no instante; sem IV,
a IV ATM do vencimento. Somente leitura.
"""
import json
import math
import sqlite3
from datetime import date, timedelta

import numpy as np
import pandas as pd

import calendario_b3 as cal
import grafico_quant as gq
import motor_regioes as mr

DIA_MS = 86_400_000
JANELA_IDADE_MS = 10 * 60_000
DU_MIN = 5
RV_DIAS = 20
HIST_DIAS = 60
P_CARA, P_BARATA, P_SKEW = 70.0, 35.0, 90.0
P_SKEW_BARATA = 10.0
MULT_BOVA = 1.0
FAIXA_STRIKES = 0.25            # strikes da estrutura dentro de +-25% do spot
N_GRADE = 4001                  # pontos da grade de payoff
DIAS_VENC_SEMANAL = 5


# ------------------------------------------------------------------ utilidades

def _iso(dia_idx):
    return (date(1970, 1, 1) + timedelta(days=int(dia_idx))).isoformat()


def _dia_idx(ts_ms):
    return int(ts_ms) // DIA_MS


def _finito(x):
    return x is not None and isinstance(x, (int, float, np.floating)) and math.isfinite(float(x))


def _pct(valor, hist):
    """Percentil de `valor` na amostra `hist` (0 a 100); None sem historico."""
    h = [float(x) for x in hist if _finito(x)]
    if valor is None or not _finito(valor) or len(h) < 5:
        return None, len(h)
    return float(100.0 * np.mean(np.array(h) <= float(valor))), len(h)


def _ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _npdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bs_preco(tipo, s, k, t, r, vol):
    """Black-Scholes de preco (tipo 'C' ou 'P'); None para prazo/vol invalidos."""
    if not (_finito(s) and _finito(k) and _finito(t) and _finito(vol)) or s <= 0 or k <= 0 or t <= 0 or vol <= 0:
        return None
    d1 = (math.log(s / k) + (r + 0.5 * vol * vol) * t) / (vol * math.sqrt(t))
    d2 = d1 - vol * math.sqrt(t)
    if tipo == "C":
        return s * _ncdf(d1) - k * math.exp(-r * t) * _ncdf(d2)
    return k * math.exp(-r * t) * _ncdf(-d2) - s * _ncdf(-d1)


def bs_gregas(tipo, s, k, t, r, vol):
    """(delta, gama, vega) de Black-Scholes: delta por opcao, gama por R$ 1, vega por 1 p.p. de IV."""
    if not (_finito(s) and _finito(k) and _finito(t) and _finito(vol)) or s <= 0 or k <= 0 or t <= 0 or vol <= 0:
        return None
    d1 = (math.log(s / k) + (r + 0.5 * vol * vol) * t) / (vol * math.sqrt(t))
    delta = _ncdf(d1) if tipo == "C" else _ncdf(d1) - 1.0
    gama = _npdf(d1) / (s * vol * math.sqrt(t))
    vega = s * _npdf(d1) * math.sqrt(t) / 100.0
    return float(delta), float(gama), float(vega)


def _t_anos(dia_iso, venc, agora_ms):
    """Prazo em anos do coletor_opcoes.prazo_anos: fracao restante do pregao de opcoes + dias uteis / 252."""
    min_dia = (int(agora_ms) % DIA_MS) / 60_000.0
    h = min_dia / 60.0
    frac = min(max((17.0 - h) / 7.0, 0.0), 1.0)
    if not cal.eh_pregao(_iso(_dia_idx(agora_ms))):
        frac = 0.0
    return (frac + cal.du_ate_vencimento(dia_iso, venc)) / 252.0


def _fechamentos(con, ativo, dia_idx, n):
    """Ultimos n fechamentos com dados ANTES de dia_idx (calendario B3), do mais antigo ao mais novo."""
    dias = mr.dias_com_dados(con, ativo, dia_idx, n)
    out = []
    for d in reversed(dias):
        ext = mr._extremos_dia(con, ativo, d)
        if ext and _finito(ext[2]):
            out.append(float(ext[2]))
    return out


def _rv(con, ativo, dia_idx, n=RV_DIAS):
    """Vol realizada anualizada (desvio dos log-retornos diarios x raiz(252)) dos n pregoes antes do dia."""
    p = _fechamentos(con, ativo, dia_idx, n + 1)
    if len(p) < 10:
        return None, len(p)
    r = np.diff(np.log(np.array(p, dtype=float)))
    if len(r) < 5 or not np.all(np.isfinite(r)):
        return None, len(p)
    return float(np.std(r, ddof=1) * math.sqrt(252)), len(p) - 1


# ------------------------------------------------------------------ superficie BOVA11

def _resumos(con, agora_ms):
    """Ultima linha de opcoes_resumo de cada vencimento nos 10 min antes de agora (DataFrame)."""
    ini = int(agora_ms) - JANELA_IDADE_MS
    try:
        df = pd.read_sql("SELECT ts_ms, vencimento, t_anos, atm_iv, iv_call25, iv_put25, rr25, taxa "
                         "FROM opcoes_resumo WHERE subjacente='BOVA11' AND ts_ms BETWEEN ? AND ? "
                         "AND atm_iv > 0 AND atm_iv < 3 ORDER BY ts_ms", con, params=(ini, int(agora_ms)))
    except sqlite3.OperationalError:
        return pd.DataFrame(columns=["ts_ms", "vencimento", "t_anos", "atm_iv", "iv_call25", "iv_put25",
                                     "rr25", "taxa"])
    if df.empty:
        return df
    return df.sort_values("ts_ms").groupby("vencimento", as_index=False).last()


def _vencimentos_validos(df, dia_iso, minimo=DU_MIN):
    """Vencimentos com >= minimo dias uteis, ordenados pelo prazo; cada linha ganha a coluna du."""
    out = []
    for r in df.itertuples():
        venc = str(r.vencimento)[:10]
        du = cal.du_ate_vencimento(dia_iso, venc)
        if du >= minimo:
            out.append(dict(vencimento=venc, du=du, t_anos=float(r.t_anos) if _finito(r.t_anos) else _t_anos(
                dia_iso, venc, 0), atm_iv=float(r.atm_iv), iv_call25=r.iv_call25, iv_put25=r.iv_put25,
                rr25=r.rr25, ts_ms=int(r.ts_ms)))
    out.sort(key=lambda x: (x["du"], x["vencimento"]))
    return out


def _historico_iv(con, agora_ms, n=HIST_DIAS):
    """(dias, iv_atm, rr25) do vencimento mais proximo com >= 5 dias uteis, ultima leitura de cada dia anterior.

    rr25 sai na convencao do modulo (put 25d - call 25d); o opcoes_resumo grava call - put, entao o sinal
    e invertido aqui. Serve de referencia para os percentis de IV e de skew.
    """
    dia = _dia_idx(agora_ms)
    try:
        rows = con.execute(
            "SELECT dia, vencimento, atm_iv, rr25 FROM ("
            "  SELECT ts_ms/86400000 dia, vencimento, atm_iv, rr25,"
            "         row_number() OVER (PARTITION BY ts_ms/86400000, vencimento ORDER BY ts_ms DESC) rn"
            "  FROM opcoes_resumo WHERE subjacente='BOVA11' AND ts_ms < ? AND atm_iv > 0 AND atm_iv < 3)"
            " WHERE rn=1", (int(dia) * DIA_MS,)).fetchall()
    except sqlite3.OperationalError:
        return [], [], []
    por_dia = {}
    for d, venc, iv, rr in rows:
        iso = _iso(d)
        du = cal.du_ate_vencimento(iso, str(venc)[:10])
        if du < DU_MIN or not _finito(iv):
            continue
        atual = por_dia.get(iso)
        if atual is None or du < atual[0]:
            por_dia[iso] = (du, float(iv), None if not _finito(rr) else -float(rr))
    dias = sorted(por_dia)[-int(n):]
    return ([d for d in dias], [por_dia[d][1] for d in dias],
            [por_dia[d][2] for d in dias if por_dia[d][2] is not None])


def _quantis(con, ativo, agora_ms, spot):
    """q05/q15/q85/q95 (e q10/q90 interpolados) do modelo de regime ou da faixa gaussiana do regime_live."""
    niveis, origem = mr._niveis_quantis(con, ativo, agora_ms, spot, {})
    por_tag = {n["tag"]: float(n["preco"]) for n in niveis if n.get("tag")}
    out = dict(origem_quantis=origem, q05=por_tag.get("q05"), q15=por_tag.get("q15"),
               q85=por_tag.get("q85"), q95=por_tag.get("q95"))
    # q10/q90 por interpolacao normal entre q15/q85 (a ESPEC usa q0,10/q0,90 no condor)
    if all(_finito(out[k]) for k in ("q15", "q85")):
        centro = 0.5 * (out["q15"] + out["q85"])
        sigma = (out["q85"] - out["q15"]) / (2.0 * mr.Z["q15"])
        out["q10"] = centro - 1.2816 * sigma
        out["q90"] = centro + 1.2816 * sigma
    else:
        out["q10"] = out["q90"] = None
    return out


def _max_pain(series, spot=None, venc=None, faixa=FAIXA_STRIKES, strikes=None):
    """Strike que minimiza o valor intrinseco total das posicoes em aberto (calls + puts).

    Restrito a um vencimento (o principal, o mesmo das estruturas), a faixa de strikes do diagnostico
    e, quando `strikes` vem, aos strikes com cotacao no instante: somar todos os vencimentos joga o max
    pain para o strike de maior OI de um vencimento distante, e a posicao D-1 da B3 traz OI absurdo em
    strikes que nao negociam (11 milhoes de contratos numa serie sem bid/ask).
    """
    if series is None or len(series) == 0:
        return None
    s = series[(series.oi > 0) & series.strike.gt(0)]
    if venc is not None and "venc" in s:
        s = s[s.venc == venc]
    if s.empty:
        return None
    if spot:
        s = s[s.strike.between(float(spot) * (1 - faixa), float(spot) * (1 + faixa))]
    if strikes:
        s = s[s.strike.isin([float(x) for x in strikes])]
    if s.empty:
        return None
    strikes = np.array(sorted(s.strike.unique()), dtype=float)
    if len(strikes) < 2:
        return None
    mult = s.mult.fillna(MULT_BOVA).to_numpy(float) if "mult" in s else np.ones(len(s))
    oi = s.oi.to_numpy(float)
    k = s.strike.to_numpy(float)
    call = (s.tipo == "C").to_numpy()
    melhor, dor = None, None
    for K in strikes:
        valor = np.where(call, np.maximum(0.0, K - k), np.maximum(0.0, k - K)) * oi * mult
        total = float(valor.sum())
        if dor is None or total < dor:
            melhor, dor = float(K), total
    return melhor, dor


def _gex(con, pos, spot, dia_iso, agora_ms, atm_iv, ivs=None, taxa=None, n_wall=6):
    """GEX/walls/flip do grafico_quant com a IV ao vivo; tolera a ausencia das tabelas de posicao.

    n_wall alto porque as paredes uteis sao escolhidas pelo lado (put wall <= spot, call wall >= spot);
    a lista crua do GEX pode ter a maior parede de put acima do spot num dia de queda.
    """
    if pos is None or not spot:
        return None
    if taxa is None:
        taxa = mr._taxa_opcoes(con, agora_ms)
    try:
        return gq.gex(pos, spot, dia_iso, taxa, ivs, atm_iv, n_wall=n_wall)
    except (sqlite3.OperationalError, ValueError, KeyError):
        return None


def _parede(walls, spot, lado):
    """Parede de put (strike <= spot) ou de call (strike >= spot) de maior |GEX|; None se nao houver."""
    if not walls:
        return None
    sinal = -1 if lado == "put" else 1
    cand = [w for w in walls if sinal * (float(w["k"]) - float(spot)) >= 0]
    if not cand:
        return None
    return float(max(cand, key=lambda w: abs(float(w["gex"])))["k"])


def superficie(con, agora_ms, ativo="BOVA11", cot=None):
    """Diagnostico da superficie do BOVA11 (ou do dolar) no instante -> dict.

    BOVA11: IV ATM e percentil em 60 pregoes, VRP contra a vol realizada de 20, RR25 e percentil, GEX
    (sinal), put/call wall, gamma flip, max pain e os quantis do dia. Dolar (ativo 'DOL'/'WDO$'):
    mesma leitura pela posicao D-1 de b3_dolar_series, com o spot do WDO$ e Black-76 sobre o forward.
    `cot` (opcional) e a tabela de cotacoes liquidas ja lida por quem chama, para nao reler.
    """
    agora_ms = int(agora_ms)
    dia = _dia_idx(agora_ms)
    dia_iso = _iso(dia)
    ativo_up = str(ativo).upper()
    if ativo_up in ("DOL", "WDO", "WDO$", "DOL$"):
        return _superficie_dolar(con, agora_ms)
    out = dict(ativo="BOVA11", dia=dia_iso, faltando=[])
    spot = mr._spot_opcoes(con, "BOVA11", agora_ms, None)
    out["spot"] = spot
    if not spot:
        out["faltando"].append("sem preco do BOVA11 (snapshot nem fechamento de D-1)")
        return out
    resumos = _resumos(con, agora_ms)
    validos = _vencimentos_validos(resumos, dia_iso)
    if not validos:
        out["faltando"].append("opcoes_resumo sem vencimento com 5+ dias uteis nos 10 min antes de agora")
        return out
    principal = validos[0]
    seguinte = validos[1] if len(validos) > 1 else None
    out.update(vencimento=principal["vencimento"], du=principal["du"], iv_atm=principal["atm_iv"],
               vencimento_seguinte=seguinte["vencimento"] if seguinte else None,
               du_seguinte=seguinte["du"] if seguinte else None,
               iv_atm_seguinte=seguinte["atm_iv"] if seguinte else None)
    dias, ivs_hist, rr_hist = _historico_iv(con, agora_ms)
    out["iv_atm_pct"], out["iv_atm_n"] = _pct(principal["atm_iv"], ivs_hist)
    rr_atual = None
    if _finito(principal["iv_call25"]) and _finito(principal["iv_put25"]):
        rr_atual = float(principal["iv_put25"]) - float(principal["iv_call25"])
    elif _finito(principal["rr25"]):
        rr_atual = -float(principal["rr25"])
    out["rr25"] = rr_atual
    out["rr25_pct"], out["rr25_n"] = _pct(rr_atual, rr_hist)
    out["rr25_convencao"] = "put25 - call25 (positivo = puts caras); opcoes_resumo.rr25 = call - put"
    out["skew"], out["skew_obs"] = _skew(rr_atual, out["rr25_pct"])
    if out["skew"] == "puts_caras" and _finito(rr_atual) and rr_atual < 0:
        out["skew_obs"] += "; em nivel as puts ainda estao abaixo das calls (caras so contra o historico)"
    rv, n_rv = _rv(con, "BOVA11", dia)
    out["rv20"], out["rv20_n"] = rv, n_rv
    out["vrp"] = (principal["atm_iv"] - rv) if (rv and _finito(principal["atm_iv"])) else None
    q = _quantis(con, "BOVA11", agora_ms, spot)
    out.update({k: q[k] for k in ("q05", "q10", "q15", "q85", "q90", "q95", "origem_quantis")})
    try:
        pos = gq.posicoes(con, dia_iso)
    except (sqlite3.OperationalError, ValueError):
        pos = None
    if pos is None:
        out["faltando"].append("sem posicao em aberto de BOVA11 (b3_posicoes_series/opcoes_manual_series)")
    else:
        out["posicoes_fonte"] = pos["fonte"]
        out["posicoes_ref"] = pos["ref"]
        taxa = mr._taxa_opcoes(con, agora_ms)
        ivs = mr._ivs(con, agora_ms)
        gx = _gex(con, pos, spot, dia_iso, agora_ms, principal["atm_iv"], ivs, taxa)
        if gx:
            out["gex"] = gx["gex_total"]
            out["gex_sinal"] = "negativo" if gx["gex_total"] < 0 else ("positivo" if gx["gex_total"] > 0 else "nulo")
            out["gamma_flip"] = gx.get("flip")
            out["put_wall"] = _parede(gx.get("put_walls"), spot, "put")
            out["call_wall"] = _parede(gx.get("call_walls"), spot, "call")
            out["gex_taxa"] = taxa
        else:
            out["faltando"].append("GEX sem strikes na faixa de +-25% do spot")
        venc_principal = principal["vencimento"]
        liquidos = None
        if cot is None:
            cot = _cotacoes(con, agora_ms)
        if not cot.empty:
            liquidos = set(float(x) for x in cot[cot.vencimento == venc_principal].strike.unique())
        mp = _max_pain(pos["series"], spot, venc_principal, strikes=liquidos)
        if mp is None:
            mp = _max_pain(pos["series"], spot, strikes=liquidos)
        if mp:
            out["max_pain"], out["max_pain_dor"] = mp
        out["max_pain_fonte"] = "posicoes D-1 do vencimento principal, strikes com cotacao no instante"
    if out.get("gex_sinal") is None:
        out["faltando"].append("sem GEX (posicao em aberto ausente)")
    return out


def _interp_iv(g, alvo):
    """IV interpolada no strike `alvo` do sorriso g (colunas strike, iv ordenadas); None fora da grade."""
    g = g[np.isfinite(g.strike) & np.isfinite(g.iv) & (g.iv > 0)].sort_values("strike")
    if len(g) < 2:
        return None
    return float(np.interp(float(alvo), g.strike.to_numpy(float), g.iv.to_numpy(float)))


def _rr_dolar(s, forward):
    """RR25 (put - call) do sorriso de um vencimento: IVs nos deltas +-0,25 por Black-76 (r=0)."""
    g = s[np.isfinite(s.iv) & (s.iv > 0)].copy()
    if len(g) < 4:
        return None
    t = float(np.maximum(s.t.fillna(0.02).iloc[0] if "t" in s else 0.02, 1e-6))
    deltas = []
    for r in g.itertuples():
        gr = bs_gregas(r.tipo, forward, float(r.strike), t, 0.0, float(r.iv))
        if gr:
            deltas.append((gr[0], float(r.iv)))
    if not deltas:
        return None
    put = min(deltas, key=lambda x: abs(x[0] + 0.25))
    call = min(deltas, key=lambda x: abs(x[0] - 0.25))
    if abs(put[0] + 0.25) > 0.12 or abs(call[0] - 0.25) > 0.12:
        return None
    return float(put[1] - call[1])


def _superficie_dolar(con, agora_ms):
    """Diagnostico das opcoes de dolar (D-1 da B3) com o spot do WDO$; sem montagem de estrutura."""
    agora_ms = int(agora_ms)
    dia_iso = _iso(_dia_idx(agora_ms))
    out = dict(ativo="DOL", dia=dia_iso, faltando=[], estrutura=None,
               motivo_estrutura="opcoes de dolar sem livro ao vivo no banco (posicao D-1): so diagnostico")
    spot = mr.preco_atual(con, "WDO$", agora_ms)
    if not spot:
        ent = mr._extremos_dia(con, "WDO$", mr._dia_anterior(_dia_idx(agora_ms)))
        spot = ent[2] if ent else None
    out["spot"] = spot
    if not spot:
        out["faltando"].append("sem preco do WDO$")
        return out
    try:
        pos = gq.posicoes_dolar(con, dia_iso)
    except (sqlite3.OperationalError, ValueError):
        pos = None
    if pos is None:
        out["faltando"].append("sem b3_dolar_series (posicao D-1 das opcoes de dolar)")
        return out
    out["posicoes_ref"] = pos["ref"]
    s = pos["series"].copy()
    s["du"] = [cal.du_ate_vencimento(dia_iso, v) for v in s.venc]
    s = s[s.du >= DU_MIN]
    if s.empty:
        out["faltando"].append("sem vencimento de dolar com 5+ dias uteis")
        return out
    venc = s.sort_values("du").venc.iloc[0]
    principal = s[s.venc == venc].copy()
    fator = float(principal.fator.dropna().iloc[0]) if principal.fator.notna().any() else 1.0
    forward = float(spot) * fator
    principal["t"] = principal.du / 252.0
    g = principal.groupby("strike").agg(iv=("iv", "mean"), tipo=("tipo", "first"))
    g = g.reset_index().assign(tipo="C")
    iv_atm = _interp_iv(g, forward)
    out.update(vencimento=venc, du=int(principal.du.iloc[0]), fator=fator, forward=forward, iv_atm=iv_atm)
    rv, n_rv = _rv(con, "WDO$", _dia_idx(agora_ms))
    out["rv20"], out["rv20_n"] = rv, n_rv
    out["vrp"] = (iv_atm - rv) if (rv and iv_atm) else None
    # percentil da IV ATM (e do RR25) nas sessoes ja publicadas da B3 (D-1), com a mesma regra de vencimento
    hist, hist_rr = [], []
    try:
        sessoes = [r[0] for r in con.execute("SELECT DISTINCT sessao FROM b3_dolar_series WHERE sessao < ? "
                                             "ORDER BY sessao", (dia_iso,))]
    except sqlite3.OperationalError:
        sessoes = []
    for sess in sessoes[-HIST_DIAS:]:
        try:
            ss = pd.read_sql("SELECT vencimento venc, tipo, strike, iv, fator, mult FROM b3_dolar_series "
                             "WHERE sessao=? AND oi > 0 AND iv > 0", con, params=(sess,))
        except sqlite3.OperationalError:
            continue
        if ss.empty:
            continue
        ss["du"] = [cal.du_ate_vencimento(sess, v) for v in ss.venc]
        ss = ss[ss.du >= DU_MIN]
        if ss.empty:
            continue
        v = ss.sort_values("du").venc.iloc[0]
        ss = ss[ss.venc == v]
        f = float(ss.fator.dropna().iloc[0]) if ss.fator.notna().any() else 1.0
        gg = ss.groupby("strike").iv.mean().reset_index()
        fech = mr._extremos_dia(con, "WDO$", (date.fromisoformat(sess) - date(1970, 1, 1)).days)
        if fech and _finito(fech[2]):
            fwd = float(fech[2]) * f
            hist.append(_interp_iv(gg, fwd))
            ss = ss.assign(t=ss.du / 252.0)
            rr = _rr_dolar(ss, fwd)
            if rr is not None:
                hist_rr.append(rr)
    out["iv_atm_pct"], out["iv_atm_n"] = _pct(iv_atm, hist)
    out["rr25"] = _rr_dolar(principal, forward)
    out["rr25_pct"], out["rr25_n"] = _pct(out["rr25"], hist_rr)
    out["skew"], out["skew_obs"] = _skew(out["rr25"], out["rr25_pct"])
    if out["skew"] == "puts_caras" and _finito(out["rr25"]) and out["rr25"] < 0:
        out["skew_obs"] += "; em nivel as puts ainda estao abaixo das calls (caras so contra o historico)"
    gx = _gex(con, pos, spot, dia_iso, agora_ms, iv_atm, None, 0.0)
    if gx:
        out["gex"] = gx["gex_total"]
        out["gex_sinal"] = "negativo" if gx["gex_total"] < 0 else ("positivo" if gx["gex_total"] > 0 else "nulo")
        out["gamma_flip"] = gx.get("flip")
        out["put_wall"] = _parede(gx.get("put_walls"), spot, "put")
        out["call_wall"] = _parede(gx.get("call_walls"), spot, "call")
    else:
        out["faltando"].append("GEX sem strikes na faixa de +-25% do spot do WDO$")
    mp = _max_pain(pos["series"], spot, venc)
    if mp is None:
        mp = _max_pain(pos["series"], spot)
    if mp:
        out["max_pain"], out["max_pain_dor"] = mp
    return out


# ------------------------------------------------------------------ cenario

def direcao_vies(vies):
    """'baixa' (-25 ou menos), 'alta' (+25 ou mais) ou 'lateral'."""
    v = float(vies or 0.0)
    if v <= -25:
        return "baixa"
    if v >= 25:
        return "alta"
    return "lateral"


def nivel_iv(diag):
    """'cara', 'barata' ou 'normal' pela regra da ESPEC (VRP e percentil da IV ATM); 'indefinida' sem dado."""
    vrp, ip = diag.get("vrp"), diag.get("iv_atm_pct")
    if vrp is None or ip is None:
        return "indefinida"
    if vrp > 0 and ip > P_CARA:
        return "cara"
    if vrp <= 0 and ip < P_BARATA:
        return "barata"
    return "normal"


def _skew(rr25, rr_pct):
    """(classe, motivo): puts caras no p90+, calls caros no p10-, neutro no meio."""
    if rr25 is None or rr_pct is None:
        return "indefinido", "RR25 sem historico suficiente"
    if rr_pct >= P_SKEW:
        return "puts_caras", f"RR25 {rr25:.3f} no p{rr_pct:.0f} (>= p{P_SKEW:.0f})"
    if rr_pct <= P_SKEW_BARATA:
        return "calls_caros", f"RR25 {rr25:.3f} no p{rr_pct:.0f} (<= p{P_SKEW_BARATA:.0f})"
    return "neutro", f"RR25 {rr25:.3f} no p{rr_pct:.0f}"


def cenario(vies, diag):
    """String 'direcao|iv|skew' do cenario (ex.: 'baixa|iv_cara|puts_caras')."""
    d = direcao_vies(vies)
    iv = nivel_iv(diag)
    sk, _ = _skew(diag.get("rr25"), diag.get("rr25_pct"))
    return f"{d}|iv_{iv}|{sk}"


def _plano(cen, diag):
    """Estruturas a favor e contra para o cenario, com o vencimento pedido e a nota da tabela.

    As 4 linhas da ESPEC 6.3.B estao marcadas com `tabela=True`; os cenarios intermediarios usam a regra
    declarada em `nota` (o modulo nao inventa que a tabela cobre o caso).
    """
    d, iv, sk = cen.split("|")
    gex_pos = diag.get("gex_sinal") == "positivo"
    nota, a_favor, contra = "", [], []
    if d == "baixa" and iv == "iv_cara":
        nota = "tabela 6.3.B linha 1 (queda + IV cara): comprar put a seco tem risco de vol crush"
        a_favor = [("trava_baixa_puts", "semanal"), ("trava_baixa_calls", "semanal")]
        contra = [("trava_alta_puts", "semanal")]
    elif d == "baixa" and iv == "iv_barata":
        nota = "tabela 6.3.B linha 2 (queda + IV barata): protecao subprecificada, convexidade a favor"
        a_favor = [("put_seca", "mensal"), ("put_backspread", "semanal")]
        contra = [("trava_alta_calls", "semanal")]
    elif d == "lateral":
        nota = ("tabela 6.3.B linha 3 (lateral + GEX positivo + IV alta): condor ancorado nas walls/q10/q90"
                if gex_pos else
                "tabela 6.3.B linha 3 pressupoe GEX positivo e IV alta; aqui GEX nao e positivo: condor sai como referencia")
        contra = [("iron_condor", "semanal")]
    elif d == "alta":
        nota = "tabela 6.3.B linha 4 (alta + IV moderada/baixa): trava de alta ou collar"
        a_favor = [("trava_alta_calls", "semanal"), ("collar", "semanal")]
        contra = [("trava_baixa_puts", "semanal")]
    elif d == "baixa":
        nota = ("IV normal: a tabela nao cobre; regra do modulo = trava de baixa a favor com o lado do "
                "skew (debito se puts caras, credito se nao) e trava de alta com puts contra")
        a_favor = ([("trava_baixa_puts", "semanal")] if sk == "puts_caras"
                   else [("trava_baixa_calls", "semanal")])
        contra = [("trava_alta_puts", "semanal")]
    else:
        nota = ("alta com IV indefinida/cara: a tabela nao cobre; regra do modulo = trava de alta com "
                "calls a favor e trava de baixa com puts somente contra esticada")
        a_favor = [("trava_alta_calls", "semanal")]
        contra = [("trava_baixa_puts", "semanal")]
    return dict(nota=nota, a_favor=a_favor, contra=contra)


# ------------------------------------------------------------------ cotacoes e montagem

def _cotacoes(con, agora_ms):
    """Ultima cotacao de cada serie nos 10 min antes de agora, com bid/ask validos -> DataFrame.

    Uma consulta por vencimento (o indice e (subjacente, vencimento, ts_ms)) com row_number por serie:
    ler as 10 min inteiras de todos os vencimentos traz dezenas de milhares de linhas e e o gargalo.
    """
    ini = int(agora_ms) - JANELA_IDADE_MS
    resumos = _resumos(con, agora_ms)
    vencs = sorted(set(str(v)[:10] for v in resumos.vencimento))
    colunas = ["ts_ms", "symbol", "vencimento", "tipo", "strike", "bid", "ask", "last", "iv", "delta"]
    if not vencs:
        return pd.DataFrame(columns=colunas)
    partes = []
    for v in vencs:
        try:
            partes.append(pd.read_sql(
                "SELECT ts_ms, symbol, vencimento, tipo, strike, bid, ask, last, iv, delta FROM ("
                "  SELECT ts_ms, symbol, vencimento, tipo, strike, bid, ask, last, iv, delta,"
                "         row_number() OVER (PARTITION BY tipo, strike ORDER BY ts_ms DESC) rn"
                "  FROM opcoes_cotacoes WHERE subjacente='BOVA11' AND vencimento=? AND ts_ms BETWEEN ? AND ?)"
                " WHERE rn=1", con, params=(v, ini, int(agora_ms))))
        except sqlite3.OperationalError:
            continue
    if not partes:
        return pd.DataFrame(columns=colunas)
    df = pd.concat(partes, ignore_index=True)
    df = df[(df.bid > 0) & (df.ask > df.bid) & (df.strike > 0)]
    if df.empty:
        return pd.DataFrame(columns=colunas)
    df["vencimento"] = df.vencimento.astype(str).str.slice(0, 10)
    df["mid"] = 0.5 * (df.bid + df.ask)
    df["idade_min"] = (int(agora_ms) - df.ts_ms) / 60_000.0
    return df[colunas + ["mid", "idade_min"]]


def _strikes(cot, venc, tipo, lo=None, hi=None):
    g = cot[(cot.vencimento == venc) & (cot.tipo == tipo)]
    if lo is not None:
        g = g[g.strike >= lo]
    if hi is not None:
        g = g[g.strike <= hi]
    return sorted(float(k) for k in g.strike.unique())


def _perto(lista, alvo):
    return min(lista, key=lambda k: abs(float(k) - float(alvo))) if lista else None


def _proximo_acima(lista, alvo):
    acima = [k for k in lista if k > alvo]
    return min(acima) if acima else None


def _proximo_abaixo(lista, alvo):
    abaixo = [k for k in lista if k < alvo]
    return max(abaixo) if abaixo else None


def _ancora(diag, chave, padrao=None):
    """Primeiro valor finito entre a chave pedida, as alternativas e o padrao."""
    for k in chave:
        if _finito(diag.get(k)):
            return float(diag[k])
    return padrao


# --- construtores: devolvem (legs, motivo) com legs = [(tipo, strike, qtd)]

def _trava_baixa_puts(cot, spot, diag, venc, dia_iso):
    """Compra put ATM/levemente OTM + vende put no put wall / q05 / strike abaixo (debito)."""
    puts = _strikes(cot, venc, "P")
    if len(puts) < 2:
        return None, f"puts liquidas insuficientes em {venc}"
    k_compra = _perto([k for k in puts if k <= spot * 1.05] or puts, spot)
    alvo = _ancora(diag, ("put_wall", "q10", "q05"), spot * 0.95)
    k_venda = _perto(puts, alvo)
    if k_venda is None or k_venda >= k_compra:
        k_venda = _proximo_abaixo(puts, k_compra)
    if k_venda is None:
        return None, f"sem put abaixo de {k_compra:.0f} em {venc}"
    return [("P", k_compra, +1), ("P", k_venda, -1)], None


def _trava_baixa_calls(cot, spot, diag, venc, dia_iso):
    """Vende call na resistencia (call wall/q90) + compra call de protecao acima (credito)."""
    calls = _strikes(cot, venc, "C")
    if len(calls) < 2:
        return None, f"calls liquidas insuficientes em {venc}"
    alvo = _ancora(diag, ("call_wall", "q90", "q95"), spot * 1.05)
    k_venda = _perto([k for k in calls if k >= spot] or calls, alvo)
    if k_venda is None:
        return None, f"sem call acima do spot em {venc}"
    if k_venda < spot:
        k_venda = _proximo_acima(calls, spot)
    k_compra = _proximo_acima(calls, k_venda)
    if k_compra is None:
        return None, f"sem call acima de {k_venda:.0f} para a protecao em {venc}"
    return [("C", k_venda, -1), ("C", k_compra, +1)], None


def _trava_alta_puts(cot, spot, diag, venc, dia_iso):
    """Vende put no suporte (put wall/q10) + compra put mais abaixo (credito)."""
    puts = _strikes(cot, venc, "P")
    if len(puts) < 2:
        return None, f"puts liquidas insuficientes em {venc}"
    alvo = _ancora(diag, ("put_wall", "q10", "q05"), spot * 0.95)
    k_venda = _perto([k for k in puts if k <= spot] or puts, alvo)
    if k_venda is None:
        return None, f"sem put abaixo do spot em {venc}"
    k_compra = _proximo_abaixo(puts, k_venda)
    if k_compra is None:
        return None, f"sem put abaixo de {k_venda:.0f} para a protecao em {venc}"
    return [("P", k_venda, -1), ("P", k_compra, +1)], None


def _trava_alta_calls(cot, spot, diag, venc, dia_iso):
    """Compra call ATM + vende call no call wall/q90 (debito)."""
    calls = _strikes(cot, venc, "C")
    if len(calls) < 2:
        return None, f"calls liquidas insuficientes em {venc}"
    k_compra = _perto(calls, spot)
    alvo = _ancora(diag, ("call_wall", "q90", "q95"), spot * 1.05)
    k_venda = _perto([k for k in calls if k > k_compra] or calls, alvo)
    if k_venda is None or k_venda <= k_compra:
        k_venda = _proximo_acima(calls, k_compra)
    if k_venda is None:
        return None, f"sem call acima de {k_compra:.0f} em {venc}"
    return [("C", k_compra, +1), ("C", k_venda, -1)], None


def _put_seca(cot, spot, diag, venc, dia_iso, atm_iv=None):
    """Compra de put seca com delta entre -0,45 e -0,35 no vencimento mensal."""
    g = cot[(cot.vencimento == venc) & (cot.tipo == "P")].copy()
    if g.empty:
        return None, f"sem put liquida em {venc}"
    t = _t_anos(dia_iso, venc, diag.get("_agora_ms", 0))
    deltas = []
    for r in g.itertuples():
        dlt = float(r.delta) if _finito(r.delta) else None
        if dlt is None or dlt >= 0:
            iv = float(r.iv) if _finito(r.iv) and r.iv > 0 else atm_iv
            gr = bs_gregas("P", spot, float(r.strike), t, 0.13, iv) if iv else None
            dlt = gr[0] if gr else None
        if dlt is not None and -0.55 <= dlt < 0:
            deltas.append((abs(dlt + 0.40), float(r.strike), dlt))
    if not deltas:
        return None, f"sem put com delta entre -0,45 e -0,35 em {venc}"
    _d, k, _dl = min(deltas)
    return [("P", k, +1)], None


def _put_backspread(cot, spot, diag, venc, dia_iso):
    """Put backspread 1x2: vende 1 put perto do spot, compra 2 puts no put wall/q10 (ratio)."""
    puts = _strikes(cot, venc, "P")
    if len(puts) < 3:
        return None, f"puts liquidas insuficientes em {venc} para o 1x2"
    k_venda = _proximo_abaixo(puts, spot) or _perto(puts, spot * 0.98)
    alvo = _ancora(diag, ("put_wall", "q10", "q05"), spot * 0.92)
    k_compra = _perto([k for k in puts if k < k_venda] or puts, alvo)
    if k_compra is None or k_compra >= k_venda:
        k_compra = _proximo_abaixo(puts, k_venda)
    if k_compra is None:
        return None, f"sem put abaixo de {k_venda:.0f} para as 2 compradas em {venc}"
    return [("P", k_venda, -1), ("P", k_compra, +2)], None


def _iron_condor(cot, spot, diag, venc, dia_iso):
    """Condor: vende put no put wall/q10 e call no call wall/q90, compra as asas fora."""
    puts, calls = _strikes(cot, venc, "P"), _strikes(cot, venc, "C")
    if len(puts) < 2 or len(calls) < 2:
        return None, f"puts/calls liquidas insuficientes em {venc}"
    kp_v = _perto(puts, _ancora(diag, ("put_wall", "q10", "q05"), spot * 0.94))
    kc_v = _perto(calls, _ancora(diag, ("call_wall", "q90", "q95"), spot * 1.06))
    if kp_v is None or kc_v is None or not (kp_v < spot < kc_v):
        return None, "walls/q10/q90 fora da faixa de strikes liquidos"
    kp_c = _proximo_abaixo(puts, kp_v)
    kc_c = _proximo_acima(calls, kc_v)
    if kp_c is None or kc_c is None:
        return None, "sem asa externa (strike alem da ponta vendida) em " + venc
    return [("P", kp_v, -1), ("P", kp_c, +1), ("C", kc_v, -1), ("C", kc_c, +1)], None


def _collar(cot, spot, diag, venc, dia_iso):
    """Collar: compra put de protecao abaixo + vende call de financiamento acima (1:1)."""
    puts, calls = _strikes(cot, venc, "P"), _strikes(cot, venc, "C")
    if not puts or not calls:
        return None, f"puts/calls liquidas insuficientes em {venc}"
    kp = _perto(puts, _ancora(diag, ("put_wall", "q10", "q05"), spot * 0.95))
    kc = _perto(calls, _ancora(diag, ("call_wall", "q90", "q95"), spot * 1.05))
    if kp is None or kc is None or not (kp < spot < kc):
        return None, "strikes do collar fora da faixa liquida"
    return [("P", kp, +1), ("C", kc, -1)], None


ESTRUTURAS = {
    "trava_baixa_puts": ("Trava de baixa com puts (débito)", _trava_baixa_puts,
                         "Compra put ATM e vende put no put wall/q05: vende o skew caro e paga menos "
                         "prêmio do que a put seca."),
    "trava_baixa_calls": ("Trava de baixa com calls (crédito)", _trava_baixa_calls,
                          "Vende call na resistência (call wall/q90) e compra proteção acima: crédito "
                          "com risco limitado, ganha na queda e na compressão de IV."),
    "trava_alta_puts": ("Trava de alta com puts (crédito)", _trava_alta_puts,
                        "Vende put no suporte (put wall/q10) e compra put abaixo: colhe o prêmio "
                        "inflado das puts se o suporte segurar."),
    "trava_alta_calls": ("Trava de alta com calls (débito)", _trava_alta_calls,
                         "Compra call ATM e vende call no call wall/q90: direcional para cima com "
                         "custo reduzido."),
    "put_seca": ("Compra de put seca", _put_seca,
                 "Put seca com delta -0,35 a -0,45 no vencimento mensal: convexidade pura na queda."),
    "put_backspread": ("Put backspread 1x2", _put_backspread,
                       "Vende 1 put perto do spot e compra 2 puts abaixo: ratio de convexidade, paga "
                       "na queda acelerada e na alta de IV."),
    "iron_condor": ("Iron condor ancorado nas walls", _iron_condor,
                    "Vende put no put wall/q10 e call no call wall/q90, asas fora: opera o travamento "
                    "do preço entre as paredes."),
    "collar": ("Collar (proteção + financiamento)", _collar,
               "Compra put de proteção abaixo e vende call acima: trava o risco da posição à vista "
               "e reduz o custo com o prêmio da call."),
}


def _payoff(tipo, s, k):
    return np.maximum(0.0, s - k) if tipo == "C" else np.maximum(0.0, k - s)


def _breakevens(pnl, grade):
    """Zeros do payoff por interpolacao linear nas trocas de sinal."""
    out = []
    for i in range(len(pnl) - 1):
        a, b = pnl[i], pnl[i + 1]
        if a == 0:
            out.append(float(grade[i]))
        elif a * b < 0:
            out.append(float(grade[i] + (grade[i + 1] - grade[i]) * (-a) / (b - a)))
    return sorted(set(round(x, 4) for x in out))


def _montar(chave, papel, venc, legs, motivo, cot, spot, taxa, dia_iso, agora_ms, atm_iv, nota):
    """Monta a estrutura com as cotacoes reais e calcula custo, breakevens, payoff e gregas."""
    nome, _fn, tese = ESTRUTURAS[chave]
    base = dict(nome=nome, chave=chave, papel=papel, vencimento=venc, pernas=[], tese=tese, notas=[nota])
    if legs is None:
        base["motivo"] = motivo or "sem serie liquida que sirva"
        return base
    t = _t_anos(dia_iso, venc, agora_ms)
    pernas, soma = [], dict(delta=0.0, gama=0.0, vega=0.0)
    custo_mid = custo_pior = 0.0
    for tipo, strike, qtd in legs:
        r = cot[(cot.vencimento == venc) & (cot.tipo == tipo) & np.isclose(cot.strike, strike)]
        if r.empty:
            base["motivo"] = f"serie {tipo} {strike:.0f} de {venc} sem bid/ask nos ultimos 10 min"
            return base
        r = r.iloc[0]
        iv = float(r.iv) if _finito(r.iv) and r.iv > 0 else atm_iv
        gr = bs_gregas(tipo, spot, float(strike), t, taxa, iv) if iv else None
        mid, bid, ask = float(r.mid), float(r.bid), float(r.ask)
        custo_mid += qtd * mid
        custo_pior += qtd * (ask if qtd > 0 else bid)
        if gr:
            soma["delta"] += qtd * gr[0]
            soma["gama"] += qtd * gr[1]
            soma["vega"] += qtd * gr[2]
        pernas.append(dict(serie=str(r["symbol"]) if "symbol" in r.index else f"{tipo}{strike:.0f}",
                           tipo="COMPRA" if qtd > 0 else "VENDA", quantidade=abs(int(qtd)),
                           strike=float(strike), vencimento=venc, mid=mid, bid=bid, ask=ask,
                           iv=iv, idade_min=float(r.idade_min)))
    grade = np.unique(np.concatenate([np.linspace(0.0, 2.0 * spot, N_GRADE),
                                      np.array([float(k) for _t, k, _q in legs])]))
    pnl = -custo_mid + np.zeros_like(grade)
    for tipo, strike, qtd in legs:
        pnl = pnl + qtd * _payoff(tipo, grade, float(strike))
    ganho, perda = float(pnl.max()), float(pnl.min())
    base.update(pernas=pernas, custo_mid=float(custo_mid), custo_pior=float(custo_pior),
                liquidez=("débito" if custo_mid > 0 else "crédito"),
                breakevens=_breakevens(pnl, grade), ganho_max=ganho, perda_max=perda,
                ganho_max_preco=float(grade[int(np.argmax(pnl >= ganho - 1e-9))]),
                perda_max_preco=float(grade[int(np.argmax(pnl <= perda + 1e-9))]),
                retorno_risco=(ganho / abs(perda) if perda < 0 else None),
                gregas={k: round(v, 6) for k, v in soma.items()},
                spot=float(spot), t_anos=round(t, 5))
    if len(pernas) >= 2:
        if custo_pior >= 0:
            base["notas"].append(f"custo no pior lado (compra no ask, venda no bid): {custo_pior:.2f}")
        else:
            base["notas"].append(f"credito no pior lado (compra no ask, venda no bid): {-custo_pior:.2f}")
    return base


def _resumo_liquidez(cot):
    """Series liquidas por vencimento/tipo e a idade maxima, para o diagnostico."""
    out = {}
    for (v, t), g in cot.groupby(["vencimento", "tipo"]):
        out[f"{v} {t}"] = len(g)
    return out


def _condicao(cen, papel, chave, diag):
    """Condicao de uso da linha da tabela 6.3.B para a estrutura (texto curto) ou None."""
    d, iv, _sk = cen.split("|")
    if papel == "contra" and chave == "trava_alta_puts" and d == "baixa":
        q = diag.get("q05")
        return (f"contra so quando o preco tocar o q05 do dia (R$ {q:.2f}) com absorcao;"
                if _finito(q) else "contra so no suporte extremo com absorcao;")
    if papel == "contra" and chave == "trava_baixa_puts" and d == "alta":
        q = diag.get("q95")
        return (f"contra so em esticada acima do q95 do dia (R$ {q:.2f}) com divergencia macro;"
                if _finito(q) else "contra so em esticada com divergencia macro;")
    if papel == "contra" and chave == "trava_alta_calls" and d == "baixa" and iv == "iv_barata":
        return "evitar venda de premio; se operar o repique em suporte forte, call seca curta ou esta trava."
    if chave == "iron_condor":
        return ("exige GEX positivo e IV alta; com GEX negativo e referencia, nao sugestao."
                if diag.get("gex_sinal") != "positivo" else "com GEX positivo e IV alta, alvo no max pain.")
    if chave == "put_seca":
        return "entrada so com aceleracao para baixo e IV ainda barata; vol crush e o risco."
    if chave == "collar":
        return "exige posicao a vista (100 BOVA11 por contrato) para proteger."
    return None


def estruturas(con, agora_ms, vies, ativo="BOVA11"):
    """Estruturas de opcoes do cenario -> {"diagnostico", "cenario", "a_favor", "contra", ...}.

    `vies` e o score do motor_cenario (-100 a +100). ativo 'BOVA11' (ou 'WIN$', que usa a mesma
    superficie do BOVA11) monta as estruturas; 'DOL'/'WDO$' devolve so o diagnostico, porque nao ha
    livro ao vivo das opcoes de dolar no banco. Somente leitura.
    """
    agora_ms = int(agora_ms)
    vies = float(vies or 0.0)
    eh_dolar = str(ativo).upper() in ("DOL", "WDO", "WDO$", "DOL$")
    cot = pd.DataFrame() if eh_dolar else _cotacoes(con, agora_ms)
    diag = superficie(con, agora_ms, ativo, None if eh_dolar else cot)
    diag["_agora_ms"] = agora_ms
    cen = cenario(vies, diag)
    plano = _plano(cen, diag)
    diag["gex_sinal"] = diag.get("gex_sinal") or "indefinido"
    resultado = {"diagnostico": {}, "cenario": cen, "vies": vies,
                 "direcao": direcao_vies(vies), "nivel_iv": nivel_iv(diag),
                 "skew": diag.get("skew") or _skew(diag.get("rr25"), diag.get("rr25_pct"))[0],
                 "nota": plano["nota"], "a_favor": [], "contra": []}
    if eh_dolar:
        resultado["diagnostico"] = {k: v for k, v in diag.items() if not k.startswith("_")}
        resultado["motivo"] = diag.get("motivo_estrutura")
        return resultado
    if not diag.get("spot"):
        resultado["diagnostico"] = {k: v for k, v in diag.items() if not k.startswith("_")}
        resultado["motivo"] = "sem spot do BOVA11 para montar estrutura"
        return resultado
    if cot.empty:
        resultado["diagnostico"] = {k: v for k, v in diag.items() if not k.startswith("_")}
        resultado["motivo"] = "opcoes_cotacoes sem bid/ask nos 10 min antes de agora"
        return resultado
    diag["series_liquidas"] = _resumo_liquidez(cot)
    diag["idade_max_min"] = float(cot.idade_min.max())
    resultado["diagnostico"] = {k: v for k, v in diag.items() if not k.startswith("_")}
    resumos = _resumos(con, agora_ms)
    validos = _vencimentos_validos(resumos, _iso(_dia_idx(agora_ms)))
    if not validos:
        resultado["motivo"] = "sem vencimento com 5+ dias uteis"
        return resultado
    semanal = validos[0]["vencimento"]
    mensal = next((v["vencimento"] for v in validos[1:]
                   if mr._terceira_sexta(v["vencimento"])), None) or validos[-1]["vencimento"]
    mapa = dict(semanal=semanal, mensal=mensal)
    atm_iv = validos[0]["atm_iv"]
    taxa = mr._taxa_opcoes(con, agora_ms)
    for papel in ("a_favor", "contra"):
        for chave, prazo in plano[papel]:
            venc = mapa.get(prazo, semanal)
            fn = ESTRUTURAS[chave][1]
            kwargs = {}
            if chave == "put_seca":
                kwargs["atm_iv"] = atm_iv
            legs, motivo = fn(cot, diag["spot"], diag, venc, _iso(_dia_idx(agora_ms)), **kwargs)
            item = _montar(chave, papel, venc, legs, motivo, cot, diag["spot"], taxa,
                           _iso(_dia_idx(agora_ms)), agora_ms, atm_iv, plano["nota"])
            cond = _condicao(cen, papel, chave, diag)
            if cond and not item.get("motivo"):
                item["notas"].append(cond)
            resultado[papel].append(item)
    if not resultado["a_favor"] and not resultado["contra"]:
        resultado["motivo"] = "cenario sem estrutura na tabela nem na regra do modulo"
    return resultado


def _agora_de(texto, agora_ms=None):
    """Converte 'AAAA-MM-DD HH:MM' na convencao do banco (hora de Brasilia como epoch UTC)."""
    if not texto:
        return int(agora_ms)
    d, _, h = str(texto).partition(" ")
    dia = (date.fromisoformat(d) - date(1970, 1, 1)).days
    hh, _, mm = (h or "00:00").partition(":")
    return dia * DIA_MS + (int(hh) * 60 + int(mm or 0)) * 60_000


def main(argv=None):
    """Imprime o diagnostico e as estruturas do instante.

    Uso: python opcoes_estruturas.py [--vies -60] [--ativo BOVA11|DOL] [--agora "2026-11-17 15:00"] [--db ...]
    """
    import argparse
    import relogio
    ap = argparse.ArgumentParser(description="Estruturas de opcoes por cenario da superficie")
    ap.add_argument("--vies", type=float, default=0.0)
    ap.add_argument("--ativo", default="BOVA11")
    ap.add_argument("--agora", default=None, help='ex.: "2026-11-17 15:00" (padrao: agora)')
    ap.add_argument("--db", default="dados/book.db")
    args = ap.parse_args(argv)
    agora = _agora_de(args.agora, relogio.local_br_ms())
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        res = estruturas(con, agora, args.vies, args.ativo)
    finally:
        con.close()
    print(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    return res


if __name__ == "__main__":
    main()
