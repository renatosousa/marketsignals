"""Correlacoes robustas, condicionadas ao regime de vol e com controle de falsas descobertas (ESPEC_V3.md 5.1.3).

Mesmos pares e regras R1-R4 de correlacoes.py (grade de 1 min, minimo de 60 retornos, IC de Fisher, beta), com:
  corr_huber   Pearson sobre os retornos winsorizados em +-3,5 x MAD de cada serie (MAD normalizada por 1,4826, ou
               seja, 3,5 desvios robustos; MAD zero -> desvio padrao; serie constante -> sem correlacao);
  spearman     correlacao de postos em paralelo, sobre os retornos originais;
  ref_regime   referencia nos 120 pregoes anteriores, restrita ao mesmo balde de vol diaria do WIN do dia (acima ou
               abaixo da mediana dos 120); minimo de 15 dias; o balde de hoje vem da vol realizada ate agora (escalada
               para o dia inteiro) ou da vol prevista pelo modelo_regime.json;
  z_regime     (corr_huber - media) / desvio da referencia condicionada;
  p_valor      bilateral de z_regime ~ Normal(0,1);
  q_valor      Benjamini-Hochberg (FDR 0,05) sobre todos os pares monitorados do dia;
  alerta       q_valor < 0,05 e n >= 120 (regra R10 de destaque, agora com FDR).

A tabela correl_diaria_v3 (data, y, x, corr_huber, spearman, n, rv_dia_win) e preenchida por atualizar(db, dias=150),
idempotente. correlacoes_v3_api devolve o mesmo formato de painel_v2.correlacoes_api acrescido das chaves novas; nao
edita painel_v2. As funcoes de calculo so leem o banco (o atualizar grava apenas no banco informado).

Uso: python correlacoes_v3.py atualizar [--db dados/book.db] [--dias 150]
     python correlacoes_v3.py dia AAAA-MM-DD [--db dados/book.db]
     python correlacoes_v3.py api [--agora ISO] [--janela pregao] [--db dados/book.db]
"""
import argparse
import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm, spearmanr

import banco
import correlacoes as corr
import modelo_regime as mr

D = corr.D
N_MIN = corr.N_MIN
MAD_K = 3.5
MAD_ESCALA = 1.4826
N_REF = 120
N_REF_MIN = 15
FDR_ALPHA = 0.05
N_ALERTA = 120
QUANTIS = (0.05, 0.15, 0.50, 0.85, 0.95)

SCHEMA = """CREATE TABLE IF NOT EXISTS correl_diaria_v3 (
    data TEXT NOT NULL, y TEXT NOT NULL, x TEXT NOT NULL,
    corr_huber REAL, spearman REAL, n INTEGER, rv_dia_win REAL,
    PRIMARY KEY (data, y, x));"""


# ------------------------------------------------------------------ utilidades
def _dia(data):
    return corr._dia(data)


def _data(dia):
    return corr._data(dia)


def _tem_tabela(con, nome):
    return bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (nome,)).fetchone())


def _sessao_min():
    return corr.SESSAO_FUTUROS[1] - corr.SESSAO_FUTUROS[0]


def _limites_winsor(x, k=MAD_K):
    """(lo, hi) da winsorizacao pela mediana e MAD da serie inteira; None se nao houver dispersao."""
    a = np.asarray(x, dtype=float)
    a = a[np.isfinite(a)]
    if a.size == 0:
        return None
    med = float(np.median(a))
    mad = float(np.median(np.abs(a - med))) * MAD_ESCALA
    if not np.isfinite(mad) or mad == 0:
        mad = float(np.std(a))
    if not np.isfinite(mad) or mad == 0:
        return None
    return med - k * mad, med + k * mad


def winsorizar(x, k=MAD_K):
    """Retornos winsorizados em mediana +- k * MAD (MAD normalizada por 1,4826).

    MAD zero cai no desvio padrao; serie constante fica como esta. Devolve ndarray float.
    """
    a = np.asarray(x, dtype=float)
    lim = _limites_winsor(a, k)
    if lim is None:
        return a.copy()
    return np.clip(a, lim[0], lim[1])


