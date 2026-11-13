"""Correlacoes do dia, referencias historicas e decomposicao do WIN (tabelas correl_diaria e fatores_dia).

Regras da ESPEC_DASHBOARD.md (secoes 4 e 6.3/6.4), iguais as do estudo_correlacoes.py:
  - retorno de preco = log-retorno; de taxa (DI1* e DAP*) = diferenca em pontos-base. Nunca entre dias.
  - grade de 1 min: ultimo preco do minuto; minuto sem dado repete a ultima cotacao por ate 5 minutos.
  - sessao: futuros 09:05-17:55; acoes e ETFs 10:05-16:55. Cada serie cobre a sessao inteira.
  - par so com n >= 60 retornos em comum, com IC de 95% por Fisher e beta = cov(ry, rx) / var(rx).
  - referencia: correlacao do pregao inteiro nos 60 pregoes anteriores (minimo 15). z = (hoje - media) / desvio.
  - fatores do WIN: regressao de r_WIN em [r_WDO, d_DI29, r_SPX_USD] nos 20 pregoes anteriores.
  - SPX_USD = IVVB11 / WDO$ e NDX_USD = NASD11 / WDO$, minuto a minuto.

Banco: ts_ms = hora de Brasilia codificada como epoch UTC; dia = ts_ms // 86_400_000; minuto do dia =
(ts_ms % 86_400_000) // 60_000. Datas em texto 'AAAA-MM-DD'.

Uso:
  python correlacoes.py atualizar [--db dados/book.db] [--dias 90]
  python correlacoes.py dia AAAA-MM-DD [--db dados/book.db]
"""
import argparse
import sqlite3
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

import banco

D = 86_400_000
SESSAO_FUTUROS = (9 * 60 + 5, 17 * 60 + 55)    # minutos do dia, inclusive
SESSAO_ACOES = (10 * 60 + 5, 16 * 60 + 55)
SINTETICOS = {"SPX_USD": ("IVVB11", "WDO$"), "NDX_USD": ("NASD11", "WDO$")}   # numerador / denominador
PARES_WIN = ["WDO$", "DI1F28", "DI1F29", "DI1F33", "SPX_USD", "NDX_USD", "BEWZ39",
             "VALE3", "PETR4", "PRIO3", "ITUB4", "BBAS3", "SMAL11", "GOLD11"]
