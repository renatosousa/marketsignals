"""Testes do painel_v2.py com banco SQLite em memoria e dados sinteticos.

  python -m unittest test_painel_v2 -v

Cobre: janela_minutos, coleta (estados das fontes), alertas, fechamento_api, correlacoes_api,
decomposicao, vol_cartao e o cache dos endpoints novos do dashboard (App.v2).
"""
import json
import math
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import numpy as np

import correlacoes as corr
import painel_v2 as p

D = 86_400_000
MIN = 60_000
DIA = 20768              # quarta 11/11/2026, pregao util
DIA_ANT = 20767          # terca 10/11/2026
SABADO = 20771           # sabado 14/11/2026


def ts(dia, hh, mm=0):
    """ts_ms de um horario de Brasilia codificado como epoch UTC."""
    return dia * D + (hh * 60 + mm) * MIN


# ------------------------------------------------------------------ esquemas

SCHEMA_COLETA = """
CREATE TABLE snapshots (ts_ms INTEGER NOT NULL, symbol TEXT NOT NULL, mid REAL, spread REAL);
CREATE TABLE macro_cotacoes (ts_ms INTEGER NOT NULL, symbol TEXT NOT NULL, mid REAL);
CREATE TABLE opcoes_resumo (ts_ms INTEGER NOT NULL, subjacente TEXT NOT NULL, vencimento TEXT NOT NULL,
                            t_anos REAL, spot REAL, atm_iv REAL);
CREATE TABLE agressao_1min (ts_ms INTEGER NOT NULL, symbol TEXT NOT NULL);
CREATE TABLE tryd_saldo (ts_ms INTEGER NOT NULL, ativo TEXT NOT NULL);
CREATE TABLE tryd_fita (dia INTEGER NOT NULL, ativo TEXT NOT NULL, num INTEGER NOT NULL, ts_ms INTEGER,
                        PRIMARY KEY (dia, ativo, num));
CREATE TABLE regime_live (ts_ms INTEGER NOT NULL, rotulo TEXT, rv_rel REAL, rv_dia_real REAL,
                          rv_dia_prev REAL);
CREATE TABLE fluxo_diario (data_ref TEXT);
CREATE TABLE b3_posicoes_resumo (sessao TEXT);
CREATE TABLE anbima_ettj (data TEXT);
CREATE TABLE leilao_resultados (data TEXT);
CREATE TABLE focus_ipca (data TEXT);
CREATE TABLE barras_1m (symbol TEXT NOT NULL, ts_ms INTEGER NOT NULL, open REAL, high REAL, low REAL,
                        close REAL);
"""

SCHEMA_MERCADO = """
CREATE TABLE barras_1m (symbol TEXT NOT NULL, ts_ms INTEGER NOT NULL, open REAL, high REAL, low REAL,
                        close REAL);
CREATE TABLE snapshots (ts_ms INTEGER NOT NULL, symbol TEXT NOT NULL, mid REAL, spread REAL);
CREATE TABLE macro_cotacoes (ts_ms INTEGER NOT NULL, symbol TEXT NOT NULL, mid REAL);
CREATE TABLE opcoes_resumo (ts_ms INTEGER NOT NULL, subjacente TEXT NOT NULL, vencimento TEXT NOT NULL,
                            t_anos REAL, spot REAL, atm_iv REAL);
CREATE TABLE regime_live (ts_ms INTEGER NOT NULL, rotulo TEXT, rv_rel REAL, rv_dia_real REAL,
                          rv_dia_prev REAL);
CREATE TABLE tryd_saldo (ts_ms INTEGER NOT NULL, ativo TEXT NOT NULL);
CREATE TABLE correl_diaria (data TEXT NOT NULL, y TEXT NOT NULL, x TEXT NOT NULL, corr REAL, beta REAL,
                            n INTEGER, calculado_em TEXT, PRIMARY KEY (data, y, x));
CREATE TABLE fatores_dia (data TEXT NOT NULL, alvo TEXT NOT NULL, fator TEXT NOT NULL, beta REAL, r2 REAL,
                          n INTEGER, PRIMARY KEY (data, alvo, fator));
"""


