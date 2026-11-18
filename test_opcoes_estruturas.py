"""Testes do opcoes_estruturas.py com banco SQLite temporario (dados sinteticos).

  python -m unittest test_opcoes_estruturas -v

Cobre: diagnostico da superficie (IV ATM e percentil, VRP, RR25 na convencao put-call, GEX, walls
escolhidas pelo lado, max pain, quantis), a classificacao do cenario, a montagem das 8 estruturas com
strikes reais (custo no mid e no pior lado, breakevens, ganho/perda maxima, gregas), o motivo quando
falta serie liquida, o dolar so com diagnostico e a causalidade das cotacoes (nada depois de agora).
"""
import sqlite3
import unittest
from datetime import date, timedelta

import opcoes_estruturas as oe

D = 86_400_000
DIA = 20774                       # terca 17/11/2026
AGORA = DIA * D + 15 * 3600000


def ts(dia, hh, mm=0, ss=0):
    return dia * D + (hh * 3600 + mm * 60 + ss) * 1000


def iso(dia):
    return (date(1970, 1, 1) + timedelta(days=dia)).isoformat()


SCHEMA = """
CREATE TABLE snapshots (ts_ms INTEGER, sessao TEXT, symbol TEXT, bid REAL, ask REAL, mid REAL, spread REAL,
                        last REAL, vol_compra_agr REAL, vol_venda_agr REAL);
CREATE TABLE macro_cotacoes (ts_ms INTEGER, sessao TEXT, symbol TEXT, bid REAL, ask REAL, last REAL);
CREATE TABLE barras_1m (symbol TEXT, ts_ms INTEGER, open REAL, high REAL, low REAL, close REAL, volume REAL);
CREATE TABLE agressao_1min (symbol TEXT, ts_ms INTEGER, preco REAL, vol_compra REAL, vol_venda REAL);
CREATE TABLE fechamento_diario (dia_idx INTEGER, ativo TEXT, maxima REAL, minima REAL, ultimo_liquido REAL,
                                poc_dia REAL, vah_dia REAL, val_dia REAL);
CREATE TABLE regime_live (ts_ms INTEGER, sessao TEXT, preco REAL, rv_dia_prev REAL, faixa_1h_pts REAL,
                          wdo_rv_rel REAL);
CREATE TABLE opcoes_resumo (ts_ms INTEGER, sessao TEXT, subjacente TEXT, vencimento TEXT, t_anos REAL,
                            atm_iv REAL, iv_call25 REAL, iv_put25 REAL, rr25 REAL, taxa REAL);
CREATE TABLE opcoes_cotacoes (ts_ms INTEGER, sessao TEXT, subjacente TEXT, symbol TEXT, vencimento TEXT,
                              tipo TEXT, strike REAL, bid REAL, ask REAL, last REAL, last_vol REAL,
                              spot REAL, iv REAL, delta REAL);
CREATE TABLE b3_posicoes_series (sessao TEXT, subjacente TEXT, vencimento TEXT, tipo TEXT, strike REAL,
                                 coberto REAL, travado REAL, descoberto REAL, vol_impl_pct REAL);
CREATE TABLE opcoes_manual_series (subjacente TEXT, capturado_em TEXT, vencimento TEXT, tipo TEXT,
                                   strike REAL, coberto REAL, travado REAL, descoberto REAL,
                                   vol_impl_pct REAL);
CREATE TABLE b3_dolar_series (sessao TEXT, vencimento TEXT, tipo TEXT, strike REAL, oi REAL, iv REAL,
                              mult REAL, fator REAL);
CREATE TABLE tryd_saldo (ts_ms INTEGER, ativo TEXT, linha INTEGER, brk TEXT, qtd REAL, premed REAL);
"""

VENC = "2026-11-27"
VENC_MENSAL = "2026-12-18"
STRIKES = [90.0 + i for i in range(21)]            # 90 a 110


def _cotacao(db, venc, tipo, strike, bid, ask, quando, iv=0.22, delta=None):
    mid = 0.5 * (bid + ask)
    db.execute("INSERT INTO opcoes_cotacoes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (quando, "S1", "BOVA11", f"BOVA{tipo}{int(strike)}", venc, tipo, strike, bid, ask, mid, 0, 100.0,
                iv, delta if delta is not None else (0.5 if tipo == "C" else -0.5)))


