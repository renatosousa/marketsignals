"""Testes do coletor_profit_rtd.py com um servidor RTD falso (sem Profit). As corretoras ficam em test_coletor_tryd.py.

  python -m unittest test_coletor_profit_rtd -v
"""
import unittest

import coletor_profit_rtd as cp


class ServidorFalso:
    """Servidor RTD que devolve sempre 100.0; guarda a fila de atualizacoes que o RefreshData entrega."""

    def __init__(self):
        self.fila = []

    def ConnectData(self, tid, partes, novo):
        return [True, 100.0]

    def RefreshData(self, n):
        fila, self.fila = self.fila, []
        return len(fila), ([t for t, _ in fila], [v for _, v in fila])


def novo_rtd(srv):
    r = object.__new__(cp.Rtd)  # sem COM
    r.srv, r.topicos, r.ids, r.valores, r._prox = srv, {}, {}, {}, cp.TOPICO_BASE
    return r


class TestRtd(unittest.TestCase):
    def test_ler_ignora_topico_de_outro_cliente(self):
        srv = ServidorFalso()
        rtd = novo_rtd(srv)
        rtd.conectar("A", "ULT")
        srv.fila = [(5, "x"), (cp.TOPICO_BASE, 7.0)]  # 5 pertence a outro cliente do servidor compartilhado
        rtd.ler()
        self.assertEqual(rtd.valores[("A", "ULT")], 7.0)
        self.assertEqual(list(rtd.valores), [("A", "ULT")])

    def test_ids_proprios_comecam_em_topico_base(self):
        rtd = novo_rtd(ServidorFalso())
        rtd.conectar("A", "ULT")
        rtd.conectar("A", "HOR")
        self.assertEqual(sorted(rtd.topicos), [cp.TOPICO_BASE, cp.TOPICO_BASE + 1])


if __name__ == "__main__":
    unittest.main()
