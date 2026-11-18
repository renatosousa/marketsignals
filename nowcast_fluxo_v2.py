"""Nowcast do fluxo do estrangeiro do dia (ESPEC_V3.md secao 5.1.1), com tres componentes e encolhimento.

A B3 publica o fluxo por tipo de investidor das acoes com 2 pregoes de atraso (fluxo_diario.data_pub). Para o dia T
o nowcast combina, sem olhar o futuro:
  f_ofi        oficial do ultimo dia publicado ate T (o oficial de T-1 ou T-2, o mais recente com data_pub <= T);
  f_tryd       Tryd de T (estimador_tryd), corrigido pela EWMA (span 10) dos erros Tryd - oficial dos dias cujo
               oficial ja foi publicado ate T (f_tryd_corr); f_tryd guarda o valor bruto;
  f_cesta      cesta_fin_liq_mi de features_fluxo_dia, so com captura_vol >= estimador_tryd.CAPTURA_MIN.

A combinacao usa pesos a priori w0 = (0,40; 0,40; 0,20) e encolhe os pesos empiricos para eles:
  w = (30 * w0 + n * w_emp) / (30 + n)
onde n e o numero de dias completos anteriores a T (oficial ja publicado, tres componentes presentes) e w_emp vem de
minimos quadrados com soma 1 (sem intercepto; SLSQP com pesos >= 0, fallback Ridge sem intercepto normalizado).
Componente ausente no dia: os pesos dos presentes sao renormalizados.

A funcao atualizar grava nowcast_fluxo no banco real; estimar, historico e avaliar so leem. O historico e
walk-forward: o nowcast de cada dia usa somente dados disponiveis naquele dia (regra causal testada em
test_nowcast_fluxo_v2.py).

Uso: python nowcast_fluxo_v2.py atualizar [--db dados/book.db]   (grava nowcast_fluxo dos dias fechados)
     python nowcast_fluxo_v2.py avaliar [--db dados/book.db]     (so le: tabela de acerto contra o oficial)
"""
import argparse
import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import minimize

import banco
import estimador_tryd as et

DIA_MS = 86_400_000
TIPO_B3 = et.TIPO_B3
CAPTURA_MIN = et.CAPTURA_MIN
W0 = np.array([0.40, 0.40, 0.20])
N0 = 30.0
COMPONENTES = ("f_ofi", "f_tryd", "f_cesta")
EWMA_SPAN = 10
TS_ESTIMATIVA_MS = 18 * 3_600_000 + 30 * 60_000  # 18:30 BRT: estimativa valida no fim do pregao

SCHEMA = """CREATE TABLE IF NOT EXISTS nowcast_fluxo (
    dia TEXT PRIMARY KEY, estimativa_mi REAL, f_ofi REAL, f_tryd REAL, f_tryd_corr REAL, f_cesta REAL,
    pesos_json TEXT, n_obs INTEGER, ts_ms INTEGER);"""


def _data(dia):
    """Texto AAAA-MM-DD do indice de dia (ts_ms // 86400000), de date ou do proprio texto."""
    if isinstance(dia, str):
        return dia
    if isinstance(dia, date):
        return dia.isoformat()
    return (date(1970, 1, 1) + timedelta(days=int(dia))).isoformat()


def _dia(data):
    """Indice de dia (ts_ms // 86400000) de AAAA-MM-DD, date ou indice."""
    if isinstance(data, (int, np.integer)):
        return int(data)
    if isinstance(data, date):
        return (data - date(1970, 1, 1)).days
    return (date.fromisoformat(str(data)[:10]) - date(1970, 1, 1)).days


def _sinal(x):
    return (x > 0) - (x < 0)


def _tem_tabela(con, nome):
    return bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (nome,)).fetchone())


def oficial(con):
    """{data_ref: (saldo_mi, data_pub)} do fluxo oficial do estrangeiro; {} se a tabela nao existir."""
    try:
        return {d: (s / 1000.0, p) for d, s, p in con.execute(
            "SELECT data_ref, saldo_mil, data_pub FROM fluxo_diario WHERE tipo=?", (TIPO_B3,))}
    except sqlite3.OperationalError:
        return {}


