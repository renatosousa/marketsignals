"""Testes do correlacoes.py com banco SQLite temporario e series sinteticas.

  python -m unittest test_correlacoes -v
"""
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

import correlacoes as c

D = 86_400_000
DIA = 20700
MIN_INI = 9 * 60 + 5
MIN_FIM = 17 * 60 + 55
MIN_ACOES_INI = 10 * 60 + 5
MIN_ACOES_FIM = 16 * 60 + 55

SCHEMA_MINIMO = """
CREATE TABLE snapshots (ts_ms INTEGER NOT NULL, symbol TEXT NOT NULL, mid REAL, spread REAL);
CREATE TABLE macro_cotacoes (ts_ms INTEGER NOT NULL, symbol TEXT NOT NULL, mid REAL);
CREATE TABLE barras_1m (symbol TEXT NOT NULL, ts_ms INTEGER NOT NULL, close REAL);
"""


def banco():
    """Banco em memoria com as tabelas minimas usadas."""
    db = sqlite3.connect(":memory:")
    db.executescript(SCHEMA_MINIMO)
    return db


def macro(db, simbolo, dia, pontos):
    """Grava cotacoes macro: pontos = [(minuto do dia, preco)]."""
    db.executemany("INSERT INTO macro_cotacoes (ts_ms, symbol, mid) VALUES (?,?,?)",
                   [(dia * D + m * 60_000, simbolo, v) for m, v in pontos])


def barras(db, simbolo, dia, minutos, precos):
    db.executemany("INSERT INTO barras_1m (symbol, ts_ms, close) VALUES (?,?,?)",
                   [(simbolo, dia * D + int(m) * 60_000, float(p)) for m, p in zip(minutos, precos)])


class TestSessao(unittest.TestCase):
    def test_taxa_e_sessao(self):
        self.assertTrue(c.eh_taxa("DI1F29"))
        self.assertTrue(c.eh_taxa("DAPK33"))
        self.assertFalse(c.eh_taxa("WDO$"))
        self.assertEqual(c.sessao("WIN$"), c.SESSAO_FUTUROS)
        self.assertEqual(c.sessao("DI1F29"), c.SESSAO_FUTUROS)
        self.assertEqual(c.sessao("BIT$"), c.SESSAO_FUTUROS)
        self.assertEqual(c.sessao("PETR4"), c.SESSAO_ACOES)
        self.assertEqual(c.sessao("SPX_USD"), c.SESSAO_ACOES)  # ETF: sessao das acoes


class TestSerie1m(unittest.TestCase):
    def test_acao_corta_fora_da_sessao_e_preenche_ate_5_min(self):
        db = banco()
        macro(db, "PETR4", DIA, [(9 * 60, 99), (10 * 60, 1), (MIN_ACOES_INI, 10), (MIN_ACOES_INI + 1, 11),
                                 (MIN_ACOES_INI + 8, 20), (17 * 60, 99), (MIN_ACOES_FIM, 30)])
        s = c.serie_1m(db, "PETR4", DIA)
        self.assertEqual((int(s.index[0]), int(s.index[-1])), (MIN_ACOES_INI, MIN_ACOES_FIM))
        self.assertEqual(len(s), MIN_ACOES_FIM - MIN_ACOES_INI + 1)
        self.assertNotIn(10 * 60, s.index)   # 10:00 nao entra
        self.assertNotIn(17 * 60, s.index)   # 17:00 nao entra
        self.assertEqual(s[MIN_ACOES_INI], 10)
        self.assertEqual(s[MIN_ACOES_INI + 5], 11)      # ffill de 5 minutos
        self.assertEqual(s[MIN_ACOES_INI + 6], 11)
        self.assertTrue(np.isnan(s[MIN_ACOES_INI + 7]))  # sexto minuto sem dado fica vazio
        self.assertEqual(s[MIN_ACOES_INI + 8], 20)
        self.assertEqual(s[MIN_ACOES_FIM], 30)

    def test_win_ao_vivo_exige_spread_positivo(self):
        db = banco()
        db.executemany("INSERT INTO snapshots (ts_ms, symbol, mid, spread) VALUES (?,?,?,?)",
                       [(DIA * D + (9 * 60 + 10) * 60_000, "WIN$", 1.0, 0.0),
                        (DIA * D + (9 * 60 + 10) * 60_000 + 30_000, "WIN$", 100.0, 0.5)])
        s = c.serie_1m(db, "WIN$", DIA, ao_vivo=True)
        self.assertEqual(s[9 * 60 + 10], 100.0)

    def test_sintetico_e_a_razao_minuto_a_minuto(self):
        db = banco()
        macro(db, "IVVB11", DIA, [(MIN_ACOES_INI, 200.0), (MIN_ACOES_INI + 1, 210.0)])
        macro(db, "WDO$", DIA, [(MIN_ACOES_INI, 5.0), (MIN_ACOES_INI + 1, 5.0)])
        s = c.serie_1m(db, "SPX_USD", DIA)
        self.assertAlmostEqual(s[MIN_ACOES_INI], 40.0)
        self.assertAlmostEqual(s[MIN_ACOES_INI + 1], 42.0)
        self.assertEqual((int(s.index[0]), int(s.index[-1])), (MIN_ACOES_INI, MIN_ACOES_FIM))

    def test_historico_prefere_barras_ao_macro(self):
        db = banco()
        macro(db, "DI1F29", DIA, [(MIN_INI, 13.0)])
        barras(db, "DI1F29", DIA, [MIN_INI, MIN_INI + 1], [14.0, 14.1])
        s = c.serie_1m(db, "DI1F29", DIA, ao_vivo=False)
        self.assertAlmostEqual(s[MIN_INI], 14.0)
        self.assertAlmostEqual(s[MIN_INI + 1], 14.1)

    def test_sem_dados_series_vazia(self):
        self.assertTrue(c.serie_1m(banco(), "VALE3", DIA).empty)


