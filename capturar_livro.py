"""Teste: captura o livro de ofertas (DOM) do MT5 aberto.

Uso: python capturar_livro.py [SIMBOLO] [SEGUNDOS]
Ex.: python capturar_livro.py WINV26 10
"""
import csv
import sys
import time
from datetime import datetime

import MetaTrader5 as mt5

symbol = sys.argv[1] if len(sys.argv) > 1 else "WINV26"
duracao = float(sys.argv[2]) if len(sys.argv) > 2 else 10.0
saida = f"livro_{symbol}_{datetime.now():%Y%m%d_%H%M%S}.csv"

if not mt5.initialize():
    sys.exit(f"initialize falhou: {mt5.last_error()}")

info = mt5.terminal_info()
conta = mt5.account_info()
print(f"Terminal: {info.name} | conectado={info.connected} | trade_allowed={info.trade_allowed}")
print(f"Conta: {conta.login if conta else '?'} @ {conta.server if conta else '?'}")

if not mt5.symbol_select(symbol, True):
    mt5.shutdown()
    sys.exit(f"symbol_select({symbol}) falhou: {mt5.last_error()}. Confira o nome do ativo no Market Watch.")

if not mt5.market_book_add(symbol):
    mt5.shutdown()
    sys.exit(f"market_book_add falhou: {mt5.last_error()} (a corretora pode nao fornecer DOM para este ativo)")

tipos = {mt5.BOOK_TYPE_SELL: "VENDA", mt5.BOOK_TYPE_BUY: "COMPRA",
         mt5.BOOK_TYPE_SELL_MARKET: "VENDA_MKT", mt5.BOOK_TYPE_BUY_MARKET: "COMPRA_MKT"}

n_snap = 0
n_vazios = 0
try:
    with open(saida, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ts", "tipo", "preco", "volume"])
        fim = time.time() + duracao
        ultimo = None
        while time.time() < fim:
            livro = mt5.market_book_get(symbol)
            if not livro:
                n_vazios += 1
            else:
                snap = tuple((b.type, b.price, b.volume) for b in livro)
                if snap != ultimo:  # so grava quando o livro muda
                    ultimo = snap
                    n_snap += 1
                    ts = datetime.now().isoformat(timespec="milliseconds")
                    for b in livro:
                        w.writerow([ts, tipos.get(b.type, b.type), b.price, b.volume])
                    if n_snap == 1:
                        print(f"\nPrimeiro snapshot ({len(livro)} niveis):")
                        for b in sorted(livro, key=lambda x: -x.price):
                            print(f"  {tipos.get(b.type, b.type):9} {b.price:>12} x {b.volume}")
            time.sleep(0.05)
finally:
    mt5.market_book_release(symbol)
    mt5.shutdown()

print(f"\nSnapshots distintos: {n_snap} | leituras vazias: {n_vazios}")
print(f"Arquivo: {saida}")
