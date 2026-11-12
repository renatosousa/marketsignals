import unittest

import backfill_barras as bb

DIA = 86_400
ONTEM, HOJE = 20_768 * DIA, 20_769 * DIA


class FakeMT5:
    """Barra diaria com o volume do dia; as de 1 min so ficam completas depois de `atraso` consultas."""
    TIMEFRAME_M1, TIMEFRAME_D1 = 1, 16408

    def __init__(self, diarias, atraso, parcial=0.05):
        self.diarias, self.atraso, self.parcial, self.consultas = diarias, atraso, parcial, 0

    def copy_rates_from_pos(self, s, tf, pos, n):
        assert tf == self.TIMEFRAME_D1
        self.consultas += 1
        return [{"time": t, "tick_volume": v} for t, v in self.diarias][-n:]

    def copy_rates_range(self, s, tf, ini, fim):
        assert tf == self.TIMEFRAME_M1
        t0 = int(ini.timestamp())
        vol = dict(self.diarias)[t0]
        if self.consultas <= self.atraso and t0 == ONTEM:  # cache velho: so o comeco de ontem
            vol = int(vol * self.parcial)
        return [{"time": t0 + 60 * i, "tick_volume": vol // 4} for i in range(4)]


class TestSincronizarMT5(unittest.TestCase):
    def test_espera_as_barras_de_1min_baterem_com_a_diaria(self):
        mt5 = FakeMT5([(ONTEM, 15_940), (HOJE, 3_584)], atraso=3)
        self.assertTrue(bb.sincronizar_mt5(mt5, "DI1F29", pausa=0))
        self.assertEqual(mt5.consultas, 4)

    def test_historico_em_dia_nao_espera(self):
        mt5 = FakeMT5([(ONTEM, 15_940), (HOJE, 3_584)], atraso=0)
        self.assertTrue(bb.sincronizar_mt5(mt5, "PETR4", pausa=0))
        self.assertEqual(mt5.consultas, 1)

    def test_desiste_depois_das_tentativas(self):
        mt5 = FakeMT5([(ONTEM, 15_940), (HOJE, 3_584)], atraso=99)
        self.assertFalse(bb.sincronizar_mt5(mt5, "DI1F29", tentativas=3, pausa=0))

    def test_sem_barra_diaria_segue(self):
        self.assertTrue(bb.sincronizar_mt5(FakeMT5([], atraso=0), "X", pausa=0))


if __name__ == "__main__":
    unittest.main()