def _tryd_do_dia(con, dia, tabela):
    """Tryd do dia em R$ mi (estimador_tryd._tryd_do_dia); None se nao houver acao com foto."""
    try:
        v, _n = et._tryd_do_dia(con, int(dia), tabela)
        return v
    except sqlite3.OperationalError:
        return None


def tryd(con):
    """{data: tryd_mi} dos dias com Tryd, da tabela tryd_estrangeiro_dia ou calculado de tryd_saldo."""
    tabela = _tem_tabela(con, "tryd_estrangeiro_dia")
    if tabela:
        linhas = con.execute("SELECT data, sum(fin_liq) / 1e6 FROM tryd_estrangeiro_dia "
                             "GROUP BY data HAVING sum(fin_liq) IS NOT NULL").fetchall()
        if linhas:
            return {d: float(v) for d, v in linhas}
    try:
        dias = [d for (d,) in con.execute("SELECT DISTINCT ts_ms / 86400000 FROM tryd_saldo ORDER BY 1")]
    except sqlite3.OperationalError:
        return {}
    out = {}
    for d in dias:
        v = _tryd_do_dia(con, d, False)
        if v is not None:
            out[_data(d)] = v
    return out


def cesta(con):
    """{data: cesta_fin_liq_mi} so dos dias com captura_vol >= CAPTURA_MIN (regra do estimador_tryd)."""
    try:
        return {d: float(v) for d, v, cap in con.execute(
            "SELECT data, cesta_fin_liq_mi, captura_vol FROM features_fluxo_dia") if v is not None and
                (cap if cap is not None else 0) >= CAPTURA_MIN}
    except sqlite3.OperationalError:
        return {}


def _ewma_erros(oficial_d, tryd_d, dia):
    """EWMA (span 10, recursiva) dos erros Tryd - oficial dos dias com oficial publicado ate `dia`; 0 sem erros.

    So entram dias anteriores a `dia` (o erro do proprio dia ainda nao e conhecido) e cuja data_pub <= dia.
    """
    pares = sorted((d, tryd_d[d] - of)
                   for d, (of, pub) in oficial_d.items()
                   if d < dia and pub is not None and pub <= dia and d in tryd_d)
    if not pares:
        return 0.0
    s = pd.Series([e for _, e in pares]).ewm(span=EWMA_SPAN, adjust=False).mean()
    return float(s.iloc[-1])


def _componentes(dia, oficial_d, tryd_d, cesta_d):
    """Componentes do dia `dia` (texto) usando so o que estava publicado ate ele -> dict com f_* (None se faltar)."""
    publicados = sorted((d, pub) for d, (_v, pub) in oficial_d.items()
                        if pub is not None and pub <= dia and d < dia)
    f_ofi = oficial_d[publicados[-1][0]][0] if publicados else None
    f_tryd = tryd_d.get(dia)
    corrigido = _ewma_erros(oficial_d, tryd_d, dia)
    f_tryd_corr = f_tryd - corrigido if f_tryd is not None else None
    return {"f_ofi": f_ofi, "f_tryd": f_tryd, "f_tryd_corr": f_tryd_corr, "f_cesta": cesta_d.get(dia)}


def _ajustar_pesos(X, y):
    """Pesos de minimos quadrados com soma 1 e sem intercepto (SLSQP, pesos >= 0).

    Fallback: Ridge sem intercepto (alpha 1) normalizado para soma 1. Sem amostra -> w0.
    """
    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=float)
    if len(y) == 0:
        return W0.copy()
    if len(y) < 3 or np.linalg.matrix_rank(X - X.mean(axis=0)) < 1:
        return W0.copy()

    def perda(w):
        r = X @ w - y
        return float(r @ r)

    try:
        r = minimize(perda, W0.copy(), method="SLSQP", bounds=[(0.0, None)] * 3,
                     constraints={"type": "eq", "fun": lambda w: float(w.sum() - 1.0)},
                     options={"maxiter": 500, "ftol": 1e-12})
        w = np.asarray(r.x, dtype=float)
        if r.success and w.shape == (3,) and abs(w.sum() - 1.0) < 1e-6 and np.all(w >= -1e-9):
            return np.clip(w, 0.0, None) / max(w.sum(), 1e-12)
    except Exception:
        pass
    try:
        w = np.linalg.solve(X.T @ X + np.eye(3), X.T @ y)
        if np.all(np.isfinite(w)) and abs(w.sum()) > 1e-12:
            w = w / w.sum()
            if np.all(w >= -1e-9):
                return np.clip(w, 0.0, None) / max(w.sum(), 1e-12)
    except np.linalg.LinAlgError:
        pass
    return W0.copy()


