"""Testes do estimador_tryd.py com dados sinteticos (os numeros de 03/11 a 12/11/2026 sao os reais, do aceite da
ESPEC_DASHBOARD.md 6.6, para o placar reproduzir o resumo publicado).

  python -m unittest test_estimador_tryd -v
"""
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

import numpy as np

import coletor_tryd as ct
import estimador_tryd as et
import players

DIA_MS = 86_400_000
FOTO_MS = 10 * 3_600_000 + 5 * 60_000  # 10:05, primeira foto util de uma acao
HOJE_DIA = et._dia("2026-11-13")

# oficial da B3 (saldo_mil, R$ mil), tryd (fin_liq, R$ mi) e cesta (R$ mi) reais; 02/11 so para a regua do dia anterior
OFICIAL_MIL = {"2026-11-02": -200.0e3, "2026-11-03": -390808.0, "2026-11-04": -1667398.0, "2026-11-05": -1734360.0,
               "2026-11-06": -459573.0, "2026-11-09": -85910.0, "2026-11-10": -696807.0, "2026-11-11": -370021.0}
PUB = {"2026-11-02": "2026-11-04", "2026-11-03": "2026-11-05", "2026-11-04": "2026-11-06", "2026-11-05": "2026-11-09",
       "2026-11-06": "2026-11-10", "2026-11-09": "2026-11-11", "2026-11-10": "2026-11-12", "2026-11-11": "2026-11-13"}
TRYD_MI = {"2026-11-03": 490.9, "2026-11-04": -506.2, "2026-11-05": -835.1, "2026-11-06": -306.0, "2026-11-09": 220.7,
           "2026-11-10": -167.0, "2026-11-11": 8.1, "2026-11-12": 263.2, "2026-11-13": 50.0}
CESTA_MI = {"2026-11-03": 438.8, "2026-11-04": -593.2, "2026-11-05": -555.4, "2026-11-06": 49.3, "2026-11-09": -192.4,
            "2026-11-10": 210.8, "2026-11-11": -425.5, "2026-11-12": 221.3, "2026-11-13": 33.0}

OUTRAS_TABELAS = """
CREATE TABLE fluxo_diario (data_ref TEXT NOT NULL, tipo TEXT NOT NULL, compras_mil REAL, vendas_mil REAL,
    saldo_mil REAL, data_pub TEXT, PRIMARY KEY (data_ref, tipo));
CREATE TABLE features_fluxo_dia (data TEXT PRIMARY KEY, cesta_fin_liq_mi REAL, captura_vol REAL);
CREATE TABLE agressao_1min (ts_ms INTEGER NOT NULL, sessao TEXT NOT NULL, symbol TEXT NOT NULL, n INTEGER,
    fin_compra REAL, fin_venda REAL, PRIMARY KEY (sessao, symbol, ts_ms));
CREATE TABLE snapshots (ts_ms INTEGER NOT NULL, sessao TEXT NOT NULL, symbol TEXT NOT NULL, mid REAL,
    PRIMARY KEY (sessao, symbol, ts_ms));
CREATE TABLE macro_cotacoes (ts_ms INTEGER NOT NULL, sessao TEXT NOT NULL, symbol TEXT NOT NULL, mid REAL);
"""


def banco_memoria():
    db = sqlite3.connect(":memory:")
    db.executescript(ct.SCHEMA)
    db.executescript(OUTRAS_TABELAS)
    return db


def foto(db, dia, hora_ms, ativo, brk, qtd, qtdc, qtdv, volc=None, volv=None, linha=0):
    db.execute("INSERT INTO tryd_saldo (ts_ms, ativo, linha, brk, qtd, qtdc, qtdv, volc, volv) VALUES (?,?,?,?,?,?,?,?,?)",
               (dia * DIA_MS + hora_ms, ativo, linha, brk, qtd, qtdc, qtdv, volc, volv))


