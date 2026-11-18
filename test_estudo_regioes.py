"""Testes do estudo_regioes.py (ESPEC 9.2) com arrays e banco SQLite temporario.

  python -m unittest test_estudo_regioes -v

Cobre: deteccao do primeiro toque na faixa, MFE/MAE e retorno liquidos com 5 pts de custo por lado,
benchmark de niveis aleatorios do mesmo lado e mesma distancia, proxy causal do vies, bootstrap por dia,
a leitura de OHLC por minuto (barras e snapshots) sem olhar o futuro e uma rodada completa do estudo em
banco sintetico (zonas reconstruidas, toques medidos e tabela por faixa de forca).
"""
import sqlite3
import unittest
from datetime import date, timedelta

import numpy as np

import calendario_b3 as cal
import estudo_regioes as er

D = 86_400_000
DIA = 20774                      # terca 17/11/2026
AGORA = DIA * D + 15 * 3600000


def ts(dia, hh, mm=0):
    return dia * D + (hh * 3600 + mm * 60) * 1000


def iso(dia):
    return (date(1970, 1, 1) + timedelta(days=dia)).isoformat()


def pregoes_antes(dia, n):
    d = date(1970, 1, 1) + timedelta(days=dia)
    out = []
    for _ in range(n):
        d = cal.pregao_anterior(d)
        out.append((d - date(1970, 1, 1)).days)
    return out


SCHEMA = """
CREATE TABLE snapshots (ts_ms INTEGER, sessao TEXT, symbol TEXT, bid REAL, ask REAL, mid REAL, spread REAL,
                        last REAL, vol_compra_agr REAL, vol_venda_agr REAL);
CREATE TABLE macro_cotacoes (ts_ms INTEGER, sessao TEXT, symbol TEXT, bid REAL, ask REAL, last REAL);
CREATE TABLE barras_1m (symbol TEXT, ts_ms INTEGER, open REAL, high REAL, low REAL, close REAL, volume REAL);
CREATE TABLE fechamento_diario (dia_idx INTEGER, ativo TEXT, maxima REAL, minima REAL, ultimo_liquido REAL,
                                poc_dia REAL, vah_dia REAL, val_dia REAL);
CREATE TABLE regime_live (ts_ms INTEGER, sessao TEXT, preco REAL, rv_dia_prev REAL, faixa_1h_pts REAL,
                          wdo_rv_rel REAL);
CREATE TABLE cenario_scores (ts_ms INTEGER PRIMARY KEY, score_estrutural REAL);
CREATE TABLE tryd_fita (ts_ms INTEGER, dia INTEGER, ativo TEXT, agressor TEXT, qtd REAL, preco REAL,
                        agente_agr TEXT);
CREATE TABLE tryd_fita_1min (ts_ms INTEGER, dia INTEGER, ativo TEXT, minuto INTEGER, vol_agr_compra REAL,
                             vol_agr_venda REAL);
CREATE TABLE tryd_saldo (ts_ms INTEGER, ativo TEXT, linha INTEGER, brk TEXT, qtd REAL, premed REAL);
CREATE TABLE opcoes_resumo (ts_ms INTEGER, subjacente TEXT, vencimento TEXT, t_anos REAL, atm_iv REAL,
                            taxa REAL);
CREATE TABLE opcoes_cotacoes (ts_ms INTEGER, subjacente TEXT, vencimento TEXT, tipo TEXT, strike REAL,
                              bid REAL, ask REAL, last REAL, iv REAL, delta REAL);
CREATE TABLE b3_posicoes_series (sessao TEXT, subjacente TEXT, vencimento TEXT, tipo TEXT, strike REAL,
                                 coberto REAL, travado REAL, descoberto REAL, vol_impl_pct REAL);
CREATE TABLE agentes (nome TEXT, grupo TEXT);
"""


def _caminho(i, n=200):
    """Preco que oscila +-300 pts em torno de 100.000 (gera toques nas zonas dos dois lados)."""
    return 100000.0 + 300.0 * np.sin(2 * np.pi * i / 25.0)


