"""Leitura de ticks de negocio do MT5 (COPY_TICKS_TRADE) para o saldo de agressao dos futuros.

Dois erros dos coletores originais inflavam o saldo do WIN (27/10/2026: o painel dizia ~150 mil contratos e o Profit
~36 mil, e os ticks brutos dao ~36 mil a partir das 09:05):
  1. ticks com as DUAS flags (BUY e SELL: negocio sem agressor definido) entravam como compra;
  2. `if ms <= ultimo_ms: continue` descartava todos os ticks do mesmo milissegundo menos o primeiro (45% do volume
     do WIN: o MT5 entrega varias execucoes de uma ordem a mercado com a mesma hora).

Aqui cada tick e contado uma vez (pelo numero de ticks ja vistos no milissegundo corrente), so entra tick com
exatamente uma flag, e o que vem antes de INICIO_MS (leilao de abertura, flagado como compra) fica de fora, como no
Profit. ts_ms segue a convencao do banco: relogio de Brasilia como epoch "UTC", entao a hora do dia e ms % 86400000.
"""
import MetaTrader5 as mt5

BUY, SELL = mt5.TICK_FLAG_BUY, mt5.TICK_FLAG_SELL
INICIO_MS = (9 * 60 + 5) * 60_000  # pregao continuo (o leilao de abertura do WIN fecha ~09:01 e o 1o minuto e ruidoso)


def lado(flags):
    """'C' compra agressora, 'V' venda agressora, 'N' sem agressor definido (as duas flags, ou nenhuma)."""
    c, v = bool(flags & BUY), bool(flags & SELL)
    return "C" if c and not v else "V" if v and not c else "N"


class LeitorTicks:
    """Devolve cada tick de negocio uma unica vez, mesmo com varios no mesmo milissegundo."""

    def __init__(self, ultimo_ms=0):
        self.reiniciar(ultimo_ms)

    def reiniciar(self, ultimo_ms):
        """Ticks de `ultimo_ms` em diante; os que ja estavam naquele milissegundo ficam de fora (ja vistos)."""
        self.ms, self.vistos = int(ultimo_ms), 10**9

    def novos(self, ticks):
        """ticks: array do copy_ticks_range. Gera (ms, lado, volume, preco) dos ticks ainda nao vistos."""
        a_pular = self.vistos  # ticks do milissegundo corrente que ja foram processados
        for t in ticks if ticks is not None else []:
            ms = int(t["time_msc"])
            if ms < self.ms:
                continue
            if ms == self.ms:
                if a_pular > 0:
                    a_pular -= 1
                    continue
                self.vistos += 1
            else:
                self.ms, self.vistos, a_pular = ms, 1, 0
            yield ms, lado(int(t["flags"])), float(t["volume_real"]) or float(t["volume"]), float(t["last"])


def em_pregao_continuo(ms):
    return ms % 86_400_000 >= INICIO_MS
