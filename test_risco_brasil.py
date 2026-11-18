"""Testes do risco_brasil.py com banco sintetico em memoria. Sem rede.

Cobre: grade de 5 min, betas por OLS walk-forward, eps acumulado, inclinacao DI,
fallback macro_cotacoes, rotulos e ausencia de lookahead no treino.
"""
import math
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

import numpy as np

import calendario_b3
import risco_brasil as rb

D = rb.D
SCHEMA = """
CREATE TABLE barras_1m (symbol TEXT NOT NULL, ts_ms INTEGER NOT NULL, open REAL, high REAL, low REAL,
                        close REAL, volume REAL, PRIMARY KEY (symbol, ts_ms));
CREATE TABLE macro_cotacoes (ts_ms INTEGER NOT NULL, sessao TEXT NOT NULL, symbol TEXT NOT NULL, grupo TEXT,
                             bid REAL, ask REAL, last REAL, mid REAL);
CREATE TABLE global_barras (ts_ms INTEGER NOT NULL, ativo TEXT NOT NULL, intervalo TEXT NOT NULL,
                            open REAL, high REAL, low REAL, close REAL, PRIMARY KEY (ts_ms, ativo, intervalo));
CREATE TABLE risco_brasil_5m (ts_ms INTEGER NOT NULL PRIMARY KEY, eps_5m REAL, eps_acum_pct REAL,
                              beta_dxy REAL, beta_em REAL, r2 REAL, incl_bps REAL, incl_var_bps REAL);
"""


def dia_idx(d):
    return (d - date(1970, 1, 1)).days


class Cenario:
    """WDO = 0,50*DXY + 0,80*EM + ruido; inclinacao DI abre 0,1 bp por barra."""

    def __init__(self, n_dias=25):
        self.dias = list(calendario_b3.pregoes("2026-09-01", "2026-10-30"))[-n_dias:]
        self.db = sqlite3.connect(":memory:")
        self.db.executescript(SCHEMA)
        self.gerar()

    def gerar(self):
        for i, dia in enumerate(self.dias):
            d = dia_idx(dia)
            rng = np.random.default_rng(100 + i)
            dxy = mxn = zar = 100.0
            wdo = 5.0
            for k, minuto in enumerate(range(rb.INI, rb.FIM + 1, rb.PASSO)):
                if k:
                    r_dxy = float(rng.normal(0, 0.0004))
                    r_em = float(rng.normal(0, 0.0006))
                    r_wdo = 0.5 * r_dxy + 0.8 * r_em + float(rng.normal(0, 0.00015))
                    dxy *= math.exp(r_dxy)
                    mxn *= math.exp(r_em)
                    zar *= math.exp(r_em * 0.9)
                    wdo *= math.exp(r_wdo)
                t = d * D + minuto * 60_000
                for ativo, preco in (("DXY", dxy), ("USDMXN", mxn), ("USDZAR", zar)):
                    self.db.execute("INSERT INTO global_barras VALUES (?,?,?,?,?,?,?)",
                                    (t, ativo, "5m", preco, preco, preco, preco))
                self.db.execute("INSERT INTO barras_1m VALUES (?,?,?,?,?,?,?)",
                                ("WDO$", t, wdo, wdo, wdo, wdo, 1.0))
                self.db.execute("INSERT INTO macro_cotacoes VALUES (?,?,?,?,?,?,?,?)",
                                (t, "t", "DI1F33", "juros", None, None, None, 12.0 + 0.01 * i + 0.0002 * k))
                self.db.execute("INSERT INTO macro_cotacoes VALUES (?,?,?,?,?,?,?,?)",
                                (t, "t", "DI1F28", "juros", None, None, None, 11.0 + 0.00005 * k))
        self.db.commit()


