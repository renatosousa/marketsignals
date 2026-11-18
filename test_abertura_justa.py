"""Testes do abertura_justa.py com banco SQLite em memoria. Sem rede.

Cobre: alvo gap, fechamento_diario com prioridade, features overnight (limites de horario),
variavel faltante virando 0, walk-forward sem olhar o futuro, metricas e prever_hoje.
"""
import math
import sqlite3
import unittest
from datetime import date

import abertura_justa as aj
import calendario_b3

D = aj.D
INIT = """
CREATE TABLE barras_1m (symbol TEXT NOT NULL, ts_ms INTEGER NOT NULL, open REAL, high REAL, low REAL,
                        close REAL, volume REAL, PRIMARY KEY (symbol, ts_ms));
CREATE TABLE global_barras (ts_ms INTEGER NOT NULL, ativo TEXT NOT NULL, intervalo TEXT NOT NULL,
                            open REAL, high REAL, low REAL, close REAL, PRIMARY KEY (ts_ms, ativo, intervalo));
CREATE TABLE macro_cotacoes (ts_ms INTEGER NOT NULL, symbol TEXT NOT NULL, mid REAL);
CREATE TABLE fechamento_diario (dia_idx INTEGER NOT NULL, ativo TEXT NOT NULL, abertura REAL, maxima REAL,
    minima REAL, ultimo_liquido REAL NOT NULL, ultimo_dia REAL, ajuste_b3 REAL, vwap_dia REAL, poc_dia REAL,
    vah_dia REAL, val_dia REAL, vol_realizada_dia REAL, fonte TEXT, ts_consolidacao_ms INTEGER NOT NULL,
    PRIMARY KEY (dia_idx, ativo));
"""


def ts(dia, hh, mm=0):
    return dia * D + (hh * 60 + mm) * 60_000


def dia_idx(d):
    return (d - date(1970, 1, 1)).days


class Cenario:
    """Serie sintetica: gap do WIN/WDO = 0,7*r_ES - 0,2*r_DXY + ruido deterministico."""

    def __init__(self, dias=90, inicio="2026-05-04"):
        self.dias = list(calendario_b3.pregoes(inicio, "2026-10-30"))[:dias]
        self.db = sqlite3.connect(":memory:")
        self.db.executescript(INIT)
        self.db.executescript(aj.SCHEMA)
        self.gerar()
        self.series = aj._series_globais(self.db)

    def gerar(self):
        base, es, dxy = 100_000.0, 100.0, 100.0
        prev_close, es_prev, dxy_prev = base, es, dxy
        for i, d in enumerate(self.dias):
            di = dia_idx(d)
            r_es = 0.003 * math.sin(i / 4.0) + 0.0004 * math.cos(i / 7.0)
            r_dxy = 0.4 * r_es + 0.0002 * math.sin(i / 3.0)
            gap = 0.7 * r_es - 0.2 * r_dxy + 0.0001 * math.cos(i / 5.0)
            abertura = prev_close * math.exp(gap)
            fechamento = abertura * math.exp(0.001 * math.sin(i / 6.0))
            for ativo in ("WIN$", "WDO$"):
                self.db.execute("INSERT INTO barras_1m VALUES (?,?,?,?,?,?,?)",
                                (ativo, ts(di, 9, 0), abertura, abertura, abertura, abertura, 10.0))
                self.db.execute("INSERT INTO barras_1m VALUES (?,?,?,?,?,?,?)",
                                (ativo, ts(di, 17, 0), fechamento, fechamento, fechamento, fechamento, 10.0))
            if i > 0:
                p_es, p_dxy = es_prev * math.exp(r_es), dxy_prev * math.exp(r_dxy)
                for ativo, p0, p1 in (("ES", es_prev, p_es), ("DXY", dxy_prev, p_dxy)):
                    self.db.execute("INSERT INTO global_barras VALUES (?,?,?,?,?,?,?)",
                                    (ts(dia_idx(self.dias[i - 1]), 17, 0), ativo, "60m", p0, p0, p0, p0))
                    self.db.execute("INSERT INTO global_barras VALUES (?,?,?,?,?,?,?)",
                                    (ts(di, 8, 0), ativo, "60m", p1, p1, p1, p1))
                es_prev, dxy_prev = p_es, p_dxy
            prev_close = fechamento
        self.db.commit()


class TestAlvo(unittest.TestCase):
    def test_historico_e_gap(self):
        c = Cenario()
        rows = aj._historico(c.db, "WIN$", c.series)
        self.assertGreater(len(rows), 80)
        r = rows[-1]
        self.assertAlmostEqual(r["gap"], math.log(r["abertura"] / r["fechamento"]))
        self.assertIn("ES", r["x"])

    def test_fechamento_diario_tem_prioridade(self):
        c = Cenario()
        d = c.dias[-2]
        c.db.execute("INSERT INTO fechamento_diario VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (dia_idx(d), "WIN$", None, None, None, 123456.0, None, None, None, None, None, None,
                      None, "teste", 0))
        rows = aj._historico(c.db, "WIN$", c.series)
        alvo = next(r for r in rows if r["dia"] == dia_idx(c.dias[-1]))
        self.assertEqual(alvo["fechamento"], 123456.0)

    def test_sem_abertura_nao_entra(self):
        c = Cenario()
        dia = dia_idx(c.dias[5])
        c.db.execute("DELETE FROM barras_1m WHERE symbol='WIN$' AND ts_ms>=? AND ts_ms<?",
                     (dia * D + 9 * 3_600_000, dia * D + 17 * 3_600_000 + 1))
        rows = aj._historico(c.db, "WIN$", c.series)
        self.assertNotIn(dia, {r["dia"] for r in rows})


