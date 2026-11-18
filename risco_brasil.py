"""Risco Brasil idiossincratico: eps_BR do WDO$ contra DXY e cesta EM (ESPEC_V3 4.3).

Grade de 5 min dentro do pregao B3 (09:05-17:55, inclusive). Retornos:
  r_WDO  barras_1m quando existirem; senao macro_cotacoes.mid (17/11/2026 so tem macro)
  r_DXY  e r_EM  global_barras 5m (EM = media de USDMXN e USDZAR)
  eps_5m = r_WDO - (b_DXY*r_DXY + b_EM*r_EM), acumulado no dia em %

Betas por OLS (com intercepto) empilhando os retornos dos 20 pregoes ANTERIORES (walk-forward: para o dia T
so entram dias < T). O r2 gravado e o da regressao de treino. A inclinacao e DI1F33 - DI1F28 em bps, e a
variacao no dia e contra o primeiro valor do dia.

Leitura: |eps_acum| < 0,25% -> dolar global; > +0,5% com inclinacao abrindo -> estresse domestico;
< -0,5% -> alivio domestico; entre os dois -> misto.

Tabela risco_brasil_5m (ts_ms, eps_5m, eps_acum_pct, beta_dxy, beta_em, r2, incl_bps, incl_var_bps).

Uso:
  python risco_brasil.py dia 2026-11-17 [--db dados/book.db]
  python risco_brasil.py atualizar [--db dados/book.db]
  python risco_brasil.py avaliar [--db dados/book.db]
"""
import argparse
import sqlite3
from datetime import date, datetime, timedelta

import numpy as np

import banco
import calendario_b3

D = 86_400_000
INI, FIM = 9 * 60 + 5, 17 * 60 + 55
PASSO = 5
N_DIAS = 20
MIN_LINHAS = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS risco_brasil_5m (
    ts_ms INTEGER NOT NULL PRIMARY KEY,
    eps_5m REAL,
    eps_acum_pct REAL,
    beta_dxy REAL,
    beta_em REAL,
    r2 REAL,
    incl_bps REAL,
    incl_var_bps REAL);