def banco_coleta():
    """Banco com as 14 fontes de coleta em dia as 14:00 de 11/11/2026 (quarta). -> (con, agora_ms)."""
    agora = DIA * D + 14 * 3_600_000
    t = agora - 10_000                                    # 13:59:50
    db = sqlite3.connect(":memory:")
    db.executescript(SCHEMA_COLETA)
    db.executemany("INSERT INTO snapshots (ts_ms, symbol, mid, spread) VALUES (?,?,?,?)",
                   [(t - 60_000, "WIN$", 100.0, 5.0), (t - 30_000, "WIN$", 101.0, 5.0),
                    (t, "WIN$", 102.0, 5.0), (t, "BOVA11", 200.0, 0.1)])
    db.executemany("INSERT INTO macro_cotacoes (ts_ms, symbol, mid) VALUES (?,?,?)",
                   [(t, "WDO$", 5.0), (t - 5 * MIN, "IB:VIX", 18.0)])
    db.execute("INSERT INTO opcoes_resumo (ts_ms, subjacente, vencimento, t_anos, spot, atm_iv) "
               "VALUES (?,?,?,?,?,?)", (t, "BOVA11", "2026-11-19", 0.1, 200.0, 0.2))
    db.execute("INSERT INTO agressao_1min (ts_ms, symbol) VALUES (?,?)", (t, "WIN$"))
    db.execute("INSERT INTO tryd_saldo (ts_ms, ativo) VALUES (?,?)", (t, "WINZ26"))
    db.execute("INSERT INTO tryd_fita (dia, ativo, num, ts_ms) VALUES (?,?,?,?)", (DIA, "WINZ26", 1, t))
    db.execute("INSERT INTO regime_live (ts_ms, rotulo) VALUES (?,?)", (t, "vol normal"))
    db.execute("INSERT INTO fluxo_diario (data_ref) VALUES ('2026-11-09')")
    db.execute("INSERT INTO b3_posicoes_resumo (sessao) VALUES ('2026-11-10')")
    db.execute("INSERT INTO anbima_ettj (data) VALUES ('2026-11-10')")
    db.execute("INSERT INTO leilao_resultados (data) VALUES ('2026-11-08')")
    db.execute("INSERT INTO focus_ipca (data) VALUES ('2026-11-06')")
    db.executemany("INSERT INTO barras_1m (symbol, ts_ms, open, high, low, close) VALUES (?,?,?,?,?,?)",
                   [(s, ts(DIA_ANT, 18, 30), 1.0, 1.0, 1.0, 1.0) for s in p.MT5_ATIVOS])
    return db, agora


def estados(c):
    return {f["nome"]: f["estado"] for f in c["fontes"]}


class TestJanela(unittest.TestCase):
    def test_janelas_no_meio_do_pregao(self):
        t1 = ts(DIA, 10, 0)                     # min_fim = 600
        self.assertEqual(p.janela_minutos(t1, "3600"), (540, 600))
        self.assertEqual(p.janela_minutos(t1, "10800"), (420, 600))
        self.assertEqual(p.janela_minutos(t1, "pregao"), (corr.SESSAO_FUTUROS[0], 600))

    def test_janela_maior_que_o_dia_comeca_em_zero(self):
        self.assertEqual(p.janela_minutos(ts(DIA, 0, 20), "3600"), (0, 20))

    def test_janela_invalida(self):
        with self.assertRaises(ValueError):
            p.janela_minutos(ts(DIA, 10, 0), "60")


