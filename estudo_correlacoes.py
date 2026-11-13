"""Quais correlacoes do banco tem sinal de verdade? Base da ESPEC_DASHBOARD.md (medido em 13/11/2026).

Estudo OFFLINE, somente leitura. Quatro partes:
  estabilidade  correlacao de retornos de 1 min WIN x WDO x DI1F29 por pregao (barras_1m, ~200 dias): media, desvio entre
                dias, por mes, R2 de WIN ~ WDO + DI e o ruido da correlacao movel de 30 min.
  leadlag       correlacao cruzada do WIN com os ativos do painel em grades de 5, 10, 30 e 60 s (snapshots x
                macro_cotacoes), defasagens -2..+2. k > 0: o ativo ANTECIPA o WIN.
  fluxo         cesta de agressao e agressao do WDO x retorno; livro do WIN (imbalance, delta, microprice) x retorno do
                mesmo intervalo e do seguinte; variacao da vol implicita x retorno do BOVA11.
  players       variacao do saldo de cada grupo de corretoras (tryd_saldo) x retorno do ativo, em 1 e 5 min, e o saldo
                final do grupo x retorno do dia; S&P e Nasdaq em reais x em dolar (IVVB11 / WDO$).

Regras iguais as da especificacao: log-retorno (DI: diferenca da taxa), sem retorno entre dias, minuto sem cotacao repete a
ultima por ate 5 min. Janela: futuros 09:05-17:55; partes com acoes 10:06-16:54.

Uso: python estudo_correlacoes.py [--db dados/book.db] [--parte estabilidade|leadlag|fluxo|players|todas] [--dias 8]
"""
import argparse
import sqlite3

import numpy as np
import pandas as pd

import players

D = 86_400_000
ATIVOS = ("WDO$", "IND$", "IVVB11", "PETR4", "VALE3", "ITUB4", "DI1F29", "BIT$")


