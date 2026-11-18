"""Testes do monitor_saude.py com banco em memoria e dados sinteticos.

  python -m unittest test_monitor_saude -v
"""
import io
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import monitor_saude as ms
import test_painel_v2 as pv2

D = 86_400_000
DIA = pv2.DIA               # quarta 11/11/2026
SABADO = pv2.SABADO


def ts(hh, mm=0, ss=0, dia=DIA):
    return pv2.ts(dia, hh, mm) + ss * 1000


def banco(sql, *linhas_por_sql):
    db = sqlite3.connect(":memory:")
    db.executescript(sql)
    for sql_insert, linhas in linhas_por_sql:
        db.executemany(sql_insert, linhas)
    return db


class TestAvaliar(unittest.TestCase):
    def test_banco_com_as_fontes_em_dia(self):
        db, agora = pv2.banco_coleta()
        db.executescript("ALTER TABLE tryd_saldo ADD COLUMN qtd REAL; "
                         "ALTER TABLE tryd_saldo ADD COLUMN qtdc REAL; "
                         "ALTER TABLE tryd_saldo ADD COLUMN qtdv REAL;")
        db.execute("UPDATE tryd_saldo SET qtd=10, qtdc=10, qtdv=0")
        itens = ms.avaliar(db, agora)
        self.assertEqual(len(itens), 18)          # 15 fontes de coleta + 3 checagens de qualidade
        self.assertEqual({i["status"] for i in itens}, {"OK"})
        por = {i["coletor"]: i for i in itens}
        self.assertEqual(por["Tryd saldo"]["ts_ultimo_dado_ms"], agora - 10_000)
        self.assertEqual(por["Tryd DDE saldo"]["metrica_qualidade"], 100.0)
        self.assertAlmostEqual(por["Tryd fita captura"]["metrica_qualidade"], 100.0)

    def test_banco_vazio_no_pregao_tudo_parado(self):
        itens = ms.avaliar(sqlite3.connect(":memory:"), ts(14))
        self.assertEqual(len(itens), 18)
        self.assertEqual({i["status"] for i in itens}, {"PARADO"})
        nomes = {i["coletor"] for i in itens}
        self.assertTrue({"Tryd saldo", "Tryd fita", "Tryd DDE saldo", "Tryd fita captura",
                         "MT5 preco congelado"} <= nomes)

    def test_fora_do_pregao_sem_dados_nao_e_falha(self):
        itens = ms.avaliar(sqlite3.connect(":memory:"), ts(20))
        self.assertEqual({i["status"] for i in itens}, {"OK"})

    def test_sabado_sem_dados_nao_e_falha(self):
        self.assertEqual({i["status"] for i in ms.avaliar(sqlite3.connect(":memory:"), ts(12, dia=SABADO))},
                         {"OK"})


