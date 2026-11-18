"""Testes do correlacoes_v3.py com banco em memoria/temporario e series sinteticas.

  python -m unittest test_correlacoes_v3 -v
"""
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

import correlacoes as corr
import correlacoes_v3 as c3

D = c3.D
DIA = 20700  # 2026-09-01
MIN_INI, MIN_FIM = corr.SESSAO_FUTUROS
SCHEMA = """
CREATE TABLE snapshots (ts_ms INTEGER NOT NULL, symbol TEXT NOT NULL, mid REAL, spread REAL);
CREATE TABLE macro_cotacoes (ts_ms INTEGER NOT NULL, symbol TEXT NOT NULL, mid REAL);
CREATE TABLE barras_1m (symbol TEXT NOT NULL, ts_ms INTEGER NOT NULL, close REAL);
""" + corr.SCHEMA + c3.SCHEMA


def banco():
    db = sqlite3.connect(":memory:")
    db.executescript(SCHEMA)
    return db


def serie_precos(rng, n, sigma=0.0006, inicio=100000.0):
    return inicio * np.exp(np.cumsum(rng.normal(0.0, sigma, n)))


def gravar_dia(db, dia, win=None, wdo=None, snapshots=True):
    """Grava o dia inteiro (09:05-17:55) em snapshots/macro e barras."""
    minutos = np.arange(MIN_INI, MIN_FIM + 1)
    if win is not None:
        if snapshots:
            db.executemany("INSERT INTO snapshots (ts_ms, symbol, mid, spread) VALUES (?,?,?,?)",
                           [(dia * D + int(m) * 60_000, "WIN$", float(p), 5.0) for m, p in zip(minutos, win)])
        db.executemany("INSERT INTO barras_1m (symbol, ts_ms, close) VALUES (?,?,?)",
                       [("WIN$", dia * D + int(m) * 60_000, float(p)) for m, p in zip(minutos, win)])
    if wdo is not None:
        db.executemany("INSERT INTO macro_cotacoes (ts_ms, symbol, mid) VALUES (?,?,?)",
                       [(dia * D + int(m) * 60_000, "WDO$", float(p)) for m, p in zip(minutos, wdo)])
        db.executemany("INSERT INTO barras_1m (symbol, ts_ms, close) VALUES (?,?,?)",
                       [("WDO$", dia * D + int(m) * 60_000, float(p)) for m, p in zip(minutos, wdo)])


class TestWinsor(unittest.TestCase):
    def test_outlier_e_limitado_e_serie_constante_fica(self):
        rng = np.random.default_rng(1)
        x = rng.normal(0.0, 0.001, 500)
        x[10] = 0.5
        w = c3.winsorizar(x)
        self.assertLess(w[10], 0.5)
        self.assertLessEqual(np.max(w), np.median(x) + c3.MAD_K * 1.4826 * np.median(np.abs(x - np.median(x))))
        const = np.full(100, 0.01)
        self.assertTrue(np.array_equal(c3.winsorizar(const), const))

    def test_huber_resiste_ao_outlier_e_spearman_ve_a_ordem(self):
        rng = np.random.default_rng(2)
        x = rng.normal(0.0, 0.001, 400)
        y = 0.8 * x + rng.normal(0.0, 0.0002, 400)
        y[20] = 0.5  # um minuto extremo
        pearson = float(np.corrcoef(y, x)[0, 1])
        huber, n = c3.correlacao_huber(pd.Series(y), pd.Series(x))
        sp, _ = c3.correlacao_spearman(pd.Series(y), pd.Series(x))
        self.assertEqual(n, 400)
        self.assertGreater(huber, 0.8)
        self.assertLess(abs(huber - 0.8), abs(pearson - 0.8))
        self.assertGreater(sp, 0.7)

    def test_amostra_curta_e_serie_constante(self):
        self.assertIsNone(c3.correlacao_huber(pd.Series(np.arange(59.0)), pd.Series(np.arange(59.0)))[0])
        self.assertIsNone(c3.correlacao_huber(pd.Series(np.zeros(100)), pd.Series(np.arange(100.0)))[0])
        self.assertIsNone(c3.correlacao_spearman(pd.Series(np.zeros(100)), pd.Series(np.arange(100.0)))[0])


