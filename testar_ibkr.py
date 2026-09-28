"""Diagnostico da conexao IBKR (somente leitura): quais contratos respondem, se o dado e tempo
real ou atrasado, e quanto historico de 1 min a IBKR entrega. Nao grava nada.

Uso: python testar_ibkr.py [--porta 4001]
"""
import argparse
from datetime import timezone

from ib_async import IB

from coletor_ibkr import resolver

TIPOS = {1: "TEMPO REAL", 2: "congelado", 3: "ATRASADO", 4: "atrasado congelado"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--porta", type=int, default=4001)
    args = ap.parse_args()

    ib = IB()
    erros = []
    ib.errorEvent += lambda req, cod, msg, contrato: erros.append((cod, msg[:110], getattr(contrato, "symbol", "")))
    ib.connect("127.0.0.1", args.porta, clientId=12, readonly=True, timeout=20)
    print("Conectado. Conta(s):", ib.managedAccounts(), "| servidor:", ib.reqCurrentTime(), flush=True)

    ib.reqMarketDataType(3)
    contratos = resolver(ib)
    tickers = {n: ib.reqMktData(c, "", False, False) for n, c in contratos.items()}
    ib.sleep(8)
    print(f"\n{'ativo':9} {'contrato':22} {'dado':12} {'bid':>10} {'ask':>10} {'ultimo':>10} {'fech.':>10}")
    for n, t in tickers.items():
        c = contratos[n]
        desc = f"{c.localSymbol or c.symbol} {c.lastTradeDateOrContractMonth or ''}".strip()
        print(f"{n:9} {desc:22} {TIPOS.get(t.marketDataType, '?'):12} {t.bid:>10} {t.ask:>10} {t.last:>10} {t.close:>10}")
    for t in tickers.values():
        ib.cancelMktData(t.contract)

    print("\nHistorico de 1 min (ultimos 2 dias, inclui fora do pregao regular):")
    for n, c in contratos.items():
        barras = ib.reqHistoricalData(c, endDateTime="", durationStr="2 D", barSizeSetting="1 min",
                                      whatToShow="TRADES", useRTH=False, formatDate=2, timeout=60)
        if barras:
            ini = barras[0].date.astimezone(timezone.utc)
            fim = barras[-1].date.astimezone(timezone.utc)
            print(f"  {n:9} {len(barras):5} barras  {ini:%d/%m %H:%M} -> {fim:%d/%m %H:%M} UTC")
        else:
            print(f"  {n:9} sem historico")

    if erros:
        print("\nMensagens da IBKR (codigo: texto):")
        for cod, msg, sym in dict.fromkeys(erros):
            print(f"  {cod} {sym}: {msg}")
    ib.disconnect()


if __name__ == "__main__":
    main()