class TestQualidade(unittest.TestCase):
    def test_celula_do_dde(self):
        self.assertTrue(ms._celula_valida(10, 10, 0))
        self.assertTrue(ms._celula_valida(9, 10, 1))       # 10 - 1 = 9
        self.assertFalse(ms._celula_valida(5, 10, 1))
        self.assertFalse(ms._celula_valida(None, 10, 1))
        self.assertFalse(ms._celula_valida(10, "", 10))

    def test_dde_saldo_percentual_de_celulas_validas(self):
        agora = ts(12)
        db = banco("CREATE TABLE tryd_saldo (ts_ms INTEGER, qtd REAL, qtdc REAL, qtdv REAL);")
        db.executemany("INSERT INTO tryd_saldo VALUES (?,?,?,?)",
                       [(agora - i * 10_000, 1, 3, 2) for i in range(4)])
        self.assertEqual(ms._tryd_dde_saldo(db, agora, True)["status"], "OK")
        db.execute("INSERT INTO tryd_saldo VALUES (?,?,?,?)", (agora - 50_000, 5, 3, 2))
        f = ms._tryd_dde_saldo(db, agora, True)
        self.assertEqual(f["status"], "DEGRADADO")
        self.assertEqual(f["metrica_qualidade"], 80.0)
        self.assertIn("1 de 5 fotos", f["detalhe"])

    def test_dde_saldo_sem_fotos(self):
        db = sqlite3.connect(":memory:")
        self.assertEqual(ms._tryd_dde_saldo(db, ts(12), True)["status"], "PARADO")
        self.assertEqual(ms._tryd_dde_saldo(db, ts(20), False)["status"], "OK")

    def test_captura_da_fita_e_a_menor_entre_os_ativos(self):
        agora = ts(12)
        db = banco("CREATE TABLE tryd_fita (ts_ms INTEGER, ativo TEXT, num INTEGER);")
        db.executemany("INSERT INTO tryd_fita VALUES (?,?,?)",
                       [(agora - 10_000, "WINZ26", 10), (agora - 9_000, "WINZ26", 20),
                        (agora - 8_000, "WDOZ26", 10), (agora - 7_000, "WDOZ26", 1000)])
        f = ms._tryd_fita_captura(db, agora, True)
        self.assertEqual(f["status"], "DEGRADADO")
        self.assertEqual(f["metrica_qualidade"], 2.0)
        self.assertIn("WDOZ26", f["detalhe"])
        db.execute("DELETE FROM tryd_fita WHERE ativo='WDOZ26'")
        self.assertEqual(ms._tryd_fita_captura(db, agora, True)["status"], "OK")

    def test_fita_sem_negocios(self):
        db = sqlite3.connect(":memory:")
        self.assertEqual(ms._tryd_fita_captura(db, ts(12), True)["status"], "PARADO")
        self.assertEqual(ms._tryd_fita_captura(db, ts(20), False)["status"], "OK")

    def test_win_congelado(self):
        agora = ts(12)
        db = banco("CREATE TABLE snapshots (ts_ms INTEGER, symbol TEXT, mid REAL);")
        db.executemany("INSERT INTO snapshots VALUES (?,?,?)",
                       [(agora - 130_000, "WIN$", 100.0), (agora - 70_000, "WIN$", 100.0),
                        (agora - 10_000, "WIN$", 100.0)])
        f = ms._win_congelado(db, agora, True)
        self.assertEqual(f["status"], "PARADO")
        self.assertIn("120 s", f["detalhe"])
        db.execute("UPDATE snapshots SET mid=101 WHERE ts_ms=?", (agora - 10_000,))
        self.assertEqual(ms._win_congelado(db, agora, True)["status"], "OK")

    def test_win_sem_snapshot(self):
        db = sqlite3.connect(":memory:")
        self.assertEqual(ms._win_congelado(db, ts(12), True)["status"], "PARADO")
        self.assertEqual(ms._win_congelado(db, ts(20), False)["status"], "OK")


