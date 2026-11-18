"""Testes do calendario_b3.py (feriados ANBIMA, dias uteis e navegacao entre pregoes).

  python -m unittest test_calendario_b3 -v

Sem banco e sem rede: so calendario.
"""
import unittest
from datetime import date

import numpy as np

import calendario_b3 as cal
import leiloes


class TestFeriados(unittest.TestCase):
    def test_carnaval_e_sexta_santa_2026(self):
        f = cal.feriados(2026)
        self.assertEqual(cal.pascoa(2026), date(2026, 4, 5))
        self.assertIn(date(2026, 2, 16), f)   # Carnaval, segunda
        self.assertIn(date(2026, 2, 17), f)   # Carnaval, terca
        self.assertIn(date(2026, 4, 3), f)    # Sexta-feira Santa
        self.assertIn(date(2026, 6, 4), f)    # Corpus Christi

    def test_carnaval_e_sexta_santa_2027(self):
        f = cal.feriados(2027)
        self.assertEqual(cal.pascoa(2027), date(2027, 3, 28))
        self.assertIn(date(2027, 2, 8), f)
        self.assertIn(date(2027, 2, 9), f)
        self.assertIn(date(2027, 3, 26), f)
        self.assertIn(date(2027, 5, 27), f)

    def test_consciencia_negra_so_desde_2024(self):
        self.assertNotIn(date(2023, 11, 20), cal.feriados(2023))
        self.assertIn(date(2024, 11, 20), cal.feriados(2024))
        self.assertIn(date(2026, 11, 20), cal.feriados(2026))

    def test_leiloes_mantem_os_mesmos_nomes(self):
        self.assertEqual(leiloes.pascoa(2026), cal.pascoa(2026))
        self.assertEqual(leiloes.feriados(2026), cal.feriados(2026))
        self.assertEqual(int(leiloes.du("2026-11-03", "2026-11-04")), 1)


class TestPregao(unittest.TestCase):
    def test_24_e_31_de_dezembro_nao_sao_pregao(self):
        self.assertFalse(cal.eh_pregao(date(2026, 12, 24)))
        self.assertFalse(cal.eh_pregao(date(2026, 12, 31)))
        self.assertTrue(cal.eh_pregao(date(2026, 12, 23)))
        self.assertTrue(cal.eh_pregao(date(2026, 12, 30)))

    def test_20_de_novembro(self):
        self.assertFalse(cal.eh_pregao("2026-11-20"))   # sexta, Consciencia Negra
        self.assertTrue(cal.eh_pregao("2026-11-19"))
        self.assertTrue(cal.eh_pregao("2023-11-20"))    # ainda nao era feriado nacional

    def test_fim_de_semana_e_natal(self):
        self.assertFalse(cal.eh_pregao(date(2026, 11, 21)))   # sabado
        self.assertFalse(cal.eh_pregao(date(2026, 12, 25)))

    def test_navegacao_atravessa_feriado(self):
        self.assertEqual(cal.pregao_anterior("2026-11-23"), date(2026, 11, 19))  # pula 20/11 e o fim de semana
        self.assertEqual(cal.proximo_pregao("2026-11-19"), date(2026, 11, 23))
        self.assertEqual(cal.pregao_anterior("2026-12-31"), date(2026, 12, 30))
        self.assertEqual(cal.proximo_pregao("2026-12-30"), date(2027, 1, 4))


class TestDiasUteis(unittest.TestCase):
    def test_du_com_feriado_no_meio(self):
        self.assertEqual(cal.du("2026-11-19", "2026-11-24"), 2)     # 19 e 23; 20 e feriado
        self.assertEqual(cal.du("2026-10-30", "2026-11-04"), 2)     # 30 e 03; 02 e feriado
        self.assertEqual(cal.du(date(2026, 11, 3), np.datetime64("2026-11-04")), 1)

    def test_du_ate_vencimento(self):
        self.assertEqual(cal.du_ate_vencimento("2026-11-18", "2026-11-19"), 1)
        self.assertEqual(cal.du_ate_vencimento("2026-11-18", "2026-11-20"), 1)   # 19 sim, 20 feriado
        self.assertEqual(cal.du_ate_vencimento("2026-11-18", "2026-11-23"), 2)   # 19 e 23
        self.assertEqual(cal.du_ate_vencimento("2026-11-16", "2026-11-18"), 2)
        self.assertEqual(cal.du_ate_vencimento("2026-11-18", "2026-11-18"), 0)   # venc <= hoje
        self.assertEqual(cal.du_ate_vencimento("2026-11-18", "2026-11-10"), 0)

    def test_somar_pregoes_atravessa_20_de_novembro(self):
        self.assertEqual(cal.somar_pregoes(date(2026, 11, 18), 2), date(2026, 11, 23))
        self.assertEqual(cal.somar_pregoes("2026-11-18", 1), date(2026, 11, 19))
        self.assertEqual(cal.somar_pregoes("2026-11-23", -2), date(2026, 11, 18))
        self.assertEqual(cal.somar_pregoes("2026-11-18", 0), date(2026, 11, 18))

    def test_lista_de_pregoes_do_intervalo(self):
        self.assertEqual(list(cal.pregoes("2026-11-18", "2026-11-23")),
                         [date(2026, 11, 18), date(2026, 11, 19), date(2026, 11, 23)])


if __name__ == "__main__":
    unittest.main()
