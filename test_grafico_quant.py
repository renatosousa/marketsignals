"""Testes do grafico_quant.py com dados sinteticos (sem banco, sem rede).

  python -m unittest test_grafico_quant -v
"""
import sqlite3
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


def banco_opcoes(manual=(), b3=()):
    """SQLite em memoria com as tabelas de posicao: manual = [(capturado_em, venc DD/MM/AAAA, tipo, strike, descob.)],
    b3 = [(sessao, venc AAAA-MM-DD, tipo, strike, descob.)]."""
    con = sqlite3.connect(":memory:")
    cols = "subjacente, vencimento, tipo, strike, coberto, travado, descoberto, vol_impl_pct"
    con.execute(f"CREATE TABLE opcoes_manual_series (capturado_em, {cols})")
    con.execute(f"CREATE TABLE b3_posicoes_series (sessao, {cols})")
    con.executemany("INSERT INTO opcoes_manual_series VALUES (?,'BOVA11',?,?,?,0,0,?,0.25)", manual)
    con.executemany("INSERT INTO b3_posicoes_series VALUES (?,'BOVA11',?,?,?,0,0,?,0.25)", b3)
    return con


class TestOpcoesRelacionados(unittest.TestCase):
    def pos(self):
        sr = lambda v, t, k, d, tr=0.0: ("2026-10-07T09:54:12", v, t, k, d + tr)
        con = banco_opcoes(manual=[
            sr("09/10/2026", "P", 195.0, 300.0), sr("09/10/2026", "P", 205.0, 900.0),   # 205 esta acima do spot
            sr("09/10/2026", "C", 210.0, 500.0, 100.0), sr("09/10/2026", "C", 190.0, 800.0),
            sr("16/10/2026", "P", 199.0, 9999.0),                                      # vencimento seguinte: fora
            sr("02/10/2026", "C", 200.0, 9999.0)])                                     # ja venceu: fora
        return gq.posicoes(con, "2026-10-07")

    def test_niveis_opcoes(self):
        o = gq.niveis_opcoes(self.pos(), spot=200.0, k_win=1000.0)
        self.assertEqual(o["vencimento"], "2026-10-09")
        self.assertEqual([x["k"] for x in o["suporte"]], [195.0])      # so puts abaixo do spot
        self.assertEqual([x["k"] for x in o["resistencia"]], [210.0])  # so calls acima
        self.assertEqual(o["suporte"][0]["pts"], 195_000)
        self.assertEqual(o["ima"], 205_000)                            # 900 de put em 205 > 600 de call em 210
        self.assertEqual(len(o["strikes"]), 4)

    def test_posicoes_sem_olhar_o_futuro(self):
        con = banco_opcoes(manual=[("2026-10-07T09:54:12", "09/10/2026", "C", 200.0, 1.0),
                                   ("2026-10-09T10:00:00", "16/10/2026", "C", 200.0, 1.0)],
                           b3=[("2026-10-08", "2026-10-16", "P", 190.0, 1.0)])
        self.assertEqual(gq.posicoes(con, "2026-10-08")["ref"], "2026-10-07")  # a B3 de 08/10 so sai a noite
        p = gq.posicoes(con, "2026-10-09")                                      # a manual de 09/10 vale no dia
        self.assertEqual((p["fonte"], p["ref"]), ("manual", "2026-10-09"))
        self.assertIsNone(gq.posicoes(con, "2026-10-06"))