class TestGravar(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.executescript(ms.SCHEMA)
        self.base = 1_000_000_000

    def ok(self, coletor="X"):
        return ms._item(coletor, 1, "OK")

    def parado(self, coletor="X", detalhe="sem leitura"):
        return ms._item(coletor, 2, "PARADO", 0.0, detalhe)

    def test_gravacao_e_alerta_na_transicao(self):
        self.assertEqual(ms.gravar(self.con, [self.ok()], self.base), [])
        self.assertEqual(self.con.execute("SELECT status, metrica_qualidade FROM saude_coletores").fetchone(),
                         ("OK", None))
        novos = ms.gravar(self.con, [self.parado()], self.base + 60_000)
        self.assertEqual(novos, [{"coletor": "X", "status": "PARADO", "texto": "X: PARADO - sem leitura"}])
        self.assertEqual(self.con.execute("SELECT status FROM saude_coletores").fetchone()[0], "PARADO")
        self.assertEqual(self.con.execute("SELECT texto, enviado_telegram FROM saude_alertas").fetchall(),
                         [("X: PARADO - sem leitura", 0)])

    def test_nao_repete_o_alerta_enquanto_continua_ruim(self):
        ms.gravar(self.con, [self.ok()], self.base)
        self.assertEqual(len(ms.gravar(self.con, [self.parado()], self.base + 60_000)), 1)
        self.assertEqual(ms.gravar(self.con, [self.parado()], self.base + 120_000), [])
        self.assertEqual(self.con.execute("SELECT count(*) FROM saude_alertas").fetchone()[0], 1)

    def test_cooldown_de_15_min_por_coletor(self):
        ms.gravar(self.con, [self.ok()], self.base)
        self.assertEqual(len(ms.gravar(self.con, [self.parado()], self.base + 60_000)), 1)
        ms.gravar(self.con, [self.ok()], self.base + 120_000)
        self.assertEqual(ms.gravar(self.con, [self.parado()], self.base + 180_000), [])      # < 15 min
        ms.gravar(self.con, [self.ok()], self.base + 900_000)
        self.assertEqual(len(ms.gravar(self.con, [self.parado()], self.base + 60_000 + ms.COOLDOWN_MS)), 1)

    def test_primeira_avaliacao_ja_avisa_da_falha(self):
        self.assertEqual(len(ms.gravar(self.con, [self.parado()], self.base)), 1)

    def test_degradado_tambem_gera_alerta(self):
        novos = ms.gravar(self.con, [ms._item("X", 1, "DEGRADADO", 80.0, "captura de 50%")], self.base)
        self.assertEqual(novos[0]["status"], "DEGRADADO")
        self.assertIn("captura de 50%", novos[0]["texto"])


class TestSaudeApi(unittest.TestCase):
    def test_sem_tabelas_devolve_listas_vazias(self):
        self.assertEqual(ms.saude_api(sqlite3.connect(":memory:")), {"coletores": [], "alertas": []})

    def test_le_coletores_e_alertas(self):
        con = sqlite3.connect(":memory:")
        con.executescript(ms.SCHEMA)
        ms.gravar(con, [ms._item("WIN", 1, "OK"), ms._item("WDO", 2, "PARADO", 0.0, "parado")], 1_000)
        r = ms.saude_api(con)
        self.assertEqual([c["coletor"] for c in r["coletores"]], ["WDO", "WIN"])
        self.assertEqual(r["coletores"][0]["detalhe"], "parado")
        self.assertEqual([a["coletor"] for a in r["alertas"]], ["WDO"])

    def test_dashboard_tem_cache_curto_da_saude(self):
        import dashboard
        self.assertEqual(dashboard.CACHE_V2_S["saude"], 15.0)


class TestTelegram(unittest.TestCase):
    class _Resp:
        def __init__(self, status_code):
            self.status_code = status_code

    def setUp(self):
        self._orig_raiz = ms.RAIZ
        self._orig_config = ms.config_telegram

    def tearDown(self):
        ms.RAIZ = self._orig_raiz
        ms.config_telegram = self._orig_config

    def _sem_env(self):
        pasta = tempfile.TemporaryDirectory()
        self.addCleanup(pasta.cleanup)
        ms.RAIZ = Path(pasta.name)
        return Path(pasta.name)

    def test_config_telegram_le_o_arquivo_e_o_ambiente_vence(self):
        pasta = self._sem_env()
        (pasta / ".env").write_text('TELEGRAM_BOT_TOKEN="do_arquivo"\n# comentario\nTELEGRAM_CHAT_ID=123\n',
                                    encoding="utf-8")
        self.assertEqual(ms.config_telegram({}), ("do_arquivo", "123"))
        self.assertEqual(ms.config_telegram({"TELEGRAM_BOT_TOKEN": "do_env"}), ("do_env", "123"))
        self.assertEqual(ms.config_telegram({"TELEGRAM_BOT_TOKEN": "a", "TELEGRAM_CHAT_ID": "b"}), ("a", "b"))

    def test_config_telegram_sem_arquivo(self):
        self._sem_env()
        self.assertEqual(ms.config_telegram({}), (None, None))
        self.assertEqual(ms.config_telegram({"TELEGRAM_BOT_TOKEN": "a"}), ("a", None))

    def test_sem_token_nao_chama_a_rede(self):
        self._sem_env()
        chamadas = []
        ms.config_telegram = lambda env=None: (None, None)
        with redirect_stdout(io.StringIO()) as out:
            self.assertFalse(ms.notificar("oi", ativo=True, http_post=lambda *a, **k: chamadas.append(1)))
        self.assertEqual(chamadas, [])
        self.assertIn("sem envio", out.getvalue())

    def test_telegram_desligado_nao_chama_a_rede_mesmo_com_token(self):
        chamadas = []
        ms.config_telegram = lambda env=None: ("token", "chat")
        with redirect_stdout(io.StringIO()):
            self.assertFalse(ms.notificar("oi", ativo=False, http_post=lambda *a, **k: chamadas.append(1)))
        self.assertEqual(chamadas, [])

    def test_envia_com_token_e_processo_ligado(self):
        chamadas = []

        def post(url, json, timeout):
            chamadas.append((url, json, timeout))
            return self._Resp(200)

        ms.config_telegram = lambda env=None: ("TOKEN", "42")
        with redirect_stdout(io.StringIO()):
            self.assertTrue(ms.notificar("oi", ativo=True, http_post=post))
        url, corpo, timeout = chamadas[0]
        self.assertEqual(url, "https://api.telegram.org/botTOKEN/sendMessage")
        self.assertEqual(corpo, {"chat_id": "42", "text": "oi"})
        self.assertEqual(timeout, 10)

    def test_erro_de_rede_nao_derruba_o_monitor(self):
        def post(*a, **k):
            raise OSError("sem rede")

        ms.config_telegram = lambda env=None: ("TOKEN", "42")
        with redirect_stdout(io.StringIO()) as out:
            self.assertFalse(ms.notificar("oi", ativo=True, http_post=post))
        self.assertIn("Telegram falhou", out.getvalue())

    def test_resposta_diferente_de_200_devolve_false(self):
        ms.config_telegram = lambda env=None: ("TOKEN", "42")
        with redirect_stdout(io.StringIO()):
            self.assertFalse(ms.notificar("oi", ativo=True, http_post=lambda *a, **k: self._Resp(500)))


if __name__ == "__main__":
    unittest.main()
