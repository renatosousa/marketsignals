"""Faixas assimetricas de retorno do WIN por regressao quantilica conformalizada (CQR), ESPEC_V3.md secao 5.1.2.

Alvo: retorno log do WIN nos proximos 60 min e ate o fechamento (17:55), padronizado pela vol prevista do modelo HAR
(z = r / sigma_prev, com sigma_prev em % e r em log). Pontos de previsao: a cada 15 min de 10:05 a 17:15, o mesmo
desenho do estudo_regime.py (~200 pregoes de barras_1m).

Covariaveis da regressao quantilica (quantis 0,05; 0,15; 0,50; 0,85; 0,95):
  log_sigma      log da vol prevista para os proximos 60 min (HAR causal);
  er_pas         eficiencia da ultima hora (regimes.er);
  ret_pas_std    retorno da ultima hora dividido por sigma_prev/100;
  min_ate_fim    minutos ate o fechamento (17:55).
Modelo B: mesma especificacao na subamostra desde 25/09/2026 com RR25 do vencimento mais proximo com 5+ dias uteis
(opcoes_resumo), sempre na ultima leitura com ts_ms <= t. GEX ficaria de fora: b3_posicoes_series so tem posicoes
desde 28/10/2026 e menos de 30 pregoes.

HAR: o sigma_prev e o HAR do modelo_regime.py reajustado por dia de forma causal (todas as linhas anteriores ao dia;
sem os EXTRAS, que exigem a maquinaria de blocos e nao mudam o alvo aqui, que e o quantil e nao a melhora da vol).
HAR longo usa a vol da hora anterior; HAR curto (sem ela) cobre 10:05-11:04. O fechamento usa
sigma_fech = sigma_60 * sqrt(minutos_ate_17:55 / 60).

CQR (Romano et al. 2019): em cada bloco de 20 pregoes, treina nas linhas anteriores aos 60 pregoes de calibracao,
calibra o ajuste aditivo de [q15,q85] e [q05,q95] pela quantil empirica dos escores de nao conformidade nesses 60
pregoes e testa nos 20 seguintes. A avaliacao separa cobertura por vol diaria (alta/baixa, corte na mediana) e por
horario, e compara a perda pinball com a faixa gaussiana atual (+-1,036 sigma e +-1,645 sigma para 70% e 90%).

treinar devolve o modelo (o atualizar grava em dados/modelo_regime_v3.json); prever devolve os quantis em pontos do
WIN a partir do preco atual. As demais funcoes so leem.

Uso: python modelo_regime_v3.py atualizar [--db dados/book.db]   (grava dados/modelo_regime_v3.json)
     python modelo_regime_v3.py avaliar [--db dados/book.db]     (so le: cobertura, pinball e A x B)
"""
import argparse
import bisect
import json
import sqlite3
import warnings
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm
from sklearn.linear_model import QuantileRegressor

import calendario_b3 as cal
import correlacoes as corr
import regimes as rg

warnings.filterwarnings("ignore", message=".*highs.*")

DIA_MS = 86_400_000
SLOTS = [f"{h:02d}:{m:02d}" for h in range(10, 18) for m in (5, 20, 35, 50)
         if "10:05" <= f"{h:02d}:{m:02d}" <= "17:15"]
QUANTIS = (0.05, 0.15, 0.50, 0.85, 0.95)
Q05, Q15, Q50, Q85, Q95 = range(5)
MIN_FECHAMENTO = 17 * 60 + 55
ALPHA_QR = 1e-4
BLOCO = 20
CALIB = 60
MIN_TREINO = 40
MIN_CAL = 15
# Modelo B tem ~34 pregoes (opcoes_resumo desde 25/09/2026): nao cabem 60 de calibracao e 40 de treino.
BLOCO_B, CALIB_B, MIN_TREINO_B, MIN_CAL_B = 20, 10, 12, 6
COLS_A = ["log_sigma", "er_pas", "ret_pas_std", "min_ate_fim"]
COLS_B = COLS_A + ["rr25"]
GEX_MIN_PREGOES = 30
INICIO_OPCOES = "2026-09-25"
DU_MIN_OPCAO = 5


# ------------------------------------------------------------------ dados
def _existe(con, tabela):
    try:
        return bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (tabela,)).fetchone())
    except sqlite3.OperationalError:
        return False


def _minuto(hhmm):
    return int(hhmm[:2]) * 60 + int(hhmm[3:])


def _ret_log(p):
    """Log-retorno decimal entre o primeiro e o ultimo preco valido da janela (None se faltar)."""
    q = p.dropna()
    if len(q) < 2 or q.iloc[0] <= 0 or q.iloc[-1] <= 0:
        return np.nan
    return float(np.log(q.iloc[-1] / q.iloc[0]))


def _dia_idx(dia):
    return (pd.Timestamp(dia).normalize().date() - date(1970, 1, 1)).days