def _pesos_do_dia(dia, linhas, oficial_d):
    """(pesos finais, n_obs) do dia `dia`: empiricos so com dias completos anteriores e oficial publicado ate ele."""
    pub = {d: p for d, (_s, p) in oficial_d.items()}
    obs = [l for l in linhas
           if l["dia"] < dia and pub.get(l["dia"]) is not None and pub[l["dia"]] <= dia
           and all(l[c] is not None for c in COMPONENTES)]
    n = len(obs)
    w_emp = _ajustar_pesos([[l[c] for c in COMPONENTES] for l in obs],
                           [oficial_d[l["dia"]][0] for l in obs]) if n else W0.copy()
    w = (N0 * W0 + n * np.asarray(w_emp, dtype=float)) / (N0 + n)
    return w, n


def _normalizar(valores, pesos):
    """Estimativa com os componentes presentes: renormaliza os pesos dos presentes; None se nenhum."""
    p = [(v, w) for v, w in zip(valores, pesos) if v is not None and w is not None]
    if not p:
        return None, [0.0, 0.0, 0.0]
    soma = sum(w for _, w in p)
    if soma <= 0:
        return None, [0.0, 0.0, 0.0]
    efetivos = [w / soma if v is not None else 0.0 for v, w in zip(valores, pesos)]
    return float(sum(v * w for v, w in p) / soma), efetivos


def _montar(con):
    """Tabela causal de componentes de todos os dias relevantes (uniao de oficial, Tryd, cesta e dias de hoje)."""
    of = oficial(con)
    tr = tryd(con)
    ce = cesta(con)
    dias = sorted(set(of) | set(tr) | set(ce))
    linhas = []
    for d in dias:
        c = _componentes(d, of, tr, ce)
        linhas.append({"dia": d, **c})
    return of, linhas


def _estimar_de(linhas, of, dia):
    """Nowcast do dia a partir das linhas causais de componentes -> dict (ou None se nada disponivel)."""
    linha = next((l for l in linhas if l["dia"] == dia), None)
    if linha is None:
        return None
    w, n = _pesos_do_dia(dia, linhas, of)
    valores = [linha[c] for c in COMPONENTES]
    est, efetivos = _normalizar(valores, w)
    if est is None:
        return None
    return {"dia": dia, "estimativa_mi": est, "f_ofi": linha["f_ofi"], "f_tryd": linha["f_tryd"],
            "f_tryd_corr": linha["f_tryd_corr"], "f_cesta": linha["f_cesta"],
            "pesos": dict(zip(COMPONENTES, map(float, efetivos))), "n_obs": int(n),
            "ts_ms": _dia(dia) * DIA_MS + TS_ESTIMATIVA_MS}


def estimar(con, dia):
    """Nowcast do dia (so le). `dia` pode ser AAAA-MM-DD ou indice de dia. None se nao houver componente."""
    dia = _data(dia)
    of, linhas = _montar(con)
    return _estimar_de(linhas, of, dia)


def historico(con, ate=None):
    """Walk-forward de todos os dias com oficial/Tryd/cesta (ate `ate`, inclusive) -> lista de dicts.

    Cada linha usa so dados publicados ate o proprio dia; nao ha vazamento do rotulo D+2.
    """
    of, linhas = _montar(con)
    limite = _data(ate) if ate is not None else None
    out = []
    for linha in linhas:
        if limite is not None and linha["dia"] > limite:
            continue
        r = _estimar_de(linhas, of, linha["dia"])
        if r is not None:
            out.append(r)
    return out


