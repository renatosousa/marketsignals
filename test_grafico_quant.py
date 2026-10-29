"""Testes do grafico_quant.py com dados sinteticos (sem banco, sem rede).

  python -m unittest test_grafico_quant -v
"""
import unittest

import numpy as np
import pandas as pd

import grafico_quant as gq


def segs(precos, vc, vv, t0=0):
    n = len(precos)
    return pd.DataFrame({"ts_ms": t0 + np.arange(n) * 1000, "last": precos, "vc": vc, "vv": vv,
                         "e": np.zeros(n), "prof": np.ones(n)})


class TestPerfilVwap(unittest.TestCase):
    def test_poc_e_area_de_valor(self):
        # 100 em 1000, 60 em 1025, 20 em 975, 10 em 1050, 10 em 950: 70% = 1000 + 1025 (160/200 = 80%)
        precos = [950, 975, 1000, 1025, 1050]
        s = segs(precos, [5, 10, 50, 30, 5], [5, 10, 50, 30, 5])
        pf = gq.perfil(s, 25.0)
        self.assertEqual(pf["poc"], 1000.0)
        self.assertEqual((pf["val"], pf["vah"]), (987.5, 1037.5))
        self.assertEqual(pf["preco"], [950.0, 975.0, 1000.0, 1025.0, 1050.0])

    def test_vwap_e_desvio(self):
        s = segs([100.0, 110.0], [1, 3], [0, 0])
        m, sd = gq.vwap(s)
        self.assertAlmostEqual(m[-1], 107.5)
        self.assertAlmostEqual(sd[-1], np.sqrt((1 * 7.5 ** 2 + 3 * 2.5 ** 2) / 4))

    def test_zona_hvn_no_pico(self):
        vol = np.full(41, 10.0)
        vol[20] = 200.0
        pf = dict(passo=50.0, preco=list(1000 + 50.0 * np.arange(41)), vc=list(vol), vv=[0.0] * 41)
        z = gq.zonas_hvn(pf)
        self.assertEqual(len(z), 1)
        self.assertEqual(z[0]["pico"], 2000.0)
        self.assertLess(z[0]["lo"], 2000.0)
        self.assertGreater(z[0]["hi"], 2000.0)


class TestAtrOu(unittest.TestCase):
    def barras_ruido(self, n=400, seed=1):
        rng = np.random.default_rng(seed)
        c = 1000 + np.cumsum(rng.normal(0, 5, n))
        return pd.DataFrame({"t": np.arange(n), "o": c, "h": c + 3, "l": c - 3, "c": c})

    def test_stops_envolvem_o_preco(self):
        b = self.barras_ruido()
        stop_c, stop_v, mom, atr = gq.atr_dinamico(b)
        ok = stop_c.notna()
        self.assertTrue(ok.sum() > 300)
        self.assertTrue((stop_c[ok] < b.c[ok]).all() and (stop_v[ok] > b.c[ok]).all())
        # distancia minima = K_MIN x ATR
        self.assertTrue(((b.c - stop_c)[ok] >= gq.K_MIN * atr[ok] - 1e-9).all())

    def test_ou_recupera_ar1(self):
        # gap AR(1) com a = 0,8 (meia-vida ~3,1 barras) somado a uma MME que acompanha o preco: a estimativa deve ficar perto
        rng = np.random.default_rng(7)
        g = np.zeros(3000)
        for i in range(1, len(g)):
            g[i] = 0.8 * g[i - 1] + rng.normal(0, 10)
        b = pd.DataFrame({"c": 1000 + g})
        gap, theta, meia = gq.reversao_media(b)
        self.assertTrue(1.0 < meia.iloc[-500:].median() < 6.0)
        self.assertTrue(theta.iloc[-500:].notna().all())


class TestOfi(unittest.TestCase):
    def test_eventos_do_topo(self):
        # bid sobe (entra qb novo), ask cai (entra qa novo), depois nada muda e o topo so troca de tamanho
        e = gq.ofi_eventos(np.array([100, 105, 105, 105.]), np.array([110, 110, 108, 108.]),
                           np.array([10, 7, 7, 12.]), np.array([10, 10, 4, 1.]))
        # t1: bid subiu -> +7; ask igual -> -10 + 10 = 0  => +7
        # t2: bid igual -> 7 - 7 = 0; ask caiu -> -4       => -4
        # t3: bid igual -> 12 - 7 = +5; ask igual -> -1 + 4 = +3 => +8
        self.assertEqual(list(e[1:]), [7.0, -4.0, 8.0])


if __name__ == "__main__":
    unittest.main()