def oficial_e_cesta(db):
    db.executemany("INSERT INTO fluxo_diario VALUES (?,?,?,?,?,?)",
                   [(d, et.TIPO_B3, 0.0, 0.0, s, PUB[d]) for d, s in OFICIAL_MIL.items()])
    db.executemany("INSERT OR REPLACE INTO features_fluxo_dia VALUES (?,?,?)", [(d, v, 0.95) for d, v in CESTA_MI.items()])


class TestCalcular(unittest.TestCase):
    def setUp(self):
        players._CACHE_FITA.clear()
        self.db = banco_memoria()
        for d, mi in TRYD_MI.items():
            foto(self.db, et._dia(d), FOTO_MS, "PETR4", "Goldman", 100, 100, 0, volc=mi * 1e6, volv=0.0)
        d3 = et._dia("2026-11-03")
        foto(self.db, d3, 9 * 3_600_000 + 30 * 60_000, "PETR4", "Goldman", 999, 999, 0, volc=999e6)  # antes das 10:00
        foto(self.db, d3, 16 * 3_600_000, "PETR4", "Goldman", 7, 10, 10, volc=1e9)                   # incoerente
        foto(self.db, d3, FOTO_MS, "PETR4", "XP", -50, 0, 50, volc=800e6, volv=300e6, linha=1)       # fora do grupo
        foto(self.db, et._dia("2026-11-04"), 15 * 3_600_000, "PETR4", "Goldman", "txt", 10, 5, volc=1e9)  # DDE texto
        foto(self.db, d3, 9 * 3_600_000 + 30 * 60_000, "WINZ26", "Goldman", 5000, 5000, 0)           # futuro: fora

    def test_ultima_foto_ignora_antes_das_10_e_linhas_invalidas(self):
        fotos = et.ultima_foto_por_corretora(self.db, "PETR4", et._dia("2026-11-03"))
        self.assertEqual(set(fotos), {"Goldman", "XP"})  # as fotos das 9:30 e das 16:00 nao valem
        ts = et._dia("2026-11-03") * DIA_MS + FOTO_MS
        self.assertEqual(fotos["Goldman"], (100, 100, 0, 490.9e6, 0.0, ts))
        self.assertEqual(fotos["XP"], (-50, 0, 50, 800e6, 300e6, ts))

    def test_so_o_grupo_estrangeiro_e_o_futuro_fica_de_fora(self):
        itens = et.calcular(self.db, et._dia("2026-11-03"))
        self.assertEqual([i["ativo"] for i in itens], ["PETR4"])  # WINZ26 e futuro
        i = itens[0]
        self.assertEqual((i["data"], i["saldo_qtd"]), ("2026-11-03", 100.0))
        self.assertEqual(i["fin_liq"], 490.9e6)             # so Goldman; o XP (volc 800, volv 300) nao entra
        self.assertEqual(i["vol_fin_grupo"], 490.9e6)
        self.assertEqual(i["vol_fin_total"], (490.9e6 + 800e6 + 300e6) / 2)
        self.assertEqual(i["foto_ms"], et._dia("2026-11-03") * DIA_MS + FOTO_MS)

    def test_qtd_texto_nao_substitui_a_foto_valida(self):
        fotos = et.ultima_foto_por_corretora(self.db, "PETR4", et._dia("2026-11-04"))
        self.assertEqual(fotos["Goldman"][3], -506.2e6)  # a foto das 15:00 com qtd texto foi ignorada

    def test_dia_sem_foto_valida(self):
        self.assertEqual(et.calcular(self.db, et._dia("2026-11-07")), [])  # sabado, sem dados