def grade(df, col, passo_ms, d, h0=10.1, h1=16.9, como="last"):
    """Serie de `col` em baldes de passo_ms entre h0 e h1 (horas) do dia d; balde sem linha fica NaN."""
    t0, t1 = d * D + int(h0 * 3_600_000), d * D + int(h1 * 3_600_000)
    df = df[(df.ts_ms >= t0) & (df.ts_ms < t1)]
    return df.groupby((df.ts_ms - t0) // passo_ms)[col].agg(como).reindex(range((t1 - t0) // passo_ms))


def ler(con, sql, *p):
    return pd.read_sql(sql, con, params=p)


def win_mid(con, d):
    return ler(con, "SELECT ts_ms, mid FROM snapshots WHERE symbol='WIN$' AND spread>0 AND ts_ms>=? AND ts_ms<?", d * D, (d + 1) * D)


def macro(con, s, d):
    return ler(con, "SELECT ts_ms, mid FROM macro_cotacoes WHERE symbol=? AND ts_ms>=? AND ts_ms<?", s, d * D, (d + 1) * D)


def dias_com_livro(con, n):
    dias = [r[0] for r in con.execute("SELECT DISTINCT ts_ms / ? FROM regime_live ORDER BY 1 DESC LIMIT ?", (D, n))]
    return sorted(dias)


def estabilidade(con):
    def barras(s):
        df = ler(con, "SELECT ts_ms, close FROM barras_1m WHERE symbol=? ORDER BY ts_ms", s)
        df["dia"], df["m"] = df.ts_ms // D, (df.ts_ms % D) // 60_000
        return df[(df.m >= 9 * 60 + 5) & (df.m <= 17 * 60 + 55)].set_index(["dia", "m"]).close
    df = pd.concat({"win": barras("WIN$"), "wdo": barras("WDO$"), "di": barras("DI1F29")}, axis=1)
    df["di"] = df.groupby(level=0)["di"].ffill(limit=5)
    r = pd.DataFrame({"win": np.log(df.win).groupby(level=0).diff(), "wdo": np.log(df.wdo).groupby(level=0).diff(),
                      "di": df.di.groupby(level=0).diff()})
    por_dia = r.groupby(level=0).apply(lambda g: pd.Series({
        "n": g.win.notna().sum(), "win_wdo": g.win.corr(g.wdo), "win_di": g.win.corr(g.di), "wdo_di": g.wdo.corr(g.di)}))
    por_dia = por_dia[por_dia.n > 300]
    print(f"== estabilidade: {len(por_dia)} pregoes, retornos de 1 min")
    print(por_dia[["win_wdo", "win_di", "wdo_di"]].describe().round(3).to_string())
    por_dia["data"] = [pd.Timestamp(d * D, unit="ms").date().isoformat() for d in por_dia.index]
    print("ultimos 12 pregoes:\n" + por_dia.tail(12).set_index("data")[["win_wdo", "win_di", "wdo_di"]].round(2).to_string())
    print("por mes:\n" + por_dia.groupby(por_dia.data.str[:7])[["win_wdo", "win_di", "wdo_di"]].mean().round(2).to_string())
    movel = r.groupby(level=0, group_keys=False).apply(lambda g: g.win.rolling(30, min_periods=25).corr(g.wdo))
    print(f"correlacao movel de 30 min WIN x WDO: desvio dentro do dia {movel.groupby(level=0).std().mean():.3f} "
          f"(desvio entre dias da correlacao do pregao inteiro: {por_dia.win_wdo.std():.3f})")
    for x in ("wdo", "di"):
        ll = {k: r.groupby(level=0).apply(lambda g: g.win.corr(g[x].shift(k))).loc[por_dia.index].tail(40).mean()
              for k in range(-3, 4)}
        print(f"defasagens de 1 min WIN x {x} (k>0: {x} antecipa), 40 pregoes:", {k: round(float(v), 3) for k, v in ll.items()})

    def r2(g):
        g = g.dropna()
        if len(g) < 200:
            return np.nan
        a = np.column_stack([np.ones(len(g)), g.wdo, g.di])
        b = np.linalg.lstsq(a, g.win, rcond=None)[0]
        return 1 - ((g.win - a @ b) ** 2).sum() / ((g.win - g.win.mean()) ** 2).sum()
    rr = r.groupby(level=0).apply(r2).dropna()
    print(f"R2 de WIN ~ WDO + DI por pregao (dentro da amostra): media {rr.mean():.3f}, ultimos 10 {rr.tail(10).round(2).tolist()}")


def leadlag(con, dias):
    print(f"\n== leadlag: {len(dias)} pregoes (k>0: o ativo antecipa o WIN)")
    for passo in (5_000, 10_000, 30_000, 60_000):
        acc = {}
        for d in dias:
            rw = np.log(grade(win_mid(con, d), "mid", passo, d).ffill()).diff()
            for s in ATIVOS:
                x = grade(macro(con, s, d), "mid", passo, d).ffill()
                rx = x.diff() if s.startswith("DI1") else np.log(x).diff()
                for k in (-2, -1, 0, 1, 2):
                    acc.setdefault((s, k), []).append(rw.corr(rx.shift(k)))
        print(f"passo {passo // 1000:>2d} s      k=-2     -1      0     +1     +2")
        for s in ATIVOS:
            print(f"  {s:8s}", "  ".join(f"{np.nanmean(acc[(s, k)]):+.2f}" for k in (-2, -1, 0, 1, 2)))


def fluxo(con, dias):
    print(f"\n== fluxo: {len(dias)} pregoes")
    acc = {}
    for d in dias:
        a = ler(con, "SELECT ts_ms, fin_compra, fin_venda FROM agressao_1min WHERE symbol<>'WDO$' AND ts_ms>=? AND ts_ms<?", d * D, (d + 1) * D)
        a["liq"] = a.fin_compra.fillna(0) - a.fin_venda.fillna(0)
        cesta = grade(a, "liq", 60_000, d, como="sum").fillna(0)
        w = ler(con, "SELECT ts_ms, vol_compra, vol_venda FROM agressao_1min WHERE symbol='WDO$' AND ts_ms>=? AND ts_ms<?", d * D, (d + 1) * D)
        w["liq"] = w.vol_compra - w.vol_venda
        agr_wdo = grade(w, "liq", 60_000, d, como="sum").fillna(0)
        rw = np.log(grade(win_mid(con, d), "mid", 60_000, d).ffill()).diff()
        rwdo = np.log(grade(macro(con, "WDO$", d), "mid", 60_000, d).ffill()).diff()
        for k in (-1, 0, 1, 2):
            acc.setdefault(("cesta -> WIN, 1 min", k), []).append(rw.corr(cesta.shift(k)))
            acc.setdefault(("agressao WDO -> WDO, 1 min", k), []).append(rwdo.corr(agr_wdo.shift(k)))
        g5 = lambda s: s.groupby(np.arange(len(s)) // 5).sum()
        for k in (-1, 0, 1):
            acc.setdefault(("cesta -> WIN, 5 min", k), []).append(g5(rw).corr(g5(cesta).shift(k)))
    for (nome, k), v in acc.items():
        print(f"  {nome:28s} k={k:+d}: {np.nanmean(v):+.3f}  (min {np.nanmin(v):+.2f}, max {np.nanmax(v):+.2f})")
    print("livro do WIN x retorno do mesmo intervalo / do intervalo seguinte:")
    for passo in (10_000, 30_000, 60_000):
        acc = {}
        for d in dias:
            w = ler(con, "SELECT ts_ms, mid, imbalance_pond, delta, microprice FROM snapshots WHERE symbol='WIN$' AND spread>0 "
                         "AND ts_ms>=? AND ts_ms<?", d * D, (d + 1) * D)
            mid = grade(w, "mid", passo, d).ffill()
            r = mid.diff()
            for nome, f in (("imbalance", grade(w, "imbalance_pond", passo, d, como="mean")),
                            ("delta", grade(w, "delta", passo, d, como="sum")),
                            ("microprice-mid", grade(w, "microprice", passo, d) - mid)):
                acc.setdefault(nome, [[], []])
                acc[nome][0].append(r.corr(f))
                acc[nome][1].append(r.shift(-1).corr(f))
        print(f"  passo {passo // 1000:>2d} s:", "; ".join(f"{n} {np.nanmean(a):+.2f} / {np.nanmean(b):+.2f}" for n, (a, b) in acc.items()))
    print("opcoes do BOVA11, 5 min: variacao da IV ATM x retorno | variacao do RR25 x retorno")
    acc = {}
    for d in dias:
        o = ler(con, "SELECT ts_ms, vencimento, atm_iv, rr25, spot FROM opcoes_resumo WHERE subjacente='BOVA11' AND ts_ms>=? AND ts_ms<?", d * D, (d + 1) * D)
        for v, g in o.groupby("vencimento"):
            iv, rr = grade(g, "atm_iv", 300_000, d).ffill(), grade(g, "rr25", 300_000, d).ffill()
            sp = np.log(grade(g, "spot", 300_000, d).ffill())
            if iv.notna().sum() >= 40:
                acc.setdefault(v, [[], []])
                acc[v][0].append(iv.diff().corr(sp.diff()))
                acc[v][1].append(rr.diff().corr(sp.diff()))
    for v, (a, b) in sorted(acc.items()):
        print(f"  {v}: {np.nanmean(a):+.2f} | {np.nanmean(b):+.2f}  ({len(a)} pregoes)")


def por_grupo(con):
    dias = [r[0] for r in con.execute("SELECT DISTINCT ts_ms / ? FROM tryd_saldo ORDER BY 1", (D,))]
    print(f"\n== players: {len(dias)} pregoes com saldo do Tryd")

    def minuto(ts_ms, v, d, h0, h1):
        t0, n = d * D + int(h0 * 3_600_000), int((h1 - h0) * 60)
        s = pd.Series(v, index=(np.array(ts_ms) - t0) // 60_000)
        return s[(s.index >= 0) & (s.index < n)].groupby(level=0).last().reindex(range(n)).ffill()

    def preco(d, ativo, h0, h1):
        df = win_mid(con, d) if ativo.startswith("WIN") else macro(con, "WDO$" if ativo.startswith("WDO") else ativo, d)
        return None if df.empty else minuto(df.ts_ms.to_numpy(), df.mid.to_numpy(), d, h0, h1)

    acc, fim = {}, {}
    for d in dias:
        for ativo in players.ativos_do_dia(con, d):
            lo = d * D
            linhas = con.execute("SELECT ts_ms, brk, qtd, sagr, qtdc, qtdv FROM tryd_saldo WHERE ativo=? AND ts_ms>=? AND ts_ms<? "
                                 "ORDER BY ts_ms", (ativo, lo, lo + D)).fetchall()
            linhas = [r for r in linhas if r[0] - lo >= players.abertura_ms(ativo)]
            linhas = players.desde_o_zeramento(players.sem_tabela_de_ontem(con, ativo, lo, linhas))
            if not linhas:
                continue
            ts, series, _, _ = players.saldo_por_grupo(linhas)
            futuro = bool(players._FUTURO.match(ativo))
            h0, h1 = (9.25, 17.9) if futuro else (10.25, 16.9)
            p = preco(d, ativo, h0, h1)
            if p is None or p.notna().sum() < 200:
                continue
            r = np.log(p).diff()
            chave = ativo[:3] if futuro else "acoes"
            g5 = lambda x: x.groupby(np.arange(len(x)) // 5).sum()
            for g in series:
                s = minuto(ts, series[g]["saldo"], d, h0, h1)
                ds = s.diff()
                if ds.abs().sum() == 0:
                    continue
                for k in (-1, 0, 1):
                    acc.setdefault((chave, g, "1m", k), []).append(r.corr(ds.shift(k)))
                    acc.setdefault((chave, g, "5m", k), []).append(g5(r).corr(g5(ds).shift(k)))
                acc.setdefault((chave, g, "nivel", 0), []).append(s.corr(p))
                fim.setdefault((chave, g), []).append((s.iloc[-1], p.dropna().iloc[-1] / p.dropna().iloc[0] - 1))
    for chave in ("WIN", "WDO", "acoes"):
        print(f"{chave}: variacao do saldo do grupo x retorno (k>0: o fluxo antecipa o preco; k<0: vem depois)")
        print(f"  {'grupo':14s} 1 min k=-1    0   +1 | 5 min k=-1    0   +1 | nivel | saldo final x retorno do dia")
        for g in players.GRUPOS:
            if (chave, g, "1m", 0) not in acc:
                continue
            m = lambda t, k: np.nanmean(acc[(chave, g, t, k)])
            a = np.array(fim[(chave, g)])
            iguais = int((np.sign(a[:, 0]) == np.sign(a[:, 1])).sum())
            rho = pd.Series(a[:, 0]).rank().corr(pd.Series(a[:, 1]).rank())
            print(f"  {g:14s}      {m('1m', -1):+.2f} {m('1m', 0):+.2f} {m('1m', 1):+.2f} |      {m('5m', -1):+.2f} {m('5m', 0):+.2f} "
                  f"{m('5m', 1):+.2f} | {m('nivel', 0):+.2f} | sinal igual {iguais}/{len(a)}, postos {rho:+.2f}")
    print("S&P e Nasdaq em reais x em dolar (dividido pelo WDO$) x WIN, 1 min:")
    for d in dias[-6:]:
        w, f = preco(d, "WIN", 10.25, 16.9), preco(d, "WDO", 10.25, 16.9)
        if w is None or f is None:
            continue
        rw, saida = np.log(w).diff(), []
        for s in ("IVVB11", "NASD11"):
            x = preco(d, s, 10.25, 16.9)
            if x is not None:
                saida.append(f"{s} em reais {rw.corr(np.log(x).diff()):+.2f}, em dolar {rw.corr(np.log(x / f).diff()):+.2f}")
        print(f"  {pd.Timestamp(d * D, unit='ms').date()}: " + "; ".join(saida))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="dados/book.db")
    ap.add_argument("--parte", default="todas", choices=["estabilidade", "leadlag", "fluxo", "players", "todas"])
    ap.add_argument("--dias", type=int, default=8, help="pregoes recentes das partes leadlag e fluxo")
    args = ap.parse_args()
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=30)
    dias = dias_com_livro(con, args.dias)
    if args.parte in ("estabilidade", "todas"):
        estabilidade(con)
    if args.parte in ("leadlag", "todas"):
        leadlag(con, dias)
    if args.parte in ("fluxo", "todas"):
        fluxo(con, dias)
    if args.parte in ("players", "todas"):
        por_grupo(con)


if __name__ == "__main__":
    main()
