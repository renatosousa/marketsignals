"""Testes do atualizador_cockpit.py: ciclo, rotulos, cache com erro, montagem e gravacao do estado.

  python -m unittest test_atualizador_cockpit -v

Nada aqui toca no banco real: usa SQLite temporario e funcoes de peca sinteticas.
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import atualizador_cockpit as a

D = 86_400_000
DIA = 20774                       # terca 17/11/2026


def ts(dia, hh, mm=0):
    return dia * D + (hh * 60 + mm) * 60_000


def caminho_vazio(pasta):
    arq = Path(pasta) / "cockpit.db"
    sqlite3.connect(arq).close()
    return str(arq)


class TestCiclo(unittest.TestCase):
    def test_ciclo_no_pregao_pre_mercado_e_fora(self):
        self.assertEqual(a.intervalo_s(ts(DIA, 10, 0)), 5.0)
        self.assertEqual(a.intervalo_s(ts(DIA, 9, 0)), 5.0)
        self.assertEqual(a.intervalo_s(ts(DIA, 18, 25)), 5.0)
        self.assertEqual(a.intervalo_s(ts(DIA, 18, 26)), 300.0)
        self.assertEqual(a.intervalo_s(ts(DIA, 7, 0)), 60.0)
        self.assertEqual(a.intervalo_s(ts(DIA, 8, 59)), 60.0)
        self.assertEqual(a.intervalo_s(ts(DIA, 6, 59)), 300.0)
        self.assertEqual(a.intervalo_s(ts(DIA, 10, 0), pregao=False), 300.0)


class TestRotulos(unittest.TestCase):
    def test_faixas_do_score(self):
        self.assertEqual(a.faixa_score(65), "ALTA FORTE")
        self.assertEqual(a.faixa_score(-68), "BAIXA FORTE")
        self.assertEqual(a.faixa_score(40), "ALTA MODERADO")
        self.assertEqual(a.faixa_score(-18), "BAIXA LEVE")
        self.assertEqual(a.faixa_score(5), "NEUTRO")
        self.assertEqual(a.faixa_score(None), "SEM DADO")

    def test_repique_quando_o_sinal_troca(self):
        self.assertEqual(a.rotulo_intraday(-68, 18), "REPIQUE LEVE")
        self.assertEqual(a.rotulo_intraday(-68, 40), "REPIQUE MODERADO")
        self.assertEqual(a.rotulo_intraday(-68, -40), "BAIXA MODERADO")
        self.assertEqual(a.rotulo_intraday(None, None), "SEM DADO")

    def test_gex(self):
        self.assertEqual(a.rotulo_gex(-1.0), "NEGATIVO (AMPLIFICADOR)")
        self.assertEqual(a.rotulo_gex(2.0), "POSITIVO (AMORTECEDOR)")
        self.assertEqual(a.rotulo_gex(0.0), "NULO")
        self.assertEqual(a.rotulo_gex(None), "SEM GEX")


class TestCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.p = a.Pecas(caminho_vazio(self.tmp.name), seco=True)

    def test_erro_guarda_o_ultimo_valor_bom_com_idade(self):
        self.p.executar("peca", 1000, lambda con, agora: {"v": 7})
        def falha(con, agora):
            raise RuntimeError("boom")
        self.p.executar("peca", 2000, falha)
        r = self.p.peca("peca", 5000)
        self.assertEqual(r["v"], 7)
        self.assertIn("boom", r["erro"])
        self.assertEqual(r["idade_s"], 4.0)

    def test_erro_na_primeira_leitura(self):
        def falha(con, agora):
            raise ValueError("sem tabela")
        self.p.executar("peca", 1000, falha)
        r = self.p.peca("peca", 1000)
        self.assertIn("sem tabela", r["erro"])
        self.assertNotIn("idade_s", r)

    def test_peca_ainda_nao_calculada(self):
        self.assertEqual(self.p.peca("nada")["erro"], "aguardando primeira leitura")

    def test_cenario_atualiza_o_vies(self):
        self.p.executar("cenario", 1000, lambda con, agora: {"_vies": -55.0, "score_estrutural": -55.0})
        self.assertEqual(self.p.vies, -55.0)


class TestEstado(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = caminho_vazio(self.tmp.name)
        self.p = a.Pecas(self.db, seco=True)
        self.agora = ts(DIA, 15, 0)

    def _preencher(self):
        p = self.p
        p.executar("cenario", self.agora, lambda con, a_: {
            "_vies": -68.0, "score_estrutural": -68.0, "score_intraday": 18.0,
            "rotulos": {"estrutural": "BAIXA FORTE", "intraday": "REPIQUE LEVE"},
            "pilares": {"macro_juros": {"nome": "Macro e juros", "peso": 0.25, "score": -40.0,
                                        "z": -0.5, "indicadores": [], "faltou": [],
                                        "tatico": {"z": 0.1, "score": 5.0}}},
            "arquetipo": {"nome": "Repique contra a tendência"}, "briefing": ["a", "b", "c", "d"],
            "gex": {"valor": -1e9, "rotulo": "NEGATIVO (AMPLIFICADOR)"},
            "validacao": {"dia_idx": DIA, "n": 119, "postos_dia": -0.18, "depois_18": False},
        })
        p.executar("saude", self.agora, lambda con, a_: {"resumo": "MT5 OK", "problemas": 0, "itens": []})
        p.executar("calendario", self.agora, lambda con, a_: {"proximo_evento": None, "alerta_evento": None})
        p.executar("abertura", self.agora, lambda con, a_: {"WIN$": {"abertura_prevista": 1.0}})
        p.executar("risco", self.agora, lambda con, a_: {"eps_acum_pct": 0.2})
        p.executar("global", self.agora, lambda con, a_: {"ativos": []})
        p.executar("quantis", self.agora, lambda con, a_: {"60min": {"q05": 1.0}})
        p.executar("opcoes", self.agora, lambda con, a_: {"diagnostico": {}, "a_favor": [], "contra": []})
        p.executar("swing", self.agora, lambda con, a_: {"WIN$": {"zonas": []}, "invalidacao": []})
        p.executar("correlacoes", self.agora, lambda con, a_: {"pares": [], "alertas": []})
        p.executar("players", self.agora, lambda con, a_: {"grupos": [{"grupo": "Estrangeiro", "saldo": -10}],
                                                          "fonte_saldo": "TRYD_SALDO", "unidade": "contratos"})
        p.executar("fluxo", self.agora, lambda con, a_: {"nowcast": None})
        for ativo in a.pv3.ATIVOS:
            p.executar(f"regioes:{ativo}", self.agora, lambda con, a_, ativo=ativo: {
                "preco": 100000.0, "zonas": ([{"id_zona": "R1", "lado": "ACIMA", "preco_min": 100800.0,
                                               "preco_centro": 101000.0, "preco_max": 101200.0,
                                               "forca": 80.0, "direcao": "VENDA",
                                               "alinhamento": "A_FAVOR_TENDENCIA"}] if ativo == "WIN$" else [])})
        p.executar("gatilho:WIN$:R1:VENDA", self.agora, lambda con, a_: {
            "id_zona": "R1", "status": "PROXIMA", "detalhe": "perto", "metricas": {"absorcao": False}})

    def test_pecas_do_ciclo_sao_chamaveis(self):
        agora = ts(DIA, 10, 0)
        lista = self.p._pecas(agora)
        nomes = [c for c, _p, _f in lista]
        self.assertIn("cenario", nomes)
        self.assertIn("validacao", nomes)
        self.assertIn("abertura", nomes)          # primeira leitura calcula, mesmo fora da janela
        for _chave, _periodo, fn in lista:
            self.assertTrue(callable(fn))

    def test_montar_tem_todas_as_chaves_e_anexa_o_gatilho(self):
        self._preencher()
        e = self.p.montar(self.agora)
        for chave in ("ts_ms", "saude", "proximo_evento", "alerta_evento", "cenario", "abertura",
                      "risco_brasil", "global", "regioes", "quantis", "opcoes", "swing", "fluxo",
                      "correlacoes", "players", "tempos", "estudo_regioes"):
            self.assertIn(chave, e)
        zona = e["regioes"]["WIN$"]["zonas"][0]
        self.assertEqual(zona["gatilho"]["status"], "PROXIMA")
        self.assertEqual(e["fluxo"]["placar"][0]["grupo"], "Estrangeiro")
        self.assertEqual(e["fluxo"]["fonte_saldo"], "TRYD_SALDO")
        json.dumps(e, ensure_ascii=False)          # tudo serializavel

    def test_gravar_estado_e_ler_de_volta(self):
        self._preencher()
        e = self.p.montar(self.agora)
        n = self.p.gravar_estado(e)
        self.assertGreater(n, 100)
        con = sqlite3.connect(self.db)
        try:
            r = con.execute("SELECT ts_ms, json, tempos_json FROM estado_cockpit_v3 WHERE id=1").fetchone()
        finally:
            con.close()
        self.assertEqual(r[0], self.agora)
        lido = json.loads(r[1])
        self.assertEqual(lido["cenario"]["score_estrutural"], -68.0)
        self.assertEqual(json.loads(r[2])["cenario"], self.p.tempo("cenario"))

        # id=1: gravar de novo substitui (nao acumula linha)
        self.p.gravar_estado(e)
        con = sqlite3.connect(self.db)
        try:
            self.assertEqual(con.execute("SELECT count(*) FROM estado_cockpit_v3").fetchone()[0], 1)
        finally:
            con.close()

    def test_gravar_derivados_uma_vez_por_minuto(self):
        self._preencher()
        e = self.p.montar(self.agora)
        n = self.p.gravar_derivados(e, self.agora)
        self.assertEqual(n, 1)
        con = sqlite3.connect(self.db)
        try:
            self.assertEqual(con.execute("SELECT count(*) FROM cenario_scores").fetchone()[0], 1)
            row = con.execute("SELECT ativo, status_gatilho FROM regioes_operacionais").fetchone()
            self.assertEqual(row, ("WIN$", "PROXIMA"))
        finally:
            con.close()
        self.assertEqual(self.p.gravar_derivados(e, self.agora + 10_000), 0)   # mesmo minuto
        self.assertEqual(self.p.gravar_derivados(e, self.agora + 60_000), 1)  # minuto seguinte: nova linha


class TestAberturaReal(unittest.TestCase):
    def test_primeira_cotacao_da_sessao(self):
        db = sqlite3.connect(":memory:")
        db.executescript("CREATE TABLE snapshots (ts_ms INTEGER, symbol TEXT, spread REAL, last REAL);"
                         "CREATE TABLE macro_cotacoes (ts_ms INTEGER, symbol TEXT, bid REAL, ask REAL, last REAL);")
        db.executemany("INSERT INTO snapshots VALUES (?,?,?,?)",
                       [(ts(DIA, 8, 59), "WIN$", 5.0, 99000.0),
                        (ts(DIA, 9, 1), "WIN$", 5.0, 100100.0),
                        (ts(DIA, 10, 0), "WIN$", 5.0, 100500.0)])
        db.executemany("INSERT INTO macro_cotacoes VALUES (?,?,?,?,?)",
                       [(ts(DIA, 9, 2), "WDO$", 5.4, 5.5, 5.45)])
        self.assertEqual(a._abertura_real(db, "WIN$", ts(DIA, 12, 0))[0], 100100.0)
        self.assertEqual(a._abertura_real(db, "WDO$", ts(DIA, 12, 0))[0], 5.45)
        self.assertEqual(a._abertura_real(db, "WIN$", ts(DIA, 8, 0)), (None, None))


if __name__ == "__main__":
    unittest.main()


class TestSaudeDaTabela(unittest.TestCase):
    def test_usa_saude_coletores_recente_e_recalcula_quando_velha(self):
        import monitor_saude as ms
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = caminho_vazio(tmp.name)
        agora = ts(DIA, 15, 0)
        con = sqlite3.connect(db)
        con.executescript(ms.SCHEMA)
        con.execute("INSERT INTO saude_coletores VALUES ('MT5 preco congelado', ?, 'OK', NULL, NULL, ?)",
                    (agora - 1000, agora - 30_000))
        con.commit()
        p = a.Pecas(db, seco=True)
        chamadas = []
        original = ms.avaliar
        ms.avaliar = lambda c, t: chamadas.append(t) or []
        try:
            r = p.saude(con, agora)
            self.assertEqual(r["origem"], "saude_coletores")
            self.assertEqual(chamadas, [])
            self.assertNotIn("ts_verificacao_ms", r["itens"][0])
            r = p.saude(con, agora + a.SAUDE_TABELA_MAX_MS + 60_000)
            self.assertEqual(r["origem"], "calculada no cockpit")
            self.assertEqual(len(chamadas), 1)
        finally:
            ms.avaliar = original
            con.close()
