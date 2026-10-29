"""Posicao em aberto das opcoes de dolar (DOL e WDO, sobre o dolar comercial) direto da B3 -> SQLite.

Mesmo portal e mesma paginacao do posicoes_b3_csv.py (BDI, dados de D-1, endpoints nao documentados):
  OpenPositionsEquities          contratos em aberto por serie (OpnIntrst). Para as opcoes de dolar a B3 NAO
                                 separa coberto/travado/descoberto como nas de acoes: so o total.
  InstrumentsDerivatives         cadastro: strike, vencimento (1o dia util do mes), call/put, multiplicador
                                 (DOL 50 = US$ 50 mil por contrato; WDO 10), estilo europeu.
  ConsolidatedTradesDerivatives  premio de referencia de TODAS as series (RefPric, R$ por US$ 1.000, mesmo sem
                                 negocio), ultimo negocio e o ajuste dos futuros de dolar de cada vencimento.

IV/delta: Black (1976) sobre o ajuste do futuro DOL do mesmo vencimento (F), taxa = Selic meta (continua), prazo =
dias uteis depois do pregao ate o vencimento / 252 (feriados ignorados, como no resto do projeto). Black e o
Black-Scholes com spot F.e^-rT, entao reusa iv_delta do posicoes_b3_csv.
fator = F do vencimento / F do futuro mais negociado (o que o WDO$ acompanha): converte o spot ao vivo do WDO$ no
forward de cada vencimento para o GEX do grafico.

Tabela (ts_ms = data do pregao 00:00 em hora de Brasilia codificada como epoch "UTC", convencao do banco):
  b3_dolar_series  sessao, ts_ms, capturado_em, ativo, symbol, vencimento, tipo, strike, mult, oi, var_oi,
                   premio_ref, ultimo, num_neg, futuro, fator, iv, delta
Idempotente: regravar a mesma sessao apaga e reinsere as linhas dela numa transacao curta.

Uso: python posicoes_b3_dolar.py [AAAA-MM-DD] [--db dados/book.db] [--log dados/posicoes_b3_dolar.log]
     sem data: ultimo pregao concluido (calendario do proprio BDI).
Saida: 0 ok | 1 erro | 2 dados da sessao ainda nao publicados (tentar mais tarde).
"""
import argparse
import logging
import math
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

import banco
import posicoes_b3_csv as pb

AQUI = pb.AQUI
ATIVOS = ("DOL", "WDO")
log = logging.getLogger("posicoes_b3_dolar")

SCHEMA = """
CREATE TABLE IF NOT EXISTS b3_dolar_series (
    sessao TEXT NOT NULL, ts_ms INTEGER NOT NULL, capturado_em TEXT NOT NULL, ativo TEXT NOT NULL,
    symbol TEXT NOT NULL, vencimento TEXT, tipo TEXT, strike REAL, mult REAL, oi REAL, var_oi REAL,
    premio_ref REAL, ultimo REAL, num_neg REAL, futuro REAL, fator REAL, iv REAL, delta REAL,
    PRIMARY KEY (sessao, symbol)
);
CREATE INDEX IF NOT EXISTS ix_b3dol_sessao ON b3_dolar_series (sessao);
"""
COLS = ["ativo", "symbol", "vencimento", "tipo", "strike", "mult", "oi", "var_oi", "premio_ref", "ultimo",
        "num_neg", "futuro", "fator", "iv", "delta"]


def selic(db_path, sessao):
    """Selic meta da sessao pela tabela do coletor_bcb (sem rede); senao pelo BCB, como no posicoes_b3_csv."""
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        try:
            r = con.execute("SELECT meta FROM bcb_selic WHERE data <= ? AND meta IS NOT NULL ORDER BY data DESC "
                            "LIMIT 1", (f"{sessao}",)).fetchone()
        finally:
            con.close()
        if r:
            return math.log(1 + r[0] / 100), r[0] / 100
    except sqlite3.Error:
        pass
    return pb.taxa_selic(sessao)


