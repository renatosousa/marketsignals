"""Testes do motor_cenario.py com banco sintetico (5 pilares, arquetipo, briefing, historico e avaliar).

  python -m unittest test_motor_cenario -v
"""
import math
import random
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import numpy as np

import motor_cenario as mc

D = mc.D

SCHEMA = """
CREATE TABLE barras_1m (symbol TEXT NOT NULL, ts_ms INTEGER NOT NULL, fonte TEXT, contrato TEXT,
    open REAL, high REAL, low REAL, close REAL, volume REAL, PRIMARY KEY (symbol, ts_ms));
CREATE TABLE macro_cotacoes (ts_ms INTEGER NOT NULL, sessao TEXT NOT NULL, symbol TEXT NOT NULL, grupo TEXT,
    bid REAL, ask REAL, last REAL, mid REAL);
CREATE INDEX ix_macro ON macro_cotacoes (symbol, ts_ms);
CREATE TABLE fechamento_diario (dia_idx INTEGER NOT NULL, ativo TEXT NOT NULL, abertura REAL, maxima REAL,
    minima REAL, ultimo_liquido REAL NOT NULL, ultimo_dia REAL, ajuste_b3 REAL, vwap_dia REAL, poc_dia REAL,
    vah_dia REAL, val_dia REAL, vol_realizada_dia REAL, fonte TEXT, ts_consolidacao_ms INTEGER NOT NULL,
    PRIMARY KEY (dia_idx, ativo));
CREATE TABLE global_barras (ts_ms INTEGER NOT NULL, ativo TEXT NOT NULL, intervalo TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, PRIMARY KEY (ts_ms, ativo, intervalo));
CREATE TABLE fluxo_diario (data_ref TEXT NOT NULL, tipo TEXT NOT NULL, compras_mil REAL, vendas_mil REAL,
    saldo_mil REAL, data_pub TEXT, PRIMARY KEY (data_ref, tipo));
CREATE TABLE focus_ipca (data TEXT NOT NULL, ts_ms INTEGER NOT NULL, data_ref INTEGER NOT NULL, mediana REAL,
    media REAL, desvio_padrao REAL, minimo REAL, maximo REAL, top5 REAL, capturado_em TEXT NOT NULL,
    PRIMARY KEY (data, data_ref));
CREATE TABLE leilao_resultados (id INTEGER PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE leilao_metricas (resultado_id INTEGER PRIMARY KEY, concessao_bps REAL, curva_bps REAL);
CREATE TABLE opcoes_resumo (ts_ms INTEGER NOT NULL, sessao TEXT NOT NULL, subjacente TEXT NOT NULL,
    vencimento TEXT NOT NULL, t_anos REAL, spot REAL, forward REAL, taxa REAL, atm_iv REAL, iv_call25 REAL,
    iv_put25 REAL, rr25 REAL, bf25 REAL, n_series INTEGER, n_iv INTEGER, PRIMARY KEY (sessao, subjacente,
    vencimento, ts_ms));
CREATE TABLE calendario_eventos (ts_ms INTEGER NOT NULL, pais TEXT NOT NULL, evento TEXT NOT NULL,
    impacto TEXT NOT NULL, fonte TEXT, previsto TEXT, anterior TEXT, PRIMARY KEY (ts_ms, pais, evento));
CREATE TABLE noticias (id TEXT PRIMARY KEY, ts_coleta_ms INTEGER NOT NULL, dup_de TEXT);
CREATE TABLE noticias_score (id TEXT NOT NULL, modelo TEXT NOT NULL, versao TEXT NOT NULL,
    sentimento REAL, relevancia REAL, PRIMARY KEY (id, modelo, versao));
CREATE TABLE regime_live (ts_ms INTEGER NOT NULL, sessao TEXT NOT NULL, preco REAL, rv_rel REAL,
    PRIMARY KEY (sessao, ts_ms));
"""

ATIVOS_GLOBAIS = {
    "ES": 7000.0, "NQ": 25000.0, "DXY": 103.0, "VIX": 20.0, "VIX9D": 21.0, "N225": 59000.0,
    "HSI": 23000.0, "CSI300": 3900.0, "STOXX50": 6300.0, "DAX": 26000.0, "FTSE": 10800.0,
    "BRENT": 90.0, "USDMXN": 18.0, "USDZAR": 17.5,
}


def _pregoes(n, fim=date(2026, 11, 17)):
    """Ultimos `n` dias uteis (seg-sex) ate `fim`, em ordem crescente."""
    out, d = [], fim
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return list(reversed(out))


