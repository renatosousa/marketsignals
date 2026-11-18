"""Testes do nowcast_fluxo_v2.py com banco em memoria e series sinteticas.

  python -m unittest test_nowcast_fluxo_v2 -v
"""
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import nowcast_fluxo_v2 as nf

TIPO = nf.TIPO_B3
SCHEMA = """
CREATE TABLE fluxo_diario (data_ref TEXT NOT NULL, tipo TEXT NOT NULL, compras_mil REAL, vendas_mil REAL,
    saldo_mil REAL, data_pub TEXT, PRIMARY KEY (data_ref, tipo));
CREATE TABLE features_fluxo_dia (data TEXT PRIMARY KEY, cesta_fin_liq_mi REAL, captura_vol REAL);
CREATE TABLE tryd_estrangeiro_dia (data TEXT NOT NULL, ativo TEXT NOT NULL, saldo_qtd REAL, fin_liq REAL,
    vol_fin_grupo REAL, vol_fin_total REAL, foto_ms INTEGER, PRIMARY KEY (data, ativo));
"""


def banco():
    db = sqlite3.connect(":memory:")
    db.executescript(SCHEMA)
    return db


def oficial(db, data, saldo_mi, pub):
    db.execute("INSERT INTO fluxo_diario VALUES (?,?,?,?,?,?)", (data, TIPO, 0.0, 0.0, saldo_mi * 1000, pub))


def tryd(db, data, mi):
    db.execute("INSERT INTO tryd_estrangeiro_dia VALUES (?,?,?,?,?,?,?)",
               (data, "PETR4", 0.0, mi * 1e6, mi * 1e6, mi * 1e6, 0))


def cesta(db, data, mi, cap=0.95):
    db.execute("INSERT OR REPLACE INTO features_fluxo_dia VALUES (?,?,?)", (data, mi, cap))


class TestComponentes(unittest.TestCase):
    def setUp(self):
        self.db = banco()
        # oficial conhecido: d1 publicado em d3, d2 em d4, d3 em d5, d4 em d6
        oficial(self.db, "2026-11-03", 100.0, "2026-11-05")
        oficial(self.db, "2026-11-04", 200.0, "2026-11-06")
        oficial(self.db, "2026-11-05", 300.0, "2026-11-09")
        oficial(self.db, "2026-11-06", 400.0, "2026-11-10")
        for d, v in (("2026-11-03", 110.0), ("2026-11-04", 220.0), ("2026-11-05", 330.0),
                     ("2026-11-06", 440.0)):
            tryd(self.db, d, v)
            cesta(self.db, d, 50.0)

    def test_f_ofi_e_o_ultimo_publicado_ate_o_dia(self):
        r = nf.estimar(self.db, "2026-11-05")
        self.assertEqual(r["f_ofi"], 100.0)  # d2 (200) so publica em 06/11 e d3 (300) em 09/11
        self.assertEqual(r["f_tryd"], 330.0)

    def test_correcao_ewma_usa_so_erros_publicados(self):
        r3 = nf.estimar(self.db, "2026-11-05")
        self.assertAlmostEqual(r3["f_tryd_corr"], 330.0 - 10.0)  # so o erro de d1 (110-100)
        r4 = nf.estimar(self.db, "2026-11-06")
        # d1 (10) e d2 (20) publicados; EWMA recursiva span 10: 10, depois 10*(9/11)+20*(2/11)
        self.assertAlmostEqual(r4["f_tryd_corr"], 440.0 - (10.0 * 9 / 11 + 20.0 * 2 / 11), places=6)

    def test_cesta_so_com_captura_minima(self):
        cesta(self.db, "2026-11-06", 999.0, cap=0.5)
        self.assertIsNone(nf.estimar(self.db, "2026-11-06")["f_cesta"])

    def test_causalidade_do_rotulo_d_mais_2(self):
        antes = nf.estimar(self.db, "2026-11-05")
        # oficial de d3 (publicado em 09/11) e de dias futuros nao podem entrar no nowcast de 05/11
        self.db.execute("UPDATE fluxo_diario SET saldo_mil=9999999 WHERE data_ref='2026-11-05'")
        self.db.execute("INSERT INTO fluxo_diario VALUES ('2026-11-09', ?, 0, 0, 9999999, '2026-11-11')", (TIPO,))
        depois = nf.estimar(self.db, "2026-11-05")
        for k in ("estimativa_mi", "f_ofi", "f_tryd_corr", "n_obs"):
            self.assertEqual(antes[k], depois[k], k)

    def test_pesos_somam_um_e_renormalizam_ausente(self):
        est, efetivos = nf._normalizar([100.0, None, 50.0], nf.W0)
        self.assertAlmostEqual(est, (100 * 0.4 + 50 * 0.2) / 0.6)
        self.assertAlmostEqual(sum(efetivos), 1.0)
        self.assertEqual(efetivos[1], 0.0)
        est, efetivos = nf._normalizar([10.0, 20.0, 30.0], nf.W0)
        self.assertAlmostEqual(sum(efetivos), 1.0)
        self.assertAlmostEqual(est, 10 * 0.4 + 20 * 0.4 + 30 * 0.2)

    def test_n_obs_conta_so_dias_completos_anteriores(self):
        # em 06/11 o oficial de d3 so publica em 09/11: nenhuma observacao completa ainda
        self.assertEqual(nf.estimar(self.db, "2026-11-06")["n_obs"], 0)
        self.assertEqual(nf.estimar(self.db, "2026-11-03")["n_obs"], 0)
        # em 09/11 o oficial de d3 ja saiu e d3 tem os tres componentes: uma observacao completa
        of, linhas = nf._montar(self.db)
        w, n = nf._pesos_do_dia("2026-11-09", linhas, of)
        self.assertEqual(n, 1)
        self.assertAlmostEqual(float(sum(w)), 1.0, places=9)

    def test_historico_e_walk_forward(self):
        h = nf.historico(self.db)
        dias = [l["dia"] for l in h]
        self.assertEqual(dias, sorted(dias))
        self.assertEqual(len(h), 4)
        # o nowcast de d3 nao conhece o oficial de d3
        self.assertEqual(next(l for l in h if l["dia"] == "2026-11-05")["f_ofi"], 100.0)


