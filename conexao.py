"""Guarda de conexao dos coletores do MT5: nao gravar dado parado como se fosse atual.

Em 20/10/2026 o PC ficou ~3 h sem internet (08:58-12:11). O terminal do MT5 parou de receber cotacoes, mas
symbol_info_tick() seguiu devolvendo o ultimo tick e os coletores gravaram milhares de snapshots com o preco
congelado (WIN em 209.305 enquanto o mercado ia a 212.000) e opcoes com o spot do BOVA11 parado.

A Guarda fecha esse buraco na origem. Um dado so vale se:
  1. terminal_info().connected e verdadeiro; e
  2. em horario de pregao (seg-sex, janela do ativo), o ultimo tick tem no maximo `max_idade_s` segundos.
Quando falha, o coletor nao grava (e avisa uma vez); ao voltar, `voltou` fica verdadeiro uma vez, para o coletor
descartar o que se acumulou (por exemplo, ticks perdidos durante a queda) em vez de jogar tudo num unico snapshot.

Uso:  guarda = Guarda("livro WIN$", mt5, "09:05", "18:20", 120)
      if not guarda.ok(tk): time.sleep(1); continue
      if guarda.voltou: ...   # (vira False sozinho na leitura seguinte de ok())
"""
import time
from datetime import datetime

from relogio import local_br_ms

CACHE_S = 3.0  # terminal_info() e barato, mas nao precisa rodar a cada iteracao


def janela_de(simbolo):
    """(inicio, fim, idade maxima do tick em s) por tipo de ativo. Futuros e DI negociam 09:00-18:25; acoes e ETFs
    10:00-17:00 (depois so o leilao de fechamento); a guarda destes so vale a partir de 10:15."""
    s = simbolo.upper()
    if s.startswith(("WIN", "WDO", "IND", "DI1", "DAP")):
        return "09:05", "18:20", 120.0
    return "10:15", "16:55", 180.0  # 10:05 gerava falso alarme: o BOVA11 fica >3 min sem tick logo depois da abertura


class Guarda:
    def __init__(self, nome, mt5, inicio="09:05", fim="18:20", max_idade_s=120.0, log=print):
        self.nome, self.mt5, self.max_idade_s, self.log = nome, mt5, max_idade_s, log
        self.inicio, self.fim = inicio, fim
        self.parado = False        # estado atual (para avisar so nas transicoes)
        self.voltou = False        # vale uma vez apos a recuperacao
        self.desde = None
        self._conectado, self._t_conn = True, 0.0

    def _em_pregao(self):
        agora = datetime.now()
        return agora.weekday() < 5 and self.inicio <= agora.strftime("%H:%M") < self.fim

    def _conectado_agora(self):
        t = time.time()
        if t - self._t_conn >= CACHE_S:
            ti = self.mt5.terminal_info()
            self._conectado = bool(ti.connected) if ti is not None else False
            self._t_conn = t
        return self._conectado

    def ok(self, tk):
        """tk: ultimo tick (symbol_info_tick) ou None. -> True se o dado e atual."""
        motivo = None
        if tk is None:
            motivo = "sem cotacao"
        elif not self._conectado_agora():
            motivo = "terminal do MT5 sem conexao com o servidor"
        elif self._em_pregao():
            idade = (local_br_ms() - tk.time_msc) / 1000
            if idade > self.max_idade_s:
                motivo = f"ultimo tick com {idade:.0f} s (limite {self.max_idade_s:.0f} s)"
        if motivo:
            if not self.parado:
                self.parado, self.desde = True, datetime.now()
                self.log(f"[{datetime.now():%H:%M:%S}] AVISO {self.nome}: dados parados ({motivo}); "
                         "nao grava ate normalizar", flush=True)
            return False
        if self.parado:
            self.parado, self.voltou = False, True
            self.log(f"[{datetime.now():%H:%M:%S}] {self.nome}: dados voltaram (parados desde "
                     f"{self.desde:%H:%M:%S})", flush=True)
        elif self.voltou:
            self.voltou = False
        return True


if __name__ == "__main__":
    class Fake:
        def __init__(self): self.c = True
        def terminal_info(self): return type("T", (), {"connected": self.c})()
    f = Fake()
    g = Guarda("teste", f, "00:00", "23:59", 120.0, log=lambda m, **k: print(m))
    novo = type("Tk", (), {"time_msc": local_br_ms()})()
    velho = type("Tk", (), {"time_msc": local_br_ms() - 600_000})()
    assert g.ok(novo) and not g.voltou
    assert datetime.now().weekday() >= 5 or not g.ok(velho)          # tick de 10 min em pregao -> parado
    assert not g.ok(None)
    if datetime.now().weekday() < 5:
        assert g.ok(novo) and g.voltou                                # voltou uma vez
        assert g.ok(novo) and not g.voltou
    f.c, g._t_conn = False, 0.0
    assert not g.ok(novo)                                             # terminal desconectado
    print("ok")