def correlacao_huber(ry, rx):
    """(corr de Pearson sobre os retornos winsorizados, n alinhado); None se n < 60 ou serie constante.

    Os limites de winsorizacao sao calculados em cada serie inteira do dia (antes do alinhamento), como manda a
    regra R1-R4 estendida da ESPEC.
    """
    d = pd.concat([ry, rx], axis=1, join="inner").dropna()
    n = int(len(d))
    if n < N_MIN:
        return None, n
    lim_a, lim_b = _limites_winsor(ry), _limites_winsor(rx)
    a = np.clip(d.iloc[:, 0].to_numpy(dtype=float), *lim_a) if lim_a else d.iloc[:, 0].to_numpy(dtype=float)
    b = np.clip(d.iloc[:, 1].to_numpy(dtype=float), *lim_b) if lim_b else d.iloc[:, 1].to_numpy(dtype=float)
    if np.std(a) == 0 or np.std(b) == 0:
        return None, n
    c = float(np.corrcoef(a, b)[0, 1])
    return (c if np.isfinite(c) else None), n


def correlacao_spearman(ry, rx):
    """Spearman dos retornos alinhados; None se n < 60 ou serie constante."""
    d = pd.concat([ry, rx], axis=1, join="inner").dropna()
    n = int(len(d))
    if n < N_MIN or d.iloc[:, 0].nunique() < 2 or d.iloc[:, 1].nunique() < 2:
        return None, n
    r = spearmanr(d.iloc[:, 0].to_numpy(dtype=float), d.iloc[:, 1].to_numpy(dtype=float)).statistic
    return (float(r) if np.isfinite(r) else None), n


def rv_dia_win(con, dia):
    """Vol realizada do WIN no dia (raiz da soma dos log-retornos de 1 min ao quadrado, %); None sem amostra."""
    s = corr.serie_1m(con, "WIN$", dia, ao_vivo=False)
    r = corr.retornos(s, "WIN$")
    return float(np.sqrt((r ** 2).sum()) * 100) if len(r) >= 10 else None


def vol_do_dia(con, dia, agora_ms=None):
    """Vol diaria do WIN (%), causal: dia fechado usa a vol realizada; dia em curso usa a parcial escalada por
    sqrt(minutos_da_sessao / minutos_observados), com fallback na vol prevista do modelo_regime.json."""
    dia = _dia(dia)
    iso = _data(dia)
    cheio = agora_ms is None or int(agora_ms) // D > dia
    if cheio:
        v = rv_dia_win(con, dia)
    else:
        min_fim = (int(agora_ms) % D) // 60_000
        s = corr.serie_1m(con, "WIN$", dia, ao_vivo=True)
        r = corr.retornos(s[s.index <= min_fim] if len(s) else s, "WIN$")
        n = int(len(r))
        v = float(np.sqrt((r ** 2).sum()) * 100 * np.sqrt(_sessao_min() / n)) if n >= 30 else None
    if v is not None and np.isfinite(v) and v > 0:
        return float(v)
    # fallback: vol diaria prevista pelo modelo_regime.json (se existir e cobrir a data)
    try:
        m = mr.carregar_modelo()
        if m:
            v = mr.prever_dia(m, iso)
            if v is not None and np.isfinite(v) and v > 0:
                return float(v)
    except Exception:
        pass
    return None


# ------------------------------------------------------------------ correlacoes do dia
def correl_do_dia(con, data, ao_vivo=False):
    """Lista de correlacoes do dia (mesmos pares de correlacoes.py) com Pearson, huber, Spearman e rv do WIN."""
    dia = _dia(data)
    iso = _data(dia)
    cache, rets = {}, {}

    def serie(s):
        if s not in cache:
            cache[s] = corr.serie_1m(con, s, dia, ao_vivo)
        return cache[s]

    def ret(s):
        if s not in rets:
            rets[s] = corr.retornos(serie(s), s)
        return rets[s]

    rv = rv_dia_win(con, dia)
    out = []
    for y, x in corr._pares_do_dia():
        p = corr.par(ret(y), ret(x))
        h, n = correlacao_huber(ret(y), ret(x))
        sp, _n = correlacao_spearman(ret(y), ret(x))
        out.append({"data": iso, "y": y, "x": x, "corr": p["corr"], "corr_huber": h, "spearman": sp,
                    "n": n, "beta": p["beta"], "ic_lo": p["ic_lo"], "ic_hi": p["ic_hi"], "rv_dia_win": rv})
    return out