class TestPlacar(unittest.TestCase):
    def setUp(self):
        players._CACHE_FITA.clear()
        self.db = banco_memoria()
        for d, mi in TRYD_MI.items():
            foto(self.db, et._dia(d), FOTO_MS, "PETR4", "Goldman", 100, 100, 0, volc=mi * 1e6, volv=0.0)
        oficial_e_cesta(self.db)

    def test_linhas_previsto_e_hoje_fora(self):
        r = et.placar(self.db, hoje_dia=HOJE_DIA)
        datas = [l["data"] for l in r["linhas"]]
        self.assertEqual(datas, sorted(TRYD_MI.keys() - {"2026-11-13"}, reverse=True))  # hoje nao e linha
        self.assertNotIn("2026-11-02", datas)  # oficial fora do periodo (tryd/cesta so de 03/11 em diante)
        nova, velha = r["linhas"][0], r["linhas"][-1]
        self.assertEqual((nova["oficial"], nova["oficial_previsto"]), (None, "2026-11-16"))  # 12/11 + 2 pregoes
        self.assertAlmostEqual(nova["tryd"], 263.2)
        self.assertAlmostEqual(nova["cesta"], 221.3)
        self.assertEqual(nova["n_ativos"], 1)
        self.assertEqual((velha["oficial"], velha["data_pub"], velha["n_ativos"]), (-390.808, "2026-11-05", 1))
        self.assertAlmostEqual(velha["tryd"], 490.9)
        self.assertAlmostEqual(velha["cesta"], 438.8)
        self.assertIsNone(velha["oficial_previsto"])  # com oficial nao ha previsao

    def test_resumo_das_estimativas_e_da_regua_do_dia_anterior(self):
        res = et.placar(self.db, hoje_dia=HOJE_DIA)["resumo"]
        self.assertEqual(res["tryd"]["n"], 7)
        self.assertEqual(res["tryd"]["sinal_certo"], 4)
        self.assertAlmostEqual(res["tryd"]["postos"], 6 / 7, places=3)
        self.assertAlmostEqual(res["tryd"]["erro_abs_medio"], 615.754, delta=0.05)
        self.assertAlmostEqual(res["tryd"]["vies"], 615.754, delta=0.05)  # acima do oficial nos 7 pregões
        self.assertEqual(res["cesta"]["sinal_certo"], 4)
        self.assertAlmostEqual(res["cesta"]["postos"], 11 / 28, places=3)
        self.assertAlmostEqual(res["cesta"]["erro_abs_medio"], 665.897, delta=0.05)
        self.assertAlmostEqual(res["cesta"]["vies"], 619.602, delta=0.05)
        self.assertEqual(res["dia_anterior"], {"n": 7, "sinal_certo": 7})  # oficial negativo em toda a amostra

    def test_cesta_com_captura_baixa_fica_fora_do_resumo(self):
        self.db.execute("UPDATE features_fluxo_dia SET captura_vol=0.8 WHERE data='2026-11-03'")
        r = et.placar(self.db, hoje_dia=HOJE_DIA)
        linha = next(l for l in r["linhas"] if l["data"] == "2026-11-03")
        self.assertAlmostEqual(linha["cesta"], 438.8)  # continua na tabela
        self.assertFalse(linha["cesta_no_resumo"])
        self.assertEqual(r["resumo"]["cesta"]["n"], 6)
        self.assertEqual(r["resumo"]["tryd"]["n"], 7)

    def test_tryd_da_tabela_quando_existe(self):
        db = banco_memoria()
        for d, mi in TRYD_MI.items():
            foto(db, et._dia(d), FOTO_MS, "PETR4", "Goldman", 100, 100, 0, volc=mi * 1e6, volv=0.0)
        oficial_e_cesta(db)
        db.executescript(et.SCHEMA)
        db.executemany("INSERT INTO tryd_estrangeiro_dia VALUES (?,?,?,?,?,?,?)",
                       [("2026-11-03", "PETR4", 100.0, 400.9e6, 400.9e6, 795.45e6, 0),
                        ("2026-11-03", "VALE3", -5.0, 30e6, 30e6, 30e6, 0)])  # valores diferentes do calcular
        r = et.placar(db, hoje_dia=HOJE_DIA)
        l03 = next(l for l in r["linhas"] if l["data"] == "2026-11-03")
        self.assertEqual(l03["n_ativos"], 2)           # 2 ativos na tabela
        self.assertAlmostEqual(l03["tryd"], 430.9)     # 400,9 + 30: da tabela, nao do calcular
        l04 = next(l for l in r["linhas"] if l["data"] == "2026-11-04")
        self.assertAlmostEqual(l04["tryd"], -506.2)    # sem linha na tabela: calculado na hora