class TestPesos(unittest.TestCase):
    def test_pesos_empiricos_soma_um(self):
        X = [[100.0, 90.0, 10.0], [200.0, 210.0, 20.0], [300.0, 290.0, 30.0], [400.0, 410.0, 40.0]]
        y = [100.0, 200.0, 300.0, 400.0]
        w = nf._ajustar_pesos(X, y)
        self.assertAlmostEqual(float(sum(w)), 1.0, places=6)
        self.assertTrue(all(v >= -1e-9 for v in w))
        self.assertAlmostEqual(float(X[0][0] * w[0] + X[0][1] * w[1] + X[0][2] * w[2]), y[0], delta=30)

    def test_sem_amostra_volta_ao_prior(self):
        self.assertTrue((nf._ajustar_pesos([], []) == nf.W0).all())

    def test_encolhimento_com_n(self):
        linhas = [{"dia": f"2026-11-{d:02d}", "f_ofi": 100.0, "f_tryd": 100.0, "f_tryd_corr": 100.0,
                   "f_cesta": 100.0} for d in range(3, 8)]
        of = {f"2026-11-{d:02d}": (100.0, f"2026-11-{d + 2:02d}") for d in range(3, 8)}
        w, n = nf._pesos_do_dia("2026-11-08", linhas, of)
        self.assertEqual(n, 4)  # d3..d6 com oficial publicado ate 08/11 (d7 publica em 09/11)
        self.assertAlmostEqual(float(sum(w)), 1.0, places=9)
        self.assertTrue(np_allclose(w, nf.W0, atol=1e-6))  # componentes iguais ao alvo: w_emp indefinido -> prior


def np_allclose(a, b, atol=1e-6):
    import numpy as np
    return np.allclose(a, b, atol=atol)


class TestResumoEAvaliar(unittest.TestCase):
    def test_resumo(self):
        r = nf._resumo([(10.0, 10.0), (-5.0, -10.0), (3.0, -1.0)])
        self.assertEqual((r["n"], r["sinal_certo"]), (3, 2))
        self.assertIsNone(r["postos"])
        self.assertAlmostEqual(r["erro_abs_medio"], 3.0)
        self.assertAlmostEqual(r["vies"], 3.0)
        r4 = nf._resumo([(1, 1), (2, 2), (3, 3), (4, 4)])
        self.assertAlmostEqual(r4["postos"], 1.0)

    def test_regra_do_dia_anterior(self):
        db = banco()
        for d, s in (("2026-11-03", 10.0), ("2026-11-04", -20.0), ("2026-11-05", 30.0)):
            oficial(db, d, s, "2026-11-20")
        r = nf.avaliar(db)
        self.assertEqual(r["dia_anterior"]["n"], 2)
        self.assertEqual(r["dia_anterior"]["sinal_certo"], 0)


class TestAtualizar(unittest.TestCase):
    def test_grava_idempotente(self):
        with tempfile.TemporaryDirectory() as d:
            arq = str(Path(d) / "teste.db")
            con = sqlite3.connect(arq)
            con.executescript(SCHEMA)
            for i, dia in enumerate(("2026-11-03", "2026-11-04", "2026-11-05")):
                pub = ("2026-11-05", "2026-11-06", "2026-11-09")[i]
                oficial(con, dia, 100.0 + i, pub)
                tryd(con, dia, 100.0 + i)
                cesta(con, dia, 100.0 + i)
            con.commit()
            con.close()
            self.assertEqual(nf.atualizar(arq), 3)
            self.assertEqual(nf.atualizar(arq), 3)
            con = sqlite3.connect(arq)
            try:
                self.assertEqual(con.execute("SELECT count(*) FROM nowcast_fluxo").fetchone()[0], 3)
                linha = con.execute("SELECT dia, f_ofi, f_cesta, n_obs FROM nowcast_fluxo "
                                    "ORDER BY dia DESC LIMIT 1").fetchone()
            finally:
                con.close()
            self.assertEqual(linha[0], "2026-11-05")
            self.assertEqual(linha[1], 100.0)
            self.assertEqual(linha[2], 102.0)


if __name__ == "__main__":
    unittest.main()
