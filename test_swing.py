"""Testes do swing.py com banco SQLite temporario (dados sinteticos).

  python -m unittest test_swing -v

Cobre: area de valor de 20/60 pregoes com POC/VAH/VAL, VWAPs ancoradas na rolagem do contrato (WIN e
WDO), no Copom e no inicio do mes, movimento esperado semanal pela IV ATM, quantis empiricos de 5
pregoes, o formato das zonas de swing (horizonte SWING, limite de 3 por lado, plano com stop e alvos)
e as frases de invalidacao com DI1F29, fluxo do estrangeiro e VWAP de 20 pregoes.
"""
import sqlite3
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

import swing

D = 86_400_000
DIA = 20774                      # terca 17/11/2026
AGORA = DIA * D + 15 * 3600000


def ts(dia, hh, mm=0):
    return dia * D + (hh * 3600 + mm * 60) * 1000


def iso(dia):
    return (date(1970, 1, 1) + timedelta(days=dia)).isoformat()


def pregoes_antes(dia, n):
    import calendario_b3 as cal
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
CREATE TABLE opcoes_resumo (ts_ms INTEGER, subjacente TEXT, vencimento TEXT, t_anos REAL, atm_iv REAL,
                            iv_call25 REAL, iv_put25 REAL, rr25 REAL, taxa REAL);
CREATE TABLE fluxo_diario (data_ref TEXT, tipo TEXT, compras_mil REAL, vendas_mil REAL, saldo_mil REAL,
                           data_pub TEXT);
CREATE TABLE bcb_selic (data TEXT, meta REAL, over_aa REAL);
CREATE TABLE calendario_eventos (ts_ms INTEGER, pais TEXT, evento TEXT, impacto TEXT, fonte TEXT);
CREATE TABLE tryd_saldo (ts_ms INTEGER, ativo TEXT, linha INTEGER, brk TEXT, qtd REAL, premed REAL);
CREATE TABLE b3_posicoes_series (sessao TEXT, subjacente TEXT, vencimento TEXT, tipo TEXT, strike REAL,
                                 coberto REAL, travado REAL, descoberto REAL, vol_impl_pct REAL);
CREATE TABLE opcoes_manual_series (subjacente TEXT, capturado_em TEXT, vencimento TEXT, tipo TEXT,
                                   strike REAL, coberto REAL, travado REAL, descoberto REAL, vol_impl_pct REAL);