def _dia_idx(d):
    return (d - date(1970, 1, 1)).days


def _ts(dia_idx, hh, mm=0):
    return dia_idx * D + hh * 3_600_000 + mm * 60_000


def construir(con, n=90, com_evento=True, semente=7):
    """Banco sintetico: 90 sessoes, barras de 5 min, macro, opcoes, fluxo, Focus, leiloes, noticias."""
    con.executescript(SCHEMA)
    rng = random.Random(semente)
    dias = _pregoes(n)
    datas = [(d, _dia_idx(d)) for d in dias]
    win, wdo, di29, di33, di28, dap = 170_000.0, 5600.0, 13.0, 13.6, 13.4, 6.0
    series = {}
    for i, (d, dia) in enumerate(datas):
        win *= 1 + rng.gauss(0, 0.004)
        wdo *= 1 + rng.gauss(0, 0.004)
        di29 += rng.gauss(0, 0.03)
        di33 += rng.gauss(0, 0.035)
        di28 += rng.gauss(0, 0.03)
        dap += rng.gauss(0, 0.01)
        series[dia] = {"WIN$": win, "WDO$": wdo}
        linhas = []
        for sym, p in (("WIN$", win), ("WDO$", wdo)):
            px = p
            for m in range(540, 1051, 5):
                px *= 1 + rng.gauss(0, 0.0004)
                linhas.append((sym, dia * D + m * 60_000, "teste", None, px, px * 1.0005, px * 0.9995, px, 100.0))
        for sym, taxa in (("DI1F29", di29), ("DI1F33", di33)):
            for m in range(550, 1051, 10):
                linhas.append((sym, dia * D + m * 60_000, "teste", None, taxa, taxa, taxa, taxa, 0.0))
        con.executemany("INSERT INTO barras_1m VALUES (?,?,?,?,?,?,?,?,?)", linhas)
        con.executemany("INSERT INTO macro_cotacoes VALUES (?,?,?,?,?,?,?,?)",
                        [(dia * D + m * 60_000, "PREGAO", "DI1F28", "J", None, None, di28, di28)
                         for m in range(550, 1051, 10)] +
                        [(dia * D + m * 60_000, "PREGAO", "DAPK29", "J", None, None, dap, dap)
                         for m in range(550, 1051, 10)])
        con.execute("INSERT INTO fechamento_diario VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (dia, "WIN$", win, win * 1.01, win * 0.99, win, win, None, win, None, None, None,
                     None, "teste", dia * D + 18 * 3_600_000))
        con.execute("INSERT INTO fechamento_diario VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (dia, "WDO$", wdo, wdo * 1.01, wdo * 0.99, wdo, wdo, None, wdo, None, None, None,
                     None, "teste", dia * D + 18 * 3_600_000))
        bova = 160.0 + 6 * math.sin(i / 9)
        con.execute("INSERT INTO fechamento_diario VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (dia, "BOVA11", bova, bova, bova, bova, bova, None, bova, None, None, None,
                     0.8 + 0.05 * math.sin(i / 3), "teste", dia * D + 17 * 3_600_000))
        con.execute("INSERT INTO opcoes_resumo VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (_ts(dia, 16), "PREGAO", "BOVA11", "2026-12-18", 0.2, bova, bova, 0.12,
                     0.18 + 0.006 * math.sin(i / 5), None, None, -0.02 + 0.004 * math.cos(i / 4), None, 10, 10))
        # fluxo oficial do estrangeiro publicado 2 sessoes depois
        pub = datas[min(i + 2, len(datas) - 1)][0].isoformat() if i + 2 < len(datas) else \
            (d + timedelta(days=4)).isoformat()
        con.execute("INSERT INTO fluxo_diario VALUES (?,?,?,?,?,?)",
                    (d.isoformat(), "Investidor Estrangeiro", 0.0, 0.0, -50_000 + 8_000 * math.sin(i / 3), pub))
        # noticias: 25 itens por dia, sentimento variando
        nid = f"n{dia}"
        con.executemany("INSERT INTO noticias VALUES (?,?,?)",
                        [(f"{nid}_{j}", _ts(dia, 9, j * 20), None) for j in range(25)])
        con.executemany("INSERT INTO noticias_score VALUES (?,?,?,?,?)",
                        [(f"{nid}_{j}", "teste", "1", 0.45 * math.sin(i + j), 0.9) for j in range(25)])
        con.execute("INSERT INTO regime_live VALUES (?,?,?,?)", (_ts(dia, 15), "PREGAO", win, 0.7))
        # global: 5m para o trio do risco Brasil (eps_BR) e 60m para os demais
        for ativo, base in ATIVOS_GLOBAIS.items():
            valor = base * (1 + 0.02 * math.sin(i / 6 + len(ativo)))
            if ativo in ("DXY", "USDMXN", "USDZAR"):
                minutos, intervalo = range(545, 1076, 5), "5m"
            else:
                minutos, intervalo = (420, 720, 900, 1020), "60m"
            for m in minutos:
                valor *= 1 + rng.gauss(0, 0.001)
                con.execute("INSERT INTO global_barras VALUES (?,?,?,?,?,?,?)",
                            (_ts(dia, m // 60, m % 60), ativo, intervalo, valor, valor, valor, valor))
    # Focus IPCA semanal (ano corrente), 20 publicacoes
    for k in range(20):
        pub = date(2026, 11, 13) - timedelta(days=7 * k)
        con.execute("INSERT INTO focus_ipca VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (pub.isoformat(), _dia_idx(pub) * D, 2026, 5.2 - 0.01 * k, None, None, None, None, None,
                     "teste"))
    # leiloes a cada 4 sessoes
    for k, (d, _dia) in enumerate(datas[8::4]):
        rid = k + 1
        con.execute("INSERT INTO leilao_resultados VALUES (?,?)", (rid, d.isoformat()))
        con.execute("INSERT INTO leilao_metricas VALUES (?,?,?)", (rid, 8.0 * math.sin(k), None))
    if com_evento:
        dia = datas[-1][1]
        con.execute("INSERT INTO calendario_eventos VALUES (?,?,?,?,?,?,?)",
                    (_ts(dia, 16), "EUA", "FOMC", "alto", "teste", None, None))
    con.commit()
    return datas


def banco_teste(**kw):
    con = sqlite3.connect(":memory:")
    datas = construir(con, **kw)
    ts = _ts(datas[-1][1], 15)
    return con, ts, datas


class TestPadronizacao(unittest.TestCase):
    def test_z_causal_truncado(self):
        hist = [0.0, 1.0, -1.0, 2.0, -2.0] * 4
        self.assertAlmostEqual(mc._z(0.0, hist, 10), 0.0)
        self.assertEqual(mc._z(100.0, hist, 10), 3.0)
        self.assertEqual(mc._z(-100.0, hist, 10), -3.0)
        sd = float(np.std(hist, ddof=1))
        self.assertAlmostEqual(mc._z(1.0, hist, 10), 1.0 / sd)

    def test_z_exige_amostra_e_dispersao(self):
        self.assertIsNone(mc._z(1.0, [1.0] * 30, 20))
        self.assertIsNone(mc._z(1.0, [1.0] * 5, 20))
        self.assertIsNone(mc._z(None, [1.0, 2.0] * 20, 20))

    def test_score_tanh(self):
        self.assertAlmostEqual(mc._score_z(1.5), 100 * math.tanh(1.0))
        self.assertAlmostEqual(mc._score_z(-3.0), -100 * math.tanh(2.0))
        self.assertIsNone(mc._score_z(None))

    def test_fator_gex(self):
        self.assertEqual(mc._fator_gex(-1.0), 1.2)
        self.assertEqual(mc._fator_gex(1.0), 0.85)
        self.assertEqual(mc._fator_gex(None), 1.0)
        self.assertEqual(mc._fator_gex(0.0), 1.0)

    def test_composto_renormaliza_pesos(self):
        pil = {"macro_juros": {"z": 2.0}, "noticias": {"z": None}, "vol_opcoes": {"z": None}}
        z, peso = mc._composto(pil)
        self.assertAlmostEqual(z, 2.0)
        self.assertAlmostEqual(peso, 0.25)
        z, peso = mc._composto({"macro_juros": {"z": 2.0}, "noticias": {"z": -1.0}})
        self.assertAlmostEqual(z, (2 * 0.25 - 1 * 0.1) / 0.35)
        self.assertAlmostEqual(peso, 0.35)
        self.assertEqual(mc._composto({"macro_juros": {"z": None}}), (None, 0.0))


class TestArquetipo(unittest.TestCase):
    def test_precedencia(self):
        c = mc._classificar_arquetipo
        self.assertEqual(c(-80, -80, -1, {"x": 1}, 0.1, -0.02, 0.02, 95, 1.2), "Dia de evento binário")
        self.assertEqual(c(70, 10, -1, None, 0.05, -0.02, 0.02, 90, 1.2), "Exaustão")
        self.assertEqual(c(-50, 20, None, None, 0.0, -0.02, 0.02, 50, 1.0),
                         "Repique contra a tendência")
        self.assertEqual(c(40, 10, None, None, 0.0, -0.02, 0.02, 50, 1.0), "Tendência alinhada")
        self.assertEqual(c(26, -10, -1.0, None, 0.0, -0.02, 0.02, 50, 1.0), "Tendência alinhada")
        self.assertEqual(c(10, 5, 1.0, None, 0.0, -0.02, 0.02, 50, 1.0), "Compressão")
        self.assertEqual(c(10, 5, None, None, 0.0, -0.02, 0.02, 50, 0.5), "Compressão")
        self.assertEqual(c(30, 5, None, None, 0.0, -0.02, 0.02, 50, 0.9), "Misto")
        self.assertEqual(c(None, None, None, None, None, None, None, None, None), "Misto")

    def test_textos_com_acento(self):
        for nome, texto in mc.TEXTO_ARQUETIPO.items():
            self.assertTrue(texto)
        self.assertIn("Tendência alinhada", mc.TEXTO_ARQUETIPO)


class TestCenario(unittest.TestCase):
    def setUp(self):
        self.con, self.ts, self.datas = banco_teste()

    def tearDown(self):
        self.con.close()

    def test_estrutura_e_scores(self):
        r = mc.cenario(self.con, self.ts)
        for chave in ("score_estrutural", "score_intraday", "pilares", "fator_gex", "gex",
                      "risco_brasil_bps", "arquetipo", "briefing", "faltantes", "ts_ms"):
            self.assertIn(chave, r)
        self.assertGreater(r["score_estrutural"], -100)
        self.assertLess(r["score_estrutural"], 100)
        self.assertAlmostEqual(r["fator_gex"], 1.0)         # sem posicoes em aberto no fixture
        self.assertGreater(r["risco_brasil_bps"], 0)        # eps sintetico acumulado +1%
        self.assertEqual(len(r["briefing"]), 4)
        self.assertEqual(r["arquetipo"]["nome"], "Dia de evento binário")
        self.assertIn("condicoes", r["arquetipo"])
        for chave in mc.PESOS:
            p = r["pilares"][chave]
            self.assertIn("z", p)
            self.assertIn("score", p)
            self.assertIn("faltou", p)
            self.assertIn("indicadores", p)
            self.assertIn("tatico", p)
            for i in p["indicadores"]:
                self.assertLessEqual(abs(i["z"]), 3.0)
                self.assertIn("n", i)

    def test_pilares_presentes_no_fixture(self):
        r = mc.cenario(self.con, self.ts)
        for chave in ("macro_juros", "fluxo_cambio", "vol_opcoes", "global_commod", "noticias"):
            self.assertIsNotNone(r["pilares"][chave]["z"], chave)
        # nowcast e eps sao intradiarios: nao entram no score estrutural
        est = [i["nome"] for i in r["pilares"]["fluxo_cambio"]["indicadores"]]
        tat = [i["nome"] for i in r["pilares"]["fluxo_cambio"]["tatico"]["indicadores"]]
        self.assertNotIn("Nowcast de fluxo do dia", est)
        self.assertNotIn("eps_BR acumulado do dia", est)
        self.assertIn("eps_BR acumulado do dia", tat)
        self.assertTrue(all(i["n"] >= 20 for i in r["pilares"]["macro_juros"]["indicadores"]))

    def test_briefing_em_pt_br(self):
        r = mc.cenario(self.con, self.ts)
        l1, l2, l3, l4 = r["briefing"]
        self.assertIn("WIN fechou em", l1)
        self.assertIn(",", l1)
        self.assertIn("Fluxo estrangeiro", l2)
        self.assertIn("Juros", l3)
        self.assertIn("Vol e opções", l4)
        self.assertIn("arquétipo", l4)

    def test_causalidade_futuro_nao_altera(self):
        antes = mc.cenario(self.con, self.ts)
        dia = self.datas[-1][1]
        # barra, cotacao global e noticia depois do instante avaliado
        self.con.execute("INSERT INTO barras_1m VALUES (?,?,?,?,?,?,?,?,?)",
                         ("WIN$", self.ts + 60_000, "teste", None, 999999, 999999, 999999, 999999, 0))
        self.con.execute("INSERT INTO barras_1m VALUES (?,?,?,?,?,?,?,?,?)",
                         ("DI1F29", self.ts + 60_000, "teste", None, 99, 99, 99, 99, 0))
        self.con.execute("INSERT INTO global_barras VALUES (?,?,?,?,?,?,?)",
                         (self.ts + 60 * 60_000, "ES", "60m", 1, 1, 1, 1))
        self.con.execute("INSERT OR REPLACE INTO fluxo_diario VALUES (?,?,?,?,?,?)",
                         ((date(1970, 1, 1) + timedelta(days=dia)).isoformat(), "Investidor Estrangeiro",
                          0, 0, 9_999_999, (date(1970, 1, 1) + timedelta(days=dia + 1)).isoformat()))
        self.con.execute("INSERT INTO noticias VALUES ('futuro', ?, NULL)", (self.ts + 3_600_000,))
        self.con.execute("INSERT INTO noticias_score VALUES ('futuro','teste','1', -0.9, 1.0)")
        self.con.commit()
        depois = mc.cenario(self.con, self.ts)
        self.assertAlmostEqual(antes["score_estrutural"], depois["score_estrutural"], places=9)
        self.assertAlmostEqual(antes["score_intraday"], depois["score_intraday"], places=9)
        for chave in mc.PESOS:
            self.assertAlmostEqual(antes["pilares"][chave]["z"] or 0.0,
                                   depois["pilares"][chave]["z"] or 0.0, places=9)
            self.assertAlmostEqual(antes["arquetipo"]["condicoes"]["retorno_5d"] or 0.0,
                                   depois["arquetipo"]["condicoes"]["retorno_5d"] or 0.0, places=9)

    def test_evento_passado_nao_classifica_evento(self):
        r = mc.cenario(self.con, self.ts + 3 * 3_600_000)   # 18:00, depois do FOMC das 16:00
        self.assertNotEqual(r["arquetipo"]["nome"], "Dia de evento binário")

    def test_historico_0915_causal(self):
        h = mc.historico(self.con, 5)
        self.assertEqual(len(h), 5)
        for linha in h:
            self.assertEqual(linha["ts_ms"] % D, (9 * 60 + 15) * 60_000)
        # a ultima linha reproduz o score estrutural do cenario no mesmo instante
        ultimo = h[-1]
        r = mc.cenario(self.con, ultimo["ts_ms"])
        self.assertAlmostEqual(ultimo["score_estrutural"], r["score_estrutural"], places=9)
        self.assertAlmostEqual(ultimo["fator_gex"], r["fator_gex"], places=9)

    def test_avaliar(self):
        r = mc.avaliar(self.con, 20)
        self.assertIn("Score estrutural", r)
        self.assertGreater(r["Score estrutural"]["dia"]["n"], 0)
        self.assertLessEqual(r["Score estrutural"]["dia"]["n"], 20)


class TestAtualizar(unittest.TestCase):
    def test_grava_cenario_scores(self):
        with tempfile.TemporaryDirectory() as tmp:
            caminho = str(Path(tmp) / "book.db")
            con = sqlite3.connect(caminho)
            datas = construir(con)
            ts = _ts(datas[-1][1], 15)
            con.commit()
            con.close()
            r = mc.atualizar(caminho, ts)
            con = sqlite3.connect(caminho)
            try:
                row = con.execute("SELECT ts_ms, score_estrutural, score_intraday, pilar_macro_juros, "
                                  "arquetipo_dia, resumo_narrativo FROM cenario_scores").fetchone()
            finally:
                con.close()
            self.assertEqual(row[0], ts)
            self.assertAlmostEqual(row[1], r["score_estrutural"], places=9)
            self.assertEqual(row[4], r["arquetipo"]["nome"])
            self.assertIn("WIN fechou", row[5])


class TestSemDados(unittest.TestCase):
    def test_banco_vazio_nao_quebra(self):
        con = sqlite3.connect(":memory:")
        r = mc.cenario(con, _ts(_dia_idx(date(2026, 11, 17)), 15))
        self.assertIsNone(r["score_estrutural"])
        self.assertIsNone(r["score_intraday"])
        self.assertEqual(len(r["briefing"]), 4)
        self.assertTrue(r["faltantes"])
        self.assertIsNone(r["risco_brasil_bps"])
        self.assertIn(r["arquetipo"]["nome"], mc.TEXTO_ARQUETIPO)
        self.assertEqual(mc.historico(con), [])
        con.close()


if __name__ == "__main__":
    unittest.main()