class TestRetornos(unittest.TestCase):
    def test_taxa_em_pontos_base(self):
        s = pd.Series([13.5, 13.6, 13.55], index=[600, 601, 602])
        r = c.retornos(s, "DI1F29")
        self.assertAlmostEqual(r.iloc[0], 10.0)
        self.assertAlmostEqual(r.iloc[1], -5.0)

    def test_preco_em_log_retorno(self):
        r = c.retornos(pd.Series([100.0, 110.0], index=[600, 601]), "PETR4")
        self.assertAlmostEqual(r.iloc[0], np.log(1.1))

    def test_sem_retorno_atravessando_buraco_maior_que_5_min(self):
        r = c.retornos(pd.Series([1.0, 2.0, 3.0, 4.0], index=[600, 601, 607, 608]), "PETR4")
        self.assertEqual(list(r.index), [601, 608])
        self.assertAlmostEqual(r[608], np.log(4 / 3))

    def test_buraco_da_serie_1m_nao_gera_retorno(self):
        db = banco()
        macro(db, "PETR4", DIA, [(MIN_ACOES_INI, 10.0), (MIN_ACOES_INI + 7, 11.0)])
        r = c.retornos(c.serie_1m(db, "PETR4", DIA), "PETR4")
        self.assertNotIn(MIN_ACOES_INI + 7, r.index)


class TestPar(unittest.TestCase):
    def test_amostra_curta_nao_calcula(self):
        rng = np.random.default_rng(0)
        d = c.par(pd.Series(rng.normal(size=59)), pd.Series(rng.normal(size=59)))
        self.assertEqual(d["n"], 59)
        self.assertIsNone(d["corr"])
        self.assertIsNone(d["ic_lo"])
        self.assertIsNone(d["beta"])

    def test_ic_contem_a_correlacao_e_beta_recuperado(self):
        rng = np.random.default_rng(1)
        x = rng.normal(size=500)
        y = 0.5 * x + rng.normal(scale=0.1, size=500)
        d = c.par(pd.Series(y), pd.Series(x))
        self.assertAlmostEqual(d["corr"], float(np.corrcoef(y, x)[0, 1]))
        self.assertLess(d["ic_lo"], d["corr"])
        self.assertGreater(d["ic_hi"], d["corr"])
        self.assertAlmostEqual(d["beta"], 0.5, delta=0.05)
        self.assertEqual(d["n"], 500)

    def test_alinha_so_minutos_com_os_dois_retornos(self):
        a = pd.Series([0.1, 0.2, 0.3], index=[1, 2, 3])
        b = pd.Series([0.1, np.nan, 0.3], index=[1, 2, 3])
        self.assertEqual(c.par(a, b)["n"], 2)

    def test_variancia_zero_do_x(self):
        d = c.par(pd.Series(np.arange(100.0)), pd.Series(np.zeros(100)))
        self.assertEqual(d["n"], 100)
        self.assertIsNone(d["corr"])
        self.assertIsNone(d["beta"])