def _dias_do_intervalo(con, dias, ate=None):
    """Dias (indices) com barras do WIN nos ultimos `dias` dias corridos (ou ate `ate`, inclusive)."""
    fim = (_dia(ate) + 1) if ate is not None else (date.today() - date(1970, 1, 1)).days + 1
    ini = fim - int(dias) if ate is None else fim - int(dias)
    return [int(r[0]) for r in con.execute(
        "SELECT DISTINCT ts_ms / 86400000 FROM barras_1m WHERE symbol='WIN$' AND ts_ms>=? AND ts_ms<? ORDER BY 1",
        (ini * D, fim * D))]


def historico(con, dias=150, ate=None, pares=None, dias_idx=None, ao_vivo=False):
    """Historico diario em memoria (nao grava): lista de dicts com data, y, x, corr, corr_huber, spearman, n e rv.

    Carrega as series de 1 min uma vez por simbolo (correlacoes._series_do_periodo), o que torna o backfill viavel.
    """
    pares = list(pares) if pares is not None else corr._pares_do_dia()
    dias_idx = sorted(set(int(d) for d in (dias_idx if dias_idx is not None else
                                           _dias_do_intervalo(con, dias, ate))))
    if not dias_idx:
        return []
    simbolos = sorted({s for p in pares for s in p} | {"WIN$"})
    series = {s: corr._series_do_periodo(con, s, dias_idx) for s in simbolos}
    out = []
    for d in dias_idx:
        iso = _data(d)
        rets = {s: corr.retornos(series[s].get(d, pd.Series(dtype=float)), s) for s in simbolos}
        rv = float(np.sqrt((rets["WIN$"] ** 2).sum()) * 100) if len(rets.get("WIN$", [])) >= 10 else None
        for y, x in pares:
            ry, rx = rets.get(y, pd.Series(dtype=float)), rets.get(x, pd.Series(dtype=float))
            p = corr.par(ry, rx)
            h, n = correlacao_huber(ry, rx)
            sp, _n = correlacao_spearman(ry, rx)
            out.append({"data": iso, "y": y, "x": x, "corr": p["corr"], "corr_huber": h, "spearman": sp,
                        "n": n, "rv_dia_win": rv})
    return out


def atualizar(db_path, dias=150):
    """Backfill idempotente de correl_diaria_v3 nos ultimos `dias` dias corridos -> numero de pregoes gravados."""
    db = banco.abrir(db_path, SCHEMA)
    try:
        dias_idx = _dias_do_intervalo(db, dias)
        if not dias_idx:
            return 0
        recentes = set(dias_idx[-2:])
        existentes = {corr._dia(r[0]) for r in db.execute("SELECT DISTINCT data FROM correl_diaria_v3")}
        precisa = [d for d in dias_idx if d not in existentes or d in recentes]
        if not precisa:
            return 0
        linhas = historico(db, dias_idx=precisa)
        agora = datetime.now().isoformat(timespec="seconds")
        with db:
            db.executemany("INSERT OR REPLACE INTO correl_diaria_v3 "
                           "(data, y, x, corr_huber, spearman, n, rv_dia_win) VALUES (?,?,?,?,?,?,?)",
                           [(l["data"], l["y"], l["x"], l["corr_huber"], l["spearman"], l["n"], l["rv_dia_win"])
                            for l in linhas])
        return len({l["data"] for l in linhas})
    finally:
        db.close()


