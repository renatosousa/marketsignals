"""Testes do consolidar_fechamento.py (tabela fechamento_diario) com banco SQLite temporario.

  python -m unittest test_consolidar_fechamento -v

Cobre: pre-abertura ignorada, ultimo_liquido das acoes antes do leilao, DI sem perfil, vwap/perfil das
barras, idempotencia, dia sem dados e a janela de consolidar_pendentes.
"""
import math
import sqlite3
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

import consolidar_fechamento as cf

D = 86_400_000
DIA = 20773              # segunda 16/11/2026, pregao util
DIA_SEG = 20774          # terca 17/11/2026

SCHEMA = """
CREATE TABLE barras_1m (symbol TEXT NOT NULL, ts_ms INTEGER NOT NULL, fonte TEXT, contrato TEXT,
                        open REAL, high REAL, low REAL, close REAL, volume REAL, PRIMARY KEY (symbol, ts_ms));
CREATE TABLE snapshots (ts_ms INTEGER NOT NULL, symbol TEXT NOT NULL, mid REAL, spread REAL);
CREATE TABLE macro_cotacoes (ts_ms INTEGER NOT NULL, symbol TEXT NOT NULL, mid REAL);
"""


def ts(dia, hh, mm=0):
    return dia * D + (hh * 60 + mm) * 60_000


def banco(caminho=":memory:"):
    db = sqlite3.connect(caminho)
    db.executescript(SCHEMA)
    return db


def barras(db, symbol, linhas):
    db.executemany("INSERT INTO barras_1m (symbol, ts_ms, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?)",
                   [(symbol, ts(d, h, m), o, hi, lo, c, v) for d, h, m, o, hi, lo, c, v in linhas])


def linha(linhas, ativo):
    return next(l for l in linhas if l["ativo"] == ativo)


class TestConsolidarDia(unittest.TestCase):
    def test_pre_abertura_ignorada(self):
        db = banco()
        barras(db, "WIN$", [
            (DIA, 8, 55, 170_000.0, 170_000.0, 170_000.0, 170_000.0, 10.0),   # cotacao de ontem
            (DIA, 9, 1, 171_000.0, 171_100.0, 170_900.0, 171_000.0, 50.0),
            (DIA, 18, 24, 171_000.0, 171_050.0, 170_950.0, 171_000.0, 20.0),
        ])
        w = linha(cf.consolidar_dia(db, DIA), "WIN$")
        self.assertEqual(w["abertura"], 171_000.0)
        self.assertEqual((w["maxima"], w["minima"]), (171_100.0, 170_900.0))
        self.assertEqual((w["ultimo_liquido"], w["ultimo_dia"]), (171_000.0, 171_000.0))
        self.assertEqual(w["fonte"], "barras_1m")

    def test_acao_usa_ate_1655_e_leilao_fica_no_ultimo_dia(self):
        db = banco()
        barras(db, "VALE3", [
            (DIA, 10, 0, 10.0, 10.2, 9.9, 10.0, 100.0),
            (DIA, 16, 50, 10.4, 10.6, 10.4, 10.5, 50.0),
            (DIA, 16, 58, 11.8, 12.0, 11.8, 12.0, 10.0),   # leilao de fechamento
        ])
        v = linha(cf.consolidar_dia(db, DIA), "VALE3")
        self.assertEqual(v["ultimo_liquido"], 10.5)
        self.assertEqual(v["ultimo_dia"], 12.0)
        self.assertEqual(v["abertura"], 10.0)

    def test_di_taxa_sem_perfil_nem_vol(self):
        db = banco()
        barras(db, "DI1F29", [
            (DIA, 10, 0, 13.5, 13.5, 13.5, 13.5, 100.0),
            (DIA, 17, 0, 13.6, 13.6, 13.6, 13.6, 20.0),
        ])
        d = linha(cf.consolidar_dia(db, DIA), "DI1F29")
        self.assertEqual((d["abertura"], d["ultimo_liquido"]), (13.5, 13.6))
        for campo in ("vwap_dia", "poc_dia", "vah_dia", "val_dia", "vol_realizada_dia"):
            self.assertIsNone(d[campo])

    def test_vwap_e_perfil_das_barras_com_volume(self):
        db = banco()
        barras(db, "WIN$", [
            (DIA, 10, 0, 100.0, 100.0, 100.0, 100.0, 100.0),
            (DIA, 10, 1, 101.0, 101.0, 101.0, 101.0, 300.0),
        ])
        w = linha(cf.consolidar_dia(db, DIA), "WIN$")
        self.assertAlmostEqual(w["vwap_dia"], (100.0 * 100 + 101.0 * 300) / 400)
        self.assertEqual(w["poc_dia"], 100.0)
        self.assertEqual((w["vah_dia"], w["val_dia"]), (102.5, 97.5))
        self.assertAlmostEqual(w["vol_realizada_dia"], 100 * abs(math.log(101.0 / 100.0)), places=6)

    def test_barra_sem_volume_nao_gera_perfil(self):
        db = banco()
        barras(db, "WDO$", [(DIA, 10, 0, 5800.0, 5800.0, 5800.0, 5800.0, 0.0)])
        w = linha(cf.consolidar_dia(db, DIA), "WDO$")
        self.assertIsNone(w["vwap_dia"])
        self.assertIsNone(w["poc_dia"])

    def test_mids_quando_nao_ha_barras(self):
        db = banco()
        db.executemany("INSERT INTO macro_cotacoes (ts_ms, symbol, mid) VALUES (?,?,?)",
                       [(ts(DIA, 8, 55), "IND$", 171_030.0),      # pre-abertura: fica de fora
                        (ts(DIA, 9, 1), "IND$", 171_000.0),
                        (ts(DIA, 17, 50), "IND$", 170_500.0),
                        (ts(DIA, 18, 29), "IND$", 170_400.0)])
        i = linha(cf.consolidar_dia(db, DIA), "IND$")
        self.assertEqual(i["abertura"], 171_000.0)
        # futuros nao tem leilao de fechamento: o ultimo liquido e o ultimo da sessao (18:29)
        self.assertEqual((i["ultimo_liquido"], i["ultimo_dia"]), (170_400.0, 170_400.0))
        self.assertEqual(i["fonte"], "macro_cotacoes")

    def test_dia_sem_dados_nao_gera_linha(self):
        self.assertEqual(cf.consolidar_dia(banco(), DIA), [])