def montar(sessao, r):
    """Junta posicao, cadastro e premios de DOL e WDO. -> DataFrame com COLS (so series com posicao em aberto)."""
    partes = []
    for ativo in ATIVOS:
        op = pb.paginas_prefixo("OpenPositionsEquities", sessao, ativo)
        if op.empty:
            raise pb.NaoPublicado(f"OpenPositionsEquities {sessao} sem {ativo}")
        ins = pb.paginas_prefixo("InstrumentsDerivatives", sessao, ativo)
        ct = pb.paginas_prefixo("ConsolidatedTradesDerivatives", sessao, ativo)
        if ins.empty or ct.empty:
            raise pb.NaoPublicado(f"cadastro ou negocios de {ativo} em {sessao} ainda nao publicados")
        n = pb.num_br
        ct = ct.assign(ajuste=ct.AdjstdQt.map(n), ref=ct.RefPric.map(n), ultimo=ct.LastPric.map(n),
                       num_neg=ct.TradQty.map(n), contratos=ct.FinInstrmQty.map(n)).set_index("TckrSymb")
        fut = ins[ins.MktNm == "FUTURE"].assign(venc=lambda x: x.XprtnDt.str[:10]).set_index("TckrSymb")
        fut = fut.join(ct[["ajuste", "contratos"]])
        partes.append(dict(ativo=ativo, op=op, ins=ins, ct=ct, fut=fut))
    # futuro de referencia: o DOL mais negociado do dia (o vencimento que o WDO$ acompanha)
    fut_dol = partes[0]["fut"].dropna(subset=["ajuste"])
    frente = float(fut_dol.loc[fut_dol.contratos.fillna(0).idxmax(), "ajuste"])
    f_venc = fut_dol.groupby("venc").ajuste.first()  # forward de cada vencimento (o DOL tem todos os meses)
    linhas = []
    for p in partes:
        o = p["ins"][p["ins"].MktNm == "OPTIONS ON SPOT"].set_index("TckrSymb")
        oi = p["op"].set_index("TckrSymb")
        x = o.join(oi[["OpnIntrst", "VartnOpnIntrst"]], how="inner").join(p["ct"][["ref", "ultimo", "num_neg"]])
        for sym, s in x.iterrows():
            venc = str(s.XprtnDt)[:10]
            tipo = {"Call": "C", "Put": "P"}.get(s.OptnTp)
            k, qtd = pb.num_br(s.ExrcPric), pb.num_br(s.OpnIntrst)
            if tipo is None or not k or not qtd or qtd <= 0:
                continue
            f = f_venc.get(venc)
            du = int(np.busday_count(sessao + timedelta(days=1), date.fromisoformat(venc)))
            t = max(du, 1) / 252
            iv = de = None
            if f and s.ref and s.ref > 0:
                iv, de = pb.iv_delta(tipo, float(s.ref), f * math.exp(-r * t), k, t, r)
            linhas.append(dict(ativo=p["ativo"], symbol=sym, vencimento=venc, tipo=tipo, strike=k,
                               mult=pb.num_br(s.CtrctMltplr), oi=qtd, var_oi=pb.num_br(s.VartnOpnIntrst),
                               premio_ref=s.ref, ultimo=s.ultimo, num_neg=s.num_neg, futuro=f,
                               fator=f / frente if f else None, iv=iv, delta=de))
    return pd.DataFrame(linhas, columns=COLS), frente


def gravar(db_path, sessao, df):
    capturado = pb.agora_br().isoformat(timespec="seconds")
    ts = pb.ms_br(datetime(sessao.year, sessao.month, sessao.day))
    limpo = lambda v: None if v is None or (isinstance(v, float) and math.isnan(v)) else (
        v.item() if isinstance(v, np.generic) else v)
    linhas = [(f"{sessao}", ts, capturado, *(limpo(rec[c]) for c in COLS)) for rec in df.to_dict("records")]
    db = banco.abrir(str(db_path), SCHEMA)
    try:
        db.execute("PRAGMA busy_timeout=30000")
        for i in range(10):  # transacao curta; se outro coletor estiver gravando, espera e tenta de novo
            try:
                db.execute("BEGIN IMMEDIATE")
                db.execute("DELETE FROM b3_dolar_series WHERE sessao=?", (f"{sessao}",))
                db.executemany(f"INSERT INTO b3_dolar_series (sessao, ts_ms, capturado_em, {', '.join(COLS)}) "
                               f"VALUES ({','.join('?' * (3 + len(COLS)))})", linhas)
                db.commit()
                break
            except sqlite3.OperationalError as e:
                db.rollback()
                if "locked" not in str(e) and "busy" not in str(e) or i == 9:
                    raise
                log.warning("banco ocupado (%s); tentando de novo", e)
                time.sleep(2 + i)
    finally:
        db.close()
    return len(linhas)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("sessao", nargs="?", help="AAAA-MM-DD (padrao: ultimo pregao concluido)")
    ap.add_argument("--db", default=str(AQUI / "dados" / "book.db"))
    ap.add_argument("--log", default=str(AQUI / "dados" / "posicoes_b3_dolar.log"))
    args = ap.parse_args(argv)
    pb.configurar_log(args.log)  # o log do pb e o mesmo arquivo: as mensagens da paginacao entram nele
    for h in pb.log.handlers:
        log.addHandler(h)
    log.setLevel(logging.INFO)
    t0 = time.time()
    try:
        sessao = date.fromisoformat(args.sessao) if args.sessao else pb.ultima_sessao()
        r, s = selic(args.db, sessao)
        log.info("inicio: sessao %s, opcoes de dolar (%s), Selic %.2f%%, db %s", sessao, "/".join(ATIVOS), s * 100,
                 args.db)
        df, frente = montar(sessao, r)
        n = gravar(args.db, sessao, df)
        por = df.groupby(["ativo", "tipo"]).oi.sum()
        log.info("ok: sessao %s | %d series com posicao | %s | futuro de referencia %.3f | IV em %d series | %.0fs",
                 sessao, n, ", ".join(f"{a} {t} {v:,.0f}" for (a, t), v in por.items()), frente,
                 int(df.iv.notna().sum()), time.time() - t0)
        return 0
    except pb.NaoPublicado as e:
        log.error("nao publicado: %s", e)
        return 2
    except Exception as e:
        log.exception("falhou: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