# ------------------------------------------------------------------ referencia por regime
def _ref_de(registros, vol_hoje, datas_vols=None, n_dias=N_REF, minimo=N_REF_MIN):
    """Referencia condicionada ao balde de vol (alta/baixa) a partir de registros diarios de um par.

    registros: [{"data", "corr_huber", "rv_dia_win"}]; datas_vols: [(data, vol)] de todos os pares (se omitido, usa
    os proprios registros). Retorna media, desvio, p10, p90, n_dias, balde, vol_mediana e vol_hoje; None se o balde
    tiver menos de `minimo` dias ou se vol_hoje for None.
    """
    if vol_hoje is None or not np.isfinite(vol_hoje) or vol_hoje <= 0:
        return None
    vols = datas_vols if datas_vols is not None else [(r["data"], r["rv_dia_win"]) for r in registros]
    vols = sorted({(d, v) for d, v in vols if v is not None and np.isfinite(v)}, key=lambda x: x[0], reverse=True)
    vols = vols[:n_dias]
    if not vols:
        return None
    mediana = float(np.median([v for _d, v in vols]))
    alta_hoje = vol_hoje >= mediana
    balde = "alta" if alta_hoje else "baixa"
    mesmo = {d for d, v in vols if (v >= mediana) == alta_hoje}
    vals = np.array([r["corr_huber"] for r in registros
                     if r["data"] in mesmo and r["corr_huber"] is not None and np.isfinite(r["corr_huber"])],
                    dtype=float)
    if len(vals) < minimo:
        return None
    return {"media": float(vals.mean()), "desvio": float(vals.std(ddof=1)) if len(vals) > 1 else None,
            "p10": float(np.percentile(vals, 10)), "p90": float(np.percentile(vals, 90)),
            "n_dias": int(len(vals)), "balde": balde, "vol_mediana": mediana, "vol_hoje": float(vol_hoje)}


def ref_regime(con, y, x, antes_de, vol_hoje, n_dias=N_REF, minimo=N_REF_MIN, historico=None):
    """Referencia condicionada ao regime do par (tabela correl_diaria_v3; sem tabela, calcula em memoria).

    Aceita o par nas duas ordens. -> dict de _ref_de ou None.
    """
    iso = _data(_dia(antes_de))
    if historico is not None:
        regs = [r for r in historico if r["data"] < iso and
                ((r["y"] == y and r["x"] == x) or (r["y"] == x and r["x"] == y))]
        return _ref_de(regs, vol_hoje, n_dias=n_dias, minimo=minimo)
    if _tem_tabela(con, "correl_diaria_v3"):
        vols = [(d, v) for d, v in con.execute(
            "SELECT data, max(rv_dia_win) FROM correl_diaria_v3 WHERE data<? AND rv_dia_win IS NOT NULL "
            "GROUP BY data ORDER BY data DESC LIMIT ?", (iso, int(n_dias)))]
        if vols:
            regs = [{"data": d, "corr_huber": c, "rv_dia_win": v} for d, c, v in con.execute(
                "SELECT data, corr_huber, rv_dia_win FROM correl_diaria_v3 WHERE data<? AND "
                "((y=? AND x=?) OR (y=? AND x=?))", (iso, y, x, x, y))]
            return _ref_de(regs, vol_hoje, datas_vols=vols, n_dias=n_dias, minimo=minimo)
    hist = historico(con, dias=n_dias + 10, ate=antes_de, pares=[(y, x)])
    return ref_regime(con, y, x, antes_de, vol_hoje, n_dias=n_dias, minimo=minimo, historico=hist)


def refs_do_dia(con, data, pares, vol_hoje, n_dias=N_REF, minimo=N_REF_MIN, hist=None):
    """{par: ref} de varios pares numa unica leitura; meta com vol_hoje, mediana e balde."""
    iso = _data(_dia(data))
    pares = list(pares)
    if hist is None and _tem_tabela(con, "correl_diaria_v3"):
        vols = [(d, v) for d, v in con.execute(
            "SELECT data, max(rv_dia_win) FROM correl_diaria_v3 WHERE data<? AND rv_dia_win IS NOT NULL "
            "GROUP BY data ORDER BY data DESC LIMIT ?", (iso, int(n_dias)))]
        if vols:
            inicio = min(d for d, _v in vols)
            chaves = {p for par in pares for p in (par, par[::-1])}
            linhas = con.execute(
                "SELECT data, y, x, corr_huber FROM correl_diaria_v3 WHERE data>=? AND data<?",
                (inicio, iso)).fetchall()
            regs = {}
            for d, y, x, c in linhas:
                if (y, x) in chaves:
                    regs.setdefault((y, x), []).append({"data": d, "corr_huber": c, "rv_dia_win": None})
            saida = {}
            for par in pares:
                regs_par = regs.get(par, []) + regs.get(par[::-1], [])
                saida[par] = _ref_de(regs_par, vol_hoje, datas_vols=vols, n_dias=n_dias, minimo=minimo)
            ref0 = next((r for r in saida.values() if r), None)
            meta = {"vol_hoje": vol_hoje, "vol_mediana": ref0["vol_mediana"] if ref0 else None,
                    "balde": ref0["balde"] if ref0 else None, "n_dias": int(n_dias)}
            return saida, meta
    hist = hist if hist is not None else historico(con, dias=n_dias + 10, ate=data, pares=pares)
    iso = _data(_dia(data))
    hist = [r for r in hist if r["data"] < iso]
    return _refs_de_hist(hist, pares, vol_hoje, n_dias, minimo)