class TestColeta(unittest.TestCase):
    def test_tudo_em_dia(self):
        db, agora = banco_coleta()
        c = p.coleta(db, agora)
        self.assertEqual(c["total"], 14)
        self.assertEqual(c["em_dia"], 14)
        self.assertEqual(c["pior"], "ok")
        self.assertEqual(set(estados(c).values()), {"ok"})
        self.assertEqual({f["nome"]: f["ultimo"] for f in c["fontes"]}["Focus"], "06/11")

    def test_focus_e_leiloes_atrasados_e_win_parado(self):
        db, agora = banco_coleta()
        db.execute("UPDATE focus_ipca SET data='2026-10-21'")            # 21 dias corridos
        db.execute("UPDATE leilao_resultados SET data='2026-11-03'")     # 8 dias corridos
        db.execute("UPDATE snapshots SET ts_ms=ts_ms-3600000 WHERE symbol='WIN$'")
        c = p.coleta(db, agora)
        e = estados(c)
        self.assertEqual(e["Focus"], "atrasado")
        self.assertEqual(e["Leilões do Tesouro"], "atrasado")
        self.assertEqual(e["WIN (livro)"], "parado")
        self.assertEqual(c["em_dia"], 11)
        self.assertEqual(c["pior"], "parado")
        detalhes = {f["nome"]: f["detalhe"] for f in c["fontes"]}
        self.assertIn("21 dias corridos", detalhes["Focus"])
        self.assertIn("8 dias corridos", detalhes["Leilões do Tesouro"])
        self.assertIn("sem leitura", detalhes["WIN (livro)"])

    def test_preco_congelado_marca_parado(self):
        db, agora = banco_coleta()
        db.execute("DELETE FROM snapshots WHERE symbol='WIN$'")
        t = agora - 10_000
        db.executemany("INSERT INTO snapshots (ts_ms, symbol, mid, spread) VALUES (?,?,?,?)",
                       [(t - 600_000, "WIN$", 100.0, 5.0), (t - 300_000, "WIN$", 100.0, 5.0),
                        (t, "WIN$", 100.0, 5.0)])
        f = p.coleta(db, agora)["fontes"][0]
        self.assertEqual(f["nome"], "WIN (livro)")
        self.assertEqual(f["estado"], "parado")
        self.assertIn("preço congelado há 600 s", f["detalhe"])

    def test_sabado_fora_do_horario(self):
        db, _ = banco_coleta()
        c = p.coleta(db, ts(SABADO, 12, 0))
        self.assertEqual(c["total"], 14)
        self.assertEqual(c["em_dia"], 14)
        self.assertEqual(c["pior"], "ok")
        self.assertEqual(set(estados(c).values()), {"fora do horario"})

    def test_tabela_ausente_vira_sem_dados_e_piora_o_resumo(self):
        db, _ = banco_coleta()
        db.execute("DROP TABLE focus_ipca")
        c = p.coleta(db, ts(SABADO, 12, 0))
        e = estados(c)
        self.assertEqual(e["Focus"], "sem dados")            # sem dados vence fora do horario
        self.assertEqual(e["Barras de 1 min"], "fora do horario")
        self.assertEqual(c["em_dia"], 13)
        self.assertEqual(c["pior"], "atrasado")

    def test_banco_vazio_todas_sem_dados(self):
        c = p.coleta(sqlite3.connect(":memory:"), ts(DIA, 14, 0))
        self.assertEqual(c["total"], 14)
        self.assertEqual(c["em_dia"], 0)
        self.assertEqual(set(estados(c).values()), {"sem dados"})
        self.assertEqual(c["pior"], "atrasado")

    def test_barras_que_nao_chegam_ao_fim_do_pregao(self):
        db, agora = banco_coleta()
        db.execute("DELETE FROM barras_1m WHERE symbol='WIN$' AND ts_ms % ? > ?", (D, 17 * 60 * MIN))
        c = p.coleta(db, agora)
        f = {x["nome"]: x for x in c["fontes"]}["Barras de 1 min"]
        self.assertEqual(f["estado"], "atrasado")
        self.assertIn("WIN$", f["detalhe"])
        self.assertEqual(c["pior"], "atrasado")

    def test_max_ts_fita_percorre_o_ultimo_dia(self):
        db = sqlite3.connect(":memory:")
        db.executescript("""
            CREATE TABLE tryd_fita (dia INTEGER NOT NULL, ativo TEXT NOT NULL, num INTEGER NOT NULL,
                                    ts_ms INTEGER, PRIMARY KEY (dia, ativo, num));
            CREATE TABLE tryd_saldo (ts_ms INTEGER NOT NULL, ativo TEXT NOT NULL);
        """)
        db.executemany("INSERT INTO tryd_fita (dia, ativo, num, ts_ms) VALUES (?,?,?,?)",
                       [(DIA_ANT, "WINZ26", 1, ts(DIA_ANT, 18, 0)),
                        (DIA, "WINZ26", 1, ts(DIA, 10, 0)),
                        (DIA, "WDOZ26", 1, ts(DIA, 10, 1))])
        db.executemany("INSERT INTO tryd_saldo (ts_ms, ativo) VALUES (?,?)",
                       [(ts(DIA, 10, 1), "WINZ26"), (ts(DIA, 10, 1), "WDOZ26")])
        self.assertEqual(p._max_ts_fita(db), ts(DIA, 10, 1))
        self.assertIsNone(p._max_ts_fita(sqlite3.connect(":memory:")))


