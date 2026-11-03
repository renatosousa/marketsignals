"""Testes do resumo de posicao em aberto da faixa (dashboard.resumo_da_faixa).

  python -m unittest test_dashboard_posicoes -v
"""
import unittest

import dashboard


def s(tipo, strike, cob=0.0, tra=0.0, des=0.0):
    return dict(tipo=tipo, strike=strike, coberto=cob, travado=tra, descoberto=des)


class TestResumoDaFaixa(unittest.TestCase):
    def test_ima_concentracao_suporte_e_resistencia(self):
        # totais por strike: 170 = 150, 180 = 400, 160 = 300, 190 = 10
        r = dashboard.resumo_da_faixa([s("C", 170, des=100), s("P", 170, des=50), s("C", 180, des=400),
                                       s("P", 160, des=300), s("C", 190, des=10)])
        self.assertEqual(r["strike_ima"], 180)
        self.assertAlmostEqual(r["concentracao_ima"], 400 / 300)
        self.assertEqual(r["suporte"], 160)             # maior put descoberta
        self.assertEqual(r["resistencia"], 180)         # maior call descoberta

    def test_sem_dados(self):
        r = dashboard.resumo_da_faixa([])
        self.assertEqual(r, dict(strike_ima=None, concentracao_ima=None, suporte=None, resistencia=None))

    def test_valores_none_contam_como_zero(self):
        r = dashboard.resumo_da_faixa([dict(tipo="C", strike=170, coberto=None, travado=None, descoberto=None),
                                       s("P", 160, des=5)])
        self.assertEqual((r["strike_ima"], r["suporte"], r["resistencia"]), (160, 160, None))

    def test_um_strike_so_nao_tem_concentracao(self):
        r = dashboard.resumo_da_faixa([s("C", 170, des=10)])
        self.assertEqual((r["strike_ima"], r["concentracao_ima"]), (170, None))


if __name__ == "__main__":
    unittest.main()