class TestReferenciaRegime(unittest.TestCase):
    def registros(self, corrs, vols):
        return [{"data": f"2026-09-{i + 1:02d}", "corr_huber": c, "rv_dia_win": v}
                for i, (c, v) in enumerate(zip(corrs, vols))]

    def test_balde_e_estatisticas(self):
        corrs = [(-0.4 + 0.01 * i) if i % 2 == 0 else (0.3 - 0.01 * i) for i in range(40)]
        vols = [1.0 if i % 2 == 0 else 2.0 for i in range(40)]
        ref = c3._ref_de(self.registros(corrs, vols), vol_hoje=0.9, n_dias=40, minimo=5)
        self.assertEqual(ref["balde"], "baixa")
        self.assertEqual(ref["n_dias"], 20)
        self.assertAlmostEqual(ref["media"], float(np.mean(corrs[::2])))
        self.assertAlmostEqual(ref["desvio"], float(np.std(corrs[::2], ddof=1)))
        self.assertLess(ref["p10"], ref["media"])
        self.assertGreater(ref["p90"], ref["media"])

    def test_minimo_de_15_e_sem_vol_hoje(self):
        corrs = [0.1] * 20
        vols = [1.0] * 10 + [2.0] * 10
        self.assertIsNone(c3._ref_de(self.registros(corrs, vols), vol_hoje=0.9, n_dias=20, minimo=15))
        self.assertIsNone(c3._ref_de(self.registros(corrs, vols), vol_hoje=None, n_dias=20, minimo=5))

    def test_z_regime(self):
        ref = {"media": 0.1, "desvio": 0.2, "p10": -0.1, "p90": 0.3, "n_dias": 20, "balde": "alta"}
        self.assertAlmostEqual(c3.z_regime(0.5, ref), 2.0)
        self.assertIsNone(c3.z_regime(None, ref))
        self.assertIsNone(c3.z_regime(0.5, None))


class TestFDR(unittest.TestCase):
    def test_benjamini_hochberg(self):
        q = c3.benjamini_hochberg([0.01, 0.04, 0.03, None, 0.2])
        self.assertAlmostEqual(q[0], 0.04)
        self.assertAlmostEqual(q[1], 0.04 * 4 / 3)
        self.assertAlmostEqual(q[2], 0.04 * 4 / 3)
        self.assertIsNone(q[3])
        self.assertAlmostEqual(q[4], 0.2)

    def test_p_valor_bilateral(self):
        self.assertAlmostEqual(c3.p_valor(0.0), 1.0)
        self.assertAlmostEqual(c3.p_valor(1.959963984540054), 0.05, places=6)
        self.assertIsNone(c3.p_valor(None))


class TestCorrelDoDia(unittest.TestCase):
    def test_pearson_huber_spearman_e_rv(self):
        db = banco()
        rng = np.random.default_rng(3)
        win = serie_precos(rng, MIN_FIM - MIN_INI + 1)
        wdo = serie_precos(rng, MIN_FIM - MIN_INI + 1)
        gravar_dia(db, DIA, win, wdo)
        linhas = c3.correl_do_dia(db, DIA, ao_vivo=False)
        l = next(x for x in linhas if (x["y"], x["x"]) == ("WIN$", "WDO$"))
        self.assertGreaterEqual(l["n"], 500)
        self.assertIsNotNone(l["corr"])
        self.assertIsNotNone(l["corr_huber"])
        self.assertIsNotNone(l["spearman"])
        self.assertGreater(l["rv_dia_win"], 0)
        self.assertAlmostEqual(l["corr"], float(np.corrcoef(
            corr.retornos(corr.serie_1m(db, "WIN$", DIA, ao_vivo=False), "WIN$"),
            corr.retornos(corr.serie_1m(db, "WDO$", DIA, ao_vivo=False), "WDO$"))[0, 1]))


