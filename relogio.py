"""Relogio comum dos coletores: hora do servidor MT5 (Brasilia codificada como epoch "UTC", a
convencao do banco), estimada como relogio do PC + offset.

A hora de um tick nunca passa da hora real do servidor, entao o maior offset visto e a melhor
estimativa. Ticks antigos (antes da abertura o ultimo negocio pode ser de dias atras) sao
ignorados: com eles o offset ficava negativo em horas e os registros saiam com a data errada.
"""
import time
from datetime import datetime, timezone

JANELA_MS = 300_000  # tick mais distante que isso do relogio do PC nao calibra


def local_br_ms():
    """Relogio de parede do PC (Brasilia) como epoch 'UTC'."""
    return datetime.now().replace(tzinfo=timezone.utc).timestamp() * 1000


class Relogio:
    def __init__(self):
        self.offset = None

    def agora(self, *ticks_ms):
        """ticks_ms: hora (time_msc) dos ticks recem-lidos. -> ms no relogio do banco."""
        local = local_br_ms()
        for t in ticks_ms:
            if t and abs(t - local) <= JANELA_MS:
                d = t - local
                self.offset = d if self.offset is None else max(self.offset, d)
        return int(local + (self.offset or 0.0))


if __name__ == "__main__":
    r = Relogio()
    antigo = local_br_ms() - 3 * 86400_000
    assert abs(r.agora(antigo) - local_br_ms()) < 1000, "tick antigo nao pode calibrar"
    assert r.offset is None
    r.agora(local_br_ms() + 2000)
    assert 1500 < r.offset < 2500
    print("ok")