class TestAlertas(unittest.TestCase):
    def test_ordem_dos_niveis_e_texto_do_descolamento(self):
        coleta_ = {"fontes": [{"nome": "Focus", "estado": "atrasado", "detalhe": "atraso de 400 s"},
                              {"nome": "WIN (livro)", "estado": "parado",
                               "detalhe": "sem leitura há 5 min"},
                              {"nome": "Macro", "estado": "ok", "detalhe": None}]}
        regime = {"alertas": [{"nivel": "serio", "texto": "VIX alto"}, "texto solto"]}
        principal = {"y": "WIN$", "x": "WDO$", "corr": -0.15, "z": 2.9, "ref": {"media": -0.47},
                     "alerta": True}
        a = p.alertas(coleta_, regime, [principal])
        self.assertEqual([x["nivel"] for x in a], ["critico", "atencao", "atencao", "info", "info"])
        self.assertEqual(a[0]["texto"], "Coleta parada: WIN (livro) (sem leitura há 5 min)")
        self.assertEqual(a[1]["texto"], "Coleta atrasada: Focus (atraso de 400 s)")
        self.assertEqual(a[2], {"nivel": "atencao", "texto": "VIX alto"})
        self.assertEqual(a[3], {"nivel": "info", "texto": "texto solto"})
        self.assertEqual(a[4]["texto"], "WIN × dólar em -0,15; o normal é -0,47 (z +2,9)")

    def test_sem_alertas(self):
        self.assertEqual(p.alertas({"fontes": []}, None, []),
                         [{"nivel": "info", "texto": "Sem alertas"}])

    def test_descolamento_exige_alerta(self):
        principal = {"y": "WIN$", "x": "WDO$", "corr": -0.15, "z": 2.9, "ref": {"media": -0.47},
                     "alerta": False}
        self.assertEqual(p.alertas({"fontes": []}, None, [principal]),
                         [{"nivel": "info", "texto": "Sem alertas"}])