class TestGex(unittest.TestCase):
    def test_gama_e_delta(self):
        g, d = gq.bs_gama_delta(np.array([True, False]), 100.0, np.array([100.0, 100.0]), 1.0, 0.0, 0.2)
        self.assertAlmostEqual(g[0], 0.0198476, places=6)  # phi(0,1) / (100 x 0,2)
        self.assertAlmostEqual(d[0], 0.5398278, places=6)
        self.assertAlmostEqual(d[1], 0.5398278 - 1, places=6)

    def serie(self, linhas, mult=1.0, fator=1.0):
        return dict(fonte="b3", ref="2026-10-07", series=pd.DataFrame(
            [dict(venc="2026-11-19", tipo=t, strike=k, oi=oi, descoberto=0.0, iv=0.25, mult=mult, fator=fator)
             for t, k, oi in linhas]))

    def test_multiplicador_e_forward(self):
        base = gq.gex(self.serie([("C", 100.0, 1e3)]), 100.0, "2026-10-07", 0.0)
        x50 = gq.gex(self.serie([("C", 100.0, 1e3)], mult=50.0), 100.0, "2026-10-07", 0.0)
        self.assertAlmostEqual(x50["gex_total"] / base["gex_total"], 50.0)
        # fator 1,02: o forward do vencimento e 102 com spot 100 -> igual a calcular com spot 102 e fator 1
        a = gq.gex(self.serie([("C", 100.0, 1e3)], fator=1.02), 100.0, "2026-10-07", 0.0)
        b = gq.gex(self.serie([("C", 100.0, 1e3)]), 102.0, "2026-10-07", 0.0)
        self.assertAlmostEqual(a["gex_total"], b["gex_total"])

    def test_flip_entre_put_e_call_simetricas(self):
        # puts em 90 e calls em 110 com o mesmo OI: o GEX total troca de sinal perto do meio
        x = gq.gex(self.serie([("P", 90.0, 1e6), ("C", 110.0, 1e6), ("C", 105.0, 2e5), ("P", 95.0, 1e5)]),
                   100.0, "2026-10-07", 0.0)
        self.assertTrue(97.0 < x["flip"] < 103.0, x["flip"])
        self.assertEqual([w["k"] for w in x["call_walls"]], [110.0, 105.0])
        self.assertEqual([w["k"] for w in x["put_walls"]], [90.0, 95.0])
        self.assertLess(x["put_walls"][0]["gex"], 0)
        self.assertGreater(x["dex_total"], 0)  # mais delta de call (OI 1,2 mi) do que de put

    def test_sem_put_nao_ha_flip(self):
        x = gq.gex(self.serie([("C", 100.0, 1e6)]), 100.0, "2026-10-07", 0.0)
        self.assertIsNone(x["flip"])
        self.assertGreater(x["gex_total"], 0)
        self.assertEqual(x["put_walls"], [])

    def test_sorriso_ao_vivo_substitui_a_iv_da_fonte(self):
        p = self.serie([("C", 100.0, 1e6)])
        ivs = pd.DataFrame({"venc": ["2026-11-19"] * 2, "strike": [95.0, 105.0], "iv": [0.30, 0.50]})
        a = gq.gex(p, 100.0, "2026-10-07", 0.0)["gex_total"]
        b = gq.gex(p, 100.0, "2026-10-07", 0.0, ivs)["gex_total"]  # IV interpolada 0,40 > 0,25: menos gama no ATM
        self.assertAlmostEqual(b / a, 0.25 / 0.40, delta=0.03)


class TestWdo(unittest.TestCase):
    def test_segundos_sem_livro(self):
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE macro_cotacoes (ts_ms, symbol, bid, ask, last)")
        con.execute("CREATE TABLE agressao_1min (ts_ms, symbol, vol_compra, vol_venda, preco)")
        d = 20754 * gq.DIA_MS
        con.executemany("INSERT INTO macro_cotacoes VALUES (?, 'WDO$', ?, ?, ?)",
                        [(d + gq.H(9, 1), 5350.0, 5350.5, 5350.0), (d + gq.H(9, 2), 5351.0, 5350.0, 5351.0)])  # 2a: leilao
        con.execute("INSERT INTO agressao_1min VALUES (?, 'WDO$', 10, 4, 5350.5)", (d + gq.H(9, 1),))
        s = gq.segundos_sem_livro(con, d)
        self.assertEqual(list(s["last"]), [5350.0, 5350.5])          # o livro cruzado fica fora
        self.assertEqual(int(s.ts_ms.iloc[1]), d + gq.H(9, 2) - 1)   # volume no fim do minuto
        self.assertEqual((s.vc.sum(), s.vv.sum()), (10.0, 4.0))
        b = gq.barras(s, 1)
        self.assertEqual((b.v.iloc[0], b.delta.iloc[0]), (14.0, 6.0))

    def test_posicoes_dolar_so_d_menos_1(self):
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE b3_dolar_series (sessao, vencimento, tipo, strike, oi, iv, mult, fator)")
        con.executemany("INSERT INTO b3_dolar_series VALUES (?, ?, 'C', 5400, ?, 0.15, 50, 1.0)",
                        [("2026-10-27", "2026-11-03", 10), ("2026-10-28", "2026-11-03", 20),
                         ("2026-10-28", "2026-10-01", 30)])                      # vencido: fora
        p = gq.posicoes_dolar(con, "2026-10-29")
        self.assertEqual((p["ref"], list(p["series"].oi)), ("2026-10-28", [20.0]))
        self.assertEqual(gq.posicoes_dolar(con, "2026-10-28")["ref"], "2026-10-27")  # a de 28/10 sai a noite
        self.assertTrue(p["series"].descoberto.isna().all())


class TestRelacionados(unittest.TestCase):
    def test_razao_e_velas(self):
        win = pd.DataFrame({"ts_ms": [0, 1000, 2000], "last": [100_000.0, 101_000.0, 102_000.0]})
        bova = pd.DataFrame({"ts_ms": [0, 2000], "last": [100.0, 100.0]})
        self.assertAlmostEqual(gq.razao(win, bova), 1010.0)  # t=1000 usa o BOVA11 de t=0 (ate 2 s antes)
        v = gq.velas(pd.DataFrame({"ts_ms": [0, 30_000, 70_000], "last": [1.0, 3.0, 2.0]}), 1,
                     pd.Series([0, 60_000, 120_000]))
        self.assertEqual(list(v.o[:2]), [1.0, 2.0])
        self.assertEqual(list(v.h[:2]), [3.0, 2.0])
        self.assertTrue(np.isnan(v.c.iloc[2]))


if __name__ == "__main__":
    unittest.main()
