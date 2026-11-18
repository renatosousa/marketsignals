"""Testes do coletor_global.py. Sem rede: fixtures JSON gravadas em testes_fixtures/."""
import json
import sqlite3
import unittest
import unittest.mock
from pathlib import Path

import coletor_global as cg

FIX = Path(__file__).parent / "testes_fixtures"
D = 86_400_000


def fixture(nome):
    return json.loads((FIX / nome).read_text(encoding="utf-8"))


class TestExtrairBarras(unittest.TestCase):
    def test_converte_epoch_utc_para_brt(self):
        meta, barras = cg.extrair_barras(fixture("global_yahoo_5m_ES.json"))
        self.assertTrue(barras)
        self.assertEqual(barras[0][0], (1794891900 - 3 * 3600) * 1000)
        self.assertEqual(barras[0][0] % (5 * 60_000), 0)
        self.assertEqual(meta["symbol"], "ES=F")
        self.assertGreater(barras[-1][4], 0)

    def test_60m_tambem_converte(self):
        _meta, barras = cg.extrair_barras(fixture("global_yahoo_60m_ES.json"))
        self.assertGreater(len(barras), 80)
        self.assertEqual(barras[0][0] % (60 * 60_000), 0)  # a ultima pode ser a barra parcial do momento
        self.assertGreater(barras[-1][0], barras[0][0])

    def test_barra_sem_close_e_descartada(self):
        payload = {"chart": {"result": [{
            "meta": {"symbol": "X"},
            "timestamp": [1000, 1300, 1600],
            "indicators": {"quote": [{"open": [1, 2, 3], "high": [1, 2, 3], "low": [1, 2, 3],
                                      "close": [1.0, None, 3.0], "volume": [0, 0, 0]}]},
        }]}}
        _meta, barras = cg.extrair_barras(payload)
        self.assertEqual([b[4] for b in barras], [1.0, 3.0])
        self.assertEqual(barras[0][0], (1000 - 10800) * 1000)

    def test_sem_resultado_levanta_erro(self):
        with self.assertRaises(ValueError):
            cg.extrair_barras({"chart": {"result": None, "error": {"description": "Not Found"}}})


class TestSessao(unittest.TestCase):
    def test_fronteiras(self):
        def t(h, m=0, dia=20775):
            return dia * D + (h * 60 + m) * 60_000
        self.assertEqual(cg.sessao(t(6, 59)), "ASIA")
        self.assertEqual(cg.sessao(t(7, 0)), "EUROPA")
        self.assertEqual(cg.sessao(t(8, 59)), "EUROPA")
        self.assertEqual(cg.sessao(t(9, 0)), "US_PRE")
        self.assertEqual(cg.sessao(t(10, 29)), "US_PRE")
        self.assertEqual(cg.sessao(t(10, 30)), "PREGAO_B3")
        self.assertEqual(cg.sessao(t(18, 29)), "PREGAO_B3")
        self.assertEqual(cg.sessao(t(18, 30)), "POS")

    def test_intervalo_do_servico(self):
        from datetime import datetime
        self.assertEqual(cg._intervalo_servico(datetime(2026, 11, 18, 8, 59)), 60.0)
        self.assertEqual(cg._intervalo_servico(datetime(2026, 11, 18, 9, 0)), 180.0)