def _rr25_causais(con):
    """{data: {"validos": [(du, venc)], "por_venc": {venc: [(ts, rr25)]}}} de opcoes_resumo.

    "validos" sao os vencimentos com >= DU_MIN_OPCAO dias uteis, do mais proximo para o mais distante. A leitura ao
    vivo usa, para o horario t, a ultima observacao com ts_ms <= t (sem espiar o fim do dia).
    """
    if not _existe(con, "opcoes_resumo"):
        return {}
    try:
        rows = con.execute("SELECT ts_ms, vencimento, rr25 FROM opcoes_resumo "
                           "WHERE subjacente='BOVA11' AND rr25 IS NOT NULL").fetchall()
    except sqlite3.OperationalError:
        return {}
    por_dia = {}
    for ts, venc, r in rows:
        if r is None or not np.isfinite(r):
            continue
        por_dia.setdefault(ts // DIA_MS, {}).setdefault(venc, []).append((int(ts), float(r)))
    out = {}
    for d, vencs in por_dia.items():
        iso = str(date(1970, 1, 1) + pd.Timedelta(days=int(d)))[:10]
        validos = sorted((cal.du_ate_vencimento(iso, v), v) for v in vencs
                         if cal.du_ate_vencimento(iso, v) >= DU_MIN_OPCAO)
        if validos:
            out[iso] = {"validos": validos,
                        "por_venc": {v: sorted(pts) for v, pts in vencs.items()}}
    return out


def _rr25_em(info, t_ms):
    """RR25 causal no instante t_ms: vencimento valido mais proximo que ja tenha leitura ate t_ms."""
    if not info:
        return None
    for _du, venc in info["validos"]:
        pts = info["por_venc"].get(venc)
        if not pts:
            continue
        i = bisect.bisect_right(pts, (int(t_ms), np.inf)) - 1
        if i >= 0:
            return float(pts[i][1])
    return None


def _rr25_diario_de(info):
    """{data: ultimo rr25 do vencimento mais proximo} (so metadado; as linhas usam _rr25_em)."""
    out = {}
    for iso, d in info.items():
        for _du, venc in d["validos"]:
            pts = d["por_venc"].get(venc)
            if pts:
                out[iso] = float(pts[-1][1])
                break
    return out


def rr25_diario(con):
    """{data: rr25} do vencimento mais proximo com >= DU_MIN_OPCAO dias uteis, ultima leitura do dia."""
    return _rr25_diario_de(_rr25_causais(con))


def montar(con):
    """Monta o dataset intradiario causal (linhas dia x horario) e as vols diarias.

    -> {"df": DataFrame, "vols": {data: rv_dia}, "rr25": {data: rr25}, "pregoes": n, "periodo": (ini, fim),
        "gex": {"usado": bool, "sessoes": n, "motivo": texto}}.
    """
    win = rg.carregar(con, "WIN$")
    dias = sorted(set(win.index.normalize()))
    vols, linhas = {}, []
    rr_info = _rr25_causais(con)
    for d in dias:
        g = rg.grade(d, rg.INICIO, rg.FIM)
        pw = rg.na_grade(win, g)
        if pw.notna().sum() < 300:
            continue
        rv_dia = rg.rv(pw)
        if not np.isfinite(rv_dia) or rv_dia <= 0:
            continue
        vols[d] = rv_dia
        iso = str(d.date())
        info_rr = rr_info.get(iso)
        for sl in SLOTS:
            t = d + pd.Timedelta(sl + ":00")
            if t not in pw.index:
                continue
            pas = pw[t - pd.Timedelta("60min"):t]
            ant = pw[t - pd.Timedelta("120min"):t - pd.Timedelta("59min")]
            fut = pw[t:t + pd.Timedelta("60min")]
            ate_fech = pw[t:d + pd.Timedelta(f"{MIN_FECHAMENTO // 60:02d}:{MIN_FECHAMENTO % 60:02d}:00")]
            preco = pw.get(t, np.nan)
            if not np.isfinite(preco) or preco <= 0:
                continue
            r60, rclose = _ret_log(fut), _ret_log(ate_fech)
            linhas.append(dict(
                dia=d, dia_idx=_dia_idx(d), slot=sl, minuto=_minuto(sl),
                preco=float(preco), rv_dia=float(rv_dia),
                rv_pas=rg.rv(pas), er_pas=rg.er(pas), ret_pas=_ret_log(pas),
                rv_ant=rg.rv(ant) if ant.notna().sum() >= 30 else np.nan,
                rv_fut=rg.rv(fut), r60=r60, rclose=rclose,
                min_ate_fim=MIN_FECHAMENTO - _minuto(sl),
                rr25=_rr25_em(info_rr, t.value // 1_000_000)))
    df = pd.DataFrame(linhas)
    if df.empty:
        return {"df": df, "vols": {}, "rr25": {}, "pregoes": 0, "periodo": (None, None), "gex": _gex(con)}
    v = pd.Series(vols).sort_index()
    ant = v.shift(1)
    hist = pd.DataFrame({"dia": v.index, "log_d1": np.log(ant),
                         "log_d5": np.log(ant.rolling(5, min_periods=3).mean()),
                         "log_d20": np.log(ant.rolling(20, min_periods=10).mean())})
    df = df.merge(hist, on="dia", how="left")
    df["log_rv_pas"] = np.log(df.rv_pas.where(df.rv_pas > 0))
    df["log_rv_ant"] = np.log(df.rv_ant.where(df.rv_ant > 0))
    df["log_rv_fut"] = np.log(df.rv_fut.where(df.rv_fut > 0))
    df = _har_causal(df)
    return {"df": df, "vols": {str(k.date()): float(x) for k, x in vols.items()},
            "rr25": _rr25_diario_de(rr_info),
            "pregoes": len(vols), "periodo": (str(dias[0].date()) if dias else None,
                                              str(dias[-1].date()) if dias else None), "gex": _gex(con)}


def _gex(con):
    """Diagnostico do GEX: so entra com >= GEX_MIN_PREGOES sessoes de posicoes (b3_posicoes_series)."""
    n = 0
    if _existe(con, "b3_posicoes_series"):
        try:
            n = con.execute("SELECT count(DISTINCT sessao) FROM b3_posicoes_series WHERE subjacente='BOVA11'"
                            ).fetchone()[0] or 0
        except sqlite3.OperationalError:
            n = 0
    usado = n >= GEX_MIN_PREGOES
    motivo = ("GEX incluido" if usado else
              f"GEX fora: {n} sessoes de b3_posicoes_series (minimo {GEX_MIN_PREGOES}); a serie comeca em 28/10/2026")
    return {"usado": usado, "sessoes": int(n), "motivo": motivo}


# ------------------------------------------------------------------ HAR causal
def _colunas_dummy():
    return [f"h_{s}" for s in SLOTS[1:]]


def _design(df, num_cols, dummy_cols):
    """Matriz [1, dummies de horario, numericas] na ordem fixa (NaN nao sao filtrados aqui)."""
    n = len(df)
    X = np.ones((n, 1 + len(dummy_cols) + len(num_cols)))
    for j, c in enumerate(dummy_cols):
        X[:, 1 + j] = (df["slot"].to_numpy() == c[2:]).astype(float)
    for j, c in enumerate(num_cols):
        X[:, 1 + len(dummy_cols) + j] = df[c].to_numpy(dtype=float)
    return X


def _fit_har(df, num_cols):
    """OLS de log_rv_fut nas dummies de horario + numericas; None se faltar amostra/dia."""
    dummies = _colunas_dummy()
    cols = ["log_rv_fut"] + num_cols
    d = df[cols].replace([np.inf, -np.inf], np.nan)
    d = df.loc[d.dropna().index]
    if len(d) < 200 or d.dia.nunique() < 10:
        return None
    X = _design(d, num_cols, dummies)
    y = d["log_rv_fut"].to_numpy(dtype=float)
    coef = np.linalg.lstsq(X, y, rcond=None)[0]
    return {"coef": coef, "num": num_cols, "dummies": dummies}


def _pred_har(aj, df, num_cols):
    if aj is None:
        return np.full(len(df), np.nan)
    return _design(df, num_cols, aj["dummies"]) @ aj["coef"]


def _har_causal(df):
    """Adiciona sigma (vol prevista %, causal) para cada linha, reajustando o HAR por dia."""
    df = df.copy()
    df["sigma"] = np.nan
    num_longo = ["log_rv_pas", "log_rv_ant", "log_d1", "log_d5", "log_d20"]
    num_curto = ["log_rv_pas", "log_d1", "log_d5", "log_d20"]
    dias = sorted(df.dia.unique())
    for d in dias:
        antes = df[df.dia < d]
        if len(antes) < 200:
            continue
        aj_l = _fit_har(antes, num_longo)
        aj_c = _fit_har(antes, num_curto)
        hoje = df[df.dia == d].copy()
        tem_ant = hoje.rv_ant.notna() & hoje.log_rv_ant.notna()
        pred = np.full(len(hoje), np.nan)
        if tem_ant.any() and aj_l is not None:
            pred[tem_ant.to_numpy()] = _pred_har(aj_l, hoje[tem_ant], num_longo)
        resto = ~tem_ant
        if resto.any() and aj_c is not None:
            pred[resto.to_numpy()] = _pred_har(aj_c, hoje[resto], num_curto)
        df.loc[hoje.index, "sigma"] = np.exp(pred)
    # retorno padronizado pela sigma prevista (em %)
    df["z60"] = df["r60"] / (df["sigma"] / 100.0)
    df["sigma_fech"] = df["sigma"] * np.sqrt(np.maximum(df["min_ate_fim"], 1) / 60.0)
    df["zfech"] = df["rclose"] / (df["sigma_fech"] / 100.0)
    df["log_sigma"] = np.log(df["sigma"])
    df["ret_pas_std"] = df["ret_pas"] / (df["sigma"] / 100.0)
    return df


# ------------------------------------------------------------------ regressao quantilica e CQR
def _fit_qr(X, y, tau):
    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=float)
    media, desvio = X.mean(axis=0), X.std(axis=0)
    desvio[desvio == 0] = 1.0
    m = QuantileRegressor(quantile=tau, alpha=ALPHA_QR, solver="highs")
    m.fit((X - media) / desvio, y)
    return {"media": media.tolist(), "desvio": desvio.tolist(), "coef": m.coef_.tolist(),
            "intercepto": float(m.intercept_)}


def _pred_qr(aj, X):
    X = np.asarray(X, dtype=float)
    return aj["intercepto"] + ((X - np.asarray(aj["media"])) / np.asarray(aj["desvio"])) @ np.asarray(aj["coef"])


def _fit_quantis(df, cols, alvo):
    """Ajusta as cinco regressoes quantilicas -> {tau: ajuste} ou None."""
    d = df[cols + [alvo]].replace([np.inf, -np.inf], np.nan).dropna()
    if len(d) < 80:
        return None
    X, y = d[cols].to_numpy(), d[alvo].to_numpy()
    return {tau: _fit_qr(X, y, tau) for tau in QUANTIS}


def _pred_quantis(ajustes, df, cols):
    if ajustes is None:
        return None
    X = df[cols].to_numpy(dtype=float)
    return np.column_stack([_pred_qr(ajustes[tau], X) for tau in QUANTIS])


def _quantil_conformal(escores, alpha):
    """Quantil empirico conservador dos escores de nao conformidade (nivel ceil((n+1)(1-alpha))/n)."""
    e = np.asarray(escores, dtype=float)
    e = e[np.isfinite(e)]
    if len(e) == 0:
        return 0.0
    nivel = min(1.0, np.ceil((len(e) + 1) * (1.0 - alpha)) / len(e))
    return float(np.quantile(e, nivel, method="higher"))


def _offsets(y, pred):
    """(off15, off05) dos intervalos centrais a partir das previsoes ``pred`` (n x 5)."""
    if pred is None or len(y) == 0:
        return 0.0, 0.0
    e15 = np.maximum(pred[:, Q15] - y, y - pred[:, Q85])
    e05 = np.maximum(pred[:, Q05] - y, y - pred[:, Q95])
    return _quantil_conformal(e15, 0.30), _quantil_conformal(e05, 0.10)


def _aplicar(pred, off15, off05):
    """Aplica os ajustes aditivos e ordena os quantis de cada linha."""
    if pred is None:
        return None
    p = np.array(pred, dtype=float, copy=True)
    p[:, Q05] -= off05
    p[:, Q15] -= off15
    p[:, Q85] += off15
    p[:, Q95] += off05
    return np.sort(p, axis=1)


def _split_walk_forward(dias, calib, min_treino, min_cal, bloco):
    """Gera (tr_dias, cal_dias, te_dias) para blocos walk-forward.

    O primeiro teste comeca quando ha `min_treino + calib` pregoes; depois os blocos andam de `bloco` em `bloco`.
    Exige os `calib` pregoes de calibracao completos (o Modelo B, sem amostra, usa calib=10).
    """
    if calib < min_cal:
        return
    ini = min_treino + calib
    while ini < len(dias):
        antes = dias[:ini]
        cal_dias = antes[-calib:]
        tr_dias = antes[:-calib]
        te_dias = dias[ini:ini + bloco]
        if len(tr_dias) >= min_treino and len(cal_dias) >= min_cal and te_dias:
            yield tr_dias, cal_dias, te_dias
        ini += bloco


def _prever_bloco(df, cols, alvo, tr_dias, cal_dias):
    """Treina em tr_dias, calibra em cal_dias e devolve (ajustes, off15, off05)."""
    tr = df[df.dia.isin(tr_dias)]
    cal = df[df.dia.isin(cal_dias)]
    aj = _fit_quantis(tr, cols, alvo)
    if aj is None:
        return None
    pcal = _pred_quantis(aj, cal[cols + [alvo]].replace([np.inf, -np.inf], np.nan).dropna(), cols)
    ycal = cal[cols + [alvo]].replace([np.inf, -np.inf], np.nan).dropna()[alvo].to_numpy(dtype=float)
    off15, off05 = _offsets(ycal, pcal)
    return aj, off15, off05


def _walk_forward(df, cols, calib, min_treino, min_cal, bloco):
    """Previsoes CQR walk-forward para os dois alvos -> DataFrame com z, previsoes e metadados."""
    dias = sorted(df.dia.unique())
    out = []
    for tr_dias, cal_dias, te_dias in _split_walk_forward(dias, calib, min_treino, min_cal, bloco):
        te = df[df.dia.isin(te_dias)]
        linhas = {"dia": te.dia.to_numpy(), "slot": te.slot.to_numpy(), "rv_dia": te.rv_dia.to_numpy(),
                  "min_ate_fim": te.min_ate_fim.to_numpy(), "z60": te.z60.to_numpy(),
                  "zfech": te.zfech.to_numpy()}
        for alvo, prefixo in (("z60", "p60"), ("zfech", "pfech")):
            r = _prever_bloco(df, cols, alvo, tr_dias, cal_dias)
            if r is None:
                continue
            aj, o15, o05 = r
            dte = te[cols + [alvo]].dropna()
            if dte.empty:
                continue
            p = _aplicar(_pred_quantis(aj, dte, cols), o15, o05)
            # alinha por indice: as linhas com NaN em cols/alvo ficam fora
            idx = {i: k for k, i in enumerate(te.index)}
            linhas[f"{prefixo}_idx"] = np.array([idx[i] for i in dte.index])
            linhas[prefixo] = p
            linhas[f"{prefixo}_off"] = (o15, o05)
        n = len(te)
        base = {k: v for k, v in linhas.items() if not k.startswith(("p60", "pfech"))}
        for prefixo, zcol in (("p60", "z60"), ("pfech", "zfech")):
            if f"{prefixo}" not in linhas:
                continue
            p = linhas[prefixo]
            idx = linhas[f"{prefixo}_idx"]
            block = {k: np.asarray(v)[idx] for k, v in base.items()}
            block["z"] = block[zcol]
            for j, nome_q in enumerate(("q05", "q15", "q50", "q85", "q95")):
                block[nome_q] = p[:, j]
            block["alvo"] = zcol
            block["off15"], block["off05"] = linhas[f"{prefixo}_off"]
            out.append(pd.DataFrame(block))
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


# ------------------------------------------------------------------ metricas
def _cobertura(pred, z):
    if pred is None or len(z) == 0:
        return {}
    p = np.asarray(pred)
    z = np.asarray(z)
    return {"n": int(len(z)),
            "c15": float(((z >= p[:, Q15]) & (z <= p[:, Q85])).mean()),
            "c05": float(((z >= p[:, Q05]) & (z <= p[:, Q95])).mean())}


def _pinball(z, pred, taus=QUANTIS):
    p = np.asarray(pred)
    z = np.asarray(z)[:, None]
    e = z - p
    return float(np.mean(np.maximum(np.asarray(taus) * e, (np.asarray(taus) - 1.0) * e)))


def _pinball_gauss(z, taus=QUANTIS):
    p = np.tile(norm.ppf(np.asarray(taus)), (len(z), 1))
    return _pinball(z, p, taus)


def _pred(d):
    """Matriz n x 5 das previsoes q05..q95 de um DataFrame de metricas."""
    return d[["q05", "q15", "q50", "q85", "q95"]].to_numpy(dtype=float)


def _metricas(prev, alvo="z60"):
    d = prev[prev.alvo == alvo].dropna(subset=["z"])
    if d.empty:
        return {}
    z = d.z.to_numpy()
    pred = _pred(d)
    geral = _cobertura(pred, z)
    med = float(np.median(d.rv_dia.unique()))
    alta = d[d.rv_dia > med]
    baixa = d[d.rv_dia <= med]
    bandas = [("10:05-12:00", 355, 999), ("12:00-14:30", 205, 355), ("14:30-17:15", 0, 205)]
    horarios = {}
    for nome, lo, hi in bandas:
        q = d[(d.min_ate_fim > lo) & (d.min_ate_fim <= hi)] if nome != "10:05-12:00" else \
            d[(d.min_ate_fim >= lo) & (d.min_ate_fim <= hi)]
        horarios[nome] = _cobertura(_pred(q) if len(q) else None, q.z.to_numpy())
    i70 = [Q15, Q85]
    i90 = [Q05, Q95]
    pin = {"cqr": _pinball(z, pred), "gauss": _pinball_gauss(z),
           "cqr70": _pinball(z, pred[:, i70], (0.15, 0.85)),
           "gauss70": _pinball_gauss(z, (0.15, 0.85)),
           "cqr90": _pinball(z, pred[:, i90], (0.05, 0.95)),
           "gauss90": _pinball_gauss(z, (0.05, 0.95))}
    return {"geral": geral, "vol_alta": _cobertura(_pred(alta) if len(alta) else None, alta.z.to_numpy()),
            "vol_baixa": _cobertura(_pred(baixa) if len(baixa) else None, baixa.z.to_numpy()),
            "horarios": horarios, "pinball": pin, "n_dias": int(d.dia.nunique()), "vol_mediana": med}


def _formatar_cobertura(m, nome):
    if not m:
        return
    g, a, b = m["geral"], m["vol_alta"], m["vol_baixa"]
    print(f"  {nome:10} n={g.get('n', 0):5d}  [q15,q85]: {g.get('c15', np.nan):6.1%}  [q05,q95]: "
          f"{g.get('c05', np.nan):6.1%}   vol alta: {a.get('c15', np.nan):5.1%}/{a.get('c05', np.nan):5.1%}  "
          f"vol baixa: {b.get('c15', np.nan):5.1%}/{b.get('c05', np.nan):5.1%}")
    for h, c in m["horarios"].items():
        if c:
            print(f"      {h:12} n={c['n']:5d}  [q15,q85]: {c['c15']:6.1%}  [q05,q95]: {c['c05']:6.1%}")


def _formatar_pinball(m, nome):
    if not m:
        return
    p = m["pinball"]
    print(f"  {nome:10} pinball 5 quantis: CQR {p['cqr']:.4f} x gauss {p['gauss']:.4f}   "
          f"70%: {p['cqr70']:.4f} x {p['gauss70']:.4f}   90%: {p['cqr90']:.4f} x {p['gauss90']:.4f}")


def _comparar_comum(df, dados_b, cols_a=COLS_A, cols_b=COLS_B):
    """Modelo A x B na subamostra comum, com o mesmo desenho reduzido de B (mesmos dias de calibracao/teste)."""
    dias = sorted(dados_b.dia.unique())
    out = []
    for tr_dias, cal_dias, te_dias in _split_walk_forward(dias, CALIB_B, MIN_TREINO_B, MIN_CAL_B, BLOCO_B):
        te = dados_b[dados_b.dia.isin(te_dias)]
        for nome, base, cols in (("A", df, cols_a), ("B", dados_b, cols_b)):
            for alvo, prefixo in (("z60", "p60"), ("zfech", "pfech")):
                tr = base[base.dia.isin(tr_dias)]
                cal = base[base.dia.isin(cal_dias)]
                aj = _fit_quantis(tr, cols, alvo)
                if aj is None:
                    continue
                dcal = cal[cols + [alvo]].replace([np.inf, -np.inf], np.nan).dropna()
                pcal = _pred_quantis(aj, dcal, cols)
                o15, o05 = _offsets(dcal[alvo].to_numpy(), pcal)
                validos = te[cols + [alvo]].replace([np.inf, -np.inf], np.nan).dropna().index
                if len(validos) == 0:
                    continue
                dte = te.loc[validos]
                p = _aplicar(_pred_quantis(aj, dte[cols], cols), o15, o05)
                linhas = {"modelo": nome, "alvo": alvo, "dia": dte.dia.to_numpy(),
                          "slot": dte.slot.to_numpy(), "rv_dia": dte.rv_dia.to_numpy(),
                          "z": dte[alvo].to_numpy()}
                for j, nome_q in enumerate(("q05", "q15", "q50", "q85", "q95")):
                    linhas[nome_q] = p[:, j]
                out.append(pd.DataFrame(linhas))
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def avaliar(con):
    """Cobertura, pinball contra a gaussiana e A x B (so le). Imprime e devolve o resumo."""
    dados = montar(con)
    df = dados["df"]
    if df.empty:
        print("sem barras de 1 min do WIN")
        return {}
    print(f"WIN: {dados['pregoes']} pregoes {dados['periodo'][0]} a {dados['periodo'][1]} | "
          f"{len(df)} linhas de 10:05 a 17:15 | {dados['gex']['motivo']}")
    prev = _walk_forward(df, COLS_A, CALIB, MIN_TREINO, MIN_CAL, BLOCO)
    res = {"dados": {"pregoes": dados["pregoes"], "periodo": dados["periodo"], "linhas": len(df),
                     "gex": dados["gex"]}}
    for alvo, nome in (("z60", "60 min"), ("zfech", "fechamento")):
        m = _metricas(prev, alvo)
        res[alvo] = m
        print(f"\nModelo A | cobertura no teste ({nome}; metas 70%+-3 e 90%+-3)")
        _formatar_cobertura(m, nome)
        _formatar_pinball(m, nome)
    b = df[df.rr25.notna() & (df.dia >= pd.Timestamp(INICIO_OPCOES))]
    if b.empty:
        print("\nModelo B: sem opcoes_resumo com RR25; nao avaliado")
        res["B"] = {}
        return res
    print(f"\nModelo B: {b.dia.nunique()} pregoes com RR25 (desde {INICIO_OPCOES}); "
          f"calibracao reduzida para {CALIB_B} pregoes por falta de amostra")
    comum = _comparar_comum(df, b)
    res["B"] = {"pregoes": int(b.dia.nunique()), "linhas": len(b)}
    if not comum.empty:
        print(f"  A x B no desenho comum: {comum.dia.nunique()} pregoes, {len(comum)} previsoes")
        for alvo, nome in (("z60", "60 min"), ("zfech", "fechamento")):
            for modelo in ("A", "B"):
                d = comum[(comum.alvo == alvo) & (comum.modelo == modelo)]
                if d.empty:
                    continue
                g = _cobertura(_pred(d), d.z.to_numpy())
                p = _pinball(d.z.to_numpy(), _pred(d))
                print(f"    {nome:10} modelo {modelo}: n={g['n']:5d}  [q15,q85]: {g['c15']:6.1%}  "
                      f"[q05,q95]: {g['c05']:6.1%}  pinball {p:.4f}")
                res["B"][f"{alvo}_{modelo}"] = {**g, "pinball": p}
    return res


# ------------------------------------------------------------------ modelo final
def _fit_final(df, cols, alvo, calib=CALIB, min_treino=MIN_TREINO):
    """CQR final: offsets fora da amostra (ultimos `calib` pregoes) e QR reajustada em tudo.

    -> {"quantis": {tau: ajuste}, "offset15", "offset05"} ou None.
    """
    dias = sorted(df.dia.unique())
    aj_cal = None
    if len(dias) >= min_treino + 10:
        cal_dias = dias[-min(calib, len(dias) - min_treino):]
        tr_dias = dias[:-len(cal_dias)]
        if len(tr_dias) >= min_treino and len(cal_dias) >= 5:
            aj_cal = _fit_quantis(df[df.dia.isin(tr_dias)], cols, alvo)
            cal = df[df.dia.isin(cal_dias)][cols + [alvo]].replace([np.inf, -np.inf], np.nan).dropna()
            if aj_cal is not None and len(cal):
                pcal = _pred_quantis(aj_cal, cal, cols)
                o15, o05 = _offsets(cal[alvo].to_numpy(), pcal)
            else:
                o15 = o05 = 0.0
        else:
            o15 = o05 = 0.0
    else:
        o15 = o05 = 0.0
    aj = _fit_quantis(df, cols, alvo) or aj_cal
    if aj is None:
        return None
    return {"quantis": {str(t): aj[t] for t in QUANTIS}, "offset15": float(o15), "offset05": float(o05)}


def treinar(con):
    """Treina o modelo final (HAR + QR A/B + offsets conformais) -> dict serializavel em JSON."""
    dados = montar(con)
    df = dados["df"]
    if df.empty:
        raise ValueError("sem barras de 1 min do WIN")
    num_longo = ["log_rv_pas", "log_rv_ant", "log_d1", "log_d5", "log_d20"]
    num_curto = ["log_rv_pas", "log_d1", "log_d5", "log_d20"]
    aj_l = _fit_har(df, num_longo)
    aj_c = _fit_har(df, num_curto)
    if aj_l is None or aj_c is None:
        raise ValueError("amostra insuficiente para o HAR")
    m = {"versao": 1, "treinado_em": datetime.now().isoformat(timespec="seconds"),
         "periodo": dados["periodo"], "pregoes": dados["pregoes"], "linhas": int(len(df)),
         "slots": SLOTS, "quantis": list(QUANTIS), "colunas_a": COLS_A, "colunas_b": COLS_B,
         "har": {"longo": {"coef": aj_l["coef"].tolist(), "num": num_longo, "dummies": aj_l["dummies"]},
                 "curto": {"coef": aj_c["coef"].tolist(), "num": num_curto, "dummies": aj_c["dummies"]}},
         "vol_diaria": dados["vols"], "gex": dados["gex"], "modelo": "A"}
    for nome, cols in (("A", COLS_A), ("B", COLS_B)):
        base = df if nome == "A" else df[df.rr25.notna() & (df.dia >= pd.Timestamp(INICIO_OPCOES))]
        if len(base) < 80:
            m[f"qr_{nome}"] = None
            continue
        q60 = _fit_final(base, cols, "z60", calib=CALIB_B, min_treino=MIN_TREINO_B)
        qfech = _fit_final(base, cols, "zfech", calib=CALIB_B, min_treino=MIN_TREINO_B)
        m[f"qr_{nome}"] = {"60min": q60, "fechamento": qfech, "linhas": int(len(base)),
                           "pregoes": int(base.dia.nunique()), "cols": cols}
        if nome == "B" and q60 is not None and qfech is not None:
            m["modelo"] = "B"
    return m


def atualizar(db_path):
    """Treina e grava dados/modelo_regime_v3.json (o unico caminho que grava o modelo) -> dict."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        m = treinar(con)
    finally:
        con.close()
    arq = Path(db_path).resolve().parent / "modelo_regime_v3.json"
    arq.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"modelo_regime_v3 salvo em {arq} | {m['pregoes']} pregoes | modelo {m['modelo']}")
    return m


# ------------------------------------------------------------------ previsao ao vivo
def _preco_em(s, minuto):
    """Ultimo preco valido ate `minuto` (indice de minutos do dia); NaN se nao houver."""
    if s is None or len(s) == 0:
        return np.nan
    q = s[s.index <= minuto].dropna()
    return float(q.iloc[-1]) if len(q) else np.nan


def _rr25_hoje(con, dia_iso, agora_ms):
    """RR25 do vencimento mais proximo com >=5 dias uteis, ultima leitura com ts_ms <= agora."""
    if not _existe(con, "opcoes_resumo"):
        return None
    try:
        rows = con.execute("SELECT vencimento, rr25, max(ts_ms) FROM opcoes_resumo "
                           "WHERE subjacente='BOVA11' AND rr25 IS NOT NULL AND ts_ms<=? "
                           "AND ts_ms>=? GROUP BY vencimento",
                           (int(agora_ms), int(agora_ms) - DIA_MS)).fetchall()
    except sqlite3.OperationalError:
        return None
    validos = [(cal.du_ate_vencimento(dia_iso, v), v, r) for v, r, _ts in rows if r is not None]
    validos = [x for x in validos if x[0] >= DU_MIN_OPCAO]
    return float(min(validos)[2]) if validos else None


def _num_vol(modelo, dia_iso):
    """log_d1/d5/d20 da vol diaria do WIN em dias anteriores a `dia_iso` (causal)."""
    v = pd.Series({pd.Timestamp(k): x for k, x in modelo["vol_diaria"].items()}).sort_index()
    v = v[v.index < pd.Timestamp(dia_iso)]
    if len(v) < 10:
        return None
    return {"log_d1": float(np.log(v.iloc[-1])), "log_d5": float(np.log(v.tail(5).mean())),
            "log_d20": float(np.log(v.tail(20).mean()))}


def _prever_por_modelo(modelo, qr, cols, valores):
    """Aplica o QR do modelo A/B e os offsets conformais -> quantis em z."""
    ajustes = {float(t): a for t, a in qr["quantis"].items()}
    X = np.array([[valores[c] for c in cols]], dtype=float)
    pred = np.column_stack([_pred_qr(ajustes[t], X) for t in QUANTIS])[0]
    return _aplicar(pred[None, :], qr["offset15"], qr["offset05"])[0]


def prever(con, agora_ms, modelo):
    """Quantis em pontos do WIN a partir do preco atual -> dict ou None fora de 10:05-17:15/sem dados.

    Usa somente dados com ts_ms <= agora_ms (snapshots do WIN e opcoes_resumo). "modelo" e "B" quando o RR25 do dia
    existe e o modelo B foi treinado; senao "A".
    """
    if not modelo or "60min" not in (modelo.get("qr_A") or {}):
        return None
    agora_ms = int(agora_ms)
    dia = agora_ms // DIA_MS
    min_fim = (agora_ms % DIA_MS) // 60_000
    slots = [(sl, _minuto(sl)) for sl in SLOTS if _minuto(sl) <= min_fim]
    if not slots or min_fim < _minuto(SLOTS[0]) or min_fim > MIN_FECHAMENTO:
        return None
    slot, t_min = slots[-1]
    dia_ts = pd.Timestamp(str(pd.Timestamp(dia * DIA_MS, unit="ms").date()))
    dia_iso = str(dia_ts.date())
    s = pd.Series(dtype=float)
    try:
        s = corr.serie_1m(con, "WIN$", dia, ao_vivo=True)
    except Exception:
        pass
    if s.empty:
        s = corr.serie_1m(con, "WIN$", dia, ao_vivo=False)
    preco = _preco_em(s, min_fim)
    if not np.isfinite(preco) or preco <= 0:
        return None
    pas = s[(s.index >= t_min - 60) & (s.index < t_min)]
    ant = s[(s.index >= t_min - 120) & (s.index < t_min - 60)]
    rv_pas, er_pas, ret_pas = rg.rv(pas), rg.er(pas), _ret_log(pas)
    if not (rv_pas and rv_pas > 0) or not np.isfinite(ret_pas):
        return None
    rv_ant = rg.rv(ant) if ant.notna().sum() >= 30 else np.nan
    vol = _num_vol(modelo, dia_iso)
    if vol is None:
        return None
    x_har = {"log_rv_pas": np.log(rv_pas), **vol}
    if np.isfinite(rv_ant) and rv_ant > 0:
        x_har["log_rv_ant"] = np.log(rv_ant)
        aj = modelo["har"]["longo"]
    else:
        aj = modelo["har"]["curto"]
    X = np.ones((1, 1 + len(aj["dummies"]) + len(aj["num"])))
    for j, c in enumerate(aj["dummies"]):
        X[0, 1 + j] = float(c[2:] == slot)
    for j, c in enumerate(aj["num"]):
        X[0, 1 + len(aj["dummies"]) + j] = x_har[c]
    sigma = float(np.exp((X @ np.asarray(aj["coef"]))[0]))
    if not np.isfinite(sigma) or sigma <= 0:
        return None
    rr = _rr25_hoje(con, dia_iso, agora_ms)
    nome = "B" if (rr is not None and modelo.get("qr_B")) else "A"
    qr = modelo["qr_B"] if nome == "B" else modelo["qr_A"]
    cols = modelo["colunas_b"] if nome == "B" else modelo["colunas_a"]
    valores = {"log_sigma": np.log(sigma), "er_pas": er_pas,
               "ret_pas_std": ret_pas / (sigma / 100.0), "min_ate_fim": MIN_FECHAMENTO - t_min, "rr25": rr}
    sigma_fech = sigma * np.sqrt(max(MIN_FECHAMENTO - t_min, 1) / 60.0)
    saida = {"modelo": nome, "dia": dia_iso, "slot": slot, "agora_ms": agora_ms, "preco": preco}
    for chave, alvo, sig in (("60min", "60min", sigma), ("fechamento", "fechamento", sigma_fech)):
        qr_alvo = qr.get(alvo)
        if not qr_alvo:
            continue
        qz = _prever_por_modelo(modelo, qr_alvo, cols, valores)
        pts = qz * sig / 100.0 * preco
        saida[chave] = {f"q{int(t * 100):02d}": float(v) for t, v in zip(QUANTIS, pts)}
        saida[chave].update({"sigma_prev": float(sig), "preco": preco, "slot": slot})
    return saida if "60min" in saida else None


def main():
    ap = argparse.ArgumentParser(description="Faixas quantilicas conformalizadas do WIN (v3).")
    ap.add_argument("comando", choices=("atualizar", "avaliar"))
    ap.add_argument("--db", default=str(Path(__file__).parent / "dados" / "book.db"))
    a = ap.parse_args()
    if a.comando == "atualizar":
        atualizar(a.db)
        return
    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    try:
        avaliar(con)
    finally:
        con.close()


if __name__ == "__main__":
    main()