class TestPrevisto(unittest.TestCase):
    def test_dois_pregoes_uteis_com_feriado_e_virada_de_ano(self):
        self.assertEqual(et._data_prevista("2026-11-12"), "2026-11-16")  # qui -> seg (fim de semana)
        self.assertEqual(et._data_prevista("2026-11-18"), "2026-11-23")  # 20/11 e feriado (leiloes.feriados)
        self.assertEqual(et._data_prevista("2026-12-30"), "2027-01-04")  # 01/01 feriado + fim de semana


class TestAtualizar(unittest.TestCase):
    def setUp(self):
        players._CACHE_FITA.clear()
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.db = str(Path(self.dir) / "b.db")
        con = sqlite3.connect(self.db)
        con.executescript(ct.SCHEMA)
        foto(con, et._dia("2026-11-02"), 9 * 3_600_000 + 30 * 60_000, "WINZ26", "Goldman", 5000, 5000, 0)  # so futuro
        for d, fin in (("2026-11-03", 100e6), ("2026-11-04", -50e6), ("2026-11-05", 30e6), ("2026-11-06", 10e6)):
            foto(con, et._dia(d), FOTO_MS, "PETR4", "Goldman", 5, 5, 0, volc=fin, volv=0.0)
        con.commit()
        con.close()
        self.hoje = et._dia("2026-11-09")

    def linhas(self):
        con = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
        try:
            return con.execute("SELECT data, ativo, saldo_qtd, fin_liq, vol_fin_total, foto_ms "
                               "FROM tryd_estrangeiro_dia ORDER BY data, ativo").fetchall()
        finally:
            con.close()

    def test_grava_os_dias_fechados_e_e_idempotente(self):
        self.assertEqual(et.atualizar(self.db, hoje_dia=self.hoje), 4)  # 02/11 so tem futuro: nao grava
        self.assertEqual([r[0] for r in self.linhas()], ["2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06"])
        self.assertEqual(self.linhas()[0], ("2026-11-03", "PETR4", 5.0, 100e6, 50e6,
                                            et._dia("2026-11-03") * DIA_MS + FOTO_MS))
        antes = self.linhas()
        self.assertEqual(et.atualizar(self.db, hoje_dia=self.hoje), 1)  # so o dia mais recente e refeito
        self.assertEqual(self.linhas(), antes)  # estado final idempotente

    def test_dia_apagado_volta(self):
        et.atualizar(self.db, hoje_dia=self.hoje)
        con = sqlite3.connect(self.db)
        con.execute("DELETE FROM tryd_estrangeiro_dia WHERE data='2026-11-04'")
        con.commit()
        con.close()
        self.assertEqual(et.atualizar(self.db, hoje_dia=self.hoje), 2)  # o apagado e o mais recente
        self.assertEqual([r[0] for r in self.linhas()], ["2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06"])

    def test_sem_dia_anterior_a_hoje(self):
        self.assertEqual(et.atualizar(self.db, hoje_dia=et._dia("2026-11-02")), 0)