class TestGravar(unittest.TestCase):
    def setUp(self):
        self.pasta = tempfile.TemporaryDirectory()
        self.addCleanup(self.pasta.cleanup)
        self.caminho = str(Path(self.pasta.name) / "teste.db")

    def test_idempotente(self):
        src = banco()
        barras(src, "WIN$", [(DIA, 10, 0, 100.0, 101.0, 99.0, 100.0, 10.0)])
        linhas = cf.consolidar_dia(src, DIA)
        self.assertTrue(linhas)
        db = banco(self.caminho)
        try:
            self.assertEqual(cf.gravar(db, linhas), len(linhas))
            cf.gravar(db, cf.consolidar_dia(src, DIA))
            rows = db.execute("SELECT dia_idx,ativo,abertura,ultimo_liquido,ultimo_dia,fonte "
                              "FROM fechamento_diario ORDER BY ativo").fetchall()
            self.assertEqual(len(rows), len(linhas))                  # a chave (dia, ativo) substitui, nao duplica
            self.assertIn((DIA, "WIN$", 100.0, 100.0, 100.0, "barras_1m"), rows)
            self.assertEqual(db.execute("SELECT count(*) FROM fechamento_diario WHERE dia_idx=?", (DIA,)).fetchone(),
                             (len(linhas),))
        finally:
            db.close()

    def test_consolidar_pendentes_pula_hoje_antes_das_1830(self):
        db = banco(self.caminho)
        barras(db, "WIN$", [(DIA, 10, 0, 100.0, 100.0, 100.0, 100.0, 10.0),
                            (DIA_SEG, 10, 0, 101.0, 101.0, 101.0, 101.0, 10.0)])
        db.commit()
        db.close()
        agora = datetime(2026, 11, 18, 18, 0)
        self.assertEqual(cf.consolidar_pendentes(self.caminho, dias=3, hoje=date(2026, 11, 18), agora=agora), 2)
        self.assertEqual(cf.consolidar_pendentes(self.caminho, dias=3, hoje=date(2026, 11, 18), agora=agora), 0)

    def test_consolidar_pendentes_inclui_hoje_depois_das_1830(self):
        db = banco(self.caminho)
        barras(db, "WIN$", [(DIA_SEG, 10, 0, 101.0, 101.0, 101.0, 101.0, 10.0)])
        db.commit()
        db.close()
        antes = datetime(2026, 11, 17, 18, 0)
        self.assertEqual(cf.consolidar_pendentes(self.caminho, dias=1, hoje=date(2026, 11, 17), agora=antes), 0)
        depois = datetime(2026, 11, 17, 19, 0)
        self.assertEqual(cf.consolidar_pendentes(self.caminho, dias=1, hoje=date(2026, 11, 17), agora=depois), 1)


if __name__ == "__main__":
    unittest.main()
