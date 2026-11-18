"""Testes do conversor_paridade.py com banco SQLite temporario.

  python -m unittest test_conversor_paridade -v

Cobre: razao WIN/BOVA11 ao vivo e por mediana, cotacao cruzada fora, tolerancia de 2 s, fallback do
fechamento com carrego do DI, arredondamentos do WIN e do WDO e o fator do dolar (gravado e recalculado).
"""
import sqlite3
import unittest

import conversor_paridade as cp

D = 86_400_000
DIA = 20774          # terca 17/11/2026, pregao util
D1 = 20773           # segunda 16/11/2026


def ts(dia, hh, mm=0, ss=0):
    return dia * D + (hh * 3600 + mm * 60 + ss) * 1000


SCHEMA = """
CREATE TABLE snapshots (ts_ms INTEGER, symbol TEXT, bid REAL, ask REAL, mid REAL, spread REAL, last REAL);
CREATE TABLE macro_cotacoes (ts_ms INTEGER, symbol TEXT, bid REAL, ask REAL, last REAL);
CREATE TABLE fechamento_diario (dia_idx INTEGER, ativo TEXT, maxima REAL, minima REAL, ultimo_liquido REAL,
                                PRIMARY KEY (dia_idx, ativo));
CREATE TABLE b3_dolar_series (sessao TEXT, vencimento TEXT, tipo TEXT, strike REAL, futuro REAL,
                              num_neg REAL, fator REAL);
"""


def banco():
    db = sqlite3.connect(":memory:")
    db.executescript(SCHEMA)
    return db


def snap(db, symbol, linhas):
    """linhas = [(ts, mid)]; bid/ask coerentes e spread > 0."""
    db.executemany("INSERT INTO snapshots (ts_ms, symbol, bid, ask, mid, spread, last) VALUES (?,?,?,?,?,?,?)",
                   [(t, symbol, m - 0.01, m + 0.01, m, 0.02, m) for t, m in linhas])


def fechamento(db, dia, ativo, ultimo):
    db.execute("INSERT INTO fechamento_diario (dia_idx, ativo, maxima, minima, ultimo_liquido) VALUES (?,?,?,?,?)",
               (dia, ativo, ultimo, ultimo, ultimo))


class TestRazaoAoVivo(unittest.TestCase):
    def test_mediana_dos_ultimos_cinco_minutos(self):
        db = banco()
        snap(db, "WIN$", [(ts(DIA, 10, 1, i), m) for i, m in enumerate((1000.0, 1010.0, 1020.0))])
        snap(db, "BOVA11", [(ts(DIA, 10, 1, i) + 100, m) for i, m in enumerate((10.0, 10.0, 10.0))])
        r = cp.k_win_bova(db, ts(DIA, 10, 6))
        self.assertEqual(r["origem"], "ao_vivo")
        self.assertAlmostEqual(r["k"], 101.0)          # medianas 100, 101 e 102

    def test_spread_zero_e_fora_da_janela_nao_entram(self):
        db = banco()
        snap(db, "WIN$", [(ts(DIA, 10, 2), 1000.0), (ts(DIA, 10, 3), 1010.0)])
        db.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?)", (ts(DIA, 10, 2), "BOVA11", 0.0, 0.0, 999.0,
                                                                    0.0, 999.0))  # cruzada: mid absurdo
        snap(db, "BOVA11", [(ts(DIA, 10, 2) + 500, 10.0), (ts(DIA, 10, 3) + 500, 10.1)])
        self.assertAlmostEqual(cp.k_win_bova(db, ts(DIA, 10, 6))["k"], 100.0)

    def test_bova_mais_distante_que_dois_segundos_nao_casa(self):
        db = banco()
        snap(db, "WIN$", [(ts(DIA, 10, 5), 1000.0)])
        snap(db, "BOVA11", [(ts(DIA, 10, 5) + 3_000, 10.0)])   # 3 s depois: fora da tolerancia
        r = cp.k_win_bova(db, ts(DIA, 10, 6))
        self.assertEqual(r["origem"], "fechamento")            # sem par ao vivo, cai no fechamento

    def test_antes_das_1005_usa_o_fechamento_com_carrego(self):
        db = banco()
        snap(db, "WIN$", [(ts(DIA, 9, 30), 1000.0)])
        snap(db, "BOVA11", [(ts(DIA, 9, 30), 10.0)])
        fechamento(db, D1, "WIN$", 1000.0)
        fechamento(db, D1, "BOVA11", 10.0)
        fechamento(db, D1, "DI1F28", 14.0)
        r = cp.k_win_bova(db, ts(DIA, 9, 40))
        self.assertEqual(r["origem"], "fechamento")
        self.assertAlmostEqual(r["k"], 100.0 * (1.14 ** (1 / 252)))


class TestArredondamentos(unittest.TestCase):
    def test_strike_em_pontos_no_tick_de_5(self):
        self.assertEqual(cp.strike_em_pontos(168.0, 1024.0), 172030.0)
        self.assertEqual(cp.strike_em_pontos(168.32, 100.0), 16830.0)
        self.assertIsNone(cp.strike_em_pontos(168.0, None))

    def test_strike_wdo_arredonda_a_meio_ponto(self):
        self.assertEqual(cp.strike_wdo(5800.0, 1.0), 5800.0)
        self.assertEqual(cp.strike_wdo(5800.0, 1.0056528), 5767.5)   # 5800 / 1,00565 = 5767,4
        self.assertIsNone(cp.strike_wdo(5800.0, None))


class TestFatorWdo(unittest.TestCase):
    def _db(self):
        db = banco()
        db.executemany("INSERT INTO b3_dolar_series (sessao, vencimento, tipo, strike, futuro, num_neg, fator) "
                       "VALUES (?,?,?,?,?,?,?)", [
                           ("2026-11-16", "2026-12-01", "C", 5800.0, 5790.0, 900.0, 1.0),
                           ("2026-11-16", "2027-01-04", "C", 5820.0, 5822.7, 100.0, 1.0056528),
                           ("2026-11-17", "2026-12-01", "C", 5800.0, 6000.0, 900.0, 1.9),
                       ])
        return db

    def test_reaproveita_o_fator_gravado_da_ultima_sessao_antes_do_dia(self):
        db = self._db()
        self.assertAlmostEqual(cp.fator_wdo(db, DIA, "2027-01-04"), 1.0056528)
        # a sessao do proprio dia (17/11) nao entra: a posicao de D-1 so sai a noite
        self.assertEqual(cp.fator_wdo(db, DIA, "2026-12-01"), 1.0)

    def test_recalcula_dos_ajustes_sem_o_fator(self):
        db = self._db()
        db.execute("UPDATE b3_dolar_series SET fator=NULL WHERE sessao='2026-11-16' AND vencimento='2027-01-04'")
        self.assertAlmostEqual(cp.fator_wdo(db, DIA, "2027-01-04"), 5822.7 / 5790.0)

    def test_sem_dado(self):
        self.assertIsNone(cp.fator_wdo(banco(), DIA, "2026-12-01"))
        self.assertIsNone(cp.fator_wdo(self._db(), DIA, "2030-01-01"))


if __name__ == "__main__":
    unittest.main()