class TestAtualizarEHistorico(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = str(Path(self.dir.name) / "teste.db")
        self.hoje = (date.today() - date(1970, 1, 1)).days
        self.dias = [self.hoje - i for i in range(20, 0, -1)]
        con = sqlite3.connect(self.db)
        con.executescript(SCHEMA)
        rng = np.random.default_rng(4)
        for d in self.dias:
            win = serie_precos(rng, MIN_FIM - MIN_INI + 1)
            wdo = serie_precos(rng, MIN_FIM - MIN_INI + 1)
            gravar_dia(con, d, win, wdo, snapshots=False)
        con.commit()
        con.close()

    def tearDown(self):
        self.dir.cleanup()

    def test_atualizar_idempotente_e_tabela(self):
        self.assertEqual(c3.atualizar(self.db, dias=40), len(self.dias))
        con = sqlite3.connect(self.db)
        try:
            n1 = con.execute("SELECT count(*) FROM correl_diaria_v3").fetchone()[0]
            linha = con.execute("SELECT data, corr_huber, spearman, n, rv_dia_win FROM correl_diaria_v3 "
                                "WHERE y='WIN$' AND x='WDO$' ORDER BY data DESC LIMIT 1").fetchone()
        finally:
            con.close()
        self.assertGreater(n1, 0)
        self.assertIsNotNone(linha[1])
        self.assertIsNotNone(linha[2])
        self.assertGreaterEqual(linha[3], 60)
        self.assertGreater(linha[4], 0)
        self.assertEqual(c3.atualizar(self.db, dias=40), 2)  # refaz os 2 pregoes mais recentes
        con = sqlite3.connect(self.db)
        try:
            self.assertEqual(con.execute("SELECT count(*) FROM correl_diaria_v3").fetchone()[0], n1)
        finally:
            con.close()

    def test_ref_regime_le_a_tabela(self):
        c3.atualizar(self.db, dias=40)
        con = sqlite3.connect(self.db)
        try:
            vol = c3.vol_do_dia(con, self.dias[-1])
            self.assertGreater(vol, 0)
            ref = c3.ref_regime(con, "WIN$", "WDO$", self.hoje, vol, n_dias=20, minimo=5)
            self.assertIsNotNone(ref)
            self.assertEqual(ref["balde"], "alta" if vol >= ref["vol_mediana"] else "baixa")
            self.assertGreaterEqual(ref["n_dias"], 5)
            self.assertIn("media", ref)
            self.assertIn("desvio", ref)
        finally:
            con.close()


class TestApi(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = str(Path(self.dir.name) / "teste.db")
        self.hoje = (date.today() - date(1970, 1, 1)).days
        dias = [self.hoje - i for i in range(30, 0, -1)]
        con = sqlite3.connect(self.db)
        con.executescript(SCHEMA)
        rng = np.random.default_rng(5)
        for d in dias:
            win = serie_precos(rng, MIN_FIM - MIN_INI + 1)
            wdo = serie_precos(rng, MIN_FIM - MIN_INI + 1)
            gravar_dia(con, d, win, wdo, snapshots=False)
        # hoje: WIN em snapshots (ao vivo) e WDO em macro
        win = serie_precos(rng, MIN_FIM - MIN_INI + 1)
        wdo = serie_precos(rng, MIN_FIM - MIN_INI + 1)
        gravar_dia(con, self.hoje, win, wdo, snapshots=True)
        con.commit()
        con.close()

    def tearDown(self):
        self.dir.cleanup()

    def test_formato_e_chaves_novas(self):
        con = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
        try:
            agora = self.hoje * D + MIN_FIM * 60_000
            api = c3.correlacoes_v3_api(con, agora, "pregao")
        finally:
            con.close()
        for k in ("janela", "alvo", "t0", "t1", "n_minutos", "tabela", "principais", "movel", "matriz",
                  "decomposicao", "regime", "fdr_alpha", "n_hipoteses"):
            self.assertIn(k, api)
        self.assertEqual(api["alvo"], "WIN")
        self.assertTrue(api["tabela"])
        l = next(x for x in api["tabela"] if x["x"] == "WDO$")
        for k in ("corr_huber", "spearman", "ref_regime", "z_regime", "p_valor", "q_valor", "alerta"):
            self.assertIn(k, l)
        self.assertIsNotNone(l["corr_huber"])
        self.assertIsNotNone(l["spearman"])
        self.assertIsNotNone(l["q_valor"])
        self.assertFalse(l["alerta"])  # n < 120 nao alerta


if __name__ == "__main__":
    unittest.main()
