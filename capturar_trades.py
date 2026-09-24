"""Teste: captura times & trades do MT5 e calcula o saldo de agressao.

Uso: python capturar_trades.py [SIMBOLO] [SEGUNDOS]
"""
import csv
import sys
import time
from datetime import datetime, timezone

import MetaTrader5 as mt5

symbol = sys.argv[1] if len(sys.argv) > 1 else "WIN$"
duracao = float(sys.argv[2]) if len(sys.argv) > 2 else 30.0
saida = f"trades_{symbol.replace('$', '')}_{datetime.now():%Y%m%d_%H%M%S}.csv"

if not mt5.initialize():
    sys.exit(f"initialize falhou: {mt5.last_error()}")
mt5.symbol_select(symbol, True)

compra = venda = 0.0
n = n_buy = n_sell = n_sem_flag = 0
ultimo_msc = 0

# comeca do tick mais recente para nao trazer historico
def agora_servidor():
    """Hora do servidor MT5 (epoch 'tipo UTC') como datetime tz-aware, exigido pelo copy_ticks_*."""
    tk = mt5.symbol_info_tick(symbol)
    return datetime.fromtimestamp(tk.time + 1, tz=timezone.utc)


tk0 = mt5.symbol_info_tick(symbol)
ultimo_msc = int(tk0.time_msc)  # so trades posteriores a este instante

with open(saida, "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["hora", "preco", "volume", "agressor", "flags"])
    fim = time.time() + duracao
    while time.time() < fim:
        desde = datetime.fromtimestamp(ultimo_msc / 1000, tz=timezone.utc)
        ticks = mt5.copy_ticks_range(symbol, desde, agora_servidor(), mt5.COPY_TICKS_TRADE)
        if ticks is not None:
            for t in ticks:
                if int(t["time_msc"]) <= ultimo_msc:
                    continue
                ultimo_msc = int(t["time_msc"])
                fl = int(t["flags"])
                vol = float(t["volume_real"]) or float(t["volume"])
                if fl & mt5.TICK_FLAG_BUY:
                    ag = "COMPRA"; compra += vol; n_buy += 1
                elif fl & mt5.TICK_FLAG_SELL:
                    ag = "VENDA"; venda += vol; n_sell += 1
                else:
                    ag = "?"; n_sem_flag += 1
                n += 1
                hora = datetime.fromtimestamp(ultimo_msc / 1000, tz=timezone.utc).strftime("%H:%M:%S.%f")[:-3]
                w.writerow([hora, t["last"], vol, ag, fl])
                if n <= 15:
                    print(f"{hora}  {t['last']:>10}  {vol:>5.0f}  {ag:6}  flags={fl}")
        time.sleep(0.1)

mt5.shutdown()
print(f"\nTrades: {n} | compra agressora: {n_buy} | venda agressora: {n_sell} | sem flag: {n_sem_flag}")
print(f"Volume agressor comprador: {compra:.0f} | vendedor: {venda:.0f} | SALDO: {compra - venda:+.0f}")
print(f"Arquivo: {saida}")