class TestReferencia(unittest.TestCase):
    def base(self, n, invertido=False, corrs=None):
        db = sqlite3.connect(":memory:")
        db.executescript(c.SCHEMA)
        corrs = corrs if corrs is not None else [-0.4 + 0.01 * i for i in range(n)]
        inicio = date(2026, 9, 1)
        rows = []
        for i, r in enumerate(corrs):
            data = (inicio + timedelta(days=i)).isoformat()
            y, x = ("WDO$", "WIN$") if invertido else ("WIN$", "WDO$")
            rows.append((data, y, x, r, 0.0, 500, "2026-11-13T08:00:00"))
        db.executemany("INSERT INTO correl_diaria (data, y, x, corr, beta, n, calculado_em) VALUES (?,?,?,?,?,?,?)", rows)
        return db

    def test_menos_de_15_dias_sem_referencia(self):
        db = self.base(14)
        self.assertIsNone(c.referencia(db, "WIN$", "WDO$", "2026-11-13"))

    def test_valores_com_20_dias(self):
        corrs = [-0.4 + 0.01 * i for i in range(20)]
        db = self.base(20, corrs=corrs)
        ref = c.referencia(db, "WIN$", "WDO$", "2026-11-13")
        self.assertEqual(ref["dias"], 20)
        self.assertAlmostEqual(ref["media"], float(np.mean(corrs)))
        self.assertAlmostEqual(ref["dp"], float(np.std(corrs, ddof=1)))
        self.assertAlmostEqual(ref["p10"], float(np.percentile(corrs, 10)))
        self.assertAlmostEqual(ref["p90"], float(np.percentile(corrs, 90)))
        self.assertAlmostEqual(c.z(-0.3, ref), (-0.3 - ref["media"]) / ref["dp"])
        self.assertIsNone(c.z(None, ref))
        self.assertIsNone(c.z(0.1, None))

    def test_aceita_o_par_invertido_e_ignora_datas_posteriores(self):
        db = self.base(20, invertido=True)
        self.assertIsNotNone(c.referencia(db, "WIN$", "WDO$", "2026-11-13"))
        self.assertIsNone(c.referencia(db, "WIN$", "WDO$", "2026-09-10"))  # so 9 dias antes

    def test_respeita_n_dias(self):
        db = self.base(30)
        self.assertEqual(c.referencia(db, "WIN$", "WDO$", "2026-11-13", n_dias=20)["dias"], 20)


class TestAjustarFatores(unittest.TestCase):
    def db(self, dias, rng):
        db = banco()
        minutos = np.arange(MIN_INI, MIN_FIM + 1)
        for d in dias:
            r_wdo = rng.normal(0.0, 0.001, len(minutos))
            r_win = -0.5 * r_wdo + rng.normal(0.0, 0.0005, len(minutos))
            barras(db, "WDO$", d, minutos, 5.0 * np.exp(np.cumsum(r_wdo)))
            barras(db, "WIN$", d, minutos, 100_000.0 * np.exp(np.cumsum(r_win)))
        return db

    def test_recupera_beta_e_ignora_fator_sem_dado(self):
        dias = list(range(DIA, DIA + 25))
        db = self.db(dias, np.random.default_rng(2))
        r = c.ajustar_fatores(db, dias[-1])
        self.assertAlmostEqual(r["betas"]["WDO$"], -0.5, delta=0.05)
        self.assertNotIn("DI1F29", r["betas"])   # sem dado em nenhum dia
        self.assertNotIn("SPX_USD", r["betas"])
        self.assertGreaterEqual(r["n"], 2000)
        self.assertGreater(r["r2"], 0.2)

    def test_menos_de_2000_linhas_retorna_none(self):
        dias = list(range(DIA, DIA + 25))
        db = self.db(dias, np.random.default_rng(3))
        self.assertIsNone(c.ajustar_fatores(db, dias[1]))  # so 1 pregao anterior


class TestDecompor(unittest.TestCase):
    def test_identidade_total_igual_fatores_mais_propria(self):
        rng = np.random.default_rng(4)
        idx = range(600, 760)
        x = pd.Series(rng.normal(0.0, 0.001, 160), index=idx)
        z = pd.Series(rng.normal(0.0, 0.0005, 160), index=idx)
        y = -0.5 * x + 0.2 * z + rng.normal(0.0, 0.0002, 160)
        d = c.decompor({"WIN$": y, "WDO$": x, "DI1F29": z}, {"WDO$": -0.5, "DI1F29": 0.2}, 100_000.0)
        soma = sum(f["pct"] for f in d["fatores"])
        self.assertAlmostEqual(d["total_pct"], soma + d["propria_pct"], places=12)
        self.assertAlmostEqual(d["total_pts"], d["total_pct"] * 1000.0)
        self.assertAlmostEqual(d["fatores"][0]["pts"], d["fatores"][0]["pct"] * 1000.0)
        self.assertEqual([f["nome"] for f in d["fatores"]], ["Dolar", "DI jan/29"])
        self.assertEqual(d["n"], 160)
        self.assertIsNotNone(d["r2_hoje"])

    def test_r2_hoje_perfeito_sem_intercepto(self):
        x = pd.Series(np.linspace(-0.01, 0.01, 100), index=range(100))
        d = c.decompor({"WIN$": -0.5 * x, "WDO$": x}, {"WDO$": -0.5}, 100.0)
        self.assertAlmostEqual(d["r2_hoje"], 1.0)
        self.assertAlmostEqual(d["propria_pct"], 0.0, places=12)

    def test_amostra_curta_sem_r2_hoje(self):
        x = pd.Series(np.linspace(-0.01, 0.01, 30), index=range(30))
        d = c.decompor({"WIN$": -0.5 * x, "WDO$": x}, {"WDO$": -0.5}, 100.0)
        self.assertEqual(d["n"], 30)
        self.assertIsNone(d["r2_hoje"])