def _refs_de_hist(hist, pares, vol_hoje, n_dias=N_REF, minimo=N_REF_MIN):
    """refs_do_dia a partir do historico em memoria."""
    saida, meta = {}, {"vol_hoje": vol_hoje, "vol_mediana": None, "balde": None, "n_dias": int(n_dias)}
    vols = sorted({(r["data"], r["rv_dia_win"]) for r in hist if r["rv_dia_win"] is not None},
                  key=lambda x: x[0], reverse=True)[:n_dias]
    for par in pares:
        regs = [r for r in hist if (r["y"], r["x"]) == par or (r["y"], r["x"]) == par[::-1]]
        saida[par] = _ref_de(regs, vol_hoje, datas_vols=vols, n_dias=n_dias, minimo=minimo)
        if saida[par]:
            meta.update({"vol_mediana": saida[par]["vol_mediana"], "balde": saida[par]["balde"]})
    return saida, meta


# ------------------------------------------------------------------ estatistica e FDR
def p_valor(z):
    """p-valor bilateral de z ~ Normal(0,1); None sem z."""
    if z is None or not np.isfinite(z):
        return None
    return float(2.0 * norm.sf(abs(z)))


def benjamini_hochberg(pvals, alpha=FDR_ALPHA):
    """q-valores de Benjamini-Hochberg na mesma ordem; None para p ausente. m = numero de p validos."""
    idx = [i for i, p in enumerate(pvals) if p is not None and np.isfinite(p)]
    q = [None] * len(pvals)
    m = len(idx)
    if m == 0:
        return q
    pares = sorted((pvals[i], i) for i in idx)
    anterior = 1.0
    for rank in range(m, 0, -1):
        p, i = pares[rank - 1]
        qv = min(anterior, p * m / rank)
        q[i] = float(min(1.0, max(0.0, qv)))
        anterior = qv
    return q


def z_regime(corr_huber, ref):
    """(corr_huber - media) / desvio da referencia condicionada; None sem referencia ou desvio zero."""
    if corr_huber is None or ref is None or not ref.get("desvio"):
        return None
    return float((corr_huber - ref["media"]) / ref["desvio"])


# ------------------------------------------------------------------ API no formato do painel_v2
def _linha_v3(con, series, data, y, x, min_ini, min_fim, hoje, refs, qmap, preco_fim):
    """Linha da tabela do painel com as chaves de correlacoes_v3."""
    p = corr.par(series.retornos(y, min_ini, min_fim), series.retornos(x, min_ini, min_fim))
    h, spearman = hoje[(y, x)]["corr_huber"], hoje[(y, x)]["spearman"]
    ref = corr.referencia(con, y, x, data) if data else None
    z = corr.z(p["corr"], ref)
    rr = refs.get((y, x))
    zr = z_regime(h, rr)
    pv = p_valor(zr)
    qv = qmap.get((y, x))
    linha = {"x": x, "nome": corr.NOMES.get(x, x), **p, "ref": ref, "z": z,
             "corr_huber": h, "spearman": spearman, "ref_regime": rr, "z_regime": zr,
             "p_valor": pv, "q_valor": qv,
             "alerta": bool(qv is not None and qv < FDR_ALPHA and p["n"] >= N_ALERTA)}
    if p["corr"] is None:
        linha["motivo"] = f"amostra curta, n = {p['n']}"
    linha["beta_pts"] = (p["beta"] * preco_fim if corr.eh_taxa(x) else p["beta"] * preco_fim * 0.01) \
        if p["beta"] is not None and preco_fim is not None else None
    return linha