class TestCalcularDia(unittest.TestCase):
    def test_betas_e_eps(self):
        c = Cenario()
        linhas = rb.calcular_dia(c.db, c.dias[-1])
        self.assertGreater(len(linhas), 100)
        u = linhas[-1]
        self.assertAlmostEqual(u["beta_dxy"], 0.5, delta=0.08)
        self.assertAlmostEqual(u["beta_em"], 0.8, delta=0.08)
        self.assertGreater(u["r2"], 0.8)
        self.assertTrue(all(np.isfinite(l["eps_5m"]) for l in linhas))
        self.assertAlmostEqual(u["eps_acum_pct"], sum(l["eps_5m"] for l in linhas) * 100)
        self.assertGreater(u["incl_var_bps"], 0)

    def test_inclinacao_em_bps(self):
        c = Cenario()
        linhas = rb.calcular_dia(c.db, c.dias[-1])
        u = linhas[0]
        vals = dict(c.db.execute("SELECT symbol, mid FROM macro_cotacoes WHERE ts_ms=?",
                                 (u["ts_ms"],)).fetchall())
        self.assertAlmostEqual(u["incl_bps"], (vals["DI1F33"] - vals["DI1F28"]) * 100, delta=1e-6)
        ini = c.db.execute("SELECT ts_ms FROM macro_cotacoes WHERE symbol='DI1F33' AND ts_ms=?",
                           (c.dias[-1] and (dia_idx(c.dias[-1]) * D + rb.INI * 60_000),)).fetchone()[0]
        v0 = dict(c.db.execute("SELECT symbol, mid FROM macro_cotacoes WHERE ts_ms=?", (ini,)).fetchall())
        self.assertAlmostEqual(u["incl_var_bps"], ((vals["DI1F33"] - vals["DI1F28"]) -
                                                   (v0["DI1F33"] - v0["DI1F28"])) * 100, delta=1e-6)

    def test_treino_nao_usa_o_dia_atual(self):
        c = Cenario()
        antes = rb.calcular_dia(c.db, c.dias[-1])[-1]
        d = dia_idx(c.dias[-1])
        rows = c.db.execute("SELECT ts_ms, close FROM barras_1m WHERE symbol='WDO$' AND ts_ms>=? AND ts_ms<? "
                            "ORDER BY ts_ms", (d * D, (d + 1) * D)).fetchall()
        c.db.executemany("UPDATE barras_1m SET close=?, open=? WHERE symbol='WDO$' AND ts_ms=?",
                         [(close * math.exp(0.001 * i), close * math.exp(0.001 * i), t)
                          for i, (t, close) in enumerate(rows)])
        c.db.commit()
        depois = rb.calcular_dia(c.db, c.dias[-1])[-1]
        self.assertEqual(antes["beta_dxy"], depois["beta_dxy"])
        self.assertEqual(antes["beta_em"], depois["beta_em"])
        self.assertNotEqual(antes["eps_acum_pct"], depois["eps_acum_pct"])

    def test_fallback_macro_cotacoes(self):
        c = Cenario()
        base = rb.calcular_dia(c.db, c.dias[-1])
        d = dia_idx(c.dias[-1])
        rows = c.db.execute("SELECT ts_ms, close FROM barras_1m WHERE symbol='WDO$' AND ts_ms>=? AND ts_ms<?",
                            (d * D, (d + 1) * D)).fetchall()
        for t, close in rows:
            c.db.execute("INSERT INTO macro_cotacoes VALUES (?,?,?,?,?,?,?,?)",
                         (t, "t", "WDO$", "cambio", None, None, None, close))
        c.db.execute("DELETE FROM barras_1m WHERE symbol='WDO$' AND ts_ms>=? AND ts_ms<?",
                     (d * D, (d + 1) * D))
        c.db.commit()
        macro = rb.calcular_dia(c.db, c.dias[-1])
        self.assertEqual(len(base), len(macro))
        self.assertAlmostEqual(base[-1]["eps_acum_pct"], macro[-1]["eps_acum_pct"], places=12)

    def test_sem_dados(self):
        db = sqlite3.connect(":memory:")
        db.executescript(SCHEMA)
        self.assertEqual(rb.calcular_dia(db, "2026-11-17"), [])

    def test_avaliar(self):
        c = Cenario()
        r = rb.avaliar(c.db)
        self.assertGreater(r["pregoes"], 10)
        self.assertGreater(r["r2_medio"], 0.8)


class TestRotulo(unittest.TestCase):
    def test_casos(self):
        self.assertEqual(rb.rotulo(0.10, 3), "dolar global")
        self.assertEqual(rb.rotulo(0.60, 3), "estresse domestico")
        self.assertEqual(rb.rotulo(0.60, -3), "misto")
        self.assertEqual(rb.rotulo(-0.60, -3), "alivio domestico")
        self.assertEqual(rb.rotulo(0.30, 3), "misto")
        self.assertIsNone(rb.rotulo(None, 3))


class TestAtualizar(unittest.TestCase):
    def test_grava_tabela(self):
        c = Cenario()
        with tempfile.TemporaryDirectory() as d:
            caminho = str(Path(d) / "risco.db")
            origem = c.db
            destino = sqlite3.connect(caminho)
            destino.executescript(SCHEMA)
            origem.backup(destino)
            destino.close()
            n = rb.atualizar(caminho, dias=3)
            self.assertEqual(n, 3)
            con = sqlite3.connect(caminho)
            try:
                total = con.execute("SELECT COUNT(*) FROM risco_brasil_5m").fetchone()[0]
                dias = con.execute("SELECT COUNT(DISTINCT ts_ms/? ) FROM risco_brasil_5m", (D,)).fetchone()[0]
            finally:
                con.close()
            self.assertGreater(total, 0)
            self.assertEqual(dias, 3)


if __name__ == "__main__":
    unittest.main()