"""


def _dia_idx(d):
    if isinstance(d, int):
        return d
    if isinstance(d, datetime):
        d = d.date()
    if isinstance(d, str):
        d = date.fromisoformat(d[:10])
    return (d - date(1970, 1, 1)).days


def _data(dia_idx):
    return date(1970, 1, 1) + timedelta(days=int(dia_idx))


def _grade():
    return list(range(INI, FIM + 1, PASSO))


def _bucket(minuto):
    """Minuto da grade de 5 min do pregao que contem `minuto`, ou None fora da sessao."""
    if minuto < INI or minuto > FIM:
        return None
    return INI + ((minuto - INI) // PASSO) * PASSO


def _serie_5m_b3(con, ativo, dia):
    """{minuto_grade: ultimo preco} do ativo no dia: barras_1m.close, senao macro_cotacoes.mid."""
    lo, hi = dia * D, (dia + 1) * D
    try:
        rows = con.execute("SELECT ts_ms, close FROM barras_1m WHERE symbol=? AND ts_ms>=? AND ts_ms<? "
                           "AND close IS NOT NULL ORDER BY ts_ms", (ativo, lo, hi)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    if not rows:
        try:
            rows = con.execute("SELECT ts_ms, mid FROM macro_cotacoes WHERE symbol=? AND ts_ms>=? AND ts_ms<? "
                               "AND mid IS NOT NULL ORDER BY ts_ms", (ativo, lo, hi)).fetchall()
        except sqlite3.OperationalError:
            rows = []
    out = {}
    for ts, preco in rows:
        b = _bucket((int(ts) % D) // 60_000)
        if b is not None and preco is not None and preco > 0:
            out[b] = float(preco)
    return out


def _serie_5m_global(con, ativo, dia):
    """{minuto_grade: close} das global_barras 5m, so nos minutos alinhados a grade 09:05+5k."""
    lo, hi = dia * D, (dia + 1) * D
    try:
        rows = con.execute("SELECT ts_ms, close FROM global_barras WHERE ativo=? AND intervalo='5m' "
                           "AND ts_ms>=? AND ts_ms<? AND close IS NOT NULL ORDER BY ts_ms",
                           (ativo, lo, hi)).fetchall()
    except sqlite3.OperationalError:
        return {}
    out = {}
    for ts, preco in rows:
        m = (int(ts) % D) // 60_000
        if INI <= m <= FIM and (m - INI) % PASSO == 0 and preco is not None and preco > 0:
            out[m] = float(preco)
    return out


def _retornos(precos):
    """{minuto final: log-retorno} apenas entre buckets consecutivos de 5 min."""
    out = {}
    itens = sorted(precos.items())
    for (b0, p0), (b1, p1) in zip(itens, itens[1:]):
        if b1 - b0 == PASSO and p0 > 0 and p1 > 0:
            out[b1] = float(np.log(p1 / p0))
    return out


def _em_do_dia(con, dia):
    """{minuto: retorno da cesta EM} = media dos retornos de USDMXN e USDZAR disponiveis."""
    mx = _retornos(_serie_5m_global(con, "USDMXN", dia))
    za = _retornos(_serie_5m_global(con, "USDZAR", dia))
    out = {}
    for b in sorted(set(mx) | set(za)):
        vals = [v for v in (mx.get(b), za.get(b)) if v is not None]
        if vals:
            out[b] = float(np.mean(vals))
    return out


def _dataset_dia(con, dia):
    """[{minuto, ts_ms, r_wdo, r_dxy, r_em}] alinhado no minuto final do retorno."""
    wdo = _retornos(_serie_5m_b3(con, "WDO$", dia))
    dxy = _retornos(_serie_5m_global(con, "DXY", dia))
    em = _em_do_dia(con, dia)
    out = []
    for b in sorted(set(wdo) & set(dxy)):
        out.append({"minuto": b, "ts_ms": dia * D + b * 60_000, "r_wdo": wdo[b], "r_dxy": dxy[b],
                    "r_em": em.get(b)})
    return out


def _ols(rows):
    """OLS de r_WDO em [1, r_DXY, r_EM] -> {"b_dxy", "b_em", "r2", "n"} ou None."""
    usados = [r for r in rows if r["r_em"] is not None]
    if len(usados) < MIN_LINHAS:
        return None
    y = np.array([r["r_wdo"] for r in usados], dtype=float)
    X = np.column_stack([np.ones(len(usados)), [r["r_dxy"] for r in usados], [r["r_em"] for r in usados]])
    if np.linalg.matrix_rank(X) < 3:
        return None
    coef = np.linalg.lstsq(X, y, rcond=None)[0]
    resid = y - X @ coef
    sst = float(((y - y.mean()) ** 2).sum())
    r2 = 1 - float((resid ** 2).sum()) / sst if sst > 0 else None
    return {"b_dxy": float(coef[1]), "b_em": float(coef[2]), "r2": r2, "n": len(usados)}


def _treino(con, dia, n_dias=N_DIAS):
    """Empilha os retornos dos `n_dias` pregoes anteriores a `dia` (walk-forward)."""
    rows = []
    d = _data(dia)
    for _ in range(int(n_dias)):
        d = calendario_b3.pregao_anterior(d)
        rows.extend(_dataset_dia(con, _dia_idx(d)))
    return rows


def _inclinacao_dia(con, dia):
    """{minuto: (DI1F33 - DI1F28) em bps} quando as duas series existem."""
    a = _serie_5m_b3(con, "DI1F33", dia)
    b = _serie_5m_b3(con, "DI1F28", dia)
    return {m: (a[m] - b[m]) * 100.0 for m in sorted(set(a) & set(b))}


def rotulo(eps_acum_pct, incl_var_bps):
    """Leitura do dia conforme ESPEC 4.3."""
    if eps_acum_pct is None:
        return None
    if abs(eps_acum_pct) < 0.25:
        return "dolar global"
    if eps_acum_pct > 0.5 and (incl_var_bps or 0) > 0:
        return "estresse domestico"
    if eps_acum_pct < -0.5:
        return "alivio domestico"
    return "misto"


def calcular_dia(con, dia):
    """Linhas de risco_brasil_5m do dia (somente leitura). [] sem betas ou sem dado do WDO."""
    dia = _dia_idx(dia)
    modelo = _ols(_treino(con, dia))
    if not modelo:
        return []
    incl = _inclinacao_dia(con, dia)
    primeiro = next(iter(incl.values()), None)
    acum, out = 0.0, []
    for r in _dataset_dia(con, dia):
        r_em = r["r_em"] if r["r_em"] is not None else 0.0
        eps = r["r_wdo"] - (modelo["b_dxy"] * r["r_dxy"] + modelo["b_em"] * r_em)
        acum += eps
        inc = incl.get(r["minuto"])
        out.append({
            "ts_ms": int(r["ts_ms"]), "eps_5m": float(eps), "eps_acum_pct": float(acum * 100.0),
            "beta_dxy": modelo["b_dxy"], "beta_em": modelo["b_em"], "r2": modelo["r2"],
            "incl_bps": inc, "incl_var_bps": (inc - primeiro) if (inc is not None and primeiro is not None) else None,
        })
    return out


def atualizar(db_path, dias=10):
    """Recalcula os ultimos `dias` pregoes com dado de WDO e grava em risco_brasil_5m. -> n de dias."""
    ro = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    try:
        dias_wdo = [r[0] for r in ro.execute(
            "SELECT DISTINCT ts_ms / ? FROM (SELECT ts_ms FROM barras_1m WHERE symbol='WDO$' "
            "UNION ALL SELECT ts_ms FROM macro_cotacoes WHERE symbol='WDO$') ORDER BY 1 DESC LIMIT ?",
            (D, int(dias)))]
        linhas = []
        for dia in sorted(dias_wdo):
            linhas.extend(calcular_dia(ro, int(dia)))
    finally:
        ro.close()
    if not linhas:
        return 0
    rw = banco.abrir(db_path, SCHEMA)
    try:
        rw.executemany("INSERT OR REPLACE INTO risco_brasil_5m VALUES (?,?,?,?,?,?,?,?)",
                       [(l["ts_ms"], l["eps_5m"], l["eps_acum_pct"], l["beta_dxy"], l["beta_em"],
                         l["r2"], l["incl_bps"], l["incl_var_bps"]) for l in linhas])
        rw.commit()
    finally:
        rw.close()
    return len(dias_wdo)


def avaliar(con, n_dias=60):
    """R2 medio da regressao WDO ~ DXY + EM nos pregoes com dado; imprime. -> dict."""
    dias = [int(r[0]) for r in con.execute(
        "SELECT DISTINCT ts_ms / ? FROM (SELECT ts_ms FROM barras_1m WHERE symbol='WDO$' "
        "UNION ALL SELECT ts_ms FROM macro_cotacoes WHERE symbol='WDO$') ORDER BY 1 DESC LIMIT ?",
        (D, int(n_dias)))]
    r2s, usados = [], 0
    for dia in sorted(dias):
        m = _ols(_treino(con, dia))
        if m and m["r2"] is not None:
            r2s.append(m["r2"])
            usados += 1
    media = float(np.mean(r2s)) if r2s else None
    print(f"R2 medio WDO ~ DXY + EM: {media if media is None else f'{media:.4f}'} "
          f"({usados} pregoes com dado, janela de {N_DIAS} pregoes anteriores)")
    return {"r2_medio": media, "pregoes": usados}


def main():
    ap = argparse.ArgumentParser(description="Risco Brasil idiossincratico (eps_BR em 5 min)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("dia")
    d.add_argument("data")
    d.add_argument("--db", default="dados/book.db")
    a = sub.add_parser("atualizar")
    a.add_argument("--db", default="dados/book.db")
    a.add_argument("--dias", type=int, default=10)
    e = sub.add_parser("avaliar")
    e.add_argument("--db", default="dados/book.db")
    args = ap.parse_args()
    if args.cmd == "atualizar":
        print(f"{atualizar(args.db, args.dias)} pregao(oes) recalculado(s)")
        return
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=30)
    try:
        if args.cmd == "avaliar":
            avaliar(con)
            return
        linhas = calcular_dia(con, args.data)
    finally:
        con.close()
    if not linhas:
        print("sem betas/dado suficientes")
        return
    u = linhas[-1]
    print(f"{args.data}: {len(linhas)} barras de 5 min")
    print(f"  eps acumulado: {u['eps_acum_pct']:+.3f}%  betas DXY={u['beta_dxy']:.3f} "
          f"EM={u['beta_em']:.3f}  R2={u['r2']:.4f}  inclinacao={u['incl_bps']} bps "
          f"var={u['incl_var_bps']} bps")
    print(f"  leitura: {rotulo(u['eps_acum_pct'], u['incl_var_bps'])}")


if __name__ == "__main__":
    main()
