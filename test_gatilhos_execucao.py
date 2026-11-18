"""Testes do gatilhos_execucao.py com book e fita sinteticos.

  python -m unittest test_gatilhos_execucao -v

Cobre: os tres estados pela distancia a faixa, a absorcao pela fita agregada (percentil 80 dos blocos de
5 min + deslocamento), o iceberg, o OFI do topo do livro virando a favor, o fallback pelos snapshots sem
fita, a causalidade (negocio depois de agora nao entra) e a direcao invalida.
"""
import sqlite3
import unittest

import gatilhos_execucao as ge

D = 86_400_000
DIA = 20774
AGORA = DIA * D + 15 * 3600000
ZONA = dict(preco_min=99990.0, preco_centro=100000.0, preco_max=100010.0)

SCHEMA = """
CREATE TABLE snapshots (ts_ms INTEGER, sessao TEXT, symbol TEXT, bid REAL, ask REAL, mid REAL, spread REAL,
                        last REAL, vol_compra_agr REAL, vol_venda_agr REAL);
CREATE TABLE niveis (ts_ms INTEGER, sessao TEXT, symbol TEXT, lado TEXT, dist INTEGER, preco REAL, volume REAL);
CREATE TABLE tryd_fita (dia INTEGER, ativo TEXT, num INTEGER, ts_ms INTEGER, hora TEXT, qtd REAL, preco REAL,
                        comprador TEXT, vendedor TEXT, agressor TEXT, var TEXT, agente_agr TEXT);
CREATE TABLE tryd_fita_1min (dia INTEGER, ativo TEXT, minuto INTEGER, grupo TEXT, vol_agr_compra REAL,
                             vol_agr_venda REAL);
"""


def ts(hh, mm=0, ss=0):
    return DIA * D + (hh * 3600 + mm * 60 + ss) * 1000


def banco():
    db = sqlite3.connect(":memory:")
    db.executescript(SCHEMA)
    return db


def precos(db, com_livro=True):
    """Uma leitura por minuto das 10:00 as 15:00; preco 100000 e parado nos ultimos 5 min."""
    linhas, niveis = [], []
    for i in range(301):
        t = ts(10, 0) + i * 60_000
        preco = 100000.0 if i >= 295 else (99990.0 if i % 2 == 0 else 100010.0)
        bid, ask = (99999.0, 100001.0) if i < 295 else (99999.0 + (i - 295), 100001.0 + (i - 295))
        linhas.append((t, "S1", "WIN$", bid, ask, (bid + ask) / 2, 2.0, preco, 10.0, 10.0))
        if com_livro:
            niveis.append((t, "S1", "WIN$", "COMPRA", 1, bid, 10.0))
            niveis.append((t, "S1", "WIN$", "VENDA", 1, ask, 10.0))
    db.executemany("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?)", linhas)
    db.executemany("INSERT INTO niveis VALUES (?,?,?,?,?,?,?)", niveis)


def fita_1min(db, ultimo=100.0, normal=10.0):
    """Blocos de 5 min do dia com volume agredido vendedor; os ultimos 5 min tem `ultimo` por minuto."""
    linhas = [(DIA, "WINZ26", m, "XP", normal, normal if m < 895 else ultimo) for m in range(540, 900)]
    db.executemany("INSERT INTO tryd_fita_1min (dia, ativo, minuto, grupo, vol_agr_compra, vol_agr_venda) "
                   "VALUES (?,?,?,?,?,?)", linhas)


def iceberg(db, agressor="Vendedor"):
    """9 negocios de 1 lote e 3 de 20 lotes do mesmo agente/preco nos ultimos 5 min."""
    linhas = []
    for i in range(9):
        linhas.append((DIA, "WINZ26", 1000 + i, ts(14, 56, i), "14:56:00.000", 1.0, 100000.0, "XP", "Ideal",
                       agressor, "+", "XP"))
    for i in range(3):
        linhas.append((DIA, "WINZ26", 2000 + i, ts(14, 57, i), "14:57:00.000", 20.0, 100000.0, "XP", "Ideal",
                       agressor, "+", "Goldman"))
    db.executemany("INSERT INTO tryd_fita VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", linhas)