def banco(caminho=":memory:", n_dias=30, dias_estudo=5):
    db = sqlite3.connect(str(caminho))
    db.executescript(SCHEMA)
    antes = pregoes_antes(DIA, n_dias)
    for d in antes:
        for hh in (10, 12, 15):
            db.execute("INSERT INTO barras_1m VALUES (?,?,?,?,?,?,?)",
                       ("WIN$", ts(d, hh), 100000.0, 100400.0, 99800.0, 100100.0, 100.0))
        db.execute("INSERT INTO fechamento_diario VALUES (?,?,?,?,?,?,?,?)",
                   (d, "WIN$", 100400.0, 99800.0, 100100.0, None, None, None))
    for k, d in enumerate(sorted(pregoes_antes(DIA, dias_estudo + 25))[:0]):
        pass
    estudos = sorted(pregoes_antes(DIA, dias_estudo))
    for d in estudos:
        for i, m in enumerate(range(10 * 60, 16 * 60 + 1)):
            p = _caminho(i)
            db.execute("INSERT INTO barras_1m VALUES (?,?,?,?,?,?,?)",
                       ("WIN$", ts(d, m // 60, m % 60), p - 5, p + 20, p - 20, p, 50.0))
        for hh in range(10, 18):
            p = _caminho((hh - 10) * 24)
            db.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (ts(d, hh), "S1", "WIN$", p - 2.5, p + 2.5, p, 5.0, p, 5.0, 5.0))
        db.execute("INSERT INTO regime_live VALUES (?,?,?,?,?,?)", (ts(d, 12), "S1", 100000.0, 1.0, 300.0, 1.0))
    db.commit()
    return db


def ohlc_falso(n=200, ini=540):
    high = np.full(n, np.nan)
    low = np.full(n, np.nan)
    close = np.full(n, np.nan)
    for i in range(n):
        close[i] = 100.0
        high[i] = 100.5
        low[i] = 99.5
    high[20] = 110.0
    low[15] = 95.0
    close[30] = 102.0
    return dict(ini=ini, high=high, low=low, close=close)


class TestToqueEMedida(unittest.TestCase):
    def test_toque_entra_na_faixa(self):
        o = ohlc_falso()
        ini = o["ini"]
        self.assertEqual(er._toque(o, ini, ini + 100, "ACIMA", 100.4, 101.0), 1)   # minuto seguinte ao slot
        self.assertEqual(er._toque(o, o["ini"] - 1, ini + 100, "ACIMA", 100.4, 101.0), 0)
        self.assertEqual(er._toque(o, ini, ini + 100, "ACIMA", 105.0, 106.0), 20)  # so no pico
        self.assertIsNone(er._toque(o, ini, ini + 100, "ACIMA", 120.0, 121.0))
        self.assertEqual(er._toque(o, ini, ini + 100, "ABAIXO", 90.0, 96.0), 15)

    def test_mfe_mae_e_custo_na_compra(self):
        o = ohlc_falso()
        fmax = {30: er._fwd(o["high"], 30)}
        fmin = {30: er._fwd_min(o["low"], 30)}
        mfe, mae, ret = er._medir(o, 0, "COMPRA", fmax, fmin, 30)
        self.assertAlmostEqual(mfe, 10.0 - er.CUSTO_LADO)          # 110 - 100 - 5
        self.assertAlmostEqual(mae, 5.0 + er.CUSTO_LADO)           # 100 - 95 + 5
        self.assertAlmostEqual(ret, 2.0 - 2 * er.CUSTO_LADO)       # 102 - 100 - 10

    def test_mfe_mae_na_venda(self):
        o = ohlc_falso()
        fmax = {30: er._fwd(o["high"], 30)}
        fmin = {30: er._fwd_min(o["low"], 30)}
        mfe, mae, ret = er._medir(o, 0, "VENDA", fmax, fmin, 30)
        self.assertAlmostEqual(mfe, 5.0 - er.CUSTO_LADO)           # 100 - 95 - 5
        self.assertAlmostEqual(mae, 10.0 + er.CUSTO_LADO)          # 110 - 100 + 5
        self.assertAlmostEqual(ret, -2.0 - 2 * er.CUSTO_LADO)

    def test_horizonte_sem_dados_devolve_none(self):
        o = ohlc_falso(n=100)
        fmax = {60: er._fwd(o["high"], 60)}
        fmin = {60: er._fwd_min(o["low"], 60)}
        self.assertIsNone(er._medir(o, 60, "COMPRA", fmax, fmin, 60))

    def test_forward_nao_olha_o_passado(self):
        serie = np.array([1.0, 5.0, 2.0, 3.0, 0.5])
        self.assertEqual(er._fwd(serie, 2)[0], 5.0)                # max de (1,2) = max(5,2)
        self.assertEqual(er._fwd(serie, 2)[1], 3.0)                # max de (2,3) = max(2,3)
        self.assertEqual(er._fwd(serie, 2)[2], 3.0)
        self.assertEqual(er._fwd_min(serie, 2)[1], 2.0)


class TestBenchmark(unittest.TestCase):
    def test_niveis_aleatorios_do_mesmo_lado(self):
        n = 400
        base = 100.0
        high = np.full(n, np.nan)
        low = np.full(n, np.nan)
        close = np.full(n, np.nan)
        for i in range(n):
            close[i] = base + 0.1 * i
            high[i] = close[i] + 0.05
            low[i] = close[i] - 0.05
        o = dict(ini=540, high=high, low=low, close=close)
        fmax = {30: er._fwd(high, 30)}
        fmin = {30: er._fwd_min(low, 30)}
        rng = np.random.default_rng(3)
        r = er._benchmark(o, 10, 1.0, "ACIMA", "COMPRA", fmax, fmin, 30, rng, 50)
        self.assertIsNotNone(r)
        self.assertGreaterEqual(r[2], 10)                          # pelo menos 10 dos 50 niveis tocaram
        # distancia absurda: nada e tocado
        self.assertIsNone(er._benchmark(o, 10, 5000.0, "ACIMA", "COMPRA", fmax, fmin, 30, rng, 50))


class TestVies(unittest.TestCase):
    def test_sinal_do_proxy(self):
        crescente = [(d, 100.0 * (1.01 ** d)) for d in range(80)]
        self.assertGreater(er._vies_proxy(crescente, 79), 25)
        decrescente = [(d, 100.0 * (0.99 ** d)) for d in range(80)]
        self.assertLess(er._vies_proxy(decrescente, 79), -25)
        self.assertEqual(er._vies_proxy(crescente[:10], 79), 0.0)


class TestBootstrap(unittest.TestCase):
    def test_media_e_intervalo(self):
        por_dia = {1: [1.0, 2.0], 2: [1.0, 2.0], 3: [1.0, 2.0], 4: [1.0, 2.0]}
        r = er._bootstrap(por_dia, n=200)
        self.assertAlmostEqual(r["media"], 1.5)
        self.assertLessEqual(r["lo"], 1.5)
        self.assertGreaterEqual(r["hi"], 1.5)
        self.assertEqual(r["n_dias"], 4)
        self.assertLess(r["p"], 0.05)

    def test_poucos_dias_nao_estima(self):
        self.assertIsNone(er._bootstrap({1: [1.0], 2: [2.0]}, n=50))


class TestOhlc(unittest.TestCase):
    def test_barras_e_snapshots(self):
        db = banco()
        dias = sorted(pregoes_antes(DIA, 2))
        o = er._ohlc_minuto(db, "WIN$", dias[0])
        self.assertIsNotNone(o)
        self.assertEqual(len(o["close"]), er.SESSAO[1] - er.SESSAO[0] + 1)
        self.assertTrue(np.isfinite(o["close"]).sum() >= 120)
        # dia sem barras: cai nos snapshots (um por minuto no pregao)
        db.execute("DELETE FROM barras_1m WHERE ts_ms BETWEEN ? AND ?",
                   (dias[1] * D, (dias[1] + 1) * D))
        for m in range(10 * 60, 17 * 60 + 1):
            p = _caminho(m - 600)
            db.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (ts(dias[1], m // 60, m % 60), "S1", "WIN$", p - 2.5, p + 2.5, p, 5.0, p, 5.0, 5.0))
        db.commit()
        o2 = er._ohlc_minuto(db, "WIN$", dias[1])
        self.assertIsNotNone(o2)
        self.assertTrue(np.isfinite(o2["close"]).sum() > 0)

    def test_ohlc_nao_inclui_barra_depois_do_dia(self):
        db = banco()
        dias = sorted(pregoes_antes(DIA, 2))
        o = er._ohlc_minuto(db, "WIN$", dias[0])
        self.assertTrue(np.nanmax(o["high"]) <= 100400.0)


class TestIntegracao(unittest.TestCase):
    def test_estudo_completo_em_banco_sintetico(self):
        db = banco()
        res = er.estudo(db, limite=3, passo_slot=30, n_random=20, n_bootstrap=200,
                        com_bandas=False, com_gatilho=False, verboso=False)
        self.assertGreaterEqual(res["dias"], 1)
        self.assertGreater(len(res["eventos"]), 0)
        self.assertTrue(res["linhas"])
        for l in res["linhas"]:
            self.assertIn(l["faixa"], ("35-59", "60-79", "80+"))
            self.assertIn(l["alinhamento"], er.ALINHAMENTOS)
            self.assertIn(l["filtro"], ("sem_filtro", "com_filtro", "sem_confirmacao"))
            self.assertGreaterEqual(l["n_toques"], 3)
            self.assertGreaterEqual(l["n_dias"], 1)
            self.assertIn(l["horizonte"], er.HORIZONTES)
            self.assertEqual(l["filtro"], "sem_filtro")          # sem fita, nao ha filtro de gatilho
        ev = res["eventos"][0]
        for k in ("dia", "criada_min", "toque_min", "lado", "direcao", "forca", "centro", "distancia"):
            self.assertIn(k, ev)
        conclusao = er._conclusao(res["linhas"])
        self.assertIsInstance(conclusao, str)
        self.assertGreater(len(conclusao), 20)

    def test_eventos_tem_medidas_nos_tres_horizontes(self):
        db = banco()
        res = er.estudo(db, limite=2, passo_slot=60, n_random=10, n_bootstrap=100,
                        com_bandas=False, com_gatilho=False, verboso=False)
        medidos = [ev for ev in res["eventos"] if ev["m"]]
        self.assertTrue(medidos)
        self.assertTrue(any(set(ev["m"]) == set(er.HORIZONTES) for ev in medidos))


if __name__ == "__main__":
    unittest.main()
