"""Testes do coletor_opcoes_rtd.py (calendario e tickers das semanais) e da leitura com dois coletores de opcoes
(grafico_quant.implicito). Sem Profit, sem rede.

  python -m unittest test_coletor_opcoes_rtd -v
"""
import sqlite3
import unittest
from datetime import date

import coletor_opcoes as co
import coletor_opcoes_rtd as cr
import grafico_quant as gq


class TestSemanais(unittest.TestCase):
    def test_novembro_2026_pula_a_terceira_sexta(self):
        # 06/11 e a W1 (vence hoje: fica de fora); 20/11 e a 3a sexta (mensal, e feriado); 27/11 e a W4
        self.assertEqual(cr.semanais(date(2026, 11, 6), 2), [(date(2026, 11, 13), 11, 2), (date(2026, 11, 27), 11, 4)])

    def test_sexta_feriado_vence_no_dia_util_anterior(self):
        # 25/12/2026 (W4) e Natal: vence na quinta 24/12
        self.assertIn((date(2026, 12, 24), 12, 4), cr.semanais(date(2026, 12, 19), 3))

    def test_ticker_call_e_put(self):
        self.assertEqual(cr.ticker("BOVA", "C", 11, 168, 2), "BOVAK168W2")
        self.assertEqual(cr.ticker("BOVA", "P", 11, 166, 2), "BOVAW166W2")
        self.assertEqual(cr.ticker("BOVA", "P", 12, 160, 4), "BOVAX160W4")

    def test_listar_series_strikes_inteiros_na_faixa(self):
        s = cr.listar_series("BOVA11", 165.7, date(2026, 11, 6), 1, 0.02)  # 162.4 .. 169.0
        self.assertEqual(sorted({k for _, _, k in s.values()}), [163.0, 164.0, 165.0, 166.0, 167.0, 168.0, 169.0])
        self.assertEqual(s["BOVAW166W2"], (date(2026, 11, 13), "P", 166.0))
        self.assertEqual(len(s), 14)


class TestImplicitoDoisColetores(unittest.TestCase):
    def test_usa_a_ultima_leitura_de_cada_vencimento(self):
        con = sqlite3.connect(":memory:")
        con.executescript(co.SCHEMA)
        dia = 20763 * 86_400_000
        linha = lambda ts, sess, venc, du, iv: (ts, sess, "BOVA11", venc, du / 252, 165.0, 165.5, 0.13, iv,
                                                *([None] * 4), 10, 10, 0, 0, 0, 0, 0.0, 0.0, 0.0, 0.0)
        con.executemany("INSERT INTO opcoes_resumo VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
            (linha(dia + 16 * 3_600_000, "mt5", "2026-11-19", 9, 0.206)),
            # o RTD grava depois, so com a semanal de 3 du (abaixo do minimo de 5): antes o max(ts) geral escondia a
            # mensal e implicito() devolvia None
            (linha(dia + 16 * 3_600_000 + 2000, "rtd", "2026-11-11", 3, 0.25)),
        ])
        r = gq.implicito(con, dia, 170_000.0)
        self.assertEqual(r["vencimento"], "2026-11-19")
        self.assertAlmostEqual(r["iv"], 0.206)


if __name__ == "__main__":
    unittest.main()
