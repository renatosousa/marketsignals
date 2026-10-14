"""Baixa barras de 1 min (historico) do MT5 e da IBKR para a tabela barras_1m do book.db.

  MT5 : WIN$, WDO$ (series continuas da corretora) e vertices do DI1, em blocos de 10 dias.
  IBKR: futuros emendados por vencimento (troca ROLAGEM dias antes do ultimo dia de negociacao
        de cada contrato), VIX e EWZ. Somente leitura (readonly=True).

ts_ms = inicio da barra, hora de Brasilia codificada como epoch "UTC" (convencao do banco).
A emenda de futuros cai entre dias; quem usa retornos intradiarios nao ve o salto da rolagem.
Idempotente (INSERT OR REPLACE): pode rodar de novo para estender o periodo.

Uso: python backfill_barras.py [--dias 120] [--db dados/book.db] [--so mt5|ibkr] [--porta 4001] [--ativos WIN$,WDO$]
"""
import argparse
import time
from datetime import datetime, timedelta, timezone

import banco

SCHEMA = """
CREATE TABLE IF NOT EXISTS barras_1m (
    symbol TEXT NOT NULL, ts_ms INTEGER NOT NULL, fonte TEXT, contrato TEXT,
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (symbol, ts_ms)
);
"""
BR = timedelta(hours=-3)  # Brasil sem horario de verao desde 2019
PAUSA_IB = 10.0           # limite da IBKR: ~60 pedidos de historico a cada 10 min

MT5_ATIVOS = ["WIN$", "WDO$", "DI1F27", "DI1F29", "DI1F33"]  # DI: close e TAXA (% a.a.), nao preco
# nome -> (simbolo, bolsa, dias de rolagem antes do ultimo dia, meses validos ou None)
IB_FUTUROS = {
    "IB:ES": ("ES", "CME", 8, None),
    "IB:NQ": ("NQ", "CME", 8, None),
    "IB:ZN": ("ZN", "CBOT", 30, None),
    "IB:CL": ("CL", "NYMEX", 5, None),
    "IB:GC": ("GC", "COMEX", 35, {2, 4, 6, 8, 10, 12}),  # so os meses liquidos do ouro
    "IB:DX": ("DX", "NYBOT", 10, None),
}
IB_OUTROS = {"IB:VIX": ("IND", "VIX", "CBOE"), "IB:EWZ": ("STK", "EWZ", "SMART")}


def gravar(db, linhas):
    db.executemany("INSERT OR REPLACE INTO barras_1m VALUES (?,?,?,?,?,?,?,?,?)", linhas)
    db.commit()


def conferir_rolagem(db, ativos=("WIN$", "WDO$"), tolerancia=0.002):
    """Os continuos da corretora (WIN$, WDO$) sao de AJUSTE PROPORCIONAL: a cada vencimento ela reescreve o
    historico inteiro na escala do contrato novo (e so guarda ~9 meses de barras de 1 min). A coleta rotineira
    so regrava os ultimos dias, entao o banco fica com duas escalas e o fechamento->abertura da emenda vira um
    retorno falso (WIN em 14/10/2026: +2,1%). Aqui se mede a razao MT5/banco numa barra antiga (12:00); se passa
    da tolerancia, o historico guardado e multiplicado por ela (cobre tambem o que o MT5 ja nao serve) e o
    periodo que o MT5 ainda devolve e baixado de novo. As barras antes da correcao ficam em
    barras_1m_antes_rolagem. -> lista dos simbolos corrigidos."""
    import MetaTrader5 as mt5
    if not mt5.initialize():
        return []
    refeitos = []
    hoje_br = int((datetime.utcnow() + BR).replace(hour=0, minute=0, second=0, microsecond=0)
                  .replace(tzinfo=timezone.utc).timestamp() * 1000)
    for s in ativos:
        candidatas = db.execute("SELECT ts_ms, close FROM barras_1m WHERE symbol=? AND ts_ms < ? "
                                "AND ts_ms % 86400000 = 43200000 ORDER BY ts_ms ASC", (s, hoje_br)).fetchall()
        razao = None
        for t, c in candidatas[:60]:  # a mais antiga que o MT5 ainda serve
            a = datetime.fromtimestamp(t / 1000, timezone.utc)
            r = mt5.copy_rates_range(s, mt5.TIMEFRAME_M1, a - timedelta(minutes=2), a + timedelta(minutes=2))
            r = [b for b in (r if r is not None else []) if int(b["time"]) * 1000 == t]
            if r and c:
                razao = float(r[-1]["close"]) / c
                break
        if razao is None or abs(razao - 1) <= tolerancia:
            continue
        ini = db.execute("SELECT min(ts_ms) FROM barras_1m WHERE symbol=?", (s,)).fetchone()[0]
        print(f"rolagem em {s}: o MT5 devolve {razao:.4f}x o que esta no banco; reescalando e regravando", flush=True)
        mt5.shutdown()
        db.execute("CREATE TABLE IF NOT EXISTS barras_1m_antes_rolagem (feito_em TEXT, symbol TEXT, ts_ms INTEGER, "
                   "open REAL, high REAL, low REAL, close REAL, volume REAL)")
        db.execute("INSERT INTO barras_1m_antes_rolagem SELECT datetime('now'), symbol, ts_ms, open, high, low, close, "
                   "volume FROM barras_1m WHERE symbol=?", (s,))
        # tudo na escala nova pela razao; o que o MT5 ainda serve e sobrescrito pelo valor exato logo abaixo
        db.execute("UPDATE barras_1m SET open=open*?, high=high*?, low=low*?, close=close*? WHERE symbol=?",
                   (razao, razao, razao, razao, s))
        db.commit()
        # backfill_mt5 espera UTC real: relogio de Brasilia + 3 h
        backfill_mt5(db, datetime.fromtimestamp(ini / 1000, timezone.utc).replace(tzinfo=None) - BR,
                     datetime.utcnow().replace(second=0, microsecond=0), [s])
        refeitos.append(s)
        mt5.initialize()
    mt5.shutdown()
    return refeitos