PARES_WDO = ["DI1F29", "SPX_USD", "GOLD11", "VALE3", "PETR4"]
MATRIZ = ["WIN$", "WDO$", "DI1F29", "SPX_USD", "VALE3", "PETR4", "ITUB4", "BBAS3", "SMAL11", "GOLD11"]
PRINCIPAIS = [("WIN$", "WDO$"), ("WIN$", "DI1F29"), ("WDO$", "DI1F29"), ("WIN$", "SPX_USD")]
FATORES_WIN = ["WDO$", "DI1F29", "SPX_USD"]
N_MIN = 60
NOMES = {
    "WIN$": "WIN", "WDO$": "Dólar", "IND$": "Índice", "BIT$": "Bitcoin",
    "DI1F27": "DI jan/27", "DI1F28": "DI jan/28", "DI1F29": "DI jan/29", "DI1F33": "DI jan/33",
    "SPX_USD": "S&P 500 em dólar", "NDX_USD": "Nasdaq em dólar", "BEWZ39": "EWZ (BDR)",
    "VALE3": "Vale", "PETR4": "Petrobras", "PRIO3": "PRIO", "ITUB4": "Itaú",
    "BBAS3": "Banco do Brasil", "SMAL11": "Small caps", "GOLD11": "Ouro",
    "IVVB11": "IVVB11", "NASD11": "NASD11",
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS correl_diaria (        -- correlacao do pregao inteiro, retornos de 1 min
    data TEXT NOT NULL, y TEXT NOT NULL, x TEXT NOT NULL,
    corr REAL, beta REAL, n INTEGER, calculado_em TEXT,
    PRIMARY KEY (data, y, x));
CREATE TABLE IF NOT EXISTS fatores_dia (          -- WIN ~ fatores, ajustado nos 20 pregoes ANTERIORES a `data`
    data TEXT NOT NULL, alvo TEXT NOT NULL, fator TEXT NOT NULL,
    beta REAL, r2 REAL, n INTEGER,
    PRIMARY KEY (data, alvo, fator));
"""


def _dia(data):
    """Indice do dia (ts_ms // 86400000) a partir de 'AAAA-MM-DD', de datetime.date ou do proprio indice."""
    if isinstance(data, (int, np.integer)):
        return int(data)
    if isinstance(data, datetime):
        data = data.date()
    if isinstance(data, date):
        return (data - date(1970, 1, 1)).days
    return (pd.Timestamp(str(data)).date() - date(1970, 1, 1)).days


def _data(dia):
    """Texto 'AAAA-MM-DD' do indice do dia."""
    return (date(1970, 1, 1) + timedelta(days=int(dia))).isoformat()


def eh_taxa(simbolo):
    """True para taxa de juros (DI1* e DAP*), cujo retorno e a diferenca em pontos-base."""
    return simbolo.startswith(("DI1", "DAP"))


def sessao(simbolo):
    """(inicio, fim) em minutos do dia, inclusive: futuros 09:05-17:55; acoes, ETFs e sinteticos 10:05-16:55."""
    if simbolo in ("WIN$", "WDO$", "IND$", "BIT$") or simbolo.startswith(("DI1", "DAP")):
        return SESSAO_FUTUROS
    return SESSAO_ACOES


def _serie_bruta(con, simbolo, dia, ao_vivo, h0, h1):
    """Ultimo preco de cada minuto da sessao, sem reindexar nem preencher (indice = minuto do dia)."""
    if not ao_vivo:  # historico: barras de 1 min quando o simbolo existe nelas naquele dia
        lo, hi = dia * D, (dia + 1) * D
        df = pd.read_sql("SELECT ts_ms, close FROM barras_1m WHERE symbol=? AND ts_ms>=? AND ts_ms<? ORDER BY ts_ms",
                         con, params=(simbolo, lo, hi))
        if not df.empty:
            df = df[(df.ts_ms % D) // 60_000 >= h0]
            df = df[(df.ts_ms % D) // 60_000 <= h1]
            df = df.dropna(subset=["close"])
            return df.assign(m=(df.ts_ms % D) // 60_000).groupby("m").close.last()
    lo, hi = dia * D + h0 * 60_000, dia * D + (h1 + 1) * 60_000
    if simbolo == "WIN$":
        q = ("SELECT ts_ms, mid FROM snapshots WHERE symbol='WIN$' AND spread>0 AND ts_ms>=? AND ts_ms<? "
             "ORDER BY ts_ms")
        df = pd.read_sql(q, con, params=(lo, hi))
    else:
        q = "SELECT ts_ms, mid FROM macro_cotacoes WHERE symbol=? AND ts_ms>=? AND ts_ms<? ORDER BY ts_ms"
        df = pd.read_sql(q, con, params=(simbolo, lo, hi))
    if df.empty:
        return pd.Series(dtype=float)
    df = df.dropna(subset=["mid"])
    return df.assign(m=(df.ts_ms % D) // 60_000).groupby("m").mid.last()


def serie_1m(con, simbolo, dia, ao_vivo=True):
    """Serie de 1 min de um simbolo no dia, com indice = minuto do dia cobrindo a sessao inteira.

    Sem dado: Series vazia. Minuto sem cotacao repete a ultima por ate 5 minutos (ffill limit=5).
    ao_vivo=True: WIN$ vem de snapshots.mid (spread > 0) e os demais de macro_cotacoes.mid.
    ao_vivo=False: barras_1m.close quando o simbolo tiver barras naquele dia; senao, como ao vivo.
    Sinteticos (SPX_USD, NDX_USD): numerador / denominador, cada um com sua serie_1m."""
    dia = _dia(dia)
    h0, h1 = sessao(simbolo)
    if simbolo in SINTETICOS:
        num, den = SINTETICOS[simbolo]
        a = serie_1m(con, num, dia, ao_vivo)
        b = serie_1m(con, den, dia, ao_vivo)
        if a.empty or b.empty:
            return pd.Series(dtype=float)
        s = (a / b).replace([np.inf, -np.inf], np.nan)
        return s.reindex(range(h0, h1 + 1)).ffill(limit=5)
    s = _serie_bruta(con, simbolo, dia, ao_vivo, h0, h1)
    if s.empty:
        return pd.Series(dtype=float)
    return s.reindex(range(h0, h1 + 1)).ffill(limit=5)


def _series_do_periodo(con, simbolo, dias):
    """{dia: serie_1m(ao_vivo=False)} de varios dias, lendo as barras do periodo de uma vez (mais rapido que dia a dia)."""
    if simbolo in SINTETICOS:
        num, den = SINTETICOS[simbolo]
        a, b = _series_do_periodo(con, num, dias), _series_do_periodo(con, den, dias)
        h0, h1 = sessao(simbolo)
        out = {}
        for d in dias:
            if d in a and d in b and not a[d].empty and not b[d].empty:
                s = (a[d] / b[d]).replace([np.inf, -np.inf], np.nan)
                out[d] = s.reindex(range(h0, h1 + 1)).ffill(limit=5)
        for d in dias:
            out.setdefault(d, pd.Series(dtype=float))
        return out
    h0, h1 = sessao(simbolo)
    df = pd.read_sql("SELECT ts_ms, close FROM barras_1m WHERE symbol=? AND ts_ms>=? AND ts_ms<? ORDER BY ts_ms",
                     con, params=(simbolo, min(dias) * D, (max(dias) + 1) * D))
    out = {}
    if not df.empty:
        df = df.dropna(subset=["close"]).assign(dia=df.ts_ms // D, m=(df.ts_ms % D) // 60_000)
        for d, g in df.groupby("dia"):
            g = g[(g.m >= h0) & (g.m <= h1)]
            out[int(d)] = g.groupby("m").close.last().reindex(range(h0, h1 + 1)).ffill(limit=5)
    for d in dias:  # dia sem barras: mesma fonte do ao vivo
        if d not in out:
            out[d] = serie_1m(con, simbolo, d, ao_vivo=True)
    return out


def retornos(serie, simbolo):
    """Retornos de 1 min: log-retorno do preco ou diferenca da taxa em pontos-base (DI1* e DAP*).

    So entre minutos consecutivos com dado: um buraco de mais de 5 minutos (ja NaN na serie) nao gera retorno."""
    if serie is None or len(serie) < 2:
        return pd.Series(dtype=float)
    if eh_taxa(simbolo):
        r = serie.diff() * 100
    else:
        r = np.log(serie.where(serie > 0)).diff()
    idx = serie.index.to_numpy()
    consecutivo = np.concatenate(([False], np.diff(idx) == 1))
    r = r[consecutivo]
    return r[np.isfinite(r)]


def par(ry, rx):
    """Correlacao, IC de 95% por Fisher, beta e n de dois retornos alinhados pelo minuto.

    Com n < 60 os campos numericos sao None (o n continua). Se var(rx) == 0, os numericos sao None."""
    d = pd.concat([ry, rx], axis=1, join="inner").dropna()
    n = int(len(d))
    vazio = {"corr": None, "n": n, "ic_lo": None, "ic_hi": None, "beta": None}
    if n < N_MIN:
        return vazio
    a, b = d.iloc[:, 0].to_numpy(dtype=float), d.iloc[:, 1].to_numpy(dtype=float)
    var_b = float(np.var(b, ddof=1))
    if not np.isfinite(var_b) or var_b == 0:
        return vazio
    var_a = float(np.var(a, ddof=1))
    cov = float(np.cov(a, b, ddof=1)[0, 1])
    corr = cov / np.sqrt(var_a * var_b) if var_a > 0 else None
    if corr is None or not np.isfinite(corr):
        return vazio
    r = float(np.clip(corr, -0.999999, 0.999999))
    z = np.arctanh(r)
    erro = 1.96 / np.sqrt(n - 3)
    return {"corr": float(r), "n": n, "ic_lo": float(np.tanh(z - erro)), "ic_hi": float(np.tanh(z + erro)),
            "beta": cov / var_b}


def _pares_do_dia():
    """Pares de correl_do_dia na ordem das listas, cada par uma vez."""
    out, vistos = [], set()
    for y, xs in (("WIN$", PARES_WIN), ("WDO$", PARES_WDO)):
        for x in xs:
            chave = frozenset((y, x))
            if chave not in vistos:
                vistos.add(chave)
                out.append((y, x))
    for i, y in enumerate(MATRIZ):
        for x in MATRIZ[i + 1:]:
            chave = frozenset((y, x))
            if chave not in vistos:
                vistos.add(chave)
                out.append((y, x))
    return out


def correl_do_dia(con, data, ao_vivo=False):
    """Lista de {"data", "y", "x", "corr", "n", "ic_lo", "ic_hi", "beta"} do dia (PARES_WIN, PARES_WDO e MATRIZ)."""
    dia = _dia(data)
    iso = _data(dia)
    cache, rets = {}, {}

    def serie(s):
        if s not in cache:
            cache[s] = serie_1m(con, s, dia, ao_vivo)
        return cache[s]

    def ret(s):
        if s not in rets:
            rets[s] = retornos(serie(s), s)
        return rets[s]

    return [{"data": iso, "y": y, "x": x, **par(ret(y), ret(x))} for y, x in _pares_do_dia()]


def referencia(con, y, x, antes_de, n_dias=60):
    """Normal do par nos `n_dias` pregoes anteriores: {"media", "dp", "p10", "p90", "dias"} ou None (menos de 15).

    Le correl_diaria e aceita o par nas duas ordens (y, x) ou (x, y)."""
    iso = _data(_dia(antes_de))
    corrs = [r[0] for r in con.execute(
        "SELECT corr FROM correl_diaria WHERE ((y=? AND x=?) OR (y=? AND x=?)) AND data<? AND corr IS NOT NULL "
        "ORDER BY data DESC LIMIT ?", (y, x, x, y, iso, int(n_dias)))]
    if len(corrs) < 15:
        return None
    a = np.asarray(corrs, dtype=float)
    return {"media": float(a.mean()), "dp": float(a.std(ddof=1)), "p10": float(np.percentile(a, 10)),
            "p90": float(np.percentile(a, 90)), "dias": int(len(a))}


def z(corr, ref):
    """(corr - media) / dp da referencia; None sem referencia, sem correlacao ou com desvio zero."""
    if corr is None or ref is None or not ref.get("dp"):
        return None
    return (corr - ref["media"]) / ref["dp"]


def movel(ry, rx, janela=60):
    """Correlacao movel de `janela` minutos (minimo de 50 pares), alinhada pelo minuto."""
    d = pd.concat([ry, rx], axis=1, join="inner").dropna()
    if len(d) < 2:
        return pd.Series(dtype=float)
    return d.iloc[:, 0].rolling(janela, min_periods=50).corr(d.iloc[:, 1])


def ajustar_fatores(con, data, alvo="WIN$", fatores=FATORES_WIN, n_dias=20):
    """Regressao por minimos quadrados (com intercepto) dos retornos de 1 min do alvo nos fatores.

    Empilha os `n_dias` pregoes anteriores a `data` (pregao = dia com barras de 1 min do alvo, series ao vivo=False).
    Linhas com algum NaN saem; fator sem dado em nenhum dia sai do modelo. Menos de 2.000 linhas: None.
    -> {"betas": {fator: beta}, "r2", "n"}."""
    dia = _dia(data)
    pregoes = [r[0] for r in con.execute(
        "SELECT DISTINCT ts_ms / ? FROM barras_1m WHERE symbol=? AND ts_ms<? ORDER BY 1 DESC LIMIT ?",
        (D, alvo, dia * D, int(n_dias)))]
    blocos = []
    if not pregoes:
        return None
    series = {s: _series_do_periodo(con, s, pregoes) for s in [alvo] + list(fatores)}
    for d in pregoes:
        cols = {alvo: retornos(series[alvo][d], alvo)}
        for f in fatores:
            cols[f] = retornos(series[f][d], f)
        blocos.append(pd.DataFrame(cols))
    if not blocos:
        return None
    df = pd.concat(blocos, ignore_index=True)
    df = df.drop(columns=[c for c in fatores if df[c].isna().all()])
    usados = [f for f in fatores if f in df.columns]
    df = df[[alvo] + usados].dropna()
    if len(df) < 2000:
        return None
    y = df[alvo].to_numpy(dtype=float)
    a = np.column_stack([np.ones(len(df))] + [df[f].to_numpy(dtype=float) for f in usados])
    coef = np.linalg.lstsq(a, y, rcond=None)[0]
    resid = y - a @ coef
    sst = float(((y - y.mean()) ** 2).sum())
    r2 = 1 - float((resid ** 2).sum()) / sst if sst > 0 else None
    return {"betas": {f: float(c) for f, c in zip(usados, coef[1:])}, "r2": r2, "n": int(len(df))}


def decompor(rets, betas, preco_ini):
    """Decompoe o retorno do WIN na janela: contribuicao de cada fator (beta x soma) e a parte propria.

    rets = {simbolo: Series de retornos} incluindo o alvo "WIN$". A contribuicao e calculada em log e convertida
    para % e pontos (x preco_ini / 100) com a mesma regra para todos. r2_hoje: R2 dos retornos de 1 min do WIN
    preditos por sum(beta*x), sem intercepto, so nos minutos com todos os dados (menos de 60: None)."""
    alvo = "WIN$"
    r_win = rets.get(alvo)
    total_log = float(r_win.sum()) if r_win is not None else 0.0
    fatores, soma = [], 0.0
    for f, b in betas.items():
        s = rets.get(f)
        log = float(s.sum()) if s is not None else 0.0
        contrib = b * log
        soma += contrib
        pct = contrib * 100
        fatores.append({"fator": f, "nome": NOMES.get(f, f), "pct": pct, "pts": pct * preco_ini / 100})
    total_pct = total_log * 100
    propria_pct = (total_log - soma) * 100

    n, r2 = 0, None
    if r_win is not None:
        cols = {alvo: r_win}
        for f in betas:
            if f in rets:
                cols[f] = rets[f]
        d = pd.concat(cols, axis=1, join="inner").dropna()
        n = int(len(d))
        if n >= N_MIN:
            y = d[alvo]
            pred = sum(betas[f] * d[f] for f in betas if f in d.columns)
            sst = float(((y - y.mean()) ** 2).sum())
            r2 = 1 - float(((y - pred) ** 2).sum()) / sst if sst > 0 else None
    return {"total_pct": total_pct, "total_pts": total_pct * preco_ini / 100,
            "fatores": fatores, "propria_pct": propria_pct, "propria_pts": propria_pct * preco_ini / 100,
            "r2_hoje": r2, "n": n}


def atualizar(db_path, dias=90):
    """Preenche correl_diaria e fatores_dia dos ultimos `dias` dias corridos. Devolve quantos pregoes gravou.

    Pregoes = dias com barras de 1 min do WIN$. Recalcula so os dias ainda sem linhas em correl_diaria, sempre
    refazendo os 2 pregoes mais recentes; fatores_dia so entra em pregao que ainda nao o tem. Transacao curta por
    pregao, porque o banco e compartilhado com os coletores."""
    corte = _dia(date.today()) - int(dias)
    db = banco.abrir(db_path, SCHEMA)
    try:
        pregoes = [r[0] for r in db.execute(
            "SELECT DISTINCT ts_ms / ? FROM barras_1m WHERE symbol='WIN$' AND ts_ms>=? ORDER BY 1",
            (D, corte * D))]
        recentes = set(pregoes[-2:])
        gravados = set()
        for dia in pregoes:
            iso = _data(dia)
            tem = db.execute("SELECT 1 FROM correl_diaria WHERE data=? LIMIT 1", (iso,)).fetchone()
            if tem and dia not in recentes:
                continue
            linhas = correl_do_dia(db, dia, ao_vivo=False)
            agora = datetime.now().isoformat(timespec="seconds")
            with db:
                db.executemany("INSERT OR REPLACE INTO correl_diaria (data, y, x, corr, beta, n, calculado_em) "
                               "VALUES (?,?,?,?,?,?,?)",
                               [(l["data"], l["y"], l["x"], l["corr"], l["beta"], l["n"], agora) for l in linhas])
            gravados.add(dia)
        for dia in pregoes:
            iso = _data(dia)
            if db.execute("SELECT 1 FROM fatores_dia WHERE data=? LIMIT 1", (iso,)).fetchone():
                continue
            r = ajustar_fatores(db, iso)
            if not r:
                continue
            with db:
                db.executemany("INSERT OR REPLACE INTO fatores_dia (data, alvo, fator, beta, r2, n) VALUES (?,?,?,?,?,?)",
                               [(iso, "WIN$", f, b, r["r2"], r["n"]) for f, b in r["betas"].items()])
            gravados.add(dia)
        return len(gravados)
    finally:
        db.close()


def tabela(linhas):
    """Tabela de texto de correl_do_dia para a CLI."""
    cab = f"{'y':8s} {'x':18s} {'corr':>7s} {'n':>5s}  {'IC 95%':>17s}  {'beta':>10s}"
    out = [cab, "-" * len(cab)]
    for l in linhas:
        nome = NOMES.get(l["x"], l["x"])
        if l["corr"] is None:
            out.append(f"{l['y']:8s} {nome:18s} {'-':>7s} {l['n']:5d}  {'-':>17s}  {'-':>10s}")
        else:
            out.append(f"{l['y']:8s} {nome:18s} {l['corr']:+7.3f} {l['n']:5d}  "
                       f"{l['ic_lo']:+8.3f}..{l['ic_hi']:+7.3f}  {l['beta']:+10.4g}")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description="Correlacoes diarias dos retornos de 1 min.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("atualizar", help="grava correl_diaria e fatores_dia")
    a.add_argument("--db", default="dados/book.db")
    a.add_argument("--dias", type=int, default=90)
    d = sub.add_parser("dia", help="imprime as correlacoes do dia (somente leitura)")
    d.add_argument("data")
    d.add_argument("--db", default="dados/book.db")
    args = ap.parse_args()
    if args.cmd == "atualizar":
        n = atualizar(args.db, args.dias)
        print(f"correlacoes: {n} pregao(oes) gravado(s)")
        return
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=30)
    try:
        linhas = correl_do_dia(con, args.data, ao_vivo=False)
    finally:
        con.close()
    print(f"correlacoes de {linhas[0]['data']} (retornos de 1 min, pregoes anteriores nao usados aqui)")
    print(tabela(linhas))


if __name__ == "__main__":
    main()