def atualizar(db_path, hoje=None):
    """Grava nowcast_fluxo (INSERT OR REPLACE) dos dias fechados -> numero de linhas.

    `hoje` (AAAA-MM-DD) limita o corte; por padrao usa todos os dias do historico causal.
    """
    db = banco.abrir(db_path, SCHEMA)
    try:
        linhas = historico(db, ate=hoje)
        db.executemany("INSERT OR REPLACE INTO nowcast_fluxo VALUES (?,?,?,?,?,?,?,?,?)",
                       [(l["dia"], l["estimativa_mi"], l["f_ofi"], l["f_tryd"], l["f_tryd_corr"],
                         l["f_cesta"], json.dumps(l["pesos"], sort_keys=True), l["n_obs"], l["ts_ms"])
                        for l in linhas])
        db.commit()
        return len(linhas)
    finally:
        db.close()


def _resumo(pares):
    """n, acertos de sinal, correlacao de postos (n >= 4), erro absoluto medio e vies (estimativa - oficial)."""
    n = len(pares)
    r = {"n": n, "sinal_certo": sum(_sinal(a) == _sinal(b) for a, b in pares), "postos": None,
         "erro_abs_medio": None, "vies": None}
    if not n:
        return r
    est, ofi = pd.Series([a for a, _ in pares], dtype=float), pd.Series([b for _, b in pares], dtype=float)
    if n >= 4:
        p = est.rank().corr(ofi.rank())
        r["postos"] = float(p) if p == p else None
    r["erro_abs_medio"] = float((est - ofi).abs().mean())
    r["vies"] = float((est - ofi).mean())
    return r


def avaliar(con):
    """Compara o nowcast v2, cada componente e a regra do dia anterior com o oficial (so le).

    Devolve {"nowcast", "f_ofi", "f_tryd_corr", "f_tryd", "f_cesta", "dia_anterior"} e imprime a tabela com o n
    de cada linha. Componentes e nowcast usam o historico causal; a regra do dia anterior usa todos os dias com
    oficial em que existe um oficial imediatamente anterior na mesma serie.
    """
    of = oficial(con)
    hist = {l["dia"]: l for l in historico(con)}
    dias_of = sorted(of)
    res = {k: _resumo([(hist[d][v], of[d][0]) for d in dias_of
                       if d in hist and hist[d].get(v) is not None]) for k, v in
           (("nowcast", "estimativa_mi"), ("f_ofi", "f_ofi"), ("f_tryd_corr", "f_tryd_corr"),
            ("f_tryd", "f_tryd"), ("f_cesta", "f_cesta"))}
    anterior = []
    for i in range(1, len(dias_of)):
        anterior.append((of[dias_of[i - 1]][0], of[dias_of[i]][0]))
    res["dia_anterior"] = _resumo(anterior)
    nomes = ["nowcast", "f_ofi", "f_tryd_corr", "f_tryd", "f_cesta"]
    print(f"{'estimativa':14}{'n':>4}{'sinal certo':>13}{'postos':>9}{'erro abs medio':>16}{'vies':>11}")
    for rotulo in nomes:
        s = res[rotulo]
        postos = "-" if s["postos"] is None else f"{s['postos']:+.2f}"
        erro = "-" if s["erro_abs_medio"] is None else f"{s['erro_abs_medio']:.1f}"
        vies = "-" if s["vies"] is None else f"{s['vies']:+.1f}"
        print(f"{rotulo:14}{s['n']:>4}{s['sinal_certo']:>8}/{s['n']:<4}{postos:>9}{erro:>16}{vies:>11}")
    s = res["dia_anterior"]
    print(f"{'dia anterior':14}{s['n']:>4}{s['sinal_certo']:>8}/{s['n']:<4}{'-':>9}{'-':>16}{'-':>11}")
    print("\nR$ mi; vies = media (estimativa - oficial). Amostra curta: leia o n de cada linha.")
    return res


def main():
    ap = argparse.ArgumentParser(description="Nowcast do fluxo do estrangeiro do dia (v2).")
    ap.add_argument("comando", choices=("atualizar", "avaliar"))
    ap.add_argument("--db", default=str(Path(__file__).parent / "dados" / "book.db"))
    a = ap.parse_args()
    if a.comando == "atualizar":
        print(f"{atualizar(a.db)} dia(s) gravado(s) em nowcast_fluxo")
        return
    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    try:
        avaliar(con)
    finally:
        con.close()


if __name__ == "__main__":
    main()
