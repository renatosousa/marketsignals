"""Testes do calendario_macro.py. Sem rede: fixtures do Forex Factory e do IBGE."""
import json
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

import calendario_macro as cm

FIX = Path(__file__).parent / "testes_fixtures"
D = 86_400_000


def ts(dia, hh, mm=0):
    return dia * D + (hh * 60 + mm) * 60_000


def fixture(nome):
    return json.loads((FIX / nome).read_text(encoding="utf-8"))


class TestForexFactory(unittest.TestCase):
    def setUp(self):
        self.eventos = cm.parse_ff(fixture("calendario_ff_20261118.json"))

    def test_converte_et_para_brt(self):
        fomc = next(e for e in self.eventos if e["evento"] == "FOMC Meeting Minutes")
        self.assertEqual(fomc["ts_ms"], ts(20775, 16, 0))
        self.assertEqual((fomc["pais"], fomc["impacto"]), ("EUA", "alto"))

    def test_so_usd_eur_cny(self):
        self.assertTrue(self.eventos)
        self.assertEqual({e["pais"] for e in self.eventos}, {"EUA", "EUR", "CHN"})
        self.assertFalse(any("RBA" in e["evento"] for e in self.eventos))

    def test_impactos_mapeados(self):
        self.assertIn("medio", {e["impacto"] for e in self.eventos})
        self.assertTrue(all(e["impacto"] in ("alto", "medio", "baixo") for e in self.eventos))


class TestIbge(unittest.TestCase):
    def setUp(self):
        self.eventos = cm.parse_ibge(fixture("calendario_ibge_20261118.json"))

    def test_ipca15_alto(self):
        e = next(x for x in self.eventos if x["evento"] == "IPCA-15" and x["ts_ms"] // D == 20783)
        self.assertEqual(e["ts_ms"], ts(20783, 12, 0))
        self.assertEqual(e["impacto"], "alto")
        self.assertEqual(e["pais"], "BRA")

    def test_pib_alto_e_pnad_medio(self):
        pib = next(x for x in self.eventos if x["evento"] == "PIB")
        self.assertEqual((pib["ts_ms"] // D, pib["impacto"]), (20789, "alto"))
        pnad = [x for x in self.eventos if x["evento"] == "PNAD Continua"]
        self.assertTrue(pnad)
        self.assertTrue(all(x["impacto"] == "medio" for x in pnad))

    def test_ignora_outros_produtos(self):
        self.assertFalse(any("Custo" in e["evento"] or "Censo" in e["evento"] for e in self.eventos))


class TestManual(unittest.TestCase):
    def test_le_csv(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "cal.csv"
            p.write_text("data,hora,pais,evento,impacto,previsto,anterior,obs\n"
                         "2026-12-09,18:30,BRA,Copom,alto,,,confirmar\n", encoding="utf-8")
            e = cm.ler_manual(p)
        self.assertEqual(len(e), 1)
        self.assertEqual(e[0]["ts_ms"], ts(20796, 18, 30))
        self.assertEqual((e[0]["impacto"], e[0]["fonte"]), ("alto", "manual"))

    def test_arquivo_do_projeto_tem_copom(self):
        e = cm.ler_manual()
        copom = [x for x in e if "Copom" in x["evento"]]
        self.assertTrue(copom)
        self.assertTrue(any(x["ts_ms"] == ts(20796, 18, 30) for x in copom))


class TestCalculados(unittest.TestCase):
    def test_quarta_mais_proxima_do_15(self):
        self.assertEqual(cm.quarta_mais_proxima_do_15(2026, 12), date(2026, 12, 16))
        self.assertEqual(cm.quarta_mais_proxima_do_15(2026, 2), date(2026, 2, 18))

    def test_terceira_sexta(self):
        self.assertEqual(cm.terceira_sexta(2026, 11), date(2026, 11, 20))
        self.assertEqual(cm.terceira_sexta(2026, 12), date(2026, 12, 18))

    def test_primeiro_pregao(self):
        self.assertEqual(cm.primeiro_pregao(2026, 12), date(2026, 12, 1))
        self.assertEqual(cm.primeiro_pregao(2026, 11), date(2026, 11, 3))   # 02/11 e feriado

    def test_eventos_do_intervalo(self):
        eventos = cm.eventos_calculados("2026-11-18", "2026-11-28")
        opcoes = [e for e in eventos if "BOVA11" in e["evento"]]
        self.assertEqual(len(opcoes), 1)
        self.assertEqual(opcoes[0]["ts_ms"], ts(20776, 17, 55))   # 20/11 e feriado: vai para 19/11
        tesouro = [e for e in eventos if "Tesouro" in e["evento"]]
        self.assertEqual({(e["ts_ms"] // D) for e in tesouro}, {20776, 20781, 20783})
        self.assertFalse(any(e["ts_ms"] // D == 20777 for e in eventos))   # 20/11 feriado


class TestEventosNoBanco(unittest.TestCase):
    def banco(self):
        db = sqlite3.connect(":memory:")
        db.executescript(cm.SCHEMA)
        return db

    def test_janela_evento(self):
        db = self.banco()
        agora = ts(20775, 16, 0)
        cm.inserir(db, [
            cm._evento(agora - 10 * 60_000, "EUA", "perto antes", "alto", "teste"),
            cm._evento(agora + 5 * 60_000, "BRA", "perto depois", "alto", "teste"),
            cm._evento(agora + 60 * 60_000, "EUA", "proximo", "alto", "teste"),
            cm._evento(agora, "BRA", "baixo", "baixo", "teste"),
        ])
        r = cm.janela_evento(db, agora)
        self.assertIn(r["alerta_evento"]["evento"], ("perto antes", "perto depois"))
        self.assertEqual(r["proximo"]["evento"], "proximo")

    def test_fora_da_janela(self):
        db = self.banco()
        agora = ts(20775, 12, 0)
        cm.inserir(db, [cm._evento(agora + 20 * 60_000, "EUA", "longe", "alto", "teste")])
        r = cm.janela_evento(db, agora)
        self.assertIsNone(r["alerta_evento"])
        self.assertEqual(r["proximo"]["evento"], "longe")

    def test_atualizar_fontes_offline_idempotente(self):
        with tempfile.TemporaryDirectory() as d:
            caminho = str(Path(d) / "cal.db")
            r1 = cm.atualizar_fontes(caminho, hoje=date(2026, 12, 1), online=False)
            r2 = cm.atualizar_fontes(caminho, hoje=date(2026, 12, 1), online=False)
            self.assertGreater(r1["manual"], 0)
            self.assertGreater(r1["calculado"], 0)
            self.assertEqual(r1["manual"], r2["manual"])
            con = sqlite3.connect(caminho)
            try:
                copom = con.execute("SELECT COUNT(*) FROM calendario_eventos WHERE evento LIKE 'Copom%' "
                                    "AND impacto='alto'").fetchone()[0]
                total = con.execute("SELECT COUNT(*) FROM calendario_eventos").fetchone()[0]
            finally:
                con.close()
            self.assertEqual(copom, 1)
            self.assertGreater(total, 10)


if __name__ == "__main__":
    unittest.main()
