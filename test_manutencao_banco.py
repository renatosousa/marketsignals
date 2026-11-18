"""Testes do manutencao_banco.py com banco sintetico (nunca contra dados/book.db).

  python -m unittest test_manutencao_banco -v

Cobre: agregacao da fita (Comprador/Vendedor/RLP, lote grande, captura com buraco no num), gravacao de
tryd_fita_1min, exportacao Parquet + checksum + idempotencia, checksum errado nao entra no manifesto,
ler_historico juntando Parquet e SQLite sem duplicar, expurgo seco e real, backup sem as brutas e estado.
"""
import gzip
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

import coletor_tryd as ct
import manutencao_banco as mb

DIA = 20740
T0 = DIA * 86_400_000 + 12 * 3_600_000


def criar_banco(pasta):
    caminho = Path(pasta) / "book.db"
    db = sqlite3.connect(str(caminho))
    db.executescript(ct.SCHEMA)
    db.execute("CREATE TABLE nucleo (ts_ms INTEGER PRIMARY KEY, valor REAL)")
    db.execute("CREATE TABLE macro_cotacoes (ts_ms INTEGER, symbol TEXT, last REAL)")
    db.commit()
    return db, caminho


def inserir_fita(db, dia, num, ts, hora, qtd, comprador, vendedor, agressor, ativo="WINZ26", preco=170000.0):
    db.execute("INSERT INTO tryd_fita (dia, ativo, num, ts_ms, hora, qtd, preco, comprador, vendedor, agressor, var) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               (dia, ativo, num, ts, hora, qtd, preco, comprador, vendedor, agressor, "+"))


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.parquet = self.tmp / "historico_parquet"
        self.db, self.caminho = criar_banco(self.tmp)

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()


class TestAgregacao(Base):
    def setUp(self):
        super().setUp()
        # minuto 720: 4 negocios lidos, num 10..50 (o 40 faltou) -> captura 4/5 = 0,8
        inserir_fita(self.db, DIA, 10, T0, "12:00:00.000", 5, "Goldman", "XP", "Comprador")
        inserir_fita(self.db, DIA, 20, T0 + 1000, "12:00:01.000", 3, "XP", "Goldman", "Vendedor")
        inserir_fita(self.db, DIA, 30, T0 + 2000, "12:00:02.000", 9, "BTG", "BTG", "RLP")
        inserir_fita(self.db, DIA, 50, T0 + 3000, "12:00:03.000", 2, "Ideal", "Genial", "Direto")
        # outro ativo, 1 negocio no minuto -> captura NULL
        inserir_fita(self.db, DIA, 10, T0 + 300_000, "12:05:00.000", 1, "XP", "Goldman", "Comprador",
                     ativo="WDOZ26")
        self.db.commit()

    def test_comprador_vendedor_rlp_e_pontas(self):
        df = mb.agregar_fita_dia(self.db, DIA)
        win = df[df["ativo"] == "WINZ26"].set_index("grupo")
        self.assertAlmostEqual(win.loc["Estrangeiro", "vol_agr_compra"], 5)
        self.assertAlmostEqual(win.loc["Estrangeiro", "vol_agr_venda"], 3)
        self.assertAlmostEqual(win.loc["XP", "vol_pas_venda"], 5)
        self.assertAlmostEqual(win.loc["XP", "vol_pas_compra"], 3)
        self.assertAlmostEqual(win.loc["Estrangeiro", "vol_agr_venda"], 3)
        self.assertAlmostEqual(win.loc["XP", "vol_agr_venda"], 0)
        self.assertAlmostEqual(win.loc["BTG", "vol_rlp"], 9)
        # Direto: so n_negocios, nas duas pontas
        self.assertEqual(win.loc["Varejo", "n_negocios"], 1)
        self.assertEqual(win.loc["Genial", "n_negocios"], 1)
        self.assertAlmostEqual(win.loc["Varejo", "vol_agr_compra"], 0)
        # RLP tem a mesma corretora nas duas pontas: n_negocios conta uma vez
        self.assertEqual(win.loc["BTG", "n_negocios"], 1)

    def test_lote_grande_pelo_percentil_95_do_ativo(self):
        df = mb.agregar_fita_dia(self.db, DIA)
        win = df[df["ativo"] == "WINZ26"].set_index("grupo")
        # qtd 2,3,5,9 -> p95 = 8,4: so o negocio de 9 e lote grande
        self.assertEqual(win.loc["BTG", "n_lotes_grandes"], 1)
        self.assertEqual(win.loc["Estrangeiro", "n_lotes_grandes"], 0)
        self.assertEqual(win.loc["XP", "n_lotes_grandes"], 0)

    def test_captura_com_buraco_no_num_e_null_com_um_negocio(self):
        df = mb.agregar_fita_dia(self.db, DIA)
        win = df[df["ativo"] == "WINZ26"]
        self.assertTrue((win["captura"] == 0.8).all())
        wdo = df[df["ativo"] == "WDOZ26"]
        self.assertTrue(wdo["captura"].isna().all())

    def test_percentil_ponderado_igual_numpy(self):
        self.assertAlmostEqual(mb._percentil([2, 3, 5, 9], [1, 1, 1, 1], 95), 8.4)
        self.assertAlmostEqual(mb._percentil([1, 10], [9, 1], 50), 1.0)
        self.assertIsNone(mb._percentil([], [], 95))

    def test_gravar_1min_idempotente(self):
        df = mb.agregar_fita_dia(self.db, DIA)
        self.assertEqual(mb.gravar_fita_1min(self.db, df), len(df))
        self.assertEqual(mb.gravar_fita_1min(self.db, df), len(df))
        n, dias = self.db.execute("SELECT COUNT(*), COUNT(DISTINCT dia) FROM tryd_fita_1min").fetchone()
        self.assertEqual((n, dias), (len(df), 1))
        self.assertEqual(mb.gravar_fita_1min(self.db, pd.DataFrame(columns=mb.COLUNAS_1MIN)), 0)


class TestArquivoFrio(Base):
    def _dois_dias(self):
        inserir_fita(self.db, DIA, 10, T0, "12:00:00.000", 5, "Goldman", "XP", "Comprador")
        inserir_fita(self.db, DIA, 20, T0 + 1000, "12:00:01.000", 3, "XP", "Goldman", "Vendedor")
        inserir_fita(self.db, DIA + 1, 10, T0 + 86_400_000, "12:00:00.000", 7, "BTG", "Ideal", "Comprador")
        self.db.commit()

    def test_exportar_confere_checksum_e_e_idempotente(self):
        self._dois_dias()
        r = mb.exportar_dia(self.caminho, "tryd_fita", DIA, base=self.parquet)
        self.assertEqual((r["acao"], r["linhas"]), ("exportado", 2))
        arquivo = mb._caminho_parquet("tryd_fita", DIA, self.parquet)
        self.assertTrue(arquivo.exists())
        n, s1, s2 = mb._soma_parquet(arquivo, "tryd_fita")
        self.assertEqual((n, s1, s2), (2, 30, 8 * 1000))
        linha = self.db.execute("SELECT arquivo, linhas, checksum, apagado_ms FROM arquivo_frio "
                                "WHERE tabela='tryd_fita' AND dia=?", (DIA,)).fetchone()
        self.assertEqual(linha[1], 2)
        self.assertEqual(linha[2], mb._checksum(2, 30, 8 * 1000))
        self.assertIsNone(linha[3])
        self.assertTrue(linha[0].endswith(f"dia={DIA}.parquet"))
        r2 = mb.exportar_dia(self.caminho, "tryd_fita", DIA, base=self.parquet)
        self.assertEqual((r2["acao"], r2["linhas"]), ("pulado", 2))

    def test_checksum_errado_nao_entra_no_manifesto(self):
        self._dois_dias()
        with mock.patch.object(mb, "_soma_parquet", return_value=(2, 30, 0)):
            r = mb.exportar_dia(self.caminho, "tryd_fita", DIA, base=self.parquet)
        self.assertEqual(r["acao"], "falhou")
        self.assertFalse(mb._caminho_parquet("tryd_fita", DIA, self.parquet).exists())
        self.assertIsNone(self.db.execute("SELECT 1 FROM arquivo_frio WHERE tabela='tryd_fita'").fetchone())

    def test_exportar_pendentes_conta_pregoes_com_dado(self):
        for i in range(12):
            inserir_fita(self.db, DIA + i, 10, (DIA + i) * 86_400_000 + T0 % 86_400_000,
                         "12:00:00.000", 1, "Goldman", "XP", "Comprador")
        self.db.commit()
        saida = mb.exportar_pendentes(self.caminho, dias_manter=10, base=self.parquet)
        self.assertEqual(len(saida), 2)
        self.assertEqual([r["dia"] for r in saida], [DIA, DIA + 1])
        self.assertTrue(all(r["acao"] == "exportado" for r in saida))

    def test_ler_historico_junta_parquet_e_sqlite_sem_duplicar(self):
        self._dois_dias()
        mb.exportar_dia(self.caminho, "tryd_fita", DIA, base=self.parquet)
        # simula o expurgo do dia exportado
        self.db.execute("DELETE FROM tryd_fita WHERE dia=?", (DIA,))
        self.db.execute("UPDATE arquivo_frio SET apagado_ms=1 WHERE tabela='tryd_fita' AND dia=?", (DIA,))
        self.db.commit()
        df = mb.ler_historico("tryd_fita", DIA, DIA + 1, db_path=self.caminho, base=self.parquet)
        self.assertEqual(len(df), 3)
        self.assertEqual(sorted(df["dia"].unique().tolist()), [DIA, DIA + 1])
        self.assertEqual(df["num"].value_counts().to_dict(), {10: 2, 20: 1})
        so_win = mb.ler_historico("tryd_fita", DIA, DIA + 1, colunas=["dia", "num"],
                                  filtro_sql="ativo='WINZ26'", db_path=self.caminho, base=self.parquet)
        self.assertEqual(list(so_win.columns), ["dia", "num"])
        self.assertEqual(len(so_win), 3)

    def test_expurgo_seco_nao_apaga_e_apagar_apaga_so_conferido(self):
        for i in range(12):
            inserir_fita(self.db, DIA + i, 10, (DIA + i) * 86_400_000 + T0 % 86_400_000,
                         "12:00:00.000", 1, "Goldman", "XP", "Comprador")
        self.db.commit()
        mb.exportar_pendentes(self.caminho, dias_manter=10, base=self.parquet)
        seco = mb.expurgar(self.caminho, dias_manter=10, apagar=False, base=self.parquet)
        self.assertEqual([r["acao"] for r in seco], ["simulado", "simulado"])
        self.assertEqual(self.db.execute("SELECT COUNT(DISTINCT dia) FROM tryd_fita").fetchone()[0], 12)
        real = mb.expurgar(self.caminho, dias_manter=10, apagar=True, base=self.parquet)
        self.assertEqual([r["acao"] for r in real], ["apagado", "apagado"])
        self.assertEqual(self.db.execute("SELECT COUNT(DISTINCT dia) FROM tryd_fita").fetchone()[0], 10)
        self.assertIsNone(self.db.execute(
            "SELECT 1 FROM tryd_fita WHERE dia IN (?,?)", (DIA, DIA + 1)).fetchone())
        marcados = self.db.execute(
            "SELECT COUNT(*) FROM arquivo_frio WHERE apagado_ms IS NOT NULL").fetchone()[0]
        self.assertEqual(marcados, 2)
        df = mb.ler_historico("tryd_fita", DIA, DIA + 1, db_path=self.caminho, base=self.parquet)
        self.assertEqual(len(df), 2)

    def test_expurgo_nao_apaga_checksum_errado(self):
        self._dois_dias()
        mb.exportar_dia(self.caminho, "tryd_fita", DIA, base=self.parquet)
        with mock.patch.object(mb, "_soma_parquet", return_value=(2, 0, 0)):
            saida = mb.expurgar(self.caminho, dias_manter=0, apagar=True, base=self.parquet)
        self.assertEqual(saida, [])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM tryd_fita WHERE dia=?", (DIA,)).fetchone()[0], 2)

    def test_exportar_tabela_com_inteiro_anulavel_preserva_null(self):
        self.db.execute("""CREATE TABLE snapshots (
            ts_ms INTEGER NOT NULL, sessao TEXT NOT NULL, symbol TEXT NOT NULL,
            bid REAL, ask REAL, mid REAL, spread REAL, last REAL,
            vol_bid_total REAL, vol_ask_total REAL, vol_bid_near REAL, vol_ask_near REAL,
            imbalance_total REAL, imbalance_near REAL, imbalance_pond REAL,
            microprice REAL, niveis_bid INTEGER, niveis_ask INTEGER,
            n_trades INTEGER, vol_compra_agr REAL, vol_venda_agr REAL, delta REAL, saldo_acum REAL,
            PRIMARY KEY (sessao, symbol, ts_ms))""")
        base = DIA * 86_400_000
        self.db.executemany("INSERT INTO snapshots (ts_ms, sessao, symbol, bid, mid, n_trades) VALUES (?,?,?,?,?,?)",
                            [(base + 1000, "s1", "WIN$", 1.5, 2.5, None),
                             (base + 2000, "s1", "WIN$", 3.0, 4.0, 7)])
        self.db.commit()
        r = mb.exportar_dia(self.caminho, "snapshots", DIA, base=self.parquet)
        self.assertEqual((r["acao"], r["linhas"]), ("exportado", 2))
        arquivo = mb._caminho_parquet("snapshots", DIA, self.parquet)
        self.assertEqual(mb._soma_parquet(arquivo, "snapshots"), mb._soma_sqlite(self.db, "snapshots", DIA))
        con = sqlite3.connect(str(self.caminho))
        con.execute("UPDATE arquivo_frio SET apagado_ms=1 WHERE tabela='snapshots' AND dia=?", (DIA,))
        con.execute("DELETE FROM snapshots")
        con.commit()
        con.close()
        df = mb.ler_historico("snapshots", DIA, DIA, db_path=self.caminho, base=self.parquet)
        self.assertEqual(len(df), 2)
        self.assertTrue(pd.isna(df.sort_values("ts_ms")["n_trades"].iloc[0]))
        self.assertEqual(df["mid"].sum(), 6.5)


class TestBackup(Base):
    def test_backup_sem_brutas_e_sem_macro(self):
        self.db.executemany("INSERT INTO tryd_fita (dia, ativo, num, ts_ms, hora, qtd) VALUES (?,?,?,?,?,?)",
                            [(DIA, "WINZ26", 10, T0, "12:00:00.000", 1)])
        self.db.executemany("INSERT INTO tryd_saldo (ts_ms, ativo, linha, brk, qtd) VALUES (?,?,?,?,?)",
                            [(T0, "WINZ26", 0, "XP", 1)])
        self.db.execute("INSERT INTO nucleo (ts_ms, valor) VALUES (1, 2.5)")
        self.db.execute("INSERT INTO macro_cotacoes (ts_ms, symbol, last) VALUES (1, 'WDO$', 5800)")
        self.db.commit()
        pasta = self.tmp / "backups"
        r = mb.backup_nucleo(self.caminho, pasta=pasta, manter=15)
        destino = Path(r["arquivo"])
        self.assertTrue(destino.name.startswith("book_core_"))
        self.assertTrue(destino.name.endswith(".db.gz"))
        self.assertFalse((pasta / "book_core_tmp.db").exists())
        copia = self.tmp / "copia.db"
        with gzip.open(destino, "rb") as f, open(copia, "wb") as g:
            g.write(f.read())
        c = sqlite3.connect(str(copia))
        tabelas = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("nucleo", tabelas)
        self.assertNotIn("tryd_fita", tabelas)
        self.assertNotIn("tryd_saldo", tabelas)
        self.assertNotIn("macro_cotacoes", tabelas)
        self.assertEqual(c.execute("SELECT valor FROM nucleo").fetchone()[0], 2.5)
        self.assertEqual(c.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        c.close()

    def test_backup_mantem_so_os_n_mais_recentes(self):
        self.db.execute("INSERT INTO nucleo (ts_ms, valor) VALUES (1, 2.5)")
        self.db.commit()
        pasta = self.tmp / "backups"
        pasta.mkdir()
        for dia in (1, 2, 3):
            (pasta / f"book_core_2026010{dia}.db.gz").write_bytes(b"x")
        mb.backup_nucleo(self.caminho, pasta=pasta, manter=2)
        nomes = sorted(p.name for p in pasta.glob("book_core_*.db.gz"))
        self.assertEqual(len(nomes), 2)
        self.assertNotIn("book_core_20260101.db.gz", nomes)

    def test_diaria_agrega_arquiva_e_nao_apaga(self):
        inserir_fita(self.db, DIA, 10, T0, "12:00:00.000", 5, "Goldman", "XP", "Comprador")
        self.db.commit()
        r = mb.diaria(self.caminho, self.parquet, dias_manter=10, pasta_backups=self.tmp / "backups")
        self.assertEqual(r["dias_agregados"], [DIA])
        self.assertGreater(r["linhas_1min"], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM tryd_fita_1min").fetchone()[0], r["linhas_1min"])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM tryd_fita").fetchone()[0], 1)
        self.assertTrue(Path(r["backup"]["arquivo"]).exists())
        # segunda rodada nao reagrega o mesmo dia
        r2 = mb.diaria(self.caminho, self.parquet, dias_manter=10, pasta_backups=self.tmp / "backups")
        self.assertEqual(r2["dias_agregados"], [])

    def test_estado_conta_sqlite_e_parquet(self):
        self.db.execute("INSERT INTO tryd_fita (dia, ativo, num, ts_ms, hora, qtd) VALUES (?,?,?,?,?,?)",
                        (DIA, "WINZ26", 10, T0, "12:00:00.000", 1))
        self.db.commit()
        mb.exportar_dia(self.caminho, "tryd_fita", DIA, base=self.parquet)
        stats = mb.estado(self.caminho, self.parquet)
        self.assertEqual(stats["tryd_fita"]["linhas_sql"], 1)
        self.assertEqual(stats["tryd_fita"]["dias_sql"], 1)
        self.assertEqual(stats["tryd_fita"]["dias_parquet"], 1)
        self.assertEqual(stats["tryd_fita"]["linhas_parquet"], 1)
        self.assertGreater(stats["tryd_fita"]["bytes_parquet"], 0)

    def test_servico_com_zero_segundos_sai_sem_gravar(self):
        inserir_fita(self.db, DIA, 10, T0, "12:00:00.000", 5, "Goldman", "XP", "Comprador")
        self.db.commit()
        mb.servico(self.caminho, 0, hora="00:00", base=self.parquet, pasta_backups=self.tmp / "backups")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM tryd_fita").fetchone()[0], 1)
        self.assertIsNone(self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tryd_fita_1min'").fetchone())
        self.assertFalse((self.tmp / "backups").exists())


if __name__ == "__main__":
    unittest.main()