class TestAtualizar(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = str(Path(self.dir.name) / "teste.db")
        hoje = (date.today() - date(1970, 1, 1)).days
        self.dias = [hoje - 4, hoje - 3, hoje - 2, hoje - 1]
        rng = np.random.default_rng(5)
        con = sqlite3.connect(self.db)
        con.executescript(SCHEMA_MINIMO)
        minutos = np.arange(MIN_INI, MIN_INI + 61)
        for d in self.dias:
            r_win = rng.normal(0.0, 0.0005, len(minutos))
            r_wdo = rng.normal(0.0, 0.0005, len(minutos))
            barras(con, "WIN$", d, minutos, 100_000.0 * np.exp(np.cumsum(r_win)))
            barras(con, "WDO$", d, minutos, 5.0 * np.exp(np.cumsum(r_wdo)))
        con.commit()
        con.close()

    def tearDown(self):
        self.dir.cleanup()

    def contar(self, sql):
        con = sqlite3.connect(self.db)
        try:
            return con.execute(sql).fetchone()[0]
        finally:
            con.close()

    def test_idempotente_e_refaz_so_os_2_mais_recentes(self):
        self.assertEqual(c.atualizar(self.db, dias=30), len(self.dias))
        n = self.contar("SELECT count(*) FROM correl_diaria")
        self.assertGreater(n, 0)
        self.assertEqual(self.contar("SELECT count(DISTINCT data) FROM correl_diaria"), len(self.dias))

        con = sqlite3.connect(self.db)
        antiga, recente = self.dias[0], self.dias[-1]
        con.execute("UPDATE correl_diaria SET calculado_em='antigo' WHERE data=?",
                    ((date(1970, 1, 1) + timedelta(days=antiga)).isoformat(),))
        con.execute("UPDATE correl_diaria SET calculado_em='antigo' WHERE data=?",
                    ((date(1970, 1, 1) + timedelta(days=recente)).isoformat(),))
        con.commit()
        con.close()

        self.assertEqual(c.atualizar(self.db, dias=30), 2)  # refez os 2 mais recentes
        self.assertEqual(self.contar("SELECT count(*) FROM correl_diaria"), n)  # nao duplicou
        con = sqlite3.connect(self.db)
        try:
            marcas = dict(con.execute(
                "SELECT data, calculado_em FROM correl_diaria WHERE y='WIN$' AND x='WDO$'").fetchall())
        finally:
            con.close()
        self.assertEqual(marcas[(date(1970, 1, 1) + timedelta(days=antiga)).isoformat()], "antigo")
        self.assertNotEqual(marcas[(date(1970, 1, 1) + timedelta(days=recente)).isoformat()], "antigo")


class TestMovel(unittest.TestCase):
    def test_correlacao_movel_minimo_50_pares(self):
        rng = np.random.default_rng(6)
        x = pd.Series(rng.normal(size=120), index=range(600, 720))
        m = c.movel(x, x)
        self.assertEqual(int(m.notna().sum()), 71)  # 120 - 50 + 1
        self.assertAlmostEqual(float(m.iloc[-1]), 1.0)
        self.assertTrue(np.isnan(m.iloc[48]))


class TestPares(unittest.TestCase):
    def test_sem_par_repetido(self):
        pares = c._pares_do_dia()
        self.assertEqual(len(pares), len({frozenset(p) for p in pares}))
        self.assertEqual(pares[0], ("WIN$", "WDO$"))
        self.assertIn(("DI1F29", "SPX_USD"), pares)
        self.assertNotIn(("WDO$", "WIN$"), pares)


if __name__ == "__main__":
    unittest.main()
