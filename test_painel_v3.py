"""Testes do painel_v3.py: leitura do estado consolidado, idade/desatualizado e recortes da tela.

  python -m unittest test_painel_v3 -v

Usa banco SQLite em memoria com a tabela estado_cockpit_v3; nada e recalculado (o atualizador e o
dono da gravacao). Cobre tambem o cache do endpoint no dashboard.App e o resumo do estudo V3-9.
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import painel_v3 as p

D = 86_400_000
DIA = 20774                        # terca 17/11/2026 (pregao)
SABADO = 20778                     # sabado 21/11/2026


def ts(dia, hh, mm=0):
    return dia * D + (hh * 60 + mm) * 60_000


SCHEMA = """
CREATE TABLE estado_cockpit_v3 (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    ts_ms INTEGER NOT NULL,
    json TEXT NOT NULL,
    tempos_json TEXT NOT NULL);
"""


def estado_exemplo():
    return {
        "ts_ms": ts(DIA, 15, 0),
        "saude": {"resumo": "MT5 OK | Tryd fita 98% | Global OK", "problemas": 0, "itens": []},
        "cenario": {"score_estrutural": -68.0, "score_intraday": 18.0,
                    "rotulos": {"estrutural": "BAIXA FORTE", "intraday": "REPIQUE LEVE"},
                    "arquetipo": {"nome": "Repique contra a tendência"},
                    "validacao": {"n": 119, "postos_dia": -0.18, "postos_seguinte": -0.11,
                                  "postos_5p": -0.18, "acerto_35_dia": 0.32, "n_sinal_dia": 41},
                    "briefing": ["linha 1", "linha 2", "linha 3", "linha 4"]},
        "regioes": {
            "WIN$": {"preco": 100000.0, "vwap": 100100.0, "gamma_flip": 99900.0, "er": 0.42,
                     "zonas": [{"id_zona": "R1", "lado": "ACIMA", "preco_centro": 101000.0,
                                "gatilho": {"status": "PROXIMA"}}]},
            "WDO$": {"preco": 5.5, "zonas": []},
            "BOVA11": {"preco": 200.0, "zonas": []},
        },
        "swing": {
            "WIN$": {"zonas": [{"id_zona": "R1", "preco_centro": 105000.0}]},
            "WDO$": {"zonas": []},
            "invalidacao": ["frase 1", "frase 2"],
        },
        "opcoes": {"diagnostico": {"spot": 200.0, "iv_atm": 0.25}, "a_favor": [], "contra": [],
                   "motivo": "teste"},
        "estudo_regioes": {"n_pregoes": 37, "texto": "heurística não validada"},
        "tempos": {"cenario": 1.0},
    }


def banco(estado=None, ts_ms=None, texto_json=None):
    db = sqlite3.connect(":memory:")
    db.executescript(SCHEMA)
    if texto_json is not None:
        db.execute("INSERT INTO estado_cockpit_v3 VALUES (1,?,?,?)", (ts(DIA, 15, 0), texto_json, "{}"))
    elif estado is not None:
        db.execute("INSERT INTO estado_cockpit_v3 VALUES (1,?,?,?)",
                   (ts_ms if ts_ms is not None else estado["ts_ms"],
                    json.dumps(estado, ensure_ascii=False), json.dumps(estado.get("tempos") or {})))
    db.commit()
    return db


class TestCockpit(unittest.TestCase):
    def test_estado_fresco_no_pregao(self):
        e = p.cockpit(banco(estado_exemplo()), agora_ms=ts(DIA, 15, 1))
        self.assertFalse(e["desatualizado"])
        self.assertAlmostEqual(e["idade_s"], 60.0, places=1)
        self.assertEqual(e["cenario"]["score_estrutural"], -68.0)
        self.assertEqual(e["tempos"], {"cenario": 1.0})

    def test_estado_velho_no_pregao_fica_desatualizado(self):
        e = p.cockpit(banco(estado_exemplo()), agora_ms=ts(DIA, 15, 3))   # 3 min depois
        self.assertTrue(e["desatualizado"])
        self.assertIn("pregao", e["motivo_idade"])

    def test_estado_velho_fora_do_pregao_nao_e_falha(self):
        e = p.cockpit(banco(estado_exemplo()), agora_ms=ts(DIA, 20, 0))
        self.assertFalse(e["desatualizado"])
        self.assertAlmostEqual(e["idade_s"], 5 * 3600.0, places=1)

    def test_sem_linha(self):
        e = p.cockpit(banco(), agora_ms=ts(DIA, 15, 0))
        self.assertTrue(e["desatualizado"])
        self.assertTrue(e["vazio"])
        self.assertIsNone(e["idade_s"])
        self.assertIn("atualizador_cockpit", e["motivo"])

    def test_json_invalido_nao_derruba(self):
        e = p.cockpit(banco(texto_json="{nao e json"), agora_ms=ts(DIA, 15, 0))
        self.assertTrue(e["desatualizado"])
        self.assertIsNone(e["idade_s"])

    def test_tabela_ausente(self):
        db = sqlite3.connect(":memory:")
        e = p.cockpit(db, agora_ms=ts(DIA, 15, 0))
        self.assertTrue(e["vazio"])


class TestRegioes(unittest.TestCase):
    def setUp(self):
        self.db = banco(estado_exemplo())
        self.agora = ts(DIA, 15, 0)

    def test_daytrade_do_win(self):
        r = p.regioes(self.db, "WIN$", "DAYTRADE", agora_ms=self.agora)
        self.assertEqual(r["ativo"], "WIN$")
        self.assertEqual(r["preco"], 100000.0)
        self.assertEqual(len(r["zonas"]), 1)
        self.assertFalse(r["desatualizado"])

    def test_aliases(self):
        self.assertEqual(p.regioes(self.db, "WIN", agora_ms=self.agora)["ativo"], "WIN$")
        self.assertEqual(p.regioes(self.db, "win$", "daytrade", agora_ms=self.agora)["horizonte"], "DAYTRADE")

    def test_swing_traz_invalidacao(self):
        r = p.regioes(self.db, "WIN$", "SWING", agora_ms=self.agora)
        self.assertEqual(len(r["zonas"]), 1)
        self.assertEqual(r["invalidacao"], ["frase 1", "frase 2"])

    def test_horizonte_sem_ativo(self):
        r = p.regioes(self.db, "BOVA11", "SWING", agora_ms=self.agora)
        self.assertEqual(r["zonas"], [])
        self.assertIn("motivo", r)

    def test_parametros_invalidos(self):
        with self.assertRaises(ValueError):
            p.regioes(self.db, "PETR4")
        with self.assertRaises(ValueError):
            p.regioes(self.db, "WIN$", "SEMANAL")


class TestOpcoes(unittest.TestCase):
    def test_estruturas_do_estado(self):
        r = p.opcoes_estruturas(banco(estado_exemplo()))
        self.assertEqual(r["diagnostico"]["spot"], 200.0)
        self.assertEqual(r["motivo"], "teste")
        self.assertIn("idade_s", r)

    def test_ativo_invalido(self):
        with self.assertRaises(ValueError):
            p.opcoes_estruturas(banco(estado_exemplo()), "WDO$")


class TestEstudo(unittest.TestCase):
    def test_csv_do_repositorio(self):
        r = p.estudo_regioes_resumo()
        if not p.ARQ_ESTUDO.exists():
            self.assertFalse(r["csv"])
        else:
            self.assertTrue(r["csv"])
            self.assertIn("heurística não validada", r["texto"])
            self.assertIn("60-79", r["detalhe"])

    def test_sem_csv_usa_texto_fixo(self):
        with mock.patch.object(p, "ARQ_ESTUDO", Path(tempfile.gettempdir()) / "nao_existe_estudo.csv"):
            r = p.estudo_regioes_resumo()
        self.assertFalse(r["csv"])
        self.assertEqual(r["n_pregoes"], 37)
        self.assertEqual(r["texto"], p.TEXTO_ESTUDO_PADRAO)

    def test_csv_proprio(self):
        with tempfile.TemporaryDirectory() as pasta:
            arq = Path(pasta) / "estudo.csv"
            arq.write_text(
                "faixa,alinhamento,filtro,horizonte,n_toques,n_dias,zona_ret,rand_ret,diff_ret\n"
                "60-79,A_FAVOR_TENDENCIA,sem_filtro,15,10,5,-100.0,50.0,-150.0\n"
                "80+,ALVO/NEUTRA,sem_filtro,15,5,4,200.0,10.0,190.0\n", encoding="utf-8")
            with mock.patch.object(p, "ARQ_ESTUDO", arq):
                r = p.estudo_regioes_resumo()
        self.assertTrue(r["csv"])
        self.assertIn("-150", r["detalhe"])
        self.assertEqual(r["faixas"]["60-79"]["n_toques"], 10)


class TestCacheDashboard(unittest.TestCase):
    def test_cockpit_usa_cache_de_2s(self):
        import dashboard
        with tempfile.TemporaryDirectory() as pasta:
            caminho = str(Path(pasta) / "cockpit.db")
            db = sqlite3.connect(caminho)
            db.executescript(SCHEMA)
            estado = estado_exemplo()
            db.execute("INSERT INTO estado_cockpit_v3 VALUES (1,?,?,?)",
                       (estado["ts_ms"], json.dumps(estado, ensure_ascii=False), "{}"))
            db.commit()
            db.close()
            app = dashboard.App(caminho)
            r1 = app.v2("cockpit_v3", ("v3", "cockpit"), p.cockpit)
            r2 = app.v2("cockpit_v3", ("v3", "cockpit"), p.cockpit)
            self.assertEqual(r1, r2)
            corpo = json.loads(r1)
            self.assertEqual(corpo["cenario"]["score_estrutural"], -68.0)
            self.assertIn("idade_s", corpo)
            self.assertLessEqual(dashboard.CACHE_V2_S["cockpit_v3"], 2.0)


if __name__ == "__main__":
    unittest.main()
