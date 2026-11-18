"""Testes do modelo_regime_v3.py com barras sinteticas de 1 min.

  python -m unittest test_modelo_regime_v3 -v
"""
import json
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

import calendario_b3 as cal
import modelo_regime_v3 as m3
import regimes as rg

D = m3.DIA_MS
MIN_INI, MIN_FIM = 9 * 60 + 5, 18 * 60 + 15
NDIAS = 85
SCHEMA = """
CREATE TABLE barras_1m (symbol TEXT NOT NULL, ts_ms INTEGER NOT NULL, close REAL);
CREATE TABLE opcoes_resumo (ts_ms INTEGER NOT NULL, subjacente TEXT, vencimento TEXT, rr25 REAL);
"""
_FIXTURE = {}


def fixture():
    """Banco sintetico, dataset e modelo treinados uma vez para todos os testes (o treino e o custo dominante)."""
    if not _FIXTURE:
        db, datas = banco()
        dados = m3.montar(db)
        _FIXTURE.update({"db": db, "datas": datas, "dados": dados, "modelo": m3.treinar(db)})
    return _FIXTURE["db"], _FIXTURE["datas"], _FIXTURE["dados"], _FIXTURE["modelo"]


def banco(dias=NDIAS):
    """Banco em memoria com ~`dias` pregoes de barras do WIN e opcoes no fim da amostra."""
    db = sqlite3.connect(":memory:")
    db.executescript(SCHEMA)
    rng = np.random.default_rng(7)
    hoje = (date(2026, 11, 16) - date(1970, 1, 1)).days
    datas = [hoje - dias + 1 + i for i in range(dias)]
    minutos = np.arange(MIN_INI, MIN_FIM + 1)
    for i, d in enumerate(datas):
        vol = 0.00035 * (1.0 + 0.6 * np.sin(i / 7.0) ** 2)
        r = rng.normal(0.0, vol, len(minutos))
        preco = 100000.0 * np.exp(np.cumsum(r))
        db.executemany("INSERT INTO barras_1m (symbol, ts_ms, close) VALUES (?,?,?)",
                       [("WIN$", d * D + int(m) * 60_000, float(p)) for m, p in zip(minutos, preco)])
        if i >= dias - 40:
            iso = (date(1970, 1, 1) + timedelta(days=d)).isoformat()
            venc_longe = cal.somar_pregoes(date.fromisoformat(iso), 10).isoformat()
            venc_curto = cal.somar_pregoes(date.fromisoformat(iso), 3).isoformat()
            db.executemany("INSERT INTO opcoes_resumo VALUES (?,?,?,?)",
                           [(d * D + 12 * 3_600_000, "BOVA11", venc_longe, -0.01),
                            (d * D + 12 * 3_600_000, "BOVA11", venc_curto, -0.05)])
    db.commit()
    return db, datas


