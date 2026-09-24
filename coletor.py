"""Coletor unificado MT5: livro de ofertas + times & trades + saldo de agressao.

Livro, trades e saldo usam o mesmo relogio (hora do servidor MT5, em ms).
Gera 3 CSVs na pasta de saida (default: dados/):
  livro_<ativo>_<data>.csv   ts_ms, tipo, preco, volume        (so quando o livro muda)
  trades_<ativo>_<data>.csv  ts_ms, preco, volume, agressor, flags
  saldo_<ativo>_<data>.csv   ts_ms, saldo, comprador, vendedor, bid, ask   (1 linha/segundo)

Uso: python coletor.py [SIMBOLO] [SEGUNDOS] [PASTA]   (SEGUNDOS=0 roda ate Ctrl+C)
"""
import csv
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import MetaTrader5 as mt5

symbol = sys.argv[1] if len(sys.argv) > 1 else "WIN$"
duracao = float(sys.argv[2]) if len(sys.argv) > 2 else 60.0
pasta = Path(sys.argv[3] if len(sys.argv) > 3 else "dados")
pasta.mkdir(parents=True, exist_ok=True)

TIPOS_LIVRO = {mt5.BOOK_TYPE_SELL: "VENDA", mt5.BOOK_TYPE_BUY: "COMPRA",
               mt5.BOOK_TYPE_SELL_MARKET: "VENDA_MKT", mt5.BOOK_TYPE_BUY_MARKET: "COMPRA_MKT"}


def utc(ms):
    """MT5 exige datetime tz-aware; o epoch do servidor e 'tipo UTC' (na verdade hora de Brasilia)."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def main():
    if not mt5.initialize():
        sys.exit(f"initialize falhou: {mt5.last_error()}")
    if not mt5.symbol_select(symbol, True):
        sys.exit(f"symbol_select({symbol}) falhou: {mt5.last_error()}")
    if not mt5.market_book_add(symbol):
        sys.exit(f"market_book_add falhou: {mt5.last_error()}")

    nome = f"{symbol.replace('$', '')}_{datetime.now():%Y%m%d_%H%M%S}"
    f_livro = open(pasta / f"livro_{nome}.csv", "w", newline="", encoding="utf-8")
    f_trades = open(pasta / f"trades_{nome}.csv", "w", newline="", encoding="utf-8")
    f_saldo = open(pasta / f"saldo_{nome}.csv", "w", newline="", encoding="utf-8")
    w_livro, w_trades, w_saldo = csv.writer(f_livro), csv.writer(f_trades), csv.writer(f_saldo)
    w_livro.writerow(["ts_ms", "tipo", "preco", "volume"])
    w_trades.writerow(["ts_ms", "preco", "volume", "agressor", "flags"])
    w_saldo.writerow(["ts_ms", "saldo", "comprador", "vendedor", "bid", "ask"])

    tk = mt5.symbol_info_tick(symbol)
    ultimo_msc = int(tk.time_msc)  # so trades posteriores ao inicio
    comprador = vendedor = 0.0
    n_trades = n_livro = 0
    ultimo_livro = None
    prox_saldo = 0
    fim = time.time() + duracao if duracao > 0 else float("inf")
    print(f"Coletando {symbol} -> {pasta}/  (Ctrl+C para parar)")

    try:
        while time.time() < fim:
            tk = mt5.symbol_info_tick(symbol)
            agora_ms = int(tk.time_msc)

            ticks = mt5.copy_ticks_range(symbol, utc(ultimo_msc), utc(agora_ms + 1000), mt5.COPY_TICKS_TRADE)
            if ticks is not None:
                for t in ticks:
                    ms = int(t["time_msc"])
                    if ms <= ultimo_msc:
                        continue
                    ultimo_msc = ms
                    fl = int(t["flags"])
                    vol = float(t["volume_real"]) or float(t["volume"])
                    if fl & mt5.TICK_FLAG_BUY:
                        ag = "COMPRA"; comprador += vol
                    elif fl & mt5.TICK_FLAG_SELL:
                        ag = "VENDA"; vendedor += vol
                    else:
                        ag = "?"
                    n_trades += 1
                    w_trades.writerow([ms, t["last"], vol, ag, fl])

            livro = mt5.market_book_get(symbol)
            if livro:
                snap = tuple((b.type, b.price, b.volume) for b in livro)
                if snap != ultimo_livro:
                    ultimo_livro = snap
                    n_livro += 1
                    for b in livro:
                        w_livro.writerow([agora_ms, TIPOS_LIVRO.get(b.type, b.type), b.price, b.volume])

            if agora_ms >= prox_saldo:
                prox_saldo = agora_ms - agora_ms % 1000 + 1000
                w_saldo.writerow([agora_ms, comprador - vendedor, comprador, vendedor, tk.bid, tk.ask])

            time.sleep(0.02)
    except KeyboardInterrupt:
        pass
    finally:
        mt5.market_book_release(symbol)
        mt5.shutdown()
        for f in (f_livro, f_trades, f_saldo):
            f.close()

    print(f"Trades: {n_trades} | mudancas no livro: {n_livro}")
    print(f"Comprador {comprador:.0f} | vendedor {vendedor:.0f} | SALDO {comprador - vendedor:+.0f}")


if __name__ == "__main__":
    main()