"""


def banco(caminho=":memory:", preco_dia=100000.0):
    db = sqlite3.connect(str(caminho))
    db.executescript(SCHEMA)
    antes = pregoes_antes(DIA, 70)
    for i, d in enumerate(antes):
        # 3 barras por pregao: o volume se concentra no preco base (que varia de um dia para o outro)
        base = 100000.0 + ((i * 37) % 11 - 5) * 60.0
        for hh, preco, vol in ((10, base, 100.0), (11, base + 200.0, 10.0), (12, base - 100.0, 90.0)):
            db.execute("INSERT INTO barras_1m VALUES (?,?,?,?,?,?,?)",
                       ("WIN$", ts(d, hh), preco, preco + 50, preco - 50, preco, vol))
        db.execute("INSERT INTO barras_1m VALUES (?,?,?,?,?,?,?)",
                   ("DI1F29", ts(d, 10), 14.9, 14.95, 14.85, 14.90, 100.0))
        db.execute("INSERT INTO barras_1m VALUES (?,?,?,?,?,?,?)",
                   ("DI1F29", ts(d, 15), 14.6, 14.65, 14.55, 14.60, 100.0))
    # dia corrente: snapshots do WIN$ a cada 30 min (sem barras do dia no banco)
    for k in range(11):
        hh, mm = divmod(10 * 60 + k * 30, 60)
        preco = preco_dia + k * 20
        db.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (ts(DIA, hh, mm), "S1", "WIN$", preco - 5, preco + 5, preco, 10.0, preco, 5.0, 5.0))
    # BOVA11 para a VWAP de 20 pregoes (2 leituras por dia)
    for d in antes[:20]:
        for hh, preco in ((10, 170.0), (16, 172.0)):
            db.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (ts(d, hh), "S1", "BOVA11", preco - 0.01, preco + 0.01, preco, 0.02, preco, 10.0, 10.0))
    db.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?)",
               (AGORA - 60_000, "S1", "BOVA11", 167.98, 168.0, 167.99, 0.02, 167.99, 10.0, 10.0))
    # IV ATM do BOVA11 para o movimento semanal
    db.execute("INSERT INTO opcoes_resumo VALUES (?,?,?,?,?,?,?,?,?)",
               (AGORA - 60_000, "BOVA11", "2026-11-27", 0.03, 0.20, 0.21, 0.19, 0.02, 0.13))
    # fluxo oficial do estrangeiro (D+2)
    for k in range(6):
        d = (date(2026, 11, 13) - timedelta(days=k))
        db.execute("INSERT INTO fluxo_diario VALUES (?,?,?,?,?,?)",
                   (d.isoformat(), "Investidor Estrangeiro", 1000.0, 2000.0, -1000.0, "2026-11-17"))
    # Selic com uma mudanca em 17/09/2026 (proxy do Copom)
    for d, m in (("2026-09-16", 14.0), ("2026-09-17", 13.75), ("2026-11-16", 13.75)):
        db.execute("INSERT INTO bcb_selic VALUES (?,?,?)", (d, m, m - 0.1))
    db.commit()
    return db


class TestPerfil(unittest.TestCase):
    def test_poc_e_area_de_valor_com_passo_fracionario(self):
        df = pd.DataFrame({"ts_ms": [1, 2, 3], "last": [100.0, 101.0, 99.0],
                           "vc": [100.0, 10.0, 90.0], "vv": [0.0, 0.0, 0.0]})
        pf = swing._perfil_va(df, 0.01)
        self.assertAlmostEqual(pf["poc"], 100.0)
        self.assertAlmostEqual(pf["vah"], 100.005)
        self.assertAlmostEqual(pf["val"], 98.995)
        self.assertEqual(len(pf["preco"]), 201)          # grade de 99 a 101 de 0,01 em 0,01

    def test_poc_usa_o_bin_de_maior_volume(self):
        df = pd.DataFrame({"ts_ms": [1, 2, 3, 4], "last": [14.80, 14.81, 15.20, 15.21],
                           "vc": [10.0, 50.0, 5.0, 5.0], "vv": [0.0, 0.0, 0.0, 0.0]})
        pf = swing._perfil_va(df, 0.005)
        self.assertAlmostEqual(pf["poc"], 14.81)


class TestAncoras(unittest.TestCase):
    def test_rolagem_win_e_wdo(self):
        self.assertEqual(swing._rolagem("WIN$", "2026-11-17")[0], "2026-10-14")
        self.assertEqual(swing._rolagem("WIN$", "2026-10-13")[0], "2026-08-12")
        self.assertEqual(swing._rolagem("WDO$", "2026-11-17")[0], "2026-11-03")
        self.assertIsNone(swing._rolagem("BOVA11", "2026-11-17")[0])
        self.assertIsNone(swing._rolagem("DI1F29", "2026-11-17")[0])

    def test_ultimo_copom_cai_na_mudanca_da_selic(self):
        db = banco()
        de, fonte = swing._ultimo_copom(db, "2026-11-17")
        self.assertEqual(de, "2026-09-17")
        self.assertIn("bcb_selic", fonte)


class TestMapa(unittest.TestCase):
    def test_mapa_tem_areas_vwaps_movimento_e_quantis(self):
        db = banco()
        m = swing.mapa_swing(db, "WIN$", AGORA)
        self.assertAlmostEqual(m["preco"], 100200.0)
        self.assertLessEqual(abs(m["areas"]["20d"]["poc"] - 100000.0), 400.0)
        self.assertLessEqual(abs(m["areas"]["60d"]["poc"] - 100000.0), 400.0)
        self.assertEqual(m["areas"]["20d"]["dias"], 20)
        self.assertEqual(m["areas"]["60d"]["dias"], 60)
        self.assertEqual(m["vwaps"]["rolagem"]["ancora"], "2026-10-14")
        self.assertEqual(m["vwaps"]["copom"]["ancora"], "2026-09-17")
        self.assertEqual(m["vwaps"]["mes"]["ancora"], "2026-11-01")
        self.assertIsNotNone(m["vwaps"]["mes"]["valor"])
        self.assertAlmostEqual(m["movimento_semanal"]["iv"], 0.20)
        self.assertAlmostEqual(m["movimento_semanal"]["sigma_pts"], 100200.0 * 0.20 * np.sqrt(5 / 252))
        self.assertIsNotNone(m["quantis_5d"])
        self.assertLess(m["quantis_5d"]["q05"], m["quantis_5d"]["q95"])
        self.assertIn("20d", m["areas"])

    def test_causalidade_nao_usa_barra_depois_de_agora(self):
        db = banco()
        m1 = swing.mapa_swing(db, "WIN$", AGORA)
        db.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (AGORA + 60_000, "S1", "WIN$", 120000.0, 120010.0, 120005.0, 10.0, 120005.0, 1e6, 1e6))
        db.commit()
        m2 = swing.mapa_swing(db, "WIN$", AGORA)
        self.assertAlmostEqual(m1["preco"], m2["preco"])
        self.assertAlmostEqual(m1["vwaps"]["mes"]["valor"], m2["vwaps"]["mes"]["valor"])


class TestZonas(unittest.TestCase):
    def test_zonas_swing_no_formato_do_motor(self):
        db = banco()
        zonas = swing.zonas_swing(db, "WIN$", AGORA, -60)
        self.assertTrue(zonas)
        self.assertLessEqual(sum(z["lado"] == "ACIMA" for z in zonas), 3)
        self.assertLessEqual(sum(z["lado"] == "ABAIXO" for z in zonas), 3)
        for z in zonas:
            self.assertEqual(z["horizonte"], "SWING")
            self.assertGreaterEqual(z["forca"], 35.0)
            self.assertLessEqual(z["preco_min"], z["preco_centro"])
            self.assertLessEqual(z["preco_centro"], z["preco_max"])
            self.assertIn(z["alinhamento"], ("A_FAVOR_TENDENCIA", "CONTRA_TENDENCIA", "ALVO", "NEUTRA"))
            self.assertIn(z["direcao"], ("COMPRA", "VENDA"))
            self.assertIn("stop", z)
            self.assertIn("rr", z)

    def test_ativo_desconhecido(self):
        with self.assertRaises(ValueError):
            swing.zonas_swing(banco(), "PETR9", AGORA, 0)

    def test_zonas_de_baixa_marcam_venda_acima(self):
        db = banco(preco_dia=98000.0)
        zonas = swing.zonas_swing(db, "WIN$", AGORA, -60)
        acima = [z for z in zonas if z["lado"] == "ACIMA"]
        self.assertTrue(acima)
        self.assertTrue(all(z["direcao"] == "VENDA" for z in acima))
        self.assertTrue(all(z["stop"] > z["preco_max"] for z in acima if z["stop"]))


class TestInvalidacao(unittest.TestCase):
    def test_frases_com_numeros(self):
        db = banco()
        frases = swing.invalidacao(db, AGORA, -60)
        texto = " ".join(frases)
        self.assertIn("DI1F29", texto)
        self.assertIn("% a.a.", texto)
        self.assertIn("estrangeiro", texto)
        self.assertIn("VWAP de 20 pregoes", texto)
        self.assertIn("R$", texto)
        self.assertTrue(any("vendedora" in f for f in frases))
        self.assertTrue(any("-" in f and "bi" in f for f in frases))

    def test_vies_comprador(self):
        db = banco()
        frases = swing.invalidacao(db, AGORA, 60)
        self.assertTrue(any("compradora" in f for f in frases))

    def test_sem_vies(self):
        db = banco()
        frases = swing.invalidacao(db, AGORA, 0)
        self.assertIn("Sem vies direcional", " ".join(frases))


if __name__ == "__main__":
    unittest.main()