class TestHoje(unittest.TestCase):
    def setUp(self):
        players._CACHE_FITA.clear()
        self.db = banco_memoria()
        self.dia = et._dia("2026-11-13")

    def test_estimativa_parcial_e_cesta_por_minuto(self):
        foto(self.db, self.dia, FOTO_MS, "PETR4", "Goldman", 10, 10, 0, volc=50e6, volv=0.0)
        foto(self.db, self.dia, FOTO_MS, "PETR4", "XP", 8, 8, 0, volc=9e6, volv=1e6, linha=1)  # fora do grupo
        foto(self.db, self.dia, FOTO_MS, "VALE3", "Goldman", -5, 0, 5, volc=20e6, volv=50e6)
        lo = self.dia * DIA_MS
        agr = [(lo + 10 * 3_600_000, "s1", "PETR4", 5, 10e6, 4e6),          # fica a sessao com mais negocios
               (lo + 10 * 3_600_000, "s2", "PETR4", 9, 3e6, 2e6),
               (lo + 10 * 3_600_000 + 60_000, "s1", "VALE3", 1, 2e6, 5e6),
               (lo + 10 * 3_600_000 + 60_000, "s1", "WDO$", 1, 100e6, 0),   # fora da cesta
               (lo + 10 * 3_600_000 - 60_000, "s1", "PETR4", 1, 1e6, 0),    # antes das 10:00
               (lo + 16 * 3_600_000 + 55 * 60_000, "s1", "PETR4", 1, 1e6, 0),  # 16:55 em diante nao entra
               (lo + 16 * 3_600_000 + 54 * 60_000, "s1", "PETR4", 1, 0.5e6, 0)]
        self.db.executemany("INSERT INTO agressao_1min VALUES (?,?,?,?,?,?)", agr)
        r = et.hoje(self.db, self.dia)
        self.assertEqual(r["por_ativo"], [{"ativo": "PETR4", "fin_liq_mi": 50.0},
                                          {"ativo": "VALE3", "fin_liq_mi": -30.0}])
        self.assertEqual(r["tryd_mi"], 20.0)
        self.assertEqual(r["cesta"]["mi"], -1.5)
        self.assertEqual(r["cesta"]["acumulado_mi"], [1.0, -2.0, -1.5])
        self.assertEqual(r["cesta"]["t"], [(lo + 10 * 3_600_000) // 1000, (lo + 10 * 3_600_000 + 60_000) // 1000,
                                           (lo + 16 * 3_600_000 + 54 * 60_000) // 1000])

    def test_dia_vazio(self):
        r = et.hoje(self.db, self.dia)
        self.assertEqual((r["por_ativo"], r["tryd_mi"], r["cesta"]["mi"], r["cesta"]["t"]), ([], 0.0, 0.0, []))


class TestMapa(unittest.TestCase):
    def setUp(self):
        players._CACHE_FITA.clear()
        self.db = banco_memoria()
        self.dia = et._dia("2026-11-12")

    def test_acoes_em_reais_e_futuros_pelo_preco_do_contrato(self):
        foto(self.db, self.dia, FOTO_MS, "PETR4", "Goldman", 100, 100, 0, volc=100e6, volv=40e6)
        foto(self.db, self.dia, FOTO_MS, "PETR4", "XP", -15, 0, 15, volc=10e6, volv=25e6, linha=1)
        h9 = 9 * 3_600_000 + 5 * 60_000
        foto(self.db, self.dia, h9, "WINZ26", "Goldman", 100, 100, 0)
        foto(self.db, self.dia, h9, "WINZ26", "XP", -100, 0, 100, linha=1)
        foto(self.db, self.dia, h9, "WDOZ26", "Goldman", 10, 10, 0)
        self.db.executemany("INSERT INTO snapshots VALUES (?,?,?,?)",
                            [(self.dia * DIA_MS + 17 * 3_600_000, "s", "WIN$", 173847.5)])
        self.db.executemany("INSERT INTO macro_cotacoes VALUES (?,?,?,?)",
                            [(self.dia * DIA_MS + 17 * 3_600_000, "s", "WDO$", 5659.75)])
        r = et.mapa_grupos(self.db, self.dia)
        self.assertEqual(r["ativos"], ["WINZ26", "WDOZ26", "PETR4"])  # ordem do seletor
        self.assertEqual(r["grupos"], players.GRUPOS)
        v = r["valores"]
        self.assertEqual((v["Estrangeiro"]["PETR4"], v["XP"]["PETR4"]), (60.0, -15.0))  # volc - volv, em R$ mi
        self.assertIsNone(v["Outros"]["PETR4"])            # grupo sem corretora na foto
        self.assertAlmostEqual(v["Estrangeiro"]["WINZ26"], 100 * 173847.5 * 0.20 / 1e6)  # contratos x preco x ponto
        self.assertAlmostEqual(v["XP"]["WINZ26"], -100 * 173847.5 * 0.20 / 1e6)
        self.assertAlmostEqual(v["Estrangeiro"]["WDOZ26"], 10 * 5659.75 * 10 / 1e6)
        self.assertIsNone(v["Institucional"]["WINZ26"])    # grupo sem serie no montar

    def test_sem_preco_do_futuro(self):
        foto(self.db, self.dia, 9 * 3_600_000 + 5 * 60_000, "WINZ26", "Goldman", 100, 100, 0)
        r = et.mapa_grupos(self.db, self.dia)  # snapshots sem WIN$ no dia
        self.assertIsNone(r["valores"]["Estrangeiro"]["WINZ26"])


class TestAcompanha(unittest.TestCase):
    """Saldo e preco sinteticos em baldes de 5 min: a diferenca do saldo replica ou nega o log-retorno."""

    def setUp(self):
        players._CACHE_FITA.clear()
        self.db = banco_memoria()

    def serie(self, dia, retornos, saldo, preco, ativo="WINZ26", m0=9 * 60 + 4, tabela="snapshots", simbolo="WIN$"):
        """Foto por balde com saldo_k (Goldman) e -saldo_k/2 (XP); preco no ultimo minuto do balde."""
        s, r = [], 0.0
        for x in retornos:
            r += x
            s.append(r)
        for k in range(len(retornos)):
            ts = dia * DIA_MS + (m0 + 5 * k) * 60_000
            foto(self.db, dia, (m0 + 5 * k) * 60_000, ativo, "Goldman", saldo(s, k), saldo(s, k), 0)
            foto(self.db, dia, (m0 + 5 * k) * 60_000, ativo, "XP", -0.5 * saldo(s, k), 0, 0.5 * saldo(s, k), linha=1)
            self.db.execute(f"INSERT INTO {tabela} VALUES (?,?,?,?)", (ts, "s", simbolo, preco(s, k)))
        return s

    def test_a_favor_e_contra_o_preco(self):
        dia = et._dia("2026-11-10")
        r = [0.01 * (1 + 0.3 * (-1) ** k) for k in range(1, 31)]  # variam, todos positivos
        s = self.serie(dia, r, lambda s, k: 1000.0 * s[k], lambda s, k: 100000.0 * np.exp(s[k]))
        itens = {i["grupo"]: i for i in et.acompanha_preco(self.db, dia, "WINZ26")}
        self.assertEqual(set(itens), {"Estrangeiro", "XP"})
        est = itens["Estrangeiro"]
        self.assertAlmostEqual(est["corr_5m"], 1.0, places=6)  # diff do saldo = 1000 x log-retorno
        self.assertEqual(est["rotulo"], "a favor do preço")
        self.assertGreater(est["nivel"], 0.9)                  # saldo e preco sobem juntos no dia
        self.assertAlmostEqual(est["saldo_final"], 1000.0 * s[-1], places=3)
        xp = itens["XP"]
        self.assertAlmostEqual(xp["corr_5m"], -1.0, places=6)  # diff do saldo = -500 x log-retorno
        self.assertEqual(xp["rotulo"], "contra o preço")

    def test_amostra_curta(self):
        dia = et._dia("2026-11-10")
        r = [0.01 * (1 + 0.3 * (-1) ** k) for k in range(1, 11)]  # 10 baldes < 24
        self.serie(dia, r, lambda s, k: 1000.0 * s[k], lambda s, k: 100000.0 * np.exp(s[k]))
        for i in et.acompanha_preco(self.db, dia, "WINZ26"):
            self.assertIsNone(i["corr_5m"])
            self.assertEqual(i["rotulo"], "amostra curta")

    def test_sem_relacao(self):
        dia = et._dia("2026-11-10")
        r = [0.01 * (1 if k % 4 in (0, 1) else -1) for k in range(1, 29)]  # padrao de 4 contra saldo de 2
        self.serie(dia, r, lambda s, k: float(k % 2), lambda s, k: 100000.0 * np.exp(s[k]))
        i = {j["grupo"]: j for j in et.acompanha_preco(self.db, dia, "WINZ26")}["Estrangeiro"]
        self.assertLess(abs(i["corr_5m"]), 0.1)
        self.assertEqual(i["rotulo"], "sem relação")

    def test_grade_com_primeiros_baldes_defasados(self):
        # o preco comeca um balde antes do saldo e sem observacao no 1o balde do saldo: o ffill precisa da semente
        # (o 1o valor do preco no grid do saldo e o ultimo observado ANTES de b0, nao None)
        dia = et._dia("2026-11-10")
        r = [0.01 * (1 + 0.3 * (-1) ** k) for k in range(1, 31)]
        s, m0, acc = [], 9 * 60 + 4, 0.0  # 09:04, baldes de 5 min a partir do 108
        for x in r:
            acc += x
            s.append(acc)
        for k in range(len(r)):
            foto(self.db, dia, (m0 + 5 * k) * 60_000, "WINZ26", "Goldman", 1000.0 * s[k], 1000.0 * s[k], 0)
        ts = lambda m: dia * DIA_MS + m * 60_000
        self.db.execute("INSERT INTO snapshots VALUES (?,?,?,?)", (ts(m0 - 6), "s", "WIN$", 100000.0 * np.exp(s[0])))
        for k in range(1, len(r)):  # sem preco no balde 108 (minuto 538 e balde 107; 548, o 109)
            self.db.execute("INSERT INTO snapshots VALUES (?,?,?,?)",
                            (ts(m0 - 1 + 5 * k), "s", "WIN$", 100000.0 * np.exp(s[k])))
        i = {j["grupo"]: j for j in et.acompanha_preco(self.db, dia, "WINZ26")}["Estrangeiro"]
        self.assertAlmostEqual(i["corr_5m"], 1.0, places=6)  # a semente do ffill vale P0 no balde 108

    def test_acao_pelo_macro_e_bova11_pelo_snapshots(self):
        dia = et._dia("2026-11-10")
        r = [0.01 * (1 + 0.3 * (-1) ** k) for k in range(1, 31)]
        m0 = 10 * 60 + 4  # acoes abrem as 10:00
        self.serie(dia, r, lambda s, k: 700.0 * s[k], lambda s, k: 30.0 * np.exp(s[k]),
                   ativo="PETR4", m0=m0, tabela="macro_cotacoes", simbolo="PETR4")
        self.serie(dia, r, lambda s, k: 700.0 * s[k], lambda s, k: 30.0 * np.exp(s[k]),
                   ativo="BOVA11", m0=m0, tabela="snapshots", simbolo="BOVA11")  # sem macro: cai no snapshots
        for ativo in ("PETR4", "BOVA11"):
            i = {j["grupo"]: j for j in et.acompanha_preco(self.db, dia, ativo)}["Estrangeiro"]
            self.assertAlmostEqual(i["corr_5m"], 1.0, places=6, msg=ativo)
            self.assertEqual(i["rotulo"], "a favor do preço")

    def test_sem_preco(self):
        dia = et._dia("2026-11-10")
        r = [0.01 * (1 + 0.3 * (-1) ** k) for k in range(1, 31)]
        s = self.serie(dia, r, lambda s, k: 1000.0 * s[k], lambda s, k: 100000.0 * np.exp(s[k]), simbolo="OUTRO$")
        i = {j["grupo"]: j for j in et.acompanha_preco(self.db, dia, "WINZ26")}["Estrangeiro"]
        self.assertIsNone(i["corr_5m"])  # snapshots WIN$ vazio: sem preco, so o saldo
        self.assertIsNone(i["nivel"])
        self.assertEqual(i["rotulo"], "amostra curta")
        self.assertAlmostEqual(i["saldo_final"], 1000.0 * s[-1], places=3)


if __name__ == "__main__":
    unittest.main()
