"""Exporta fatias compactas do book.db em CSV para alimentar outro bot/assistente (ex.: Grok).

O book.db passa de 2 GB e nao e algo que se anexe a um chat; aqui saem so agregados de 1 minuto
e as tabelas pequenas, em dados/export_bot/. Somente leitura. Timestamps (coluna `hora`) sao a hora
de Brasilia (o banco guarda o relogio de Brasilia como epoch "UTC").

Uso: python exportar_para_bot.py [--desde 2026-09-24] [--saida dados/export_bot]
"""
import argparse
import sqlite3
from pathlib import Path

import pandas as pd

DB = Path(__file__).parent / "dados" / "book.db"


def ms(data):
    return int(pd.Timestamp(f"{data} 00:00", tz="UTC").timestamp() * 1000)


def hora(df):
    df.insert(0, "hora", pd.to_datetime(df.pop("ts_ms"), unit="ms").dt.strftime("%Y-%m-%d %H:%M:%S"))
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--desde", default="2026-09-24")
    ap.add_argument("--saida", default=str(Path(__file__).parent / "dados" / "export_bot"))
    args = ap.parse_args()
    out = Path(args.saida)
    out.mkdir(parents=True, exist_ok=True)
    a = ms(args.desde)
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    q = lambda sql, *p: pd.read_sql(sql, con, params=p)

    def salvar(nome, df):
        df.to_csv(out / nome, index=False, float_format="%.6g")
        print(f"{nome:28} {len(df):>8,} linhas  {(out / nome).stat().st_size / 2**20:6.2f} MB")

    # barras de 1 min: B3 (MT5) desde sempre que houver; IB:* so o que existe (acaba em 28/09, VIX 30/09)
    b = q("SELECT symbol, ts_ms, open, high, low, close, volume FROM barras_1m WHERE ts_ms >= ? ORDER BY symbol, ts_ms", a)
    salvar("barras_1m.csv", hora(b))

    # livro + agressao agregados por minuto (WIN$ e BOVA11)
    s = q("SELECT symbol, ts_ms, mid, spread, imbalance_pond, imbalance_near, microprice, vol_compra_agr, "
          "vol_venda_agr, delta, saldo_acum FROM snapshots WHERE ts_ms >= ? AND spread > 0 ORDER BY symbol, ts_ms", a)
    s["min"] = (s["ts_ms"] // 60000) * 60000
    g = s.groupby(["symbol", "min"]).agg(
        mid=("mid", "last"), spread=("spread", "mean"), imbalance_pond=("imbalance_pond", "mean"),
        imbalance_near=("imbalance_near", "mean"), vol_compra_agr=("vol_compra_agr", "sum"),
        vol_venda_agr=("vol_venda_agr", "sum"), delta=("delta", "sum"), saldo_acum=("saldo_acum", "last")
    ).reset_index().rename(columns={"min": "ts_ms"})
    salvar("livro_agressao_1min.csv", hora(g))

    # cotacoes macro/juros/acoes: ultimo mid por minuto
    m = q("SELECT symbol, ts_ms, mid FROM macro_cotacoes WHERE ts_ms >= ? ORDER BY symbol, ts_ms", a)
    m["min"] = (m["ts_ms"] // 60000) * 60000
    gm = m.groupby(["symbol", "min"]).agg(mid=("mid", "last")).reset_index().rename(columns={"min": "ts_ms"})
    salvar("macro_1min.csv", hora(gm))

    # opcoes (coletor MT5): resumo por vencimento, 1 por minuto
    o = q("SELECT ts_ms, vencimento, spot, forward, atm_iv, iv_call25, iv_put25, rr25, bf25, "
          "neg_call_compra, neg_call_venda, neg_put_compra, neg_put_venda FROM opcoes_resumo WHERE ts_ms >= ? "
          "ORDER BY vencimento, ts_ms", a)
    o["min"] = (o["ts_ms"] // 60000) * 60000
    ag = {c: "last" for c in ("spot", "forward", "atm_iv", "iv_call25", "iv_put25", "rr25", "bf25")}
    ag.update({c: "sum" for c in ("neg_call_compra", "neg_call_venda", "neg_put_compra", "neg_put_venda")})
    go = o.groupby(["vencimento", "min"]).agg(ag).reset_index().rename(columns={"min": "ts_ms"})
    salvar("opcoes_resumo_1min.csv", hora(go))

    # regime (1 linha por minuto, ja e pequeno)
    r = q("SELECT * FROM regime_live WHERE ts_ms >= ? ORDER BY ts_ms", a).drop(columns=["sessao"])
    salvar("regime_1min.csv", hora(r))

    existe = lambda t: con.execute("SELECT 1 FROM sqlite_master WHERE name=?", (t,)).fetchone() is not None
    # fluxo de investidores da B3 (D+2), por tipo: compras, vendas e saldo diarios (R$ mil)
    if existe("fluxo_diario"):
        salvar("fluxo_investidores_dia.csv", q("SELECT * FROM fluxo_diario ORDER BY data_ref, tipo"))
    # agressao por minuto da cesta de acoes e do WDO, e as variaveis diarias derivadas
    if existe("agressao_1min"):
        g = q("SELECT ts_ms, symbol, n, vol_compra, vol_venda, vol_neutro, fin_compra, fin_venda, maior_lote, preco "
              "FROM agressao_1min WHERE ts_ms >= ? ORDER BY symbol, ts_ms", a)
        salvar("agressao_1min.csv", hora(g))
    if existe("features_fluxo_dia"):
        salvar("features_fluxo_dia.csv", q("SELECT * FROM features_fluxo_dia ORDER BY data"))

    # posicao em aberto colada manualmente (coberto/travado/descoberto/titulares/lancadores)
    salvar("posicao_aberta_series.csv", q("SELECT * FROM opcoes_manual_series ORDER BY capturado_em, vencimento, strike"))
    salvar("posicao_aberta_resumo.csv", q("SELECT * FROM opcoes_manual_resumo ORDER BY capturado_em, vencimento"))
    con.close()
    print(f"\npronto em {out}")


if __name__ == "__main__":
    main()