def banco(caminho=":memory:"):
    db = sqlite3.connect(str(caminho))
    db.executescript(SCHEMA)
    # spot do BOVA11 no instante
    for m in range(0, 61):
        preco = 100.0 + (m % 5) * 0.1
        db.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (AGORA - m * 60_000, "S1", "BOVA11", preco - 0.01, preco + 0.01, preco, 0.02, preco, 5.0, 5.0))
    # 21 pregoes de fechamento para a vol realizada de 20 (oscila +-2,5% ao dia: RV ~40% > IV de 30%)
    for k in range(21):
        d = DIA - 1 - k
        preco = 100.0 * (1.025 if k % 2 else 0.975)
        db.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (ts(d, 16, 0), "S1", "BOVA11", preco - 0.01, preco + 0.01, preco, 0.02, preco, 1.0, 1.0))
    db.execute("INSERT INTO regime_live VALUES (?,?,?,?,?,?)", (AGORA - 60_000, "S1", 100.0, 1.2, 300.0, 1.0))
    # resumos: hoje (IV ATM 0,30 no p100) e 12 dias de historico de 0,15 a 0,26
    db.execute("INSERT INTO opcoes_resumo VALUES (?,?,?,?,?,?,?,?,?,?)",
               (AGORA - 60_000, "S1", "BOVA11", VENC, 0.03, 0.30, 0.31, 0.29, 0.02, 0.13))
    db.execute("INSERT INTO opcoes_resumo VALUES (?,?,?,?,?,?,?,?,?,?)",
               (AGORA - 60_000, "S1", "BOVA11", VENC_MENSAL, 0.09, 0.28, 0.29, 0.27, 0.02, 0.13))
    for k in range(12):
        d = DIA - 1 - k
        iv = 0.15 + 0.01 * k
        db.execute("INSERT INTO opcoes_resumo VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (ts(d, 16, 0), "D", "BOVA11", VENC, 0.05, iv, iv + 0.01, iv - 0.01, 0.02, 0.13))
    # grade liquida de calls e puts no vencimento semanal e no mensal
    for venc, base in ((VENC, 0.0), (VENC_MENSAL, 0.0)):
        for k in STRIKES:
            intr_c = max(0.0, 100.0 - k)
            intr_p = max(0.0, k - 100.0)
            dist = abs(k - 100.0)
            _cotacao(db, venc, "C", k, max(0.05, intr_c + 3.0 - dist * 0.12), max(0.06, intr_c + 3.1 - dist * 0.12),
                     AGORA - 120_000)
            _cotacao(db, venc, "P", k, max(0.05, intr_p + 3.0 - dist * 0.12), max(0.06, intr_p + 3.1 - dist * 0.12),
                     AGORA - 120_000)
    # posicao em aberto D-1: put wall 95 (put), call wall 105 (call)
    for k in STRIKES:
        oi_c = 5000.0 if k == 105.0 else 100.0
        oi_p = 5000.0 if k == 95.0 else 100.0
        db.execute("INSERT INTO b3_posicoes_series VALUES (?,?,?,?,?,?,?,?,?)",
                   ("2026-11-16", "BOVA11", VENC, "C", k, oi_c, 0.0, oi_c, 0.22))
        db.execute("INSERT INTO b3_posicoes_series VALUES (?,?,?,?,?,?,?,?,?)",
                   ("2026-11-16", "BOVA11", VENC, "P", k, oi_p, 0.0, oi_p, 0.22))
    # dolar: WDO$ a 5788 e uma sessao de posicao D-1
    db.execute("INSERT INTO macro_cotacoes VALUES (?,?,?,?,?,?)", (AGORA - 60_000, "S1", "WDO$", 5787.5, 5788.5, 5788.0))
    for k in (5500.0, 5700.0, 5800.0, 5900.0, 6000.0):
        db.execute("INSERT INTO b3_dolar_series VALUES (?,?,?,?,?,?,?,?)",
                   ("2026-11-16", "2026-12-01", "C", k, 3000.0, 0.15, 50.0, 1.0))
        db.execute("INSERT INTO b3_dolar_series VALUES (?,?,?,?,?,?,?,?)",
                   ("2026-11-16", "2026-12-01", "P", k, 3000.0, 0.16, 50.0, 1.0))
    db.commit()
    return db


class TestSuperficie(unittest.TestCase):
    def test_diagnostico_completo(self):
        db = banco()
        d = oe.superficie(db, AGORA)
        self.assertAlmostEqual(d["spot"], 100.0, places=1)
        self.assertEqual(d["vencimento"], VENC)
        self.assertEqual(d["du"], 7)
        self.assertAlmostEqual(d["iv_atm"], 0.30)
        self.assertAlmostEqual(d["iv_atm_pct"], 100.0)          # 0,30 acima de todo o historico
        self.assertAlmostEqual(d["rr25"], -0.02, places=6)      # put25 - call25 (resumo grava call - put)
        self.assertIsNotNone(d["vrp"])
        self.assertLess(d["vrp"], 0)                            # IV 30% contra RV de 20 pregoes pequena
        self.assertIn(d["gex_sinal"], ("negativo", "positivo"))
        self.assertAlmostEqual(d["put_wall"], 95.0)
        self.assertAlmostEqual(d["call_wall"], 105.0)
        self.assertIn(d["max_pain"], STRIKES)
        self.assertLessEqual(abs(d["max_pain"] - d["spot"]), 25.0)
        self.assertEqual(d["posicoes_ref"], "2026-11-16")

    def test_causalidade_das_cotacoes(self):
        db = banco()
        _cotacao(db, VENC, "P", 100.0, 0.01, 0.02, AGORA + 60_000)
        cot = oe._cotacoes(db, AGORA)
        linha = cot[(cot.vencimento == VENC) & (cot.tipo == "P") & (cot.strike == 100.0)]
        self.assertEqual(len(linha), 1)
        self.assertGreater(linha.iloc[0].bid, 1.0)              # a cotacao futura nao entra

    def test_dolar_so_diagnostico(self):
        db = banco()
        r = oe.estruturas(db, AGORA, -60, "DOL")
        self.assertEqual(r["a_favor"], [])
        self.assertEqual(r["contra"], [])
        self.assertIn("livro", r["motivo"])
        d = r["diagnostico"]
        self.assertEqual(d["ativo"], "DOL")
        self.assertAlmostEqual(d["spot"], 5788.0)
        self.assertIsNotNone(d["iv_atm"])
        self.assertIsNotNone(d["put_wall"])
        self.assertIsNotNone(d["call_wall"])


class TestCenario(unittest.TestCase):
    def test_direcao_e_iv(self):
        self.assertEqual(oe.direcao_vies(-60), "baixa")
        self.assertEqual(oe.direcao_vies(-24), "lateral")
        self.assertEqual(oe.direcao_vies(60), "alta")
        self.assertEqual(oe.nivel_iv(dict(vrp=0.03, iv_atm_pct=85)), "cara")
        self.assertEqual(oe.nivel_iv(dict(vrp=-0.01, iv_atm_pct=20)), "barata")
        self.assertEqual(oe.nivel_iv(dict(vrp=0.01, iv_atm_pct=50)), "normal")
        self.assertEqual(oe.nivel_iv(dict(vrp=None, iv_atm_pct=50)), "indefinida")

    def test_cenario_queda_iv_cara_puts_caras(self):
        diag = dict(vrp=0.05, iv_atm_pct=85, rr25=0.03, rr25_pct=95)
        self.assertEqual(oe.cenario(-70, diag), "baixa|iv_cara|puts_caras")
        plano = oe._plano("baixa|iv_cara|puts_caras", dict(gex_sinal="negativo"))
        self.assertEqual([x[0] for x in plano["a_favor"]], ["trava_baixa_puts", "trava_baixa_calls"])
        self.assertEqual([x[0] for x in plano["contra"]], ["trava_alta_puts"])
        self.assertIn("linha 1", plano["nota"])

    def test_cenario_lateral_sem_gex_positivo_avisa(self):
        plano = oe._plano("lateral|iv_cara|neutro", dict(gex_sinal="negativo"))
        self.assertIn("GEX nao e positivo", plano["nota"])
        self.assertEqual([x[0] for x in plano["contra"]], ["iron_condor"])


class TestEstruturas(unittest.TestCase):
    def test_todas_as_estruturas_montam_com_strikes_reais(self):
        db = banco()
        cot = oe._cotacoes(db, AGORA)
        diag = oe.superficie(db, AGORA, cot=cot)
        diag["_agora_ms"] = AGORA
        taxa = 0.13
        for chave, (nome, fn, _tese) in oe.ESTRUTURAS.items():
            venc = VENC_MENSAL if chave == "put_seca" else VENC
            kwargs = {"atm_iv": 0.30} if chave == "put_seca" else {}
            legs, motivo = fn(cot, diag["spot"], diag, venc, iso(DIA), **kwargs)
            self.assertIsNone(motivo, f"{chave}: {motivo}")
            e = oe._montar(chave, "a_favor", venc, legs, motivo, cot, diag["spot"], taxa, iso(DIA), AGORA, 0.30, "")
            self.assertIsNone(e.get("motivo"), f"{chave}: {e.get('motivo')}")
            self.assertTrue(e["pernas"])
            self.assertGreaterEqual(e["custo_pior"], e["custo_mid"] - 1e-9)
            self.assertLess(e["perda_max"], 0)
            self.assertGreaterEqual(e["ganho_max"], 0)
            self.assertTrue(e["breakevens"])
            self.assertTrue(all(k in e["gregas"] for k in ("delta", "gama", "vega")))
            # todos os strikes sao da grade do banco
            for p in e["pernas"]:
                self.assertIn(p["strike"], STRIKES)
                self.assertGreater(p["bid"], 0)
                self.assertGreater(p["ask"], p["bid"])

    def test_trava_de_baixa_tem_limites_conhecidos(self):
        db = banco()
        cot = oe._cotacoes(db, AGORA)
        diag = oe.superficie(db, AGORA, cot=cot)
        diag["_agora_ms"] = AGORA
        legs, motivo = oe._trava_baixa_puts(cot, 100.0, diag, VENC, iso(DIA))
        self.assertIsNone(motivo)
        e = oe._montar("trava_baixa_puts", "a_favor", VENC, legs, None, cot, 100.0, 0.13, iso(DIA), AGORA, 0.30, "")
        compra = [p for p in e["pernas"] if p["tipo"] == "COMPRA"][0]
        venda = [p for p in e["pernas"] if p["tipo"] == "VENDA"][0]
        self.assertLess(venda["strike"], compra["strike"])
        largura = compra["strike"] - venda["strike"]
        self.assertAlmostEqual(e["perda_max"], -e["custo_mid"], places=6)
        self.assertAlmostEqual(e["ganho_max"], largura - e["custo_mid"], places=6)
        self.assertEqual(len(e["breakevens"]), 1)

    def test_sem_serie_liquida_devolve_motivo(self):
        db = banco()
        cot = oe._cotacoes(db, AGORA)
        diag = oe.superficie(db, AGORA, cot=cot)
        diag["spot"] = 100.0
        venc = "2026-12-04"          # vencimento sem nenhuma cotacao no banco
        legs, motivo = oe._trava_baixa_puts(cot, 100.0, diag, venc, iso(DIA))
        self.assertIsNone(legs)
        self.assertIn("insuficientes", motivo)

    def test_estruturas_por_vies(self):
        db = banco()
        baixa = oe.estruturas(db, AGORA, -60)
        self.assertEqual(baixa["direcao"], "baixa")
        self.assertTrue(baixa["a_favor"])
        self.assertTrue(all(e.get("motivo") is None for e in baixa["a_favor"]))
        lateral = oe.estruturas(db, AGORA, 0)
        self.assertEqual(lateral["direcao"], "lateral")
        self.assertTrue(lateral["contra"])
        self.assertEqual(lateral["contra"][0]["chave"], "iron_condor")

    def test_max_pain_de_um_vencimento(self):
        db = banco()
        pos = __import__("grafico_quant").posicoes(db, "2026-11-17")
        mp = oe._max_pain(pos["series"], 100.0, VENC, strikes=STRIKES)
        self.assertIsNotNone(mp)
        # com puts pesadas em 95 e calls em 105, o max pain fica entre as duas paredes
        self.assertLessEqual(mp[0], 105.0)
        self.assertGreaterEqual(mp[0], 95.0)


if __name__ == "__main__":
    unittest.main()