def correlacoes_v3_api(con, agora_ms, janela="pregao"):
    """Painel de correlacoes v3 no mesmo formato de painel_v2.correlacoes_api (alvo WIN), com as chaves novas.

    Estritamente causal: usa a janela ate agora_ms e o historico de correl_diaria_v3 (ou calcula em memoria se a
    tabela ainda nao foi preenchida). O FDR e aplicado sobre todos os pares monitorados do dia.
    """
    import painel_v2 as pv2
    if janela not in pv2.JANELAS:
        raise ValueError(f"janela invalida ({' ou '.join(pv2.JANELAS)})")
    t1 = int(agora_ms)
    dia = t1 // D
    data = corr._data(dia)
    series = pv2._SeriesDia(con, dia)
    min_ini, min_fim = pv2.janela_minutos(t1, janela)
    trecho = series.trecho("WIN$", min_ini, min_fim)
    preco_fim = float(trecho.iloc[-1]) if len(trecho) else None
    pares = corr._pares_do_dia()
    hoje = {}
    for y, x in pares:
        ry, rx = series.retornos(y, min_ini, min_fim), series.retornos(x, min_ini, min_fim)
        h, _n = correlacao_huber(ry, rx)
        sp, _n = correlacao_spearman(ry, rx)
        hoje[(y, x)] = {"corr_huber": h, "spearman": sp}
    vol = vol_do_dia(con, dia, t1)
    try:
        refs, meta = refs_do_dia(con, data, pares, vol)
    except Exception:
        refs, meta = {}, {"vol_hoje": vol, "vol_mediana": None, "balde": None, "n_dias": 0}
    pvals, chaves = [], []
    for par in pares:
        zr = z_regime(hoje[par]["corr_huber"], refs.get(par))
        pvals.append(p_valor(zr))
        chaves.append(par)
    qs = benjamini_hochberg(pvals)
    qmap = {k: q for k, q in zip(chaves, qs)}
    tabela = [_linha_v3(con, series, data, "WIN$", x, min_ini, min_fim, hoje, refs, qmap, preco_fim)
              for x in corr.PARES_WIN]
    tabela.sort(key=lambda l: (l["z"] is None, -abs(l["z"] or 0)))
    principais = []
    for py, px in corr.PRINCIPAIS:
        p = corr.par(series.retornos(py, min_ini, min_fim), series.retornos(px, min_ini, min_fim))
        ref = corr.referencia(con, py, px, data) if data else None
        z = corr.z(p["corr"], ref)
        h = hoje.get((py, px), hoje.get((px, py), {})).get("corr_huber")
        sp = hoje.get((py, px), hoje.get((px, py), {})).get("spearman")
        rr = refs.get((py, px)) or refs.get((px, py))
        zr = z_regime(h, rr)
        qv = qmap.get((py, px), qmap.get((px, py)))
        principais.append({"y": py, "x": px, "nome": corr.NOMES.get(px, px), "corr": p["corr"], "n": p["n"],
                           "ref": ref, "z": z, "corr_huber": h, "spearman": sp, "ref_regime": rr,
                           "z_regime": zr, "p_valor": p_valor(zr), "q_valor": qv,
                           "alerta": bool(qv is not None and qv < FDR_ALPHA and p["n"] >= N_ALERTA)})
    movel = []
    for py, px in corr.PRINCIPAIS:
        ref = corr.referencia(con, py, px, data) if data else None
        ry = series.retornos(py)
        ry = ry[ry.index <= min_fim]
        rx = series.retornos(px)
        rx = rx[rx.index <= min_fim]
        m = corr.movel(ry, rx)
        movel.append({"par": f"{py}|{px}", "t": [dia * 86400 + int(i) * 60 for i in m.index],
                      "corr": [float(v) if v == v else None for v in m.to_numpy()],
                      "p10": ref["p10"] if ref else None, "p90": ref["p90"] if ref else None})
    return {"janela": janela, "alvo": "WIN",
            "t0": dia * 86400 + min_ini * 60, "t1": t1 / 1000,
            "n_minutos": min_fim - min_ini, "tabela": tabela, "principais": principais, "movel": movel,
            "matriz": pv2._matriz(con, series, data, min_ini, min_fim),
            "decomposicao": pv2.decomposicao(con, dia, janela, _series=series),
            "regime": meta, "fdr_alpha": FDR_ALPHA, "n_hipoteses": sum(p is not None for p in pvals)}


