"""Leitura de ticks de negocio do MT5 (COPY_TICKS_TRADE) para o saldo de agressao (futuros e acoes).

Erros dos coletores originais (27/10/2026: o painel dizia ~616 mil contratos de saldo no WIN e o Profit ~36 mil):
  1. ticks com as DUAS flags (BUY e SELL: negocio sem agressor definido) entravam como compra;
  2. `if ms <= ultimo_ms: continue` descartava todos os ticks do mesmo milissegundo menos o primeiro (45% do volume
     do WIN: o MT5 entrega varias execucoes de uma ordem a mercado com a mesma hora);
  3. as acoes eram lidas so pelo ultimo tick a cada consulta (~60% do volume, saldo com sinal trocado);
  4. a impressao dos leiloes (abertura do WIN, fechamento das acoes, reabertura apos circuit breaker) vem flagada
     como compra: no PETR4 de 27/10 foram 5,6 mi de acoes (~R$ 300 mi) num unico milissegundo.

Aqui cada tick e contado uma vez (pelo numero de ticks ja vistos no milissegundo corrente), so entra tick com
exatamente uma flag, e a rajada que vem depois de PAUSA_LEILAO_MS sem negocio e tratada como leilao (sem agressor).
Janela de contagem: futuros a partir de 09:05 (como no Profit); acoes de 10:00 a 16:55 (pregao continuo).
ts_ms segue a convencao do banco: relogio de Brasilia como epoch "UTC", entao a hora do dia e ms % 86400000.
"""
import MetaTrader5 as mt5
import numpy as np

BUY, SELL = mt5.TICK_FLAG_BUY, mt5.TICK_FLAG_SELL
H = lambda hh, mm: (hh * 60 + mm) * 60_000
INICIO_MS = H(9, 5)                      # futuros: pregao continuo (o leilao de abertura do WIN fecha ~09:01)
JANELA_ACOES = (H(10, 0), H(16, 55))     # acoes: depois disso vem o leilao de fechamento e o after-market
PAUSA_LEILAO_MS = 60_000                 # rajada depois de 1 min sem negocio = impressao de leilao


def lado(flags):
    """'C' compra agressora, 'V' venda agressora, 'N' sem agressor definido (as duas flags, ou nenhuma)."""
    c, v = bool(flags & BUY), bool(flags & SELL)
    return "C" if c and not v else "V" if v and not c else "N"


def sinais(ms, flags):
    """Versao vetorizada para o historico: +1 compra, -1 venda, 0 sem agressor ou leilao. ms em ordem crescente."""
    ms, fl = np.asarray(ms, dtype=np.int64), np.asarray(flags, dtype=np.int64)
    c, v = (fl & BUY) > 0, (fl & SELL) > 0
    s = np.where(c & ~v, 1, np.where(v & ~c, -1, 0))
    novo = np.r_[True, np.diff(ms) > 0]                       # primeiro tick de cada milissegundo
    pausa = np.r_[True, np.diff(ms) >= PAUSA_LEILAO_MS]       # (o 1o tick do dia conta como depois de pausa)
    leilao_ms = np.maximum.accumulate(np.where(novo, np.where(pausa, ms, -1), -1))  # ms do ultimo inicio de leilao
    s[leilao_ms == ms] = 0
    return s


class LeitorTicks:
    """Devolve cada tick de negocio uma unica vez, mesmo com varios no mesmo milissegundo."""

    def __init__(self, ultimo_ms=0):
        self.reiniciar(ultimo_ms)

    def reiniciar(self, ultimo_ms):
        """Ticks de `ultimo_ms` em diante; os que ja estavam naquele milissegundo ficam de fora (ja vistos)."""
        self.ms, self.vistos, self.leilao = int(ultimo_ms), 10**9, False

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
                self.leilao = ms - self.ms >= PAUSA_LEILAO_MS
                self.ms, self.vistos, a_pular = ms, 1, 0
            yield (ms, "N" if self.leilao else lado(int(t["flags"])),
                   float(t["volume_real"]) or float(t["volume"]), float(t["last"]))


def em_pregao_continuo(ms, janela=(INICIO_MS, None)):
    h = ms % 86_400_000
    return h >= janela[0] and (janela[1] is None or h < janela[1])