class TestFechamento(unittest.TestCase):
    def banco(self):
        """WIN$ com 4 barras no dia e 1 no anterior, DI1F28 e opcoes em dois dias."""
        db = sqlite3.connect(":memory:")
        db.executescript(SCHEMA_MERCADO)
        db.executemany("INSERT INTO barras_1m (symbol, ts_ms, open, high, low, close) "
                       "VALUES (?,?,?,?,?,?)",
                       [("WIN$", ts(DIA, 9, 30), 100.0, 101.0, 99.0, 100.0),
                        ("WIN$", ts(DIA, 11, 0), 110.0, 111.0, 109.0, 110.0),
                        ("WIN$", ts(DIA, 14, 0), 105.0, 106.0, 104.0, 105.0),
                        ("WIN$", ts(DIA, 17, 0), 95.0, 96.0, 94.0, 95.0),
                        ("WIN$", ts(DIA_ANT, 18, 20), 90.0, 90.0, 90.0, 90.0)])
        db.executemany("INSERT INTO macro_cotacoes (ts_ms, symbol, mid) VALUES (?,?,?)",
                       [(ts(DIA_ANT, 17, 0), "DI1F28", 13.0), (ts(DIA, 14, 0), "DI1F28", 13.5)])
        db.executemany("INSERT INTO opcoes_resumo (ts_ms, subjacente, vencimento, t_anos, spot, atm_iv) "
                       "VALUES (?,?,?,?,?,?)",
                       [(ts(DIA_ANT, 14, 0), "BOVA11", "2026-11-19", 0.1, 200.0, 0.18),
                        (ts(DIA, 14, 0), "BOVA11", "2026-11-19", 0.1, 200.0, 0.20)])
        db.execute("INSERT INTO regime_live (ts_ms, rotulo, rv_rel, rv_dia_real, rv_dia_prev) "
                   "VALUES (?,?,?,?,?)", (ts(DIA, 14, 0), "vol normal", 0.5, 0.5, 0.6))
        return db

    def test_precos_faixas_di_vol_e_regime(self):
        r = p.fechamento_api(self.banco(), DIA)
        w = r["win"]
        self.assertEqual((r["dia"], r["data"]), (DIA, "2026-11-11"))
        self.assertEqual((w["abertura"], w["minima"], w["maxima"], w["ultimo"]),
                         (100.0, 94.0, 111.0, 95.0))
        self.assertEqual(w["anterior"], 90.0)
        self.assertAlmostEqual(w["var_pct"], (95.0 / 90.0 - 1) * 100)
        faixas = [f["var_pct"] for f in r["win_faixas"]]
        self.assertEqual([f["faixa"] for f in r["win_faixas"]], ["09-10", "10-13", "13-16", "16-fim"])
        self.assertAlmostEqual(faixas[0], 0.0)
        self.assertAlmostEqual(faixas[1], 10.0)
        self.assertAlmostEqual(faixas[2], (105.0 / 110.0 - 1) * 100)
        self.assertAlmostEqual(faixas[3], (95.0 / 105.0 - 1) * 100)
        # as faixas sao encadeadas: o produto delas reconstroi a variacao do primeiro ao ultimo preco
        prod = np.prod([1 + v / 100 for v in faixas])
        self.assertAlmostEqual(prod, 95.0 / 100.0)
        self.assertEqual([d["simbolo"] for d in r["di"]], ["DI1F28", "DI1F29", "DI1F33"])
        self.assertAlmostEqual(r["di"][0]["taxa"], 13.5)
        self.assertAlmostEqual(r["di"][0]["var_bps"], 50.0)
        self.assertIsNone(r["di"][1]["taxa"])
        self.assertEqual([v["vencimento"] for v in r["vol"]], ["2026-11-19"])
        self.assertAlmostEqual(r["vol"][0]["atm_iv"], 20.0)
        self.assertAlmostEqual(r["vol"][0]["var_dia"], 2.0)
        self.assertEqual(r["regime"], {"rotulo": "vol normal", "rv_rel": 0.5})
        self.assertIsNone(r["estrangeiro"])
        self.assertEqual(r["players"], {})
        self.assertEqual(r["texto_final"], "Leitura dos dados, não recomendação.")
        self.assertTrue(all(a["var_pct"] is None for a in r["acoes"]))

    def test_aceita_texto_e_igual_ao_indice(self):
        db = self.banco()
        self.assertEqual(p.fechamento_api(db, "2026-11-11")["win"], p.fechamento_api(db, DIA)["win"])

    def test_dia_none_sem_snapshots_devolve_vazio(self):
        db = sqlite3.connect(":memory:")
        db.executescript(SCHEMA_MERCADO)
        r = p.fechamento_api(db, None)
        self.assertIsNone(r["dia"])
        self.assertIsNone(r["win"]["abertura"])
        self.assertEqual(r["win_faixas"], [])
        self.assertEqual(r["texto_final"], "Leitura dos dados, não recomendação.")


def banco_correlacoes(semente=0):
    """WIN$ (snapshots) e WDO$ (macro) de 10:00 a 11:40 de 11/11/2026, WIN = -0,5 x WDO + ruido."""
    db = sqlite3.connect(":memory:")
    db.executescript(SCHEMA_MERCADO)
    minutos = np.arange(10 * 60, 11 * 60 + 41)
    rng = np.random.default_rng(semente)
    r_wdo = rng.normal(0.0, 0.001, len(minutos))
    r_win = -0.5 * r_wdo + rng.normal(0.0, 0.0005, len(minutos))
    wdo = 5.0 * np.exp(np.cumsum(r_wdo))
    win = 100_000.0 * np.exp(np.cumsum(r_win))
    db.executemany("INSERT INTO snapshots (ts_ms, symbol, mid, spread) VALUES (?,?,?,?)",
                   [(ts(DIA, 0, int(m)), "WIN$", float(v), 5.0) for m, v in zip(minutos, win)])
    db.executemany("INSERT INTO macro_cotacoes (ts_ms, symbol, mid) VALUES (?,?,?)",
                   [(ts(DIA, 0, int(m)), "WDO$", float(v)) for m, v in zip(minutos, wdo)])
    return db