class TestBanco(unittest.TestCase):
    def banco(self):
        db = sqlite3.connect(":memory:")
        db.executescript(cg.SCHEMA)
        return db

    def test_backfill_idempotente(self):
        db = self.banco()
        chamadas = []

        def fonte(ativo, intervalo, range_):
            chamadas.append((ativo, intervalo, range_))
            return [(20775 * D + 9 * 3_600_000, 10.0, 11.0, 9.0, 10.5, 0.0),
                    (20775 * D + 10 * 3_600_000, 10.5, 12.0, 10.0, 11.5, 0.0)]

        n1 = cg.backfill(db, "5m", "5d", fonte=fonte)
        n2 = cg.backfill(db, "5m", "5d", fonte=fonte)
        self.assertGreater(n1, 0)
        self.assertEqual(n2, 0)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM global_barras").fetchone()[0], n1)
        self.assertNotIn("MINERIO", [c[0] for c in chamadas])

    def test_backfill_minerio_so_diario(self):
        db = self.banco()
        vistos = []

        def fonte(ativo, intervalo, range_):
            vistos.append(ativo)
            return [(20775 * D, 1.0, 2.0, 0.5, 1.5, None)]

        n = cg.backfill(db, "1d", "5d", fonte=fonte)
        self.assertEqual(vistos, ["MINERIO"])
        self.assertEqual(n, 1)

    def test_gravar_so_quando_muda_e_variacao(self):
        db = self.banco()
        dia = 20773
        prev = 20770
        db.execute("INSERT INTO global_barras VALUES (?,?,?,?,?,?,?)",
                   (prev * D + 18 * 3_600_000, "N225", "60m", 1, 1, 1, 100.0))
        cot = {"ativo": "N225", "ts_ms": dia * D + 2 * 3_600_000, "ultimo": 110.0, "intervalo": "5m",
               "barras": [(dia * D + 2 * 3_600_000, 108.0, 111.0, 107.0, 110.0, 0.0)]}
        self.assertTrue(cg.gravar(db, cot, cg.referencia_pregao_anterior(db, "N225", dia)))
        self.assertFalse(cg.gravar(db, cot, cg.referencia_pregao_anterior(db, "N225", dia)))
        linha = db.execute("SELECT ultimo, variacao_pct_ref_b3, sessao, fonte FROM global_cotacoes").fetchone()
        self.assertEqual(linha[0], 110.0)
        self.assertAlmostEqual(linha[1], 10.0)
        self.assertEqual(linha[2], "ASIA")
        self.assertEqual(linha[3], "yahoo:5m")

    def test_referencia_usa_pregao_anterior(self):
        db = self.banco()
        # segunda 16/11; o pregao anterior e sexta 13/11 (20770), nao o domingo
        db.execute("INSERT INTO global_barras VALUES (?,?,?,?,?,?,?)",
                   (20770 * D + 18 * 3_600_000, "DXY", "60m", 1, 1, 1, 99.0))
        self.assertEqual(cg.referencia_pregao_anterior(db, "DXY", 20773), 99.0)
        self.assertIsNone(cg.referencia_pregao_anterior(db, "OURO", 20773))

    def test_variacao_sem_referencia(self):
        self.assertIsNone(cg.variacao(10.0, None))
        self.assertIsNone(cg.variacao(10.0, 0))


class TestConsultarAtivo(unittest.TestCase):
    def test_intraday(self):
        payload = fixture("global_yahoo_5m_N225.json")
        with unittest.mock.patch.object(cg, "_get_json", return_value=payload):
            cot = cg.consultar_ativo(None, "N225")
        self.assertEqual(cot["ativo"], "N225")
        self.assertEqual(cot["intervalo"], "5m")
        self.assertEqual(cot["ts_ms"], (payload["chart"]["result"][0]["timestamp"][-1] - 10800) * 1000)
        self.assertEqual(cot["ultimo"], payload["chart"]["result"][0]["indicators"]["quote"][0]["close"][-1])

    def test_minerio_meta_antigo_cai_para_barra(self):
        payload = fixture("global_yahoo_1d_TIO.json")
        with unittest.mock.patch.object(cg, "_get_json", return_value=payload):
            cot = cg.consultar_ativo(None, "MINERIO")
        r = payload["chart"]["result"][0]
        ultimo = [c for c in r["indicators"]["quote"][0]["close"] if c is not None][-1]
        self.assertEqual(cot["intervalo"], "1d")
        self.assertEqual(cot["ultimo"], ultimo)
        self.assertEqual(cot["ts_ms"], (r["timestamp"][-1] - 10800) * 1000)

    def test_minerio_usa_meta_quando_coerente(self):
        payload = fixture("global_yahoo_1d_TIO.json")
        r = payload["chart"]["result"][0]
        r["meta"]["regularMarketTime"] = r["timestamp"][-1]
        r["meta"]["regularMarketPrice"] = 95.0
        with unittest.mock.patch.object(cg, "_get_json", return_value=payload):
            cot = cg.consultar_ativo(None, "MINERIO")
        self.assertEqual(cot["ultimo"], 95.0)
        self.assertEqual(cot["ts_ms"], (r["timestamp"][-1] - 10800) * 1000)


if __name__ == "__main__":
    unittest.main()