# ------------------------------------------------------------------ CLI
def _resumo_dia(con, data):
    """Linhas principais do dia com Pearson, huber, Spearman, z simples, z_regime, q e alerta."""
    linhas = correl_do_dia(con, data, ao_vivo=False)
    por_par = {(l["y"], l["x"]): l for l in linhas}
    iso = _data(_dia(data))
    vol = vol_do_dia(con, data)
    pares = corr._pares_do_dia()
    refs, _meta = refs_do_dia(con, data, pares, vol)
    pvals = [p_valor(z_regime(por_par[p]["corr_huber"], refs.get(p))) if p in por_par else None for p in pares]
    qs = benjamini_hochberg(pvals)
    qmap = {p: q for p, q in zip(pares, qs)}
    out = []
    for y, x in corr.PRINCIPAIS:
        l = por_par.get((y, x))
        if not l:
            continue
        ref = corr.referencia(con, y, x, iso)
        rr = refs.get((y, x)) or refs.get((x, y))
        zr = z_regime(l["corr_huber"], rr)
        q = qmap.get((y, x), qmap.get((x, y)))
        out.append({**l, "ref": ref, "z": corr.z(l["corr"], ref), "ref_regime": rr, "z_regime": zr,
                    "p_valor": p_valor(zr), "q_valor": q,
                    "alerta": bool(q is not None and q < FDR_ALPHA and l["n"] >= N_ALERTA)})
    return {"data": iso, "vol_win": vol, "linhas": out, "n_pares": len(pares)}


def _txt(v, nd=3, sinal=False):
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "-"
    return f"{v:+.{nd}f}" if sinal else f"{v:.{nd}f}"


def main():
    ap = argparse.ArgumentParser(description="Correlacoes v3: Huber, Spearman, regime de vol e FDR.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("atualizar")
    a.add_argument("--db", default=str(Path(__file__).parent / "dados" / "book.db"))
    a.add_argument("--dias", type=int, default=150)
    d = sub.add_parser("dia")
    d.add_argument("data")
    d.add_argument("--db", default=str(Path(__file__).parent / "dados" / "book.db"))
    p = sub.add_parser("api")
    p.add_argument("--agora", default=None, help="ISO; padrao = ultimo snapshot do WIN")
    p.add_argument("--janela", default="pregao")
    p.add_argument("--db", default=str(Path(__file__).parent / "dados" / "book.db"))
    args = ap.parse_args()
    if args.cmd == "atualizar":
        print(f"{atualizar(args.db, args.dias)} pregao(oes) gravado(s) em correl_diaria_v3")
        return
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        if args.cmd == "dia":
            r = _resumo_dia(con, args.data)
            print(f"correlacoes v3 de {r['data']} | vol do WIN {r['vol_win'] if r['vol_win'] is None else round(r['vol_win'], 2)}")
            for l in r["linhas"]:
                rr = l["ref_regime"]
                print(f"  {l['y']:6} x {l['x']:8} n={l['n']:4d} pearson={_txt(l['corr'], 3, True)} "
                      f"huber={_txt(l['corr_huber'], 3, True)} spearman={_txt(l['spearman'], 3, True)} "
                      f"z={_txt(l['z'], 2, True)} z_regime={_txt(l['z_regime'], 2, True)} "
                      f"q={_txt(l['q_valor'])} alerta={l['alerta']} "
                      f"balde={rr['balde'] if rr else '-'} dias={rr['n_dias'] if rr else '-'}")
            return
        if args.agora:
            agora = int(pd.Timestamp(args.agora).timestamp() * 1000)
        else:
            r = con.execute("SELECT max(ts_ms) FROM snapshots WHERE symbol='WIN$'").fetchone()
            agora = int(r[0])
        print(json.dumps(correlacoes_v3_api(con, agora, args.janela), ensure_ascii=False, indent=1))
    finally:
        con.close()


if __name__ == "__main__":
    main()