def referencia(db, y, x, corrs):
    """20 dias de correl_diaria antes de 11/11/2026."""
    inicio = date(2026, 10, 1)
    db.executemany("INSERT INTO correl_diaria (data, y, x, corr, beta, n, calculado_em) "
                   "VALUES (?,?,?,?,?,?,?)",
                   [((inicio + timedelta(days=i)).isoformat(), y, x, c, 0.0, 300,
                     "2026-11-01T08:00:00") for i, c in enumerate(corrs)])


class TestCorrelacoes(unittest.TestCase):
    def test_tabela_ordena_por_z_e_sem_z_no_fim(self):
        db = banco_correlacoes()
        referencia(db, "WIN$", "WDO$", [-0.5 + 0.01 * (i % 3) for i in range(20)])
        r = p.correlacoes_api(db, "pregao", "WIN")
        self.assertEqual(r["n_minutos"], 700 - 545)              # 09:05 ate 11:40
        self.assertEqual(len(r["tabela"]), len(corr.PARES_WIN))
        self.assertEqual(r["tabela"][0]["x"], "WDO$")            # unico par com referencia
        self.assertIsNotNone(r["tabela"][0]["z"])
        self.assertLess(r["tabela"][0]["corr"], -0.5)
        self.assertTrue(all(l["z"] is None for l in r["tabela"][1:]))
        self.assertTrue(all(l["ref"] is None for l in r["tabela"][1:]))
        self.assertEqual(r["decomposicao"], {"motivo": "sem betas: rode correlacoes.py atualizar"})
        principal = r["principais"][0]
        self.assertEqual((principal["y"], principal["x"]), ("WIN$", "WDO$"))
        self.assertLess(principal["corr"], -0.5)
        self.assertFalse(principal["alerta"])
        self.assertIsNotNone(r["movel"][0]["p10"])
        self.assertIsNone(r["movel"][1]["p10"])
        self.assertEqual(r["matriz"]["simbolos"], corr.MATRIZ)
        self.assertIsNone(r["matriz"]["hoje"][0][0])

    def test_decomposicao_com_betas(self):
        db = banco_correlacoes(1)
        db.execute("INSERT INTO fatores_dia (data, alvo, fator, beta, r2, n) VALUES (?,?,?,?,?,?)",
                   ("2026-11-10", "WIN$", "WDO$", -0.5, 0.8, 2000))
        d = p.decomposicao(db, DIA, "pregao")
        self.assertEqual(d["betas_data"], "2026-11-10")
        self.assertEqual(d["r2_treino"], 0.8)
        self.assertEqual(d["data"], "2026-11-11")
        self.assertEqual([f["fator"] for f in d["fatores"]], ["WDO$"])
        self.assertGreater(d["n"], 60)
        self.assertGreater(d["r2_hoje"], 0.05)
        self.assertTrue(d["texto"].startswith("o normal explica"))
        self.assertTrue(d["texto"].endswith("% do minuto a minuto de hoje"))
        self.assertAlmostEqual(d["total_pct"],
                               sum(f["pct"] for f in d["fatores"]) + d["propria_pct"], places=10)

    def test_decomposicao_sem_preco_do_win(self):
        db = banco_correlacoes()
        db.execute("INSERT INTO fatores_dia (data, alvo, fator, beta, r2, n) VALUES (?,?,?,?,?,?)",
                   ("2026-11-10", "WIN$", "WDO$", -0.5, 0.8, 2000))
        self.assertIsNone(p.decomposicao(db, DIA_ANT, "pregao"))   # dia sem snapshots do WIN$


class TestCorrBaldes(unittest.TestCase):
    def test_amostra_curta(self):
        self.assertEqual(p._corr_baldes({}, {}, 5, 40)[0], None)
        self.assertEqual(p._corr_baldes({0: 1.0, 1: 2.0}, {0: 1.0, 1: 2.0}, 5, 40), (None, 1))

    def test_correlacao_de_5_min(self):
        rng = np.random.default_rng(2)
        minutos = list(range(0, 300))
        var = rng.normal(0.0, 1.0, len(minutos))
        a = -np.cumsum(var)                        # a cai quando b sobe
        b = np.exp(0.001 * np.cumsum(var))
        corr_, n = p._corr_baldes(dict(zip(minutos, a)), dict(zip(minutos, b)), 5, 40)
        self.assertLess(corr_, -0.9)
        self.assertEqual(n, 60 - 1)                # 60 baldes de 5 min em 300 minutos