def backfill_mt5(db, inicio, fim, ativos=None):
    import MetaTrader5 as mt5
    from coletor_agressao import CESTA  # acoes da cesta de agressao: o volume real por minuto escala a captura
    if not mt5.initialize():
        raise SystemExit(f"MT5: {mt5.last_error()}")
    for s in (ativos or MT5_ATIVOS + CESTA):
        mt5.symbol_select(s, True)
        total, a = 0, inicio
        while a < fim:
            b = min(a + timedelta(days=10), fim)
            # MT5 le datas como hora do servidor (Brasilia) marcada como UTC
            r = mt5.copy_rates_range(s, mt5.TIMEFRAME_M1, (a + BR).replace(tzinfo=timezone.utc),
                                     (b + BR).replace(tzinfo=timezone.utc))
            if r is not None and len(r):
                gravar(db, [(s, int(x["time"]) * 1000, "mt5", s, float(x["open"]), float(x["high"]),
                             float(x["low"]), float(x["close"]), float(x["real_volume"] or x["tick_volume"]))
                            for x in r])
                total += len(r)
            a = b
        print(f"MT5 {s}: {total} barras", flush=True)
    mt5.shutdown()


def barras_ib(ib, contrato, ini, fim):
    """Barras de 1 min de [ini, fim) (UTC), em pedidos de 2 semanas do fim para o inicio."""
    out, fim_req = [], fim
    while fim_req > ini:
        barras = ib.reqHistoricalData(contrato, endDateTime=fim_req.strftime("%Y%m%d %H:%M:%S UTC"),
                                      durationStr="2 W", barSizeSetting="1 min", whatToShow="TRADES",
                                      useRTH=False, formatDate=2, timeout=120)
        time.sleep(PAUSA_IB)
        if not barras:
            break
        out += [x for x in barras if ini <= x.date < fim]
        primeiro = barras[0].date
        if primeiro >= fim_req:
            break
        fim_req = primeiro
    return out


def linhas_ib(nome, contrato, barras):
    # x.date e UTC (formatDate=2); desloca para a hora de Brasilia, convencao do banco
    return [(nome, int((x.date.timestamp() + BR.total_seconds()) * 1000), "ibkr", contrato,
             x.open, x.high, x.low, x.close, float(x.volume)) for x in barras]


def backfill_ibkr(db, inicio, fim, porta):
    from ib_async import IB, Future, Index, Stock
    ib = IB()
    ib.connect("127.0.0.1", porta, clientId=14, readonly=True, timeout=20)
    ini_utc, fim_utc = inicio.replace(tzinfo=timezone.utc), fim.replace(tzinfo=timezone.utc)

    for nome, (sym, bolsa, rolagem, meses) in IB_FUTUROS.items():
        det = ib.reqContractDetails(Future(sym, exchange=bolsa, includeExpired=True))
        cs = sorted((d.contract for d in det), key=lambda c: c.lastTradeDateOrContractMonth)
        if meses:  # ouro: o ultimo dia de negociacao cai no proprio mes do contrato
            cs = [c for c in cs if int(c.lastTradeDateOrContractMonth[4:6]) in meses]
        total, seg_ini = 0, None
        for c in cs:
            venc = datetime.strptime(c.lastTradeDateOrContractMonth[:8], "%Y%m%d").replace(tzinfo=timezone.utc)
            seg_fim = venc - timedelta(days=rolagem)
            a = max(seg_ini or ini_utc, ini_utc)
            b = min(seg_fim, fim_utc)
            seg_ini = seg_fim
            if b <= a:
                if a >= fim_utc:
                    break
                continue
            barras = barras_ib(ib, c, a, b)
            gravar(db, linhas_ib(nome, c.localSymbol, barras))
            total += len(barras)
            print(f"IBKR {nome} {c.localSymbol}: {len(barras)} barras ({a:%d/%m} -> {b:%d/%m})", flush=True)
        print(f"IBKR {nome}: {total} barras", flush=True)

    for nome, (tipo, sym, bolsa) in IB_OUTROS.items():
        c = Index(sym, bolsa) if tipo == "IND" else Stock(sym, bolsa, "USD")
        ib.qualifyContracts(c)
        barras = barras_ib(ib, c, ini_utc, fim_utc)
        gravar(db, linhas_ib(nome, sym, barras))
        print(f"IBKR {nome}: {len(barras)} barras", flush=True)
    ib.disconnect()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dias", type=int, default=120)
    ap.add_argument("--db", default="dados/book.db")
    ap.add_argument("--so", choices=["mt5", "ibkr"])
    ap.add_argument("--porta", type=int, default=4001)
    ap.add_argument("--ativos", help="so estes simbolos do MT5, separados por virgula (ex.: WIN$,WDO$ apos rolagem)")
    args = ap.parse_args()

    db = banco.abrir(args.db, SCHEMA)
    fim = datetime.utcnow().replace(second=0, microsecond=0)
    inicio = fim - timedelta(days=args.dias)
    if args.so in (None, "mt5"):
        backfill_mt5(db, inicio, fim, args.ativos.split(",") if args.ativos else None)
    if args.so in (None, "ibkr"):
        backfill_ibkr(db, inicio, fim, args.porta)
    db.close()


if __name__ == "__main__":
    main()