class TestEstados(unittest.TestCase):
    def test_distante_proxima_e_em_teste(self):
        db = banco()
        precos(db)
        fita_1min(db)
        longe = dict(preco_min=110000.0, preco_centro=110005.0, preco_max=110010.0)
        perto = dict(preco_min=100015.0, preco_centro=100020.0, preco_max=100030.0)
        r = ge.estado_zona(db, "WIN$", longe, AGORA, "VENDA")
        self.assertEqual(r["status"], "DISTANTE")
        self.assertGreater(r["metricas"]["distancia"], r["metricas"]["atr15"])
        r = ge.estado_zona(db, "WIN$", perto, AGORA, "VENDA")
        self.assertEqual(r["status"], "PROXIMA")
        self.assertLessEqual(r["metricas"]["distancia"], r["metricas"]["atr15"])
        self.assertEqual(r["metricas"]["preco"], 100000.0)

    def test_sem_preco_ou_atr(self):
        r = ge.estado_zona(banco(), "WIN$", ZONA, AGORA, "COMPRA")
        self.assertEqual(r["status"], "DISTANTE")
        self.assertIn("sem preco", r["detalhe"])

    def test_direcao_invalida(self):
        with self.assertRaises(ValueError):
            ge.estado_zona(banco(), "WIN$", ZONA, AGORA, "LATERAL")


class TestAbsorcao(unittest.TestCase):
    def test_absorcao_com_ofi_confirma_o_gatilho(self):
        db = banco()
        precos(db)
        fita_1min(db)
        iceberg(db)
        r = ge.estado_zona(db, "WIN$", ZONA, AGORA, "COMPRA")
        m = r["metricas"]
        self.assertEqual(r["status"], "GATILHO_CONFIRMADO")
        self.assertEqual(m["fonte_absorcao"], "tryd_fita")
        self.assertTrue(m["absorcao"])
        self.assertEqual(m["vol_contra_5min"], 400.0)             # 14:56 a 14:59 x 100
        self.assertGreaterEqual(m["vol_contra_5min"], m["p80_blocos_5min"])
        self.assertEqual(m["deslocamento_5min"], 0.0)
        self.assertTrue(m["ofi_a_favor"])
        self.assertGreater(m["ofi_atual"], 0)
        self.assertEqual(m["iceberg"][0]["lote"], 20.0)           # lote grande do agente repetido
        self.assertIn("absorcao", r["detalhe"])

    def test_sem_absorcao_fica_em_teste_sem_defesa(self):
        db = banco()
        precos(db)
        fita_1min(db, ultimo=1.0, normal=10.0)                    # ultimos 5 min abaixo do p80
        r = ge.estado_zona(db, "WIN$", ZONA, AGORA, "COMPRA")
        self.assertEqual(r["status"], "EM_TESTE")
        self.assertEqual(r["detalhe"], "EM_TESTE (sem defesa ainda)")
        self.assertFalse(r["metricas"]["absorcao"])

    def test_absorcao_com_deslocamento_grande_nao_conta(self):
        db = banco()
        precos(db)
        fita_1min(db)
        db.execute("UPDATE snapshots SET last=100400 WHERE ts_ms BETWEEN ? AND ?", (ts(14, 55), ts(14, 59)))
        r = ge.estado_zona(db, "WIN$", ZONA, AGORA, "COMPRA")
        m = r["metricas"]
        self.assertFalse(m["absorcao"])
        self.assertGreater(m["deslocamento_5min"], ge.DESLOCAMENTO_ATR15 * m["atr15"])

    def test_causalidade_ignora_negocio_depois_de_agora(self):
        db = banco()
        precos(db)
        fita_1min(db)
        db.execute("INSERT INTO tryd_fita_1min (dia, ativo, minuto, grupo, vol_agr_compra, vol_agr_venda) "
                   "VALUES (?,?,?,?,?,?)", (DIA, "WINZ26", 901, "XP", 0.0, 999999.0))
        m = ge.estado_zona(db, "WIN$", ZONA, AGORA, "COMPRA")["metricas"]
        self.assertEqual(m["vol_contra_5min"], 400.0)

    def test_sem_fita_usa_os_snapshots(self):
        db = banco()
        precos(db)
        db.execute("UPDATE snapshots SET vol_venda_agr=1000 WHERE ts_ms BETWEEN ? AND ?",
                   (ts(14, 55), AGORA))
        m = ge.estado_zona(db, "WIN$", ZONA, AGORA, "COMPRA")["metricas"]
        self.assertEqual(m["fonte_absorcao"], "snapshots")
        self.assertTrue(m["absorcao"])
        self.assertAlmostEqual(m["vol_contra_5min"], 6000.0)      # 6 leituras x 1000 (14:55 a 15:00)

    def test_negociacao_depois_da_agora_nao_muda_a_absorcao_da_fita(self):
        db = banco()
        precos(db)
        fita_1min(db, ultimo=1.0, normal=10.0)
        db.execute("INSERT INTO tryd_fita VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (DIA, "WINZ26", 9000, ts(15, 1), "15:01:00.000", 1e6, 100000.0, "XP", "Ideal", "Vendedor", "+",
                    "Goldman"))
        m = ge.estado_zona(db, "WIN$", ZONA, AGORA, "COMPRA")["metricas"]
        self.assertEqual(m["vol_contra_5min"], 4.0)


if __name__ == "__main__":
    unittest.main()