class TestMontarETreinar(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db, cls.datas, cls.dados, cls.modelo = fixture()

    def test_dataset_causal_e_colunas(self):
        df = self.dados["df"]
        self.assertGreater(self.dados["pregoes"], NDIAS - 5)
        for c in ("dia", "slot", "rv_dia", "r60", "rclose", "sigma", "z60", "zfech", "log_sigma",
                  "ret_pas_std", "min_ate_fim", "rr25"):
            self.assertIn(c, df)
        self.assertGreater(df.sigma.notna().sum(), 1000)
        self.assertGreater(df.z60.notna().sum(), 1000)
        self.assertTrue(np.isfinite(df.z60.dropna()).all())
        self.assertEqual(self.dados["gex"]["usado"], False)
        self.assertIn("GEX fora", self.dados["gex"]["motivo"])

    def test_treinar_json_e_modelos(self):
        m = self.modelo
        self.assertEqual(m["versao"], 1)
        self.assertIn("har", m)
        self.assertIsNotNone(m["qr_A"])
        self.assertIsNotNone(m.get("qr_B"))
        self.assertIn("60min", m["qr_A"])
        self.assertIn("fechamento", m["qr_A"])
        s = json.dumps(m)  # tem de ser serializavel
        self.assertGreater(len(s), 100)
        self.assertGreater(m["pregoes"], NDIAS - 5)


class TestPrever(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db, cls.datas, _dados, cls.modelo = fixture()
        cls.dia = cls.datas[-1]
        cls.agora = cls.dia * D + (14 * 60) * 60_000

    def test_quantis_em_pontos_e_ordem(self):
        r = m3.prever(self.db, self.agora, self.modelo)
        self.assertIsNotNone(r)
        self.assertIn(r["modelo"], ("A", "B"))
        self.assertIn("60min", r)
        self.assertIn("fechamento", r)
        for h in ("60min", "fechamento"):
            q = r[h]
            self.assertLess(q["q05"], q["q15"])
            self.assertLess(q["q15"], q["q50"])
            self.assertLess(q["q50"], q["q85"])
            self.assertLess(q["q85"], q["q95"])
            self.assertGreater(q["sigma_prev"], 0)
            self.assertGreater(q["preco"], 0)
            # pontos do WIN: ordem de grandeza compativel com sigma_prev (%) x preco
            self.assertLess(abs(q["q95"]), q["preco"] * 0.2)

    def test_sem_lookahead_nas_barras_futuras(self):
        antes = m3.prever(self.db, self.agora, self.modelo)
        min_fim = (self.agora % D) // 60_000
        ini = self.dia * D + (min_fim + 1) * 60_000
        fim = (self.dia + 1) * D
        orig = self.db.execute("SELECT ts_ms, close FROM barras_1m WHERE symbol='WIN$' AND ts_ms>=? AND ts_ms<?",
                               (ini, fim)).fetchall()
        try:
            self.db.execute("UPDATE barras_1m SET close = 999999.0 WHERE symbol='WIN$' AND ts_ms>=? AND ts_ms<?",
                            (ini, fim))
            self.db.commit()
            depois = m3.prever(self.db, self.agora, self.modelo)
        finally:
            self.db.executemany("UPDATE barras_1m SET close=? WHERE symbol='WIN$' AND ts_ms=?", orig)
            self.db.commit()
        self.assertEqual(antes["60min"], depois["60min"])
        self.assertEqual(antes["fechamento"], depois["fechamento"])

    def test_fora_do_horario(self):
        self.assertIsNone(m3.prever(self.db, self.dia * D + 9 * 60 * 60_000, self.modelo))
        self.assertIsNone(m3.prever(self.db, self.dia * D + 18 * 60 * 60_000, self.modelo))


class TestMetricas(unittest.TestCase):
    def prev(self, z, pred, rv=None):
        z = np.asarray(z, dtype=float)
        pred = np.asarray(pred, dtype=float)
        n = len(z)
        return pd.DataFrame({"alvo": "z60", "z": z, "q05": pred[:, 0], "q15": pred[:, 1], "q50": pred[:, 2],
                             "q85": pred[:, 3], "q95": pred[:, 4],
                             "rv_dia": np.asarray(rv if rv is not None else np.ones(n)),
                             "min_ate_fim": np.full(n, 300), "dia": np.arange(n)})

    def test_cobertura_e_pinball(self):
        z = np.zeros(100)
        pred = np.tile([-1.0, -0.5, 0.0, 0.5, 1.0], (100, 1))
        m = m3._metricas(self.prev(z, pred), "z60")
        self.assertAlmostEqual(m["geral"]["c15"], 1.0)
        self.assertAlmostEqual(m["geral"]["c05"], 1.0)
        self.assertEqual(m["geral"]["n"], 100)
        self.assertLess(m["pinball"]["cqr"], m["pinball"]["gauss"])

    def test_separa_vol_alta_e_baixa(self):
        z = np.zeros(4)
        pred = np.tile([-1.0, -0.5, 0.0, 0.5, 1.0], (4, 1))
        m = m3._metricas(self.prev(z, pred, rv=[1.0, 1.0, 2.0, 2.0]), "z60")
        self.assertEqual(m["vol_alta"]["n"], 2)
        self.assertEqual(m["vol_baixa"]["n"], 2)

    def test_cobertura_vazia(self):
        self.assertEqual(m3._cobertura(None, np.array([])), {})


class TestConformal(unittest.TestCase):
    def test_quantil_conservador(self):
        self.assertAlmostEqual(m3._quantil_conformal([1.0, 2.0, 3.0, 4.0], 0.5), 4.0)
        self.assertEqual(m3._quantil_conformal([], 0.1), 0.0)

    def test_offsets_expandem_intervalo(self):
        y = np.array([3.0])
        pred = np.array([[-1.0, -0.5, 0.0, 0.5, 1.0]])
        o15, o05 = m3._offsets(y, pred)
        self.assertGreater(o15, 0)
        self.assertGreater(o05, 0)
        p = m3._aplicar(pred, o15, o05)[0]
        self.assertLessEqual(p[0], -1.0)
        self.assertGreaterEqual(p[4], 1.0)
        self.assertAlmostEqual(p[0], -1.0 - o05)
        self.assertAlmostEqual(p[4], 1.0 + o05)
        self.assertTrue(np.all(np.diff(p) >= 0))


class TestAtualizar(unittest.TestCase):
    def test_atualizar_grava_json(self):
        with tempfile.TemporaryDirectory() as d:
            arq = str(Path(d) / "book.db")
            db = sqlite3.connect(arq)
            db.executescript(SCHEMA)
            rng = np.random.default_rng(9)
            hoje = (date(2026, 11, 16) - date(1970, 1, 1)).days
            minutos = np.arange(MIN_INI, MIN_FIM + 1)
            for i in range(25):
                dia = hoje - 25 + i
                preco = 100000.0 * np.exp(np.cumsum(rng.normal(0.0, 0.0004, len(minutos))))
                db.executemany("INSERT INTO barras_1m (symbol, ts_ms, close) VALUES (?,?,?)",
                               [("WIN$", dia * D + int(m) * 60_000, float(p)) for m, p in zip(minutos, preco)])
            db.commit()
            db.close()
            m = m3.atualizar(arq)
            arq_json = Path(d) / "modelo_regime_v3.json"
            self.assertTrue(arq_json.exists())
            self.assertIn("qr_A", json.loads(arq_json.read_text(encoding="utf-8")))
            self.assertEqual(m["modelo"], "A")


if __name__ == "__main__":
    unittest.main()