class TestFeatures(unittest.TestCase):
    def test_limite_0855_ignora_barra_depois(self):
        c = Cenario()
        dia = c.dias[40]
        x1, _ = aj.features_dia(c.series, dia_idx(dia))
        d = dia_idx(dia)
        c.db.execute("INSERT INTO global_barras VALUES (?,?,?,?,?,?,?)",
                     (ts(d, 10, 0), "ES", "60m", 999.0, 999.0, 999.0, 999.0))
        s2 = aj._series_globais(c.db)
        x2, _ = aj.features_dia(s2, d)
        self.assertEqual(x1["ES"], x2["ES"])

    def test_us10y_ausente_vira_zero_e_marca(self):
        c = Cenario()
        x, faltantes = aj.features_dia(c.series, dia_idx(c.dias[30]))
        self.assertEqual(x["US10Y"], 0.0)
        self.assertIn("US10Y", faltantes)

    def test_minerio_diario(self):
        c = Cenario()
        d = dia_idx(c.dias[20])
        c.db.execute("INSERT INTO global_barras VALUES (?,?,?,?,?,?,?)",
                     (ts(d - 2, 12, 0), "MINERIO", "1d", 90, 90, 90, 90.0))
        c.db.execute("INSERT INTO global_barras VALUES (?,?,?,?,?,?,?)",
                     (ts(d - 1, 12, 0), "MINERIO", "1d", 99, 99, 99, 99.0))
        x, faltantes = aj.features_dia(aj._series_globais(c.db), d)
        self.assertAlmostEqual(x["MINERIO"], math.log(99 / 90))
        self.assertNotIn("MINERIO", faltantes)


class TestModelo(unittest.TestCase):
    def test_walk_forward_nao_usa_o_proprio_dia(self):
        c = Cenario()
        rows = aj._historico(c.db, "WIN$", c.series)
        preds = aj._walk_forward(rows)
        self.assertTrue(all(p["pred"] is None for p in preds[:aj.MIN_TREINO]))
        self.assertTrue(any(p["pred"] is not None for p in preds[aj.MIN_TREINO:]))
        # mudar o alvo do ultimo dia nao pode mudar a previsao dos dias anteriores
        antes = [p["pred"] for p in preds[:-1]]
        rows[-1] = dict(rows[-1], gap=rows[-1]["gap"] + 1.0)
        depois = [p["pred"] for p in aj._walk_forward(rows)[:-1]]
        self.assertEqual(antes, depois)

    def test_metricas_e_avaliacao(self):
        c = Cenario()
        r = aj.avaliar(c.db)
        for ativo in aj.ATIVOS:
            m = r[ativo]["modelo"]
            self.assertGreater(m["n"], 40)
            self.assertIsNotNone(m["r2"])
            self.assertIsNotNone(m["mae_pts"])
            self.assertIn("gap_zero", r[ativo])
            self.assertIn("so_es", r[ativo])

    def test_prever_hoje_ignora_dados_depois_de_agora(self):
        c = Cenario()
        dia = calendario_b3.proximo_pregao(c.dias[-1])
        d = dia_idx(dia)
        agora = ts(d, 8, 0)
        # completa a madrugada de hoje com ES/DXY ate 08:00
        p_es = c.db.execute("SELECT close FROM global_barras WHERE ativo='ES' ORDER BY ts_ms DESC LIMIT 1"
                            ).fetchone()[0]
        p_dxy = c.db.execute("SELECT close FROM global_barras WHERE ativo='DXY' ORDER BY ts_ms DESC LIMIT 1"
                             ).fetchone()[0]
        c.db.execute("INSERT INTO global_barras VALUES (?,?,?,?,?,?,?)",
                     (ts(d, 8, 0), "ES", "60m", p_es * 1.01, p_es * 1.01, p_es * 1.01, p_es * 1.01))
        c.db.execute("INSERT INTO global_barras VALUES (?,?,?,?,?,?,?)",
                     (ts(d, 8, 0), "DXY", "60m", p_dxy * 1.005, p_dxy * 1.005, p_dxy * 1.005, p_dxy * 1.005))
        c.db.commit()
        antes = aj.prever_hoje(c.db, agora)
        self.assertIsNotNone(antes["WIN$"])
        # dados posteriores as 08:00 (pregao e madrugada seguinte) nao podem mudar a previsao
        c.db.execute("INSERT INTO global_barras VALUES (?,?,?,?,?,?,?)",
                     (ts(d, 10, 0), "ES", "60m", 9999.0, 9999.0, 9999.0, 9999.0))
        c.db.execute("INSERT INTO barras_1m VALUES (?,?,?,?,?,?,?)",
                     ("WIN$", ts(d, 9, 0), 999999.0, 999999.0, 999999.0, 999999.0, 1.0))
        c.db.commit()
        depois = aj.prever_hoje(c.db, agora)
        self.assertEqual(antes["WIN$"]["gap_previsto"], depois["WIN$"]["gap_previsto"])
        self.assertIsNotNone(antes["WIN$"]["abertura_prevista"])
        self.assertIsNotNone(antes["WIN$"]["sigma_erro"])

    def test_distorcao(self):
        self.assertTrue(aj.distorcao_abertura(110.0, 100.0, 5.0))
        self.assertFalse(aj.distorcao_abertura(106.0, 100.0, 5.0))
        self.assertFalse(aj.distorcao_abertura(110.0, 100.0, None))


if __name__ == "__main__":
    unittest.main()