class TestVolCartao(unittest.TestCase):
    def banco(self, dias=30):
        db = sqlite3.connect(":memory:")
        db.executescript(SCHEMA_MERCADO)
        rows, ops = [], []
        for i in range(dias):
            d = DIA - (dias - 1 - i)
            rows.append((ts(d, 14, 0), "vol normal", 0.5, 0.5, 0.6))
            ops.append((ts(d, 14, 0), "BOVA11", "2026-12-18", 0.1, 200.0, 0.20))
        db.executemany("INSERT INTO regime_live (ts_ms, rotulo, rv_rel, rv_dia_real, rv_dia_prev) "
                       "VALUES (?,?,?,?,?)", rows)
        db.executemany("INSERT INTO opcoes_resumo (ts_ms, subjacente, vencimento, t_anos, spot, atm_iv) "
                       "VALUES (?,?,?,?,?,?)", ops)
        # vencimento curto demais (t_anos < 5/252): a regra de grafico_quant.implicito o descarta
        db.execute("INSERT INTO opcoes_resumo (ts_ms, subjacente, vencimento, t_anos, spot, atm_iv) "
                   "VALUES (?,?,?,?,?,?)", (ts(DIA, 14, 0), "BOVA11", "2026-11-19", 0.01, 200.0, 0.99))
        return db

    def test_implicita_realizada_e_previsao(self):
        r = p.vol_cartao(self.banco(), DIA)
        self.assertEqual(r["vencimento"], "2026-12-18")
        self.assertAlmostEqual(r["implicita"], 20.0)
        self.assertAlmostEqual(r["realizada"], 0.5 * math.sqrt(252))
        self.assertAlmostEqual(r["prevista"], 0.6 * math.sqrt(252))
        self.assertAlmostEqual(r["dif"], 20.0 - 0.5 * math.sqrt(252))
        self.assertEqual(len(r["serie"]["datas"]), 30)
        self.assertEqual(r["serie"]["datas"][-1], "2026-11-11")
        self.assertAlmostEqual(r["serie"]["implicita"][0], 20.0)
        self.assertIsNone(r["vol_preco"])         # menos de 40 baldes de 5 min

    def test_vol_preco_com_40_baldes(self):
        db = self.banco()
        rng = np.random.default_rng(3)
        iv = 0.2 + 0.001 * rng.normal(size=240)
        spot = 200.0 * np.exp(np.cumsum(0.0005 * rng.normal(size=240)))
        db.executemany("INSERT INTO opcoes_resumo (ts_ms, subjacente, vencimento, t_anos, spot, atm_iv) "
                       "VALUES (?,?,?,?,?,?)",
                       [(ts(DIA, 10, 0) + i * MIN, "BOVA11", "2026-12-18", 0.1, float(s), float(v))
                        for i, (v, s) in enumerate(zip(iv, spot))])
        r = p.vol_cartao(db, DIA)
        self.assertIsNotNone(r["vol_preco"])
        self.assertLessEqual(abs(r["vol_preco"]), 1.0)


class TestCacheV2(unittest.TestCase):
    def test_guarda_por_chave_e_limpa_o_json(self):
        import dashboard
        with tempfile.TemporaryDirectory() as pasta:
            caminho = str(Path(pasta) / "vazio.db")
            sqlite3.connect(caminho).close()
            app = dashboard.App(caminho)
            chamadas = []

            def calculo(con):
                chamadas.append(1)
                return {"a": len(chamadas), "b": float("nan")}

            r1 = app.v2("fechamento", ("v2", "teste"), calculo)
            r2 = app.v2("fechamento", ("v2", "teste"), calculo)
            self.assertEqual(r1, r2)                  # segunda chamada veio do cache
            self.assertEqual(len(chamadas), 1)
            self.assertEqual(json.loads(r1), {"a": 1, "b": None})
            app.v2("fechamento", ("v2", "outra"), calculo)
            self.assertEqual(len(chamadas), 2)


if __name__ == "__main__":
    unittest.main()
