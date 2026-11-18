"""Processo de fundo do cockpit v3: pre-calcula o estado consolidado e grava em estado_cockpit_v3.

  python atualizador_cockpit.py COCKPIT <segundos> [--db dados/book.db] [--uma-vez] [--seco]
                                           [--salvar-json CAMINHO]

Padrao supervisor (como os coletores): o primeiro argumento e o nome do grupo e o segundo, quantos
segundos rodar (0 = sem limite). O ciclo do laco e 5 s no pregao (09:00-18:25 de um pregao do
calendario_b3), 60 s das 07:00 as 09:00 e 300 s fora disso.

Cada peca tem periodo e cache proprios: cenario 30 s, reguas WIN$/WDO$ 30 s, BOVA11 120 s, estado
do gatilho das zonas ativas 5 s, estruturas de opcoes 120 s, swing 600 s, abertura justa 60 s no
pre-mercado (e uma vez na partida), risco Brasil 60 s, correlacoes v3 60 s, saude 30 s, calendario
300 s, players 60 s, quantis 30 s e global 60 s. Pecas lentas rodam em threads proprias: o estado e
reescrito a cada ciclo com o ultimo valor bom e a idade, sem esperar a peca mais cara.

Erro numa peca nao derruba o ciclo: ela sai com {"erro": "..."} e, quando existe, o ultimo valor bom
com "idade_s". A partida chama desempenho.sem_modo_eficiencia() (o supervisor sobe sem janela).

Gravacao: UMA linha em `estado_cockpit_v3` (id=1) com INSERT OR REPLACE em transacao curta; a cada
minuto uma linha em `cenario_scores` (o que ja foi calculado) e as zonas em `regioes_operacionais`.

Validacao do score (obrigatoria na tela): uma vez por dia (na partida e depois das 18:00) roda
motor_cenario.avaliar(con) e guarda o resumo em estado.cenario.validacao. O numero e o medido no
banco: em 18/11/2026, 119 pregoes com postos -0,18 (dia), -0,11 (seguinte), -0,18 (5p) e acerto de
32-40% com |score| >= 35 - sem sinal direcional positivo.

  --uma-vez     calcula tudo uma vez (sincrono) e grava; com --seco so imprime os tempos
  --seco        nao grava nada: calcula, imprime o tempo de cada peca e o tamanho do JSON
"""
import argparse
import contextlib
import io
import json
import math
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

import abertura_justa as aj
import banco
import calendario_b3 as cal
import calendario_macro as cm
import coletor_global as cg
import correlacoes_v3 as c3
import desempenho
import gatilhos_execucao as ge
import modelo_regime_v3 as m3
import monitor_saude as ms
import motor_cenario as mc
import motor_regioes as mr
import nowcast_fluxo_v2 as nf
import opcoes_estruturas as oe
import painel_v3 as pv3
import players
import regimes as rg
import relogio
import risco_brasil as rb
import swing as sw

D = 86_400_000
ARQ_MODELO = Path(__file__).resolve().parent / "dados" / "modelo_regime_v3.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS estado_cockpit_v3 (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    ts_ms INTEGER NOT NULL,
    json TEXT NOT NULL,
    tempos_json TEXT NOT NULL);
"""

# periodo de cada peca (ms); None = so na partida
SAUDE_TABELA_MAX_MS = 180_000   # idade maxima de saude_coletores para usar sem recalcular
PERIODOS = {
    "cenario": 30_000,
    "regioes:WIN$": 30_000,
    "regioes:WDO$": 30_000,
    "regioes:BOVA11": 120_000,
    "opcoes": 120_000,
    "swing": 600_000,
    "abertura": 60_000,
    "risco": 60_000,
    "correlacoes": 60_000,
    "saude": 30_000,
    "calendario": 300_000,
    "players": 180_000,   # saldo_do_dia pela fita leva ~47 s no banco real
    "fluxo": 60_000,
    "global": 60_000,
    "quantis": 30_000,
    "gatilho": 5_000,
    "validacao": 60_000,
}

# ativos principais da madrugada/europa/EUA para a variacao desde as 18:00 de D-1
GLOBAIS = (("N225", "Nikkei 225"), ("HSI", "Hang Seng"), ("CSI300", "CSI 300"),
           ("STOXX50", "Euro Stoxx 50"), ("DAX", "DAX"), ("FTSE", "FTSE 100"),
           ("ES", "S&P 500 futuro"), ("NQ", "Nasdaq futuro"), ("DXY", "Dólar (DXY)"),
           ("VIX", "VIX"), ("VIX9D", "VIX 9 dias"), ("BRENT", "Brent"),
           ("MINERIO", "Minério de ferro"), ("OURO", "Ouro"), ("US10Y", "US 10 anos"),
           ("EWZ", "EWZ"))


# ------------------------------------------------------------------ utilidades

def _iso(dia_idx):
    return (date(1970, 1, 1) + timedelta(days=int(dia_idx))).isoformat()


def _minuto(agora_ms):
    return (int(agora_ms) % D) // 60_000


def _limpo(v):
    """Converte numpy/NaN para JSON (o mesmo criterio de dashboard.json_limpo)."""
    if isinstance(v, dict):
        return {str(k): _limpo(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_limpo(x) for x in v]
    if isinstance(v, (bool, np.bool_)):
        return bool(v)
    if isinstance(v, (int, np.integer)):
        return int(v)
    if isinstance(v, (float, np.floating)):
        return None if not math.isfinite(float(v)) else round(float(v), 6)
    return v


def intervalo_s(agora_ms, pregao=True):
    """Ciclo do laco: 5 s no pregao (09:00-18:25), 60 s das 07:00 as 09:00 e 300 s fora."""
    m = _minuto(agora_ms)
    if pregao and 9 * 60 <= m <= 18 * 60 + 25:
        return 5.0
    if pregao and 7 * 60 <= m < 9 * 60:
        return 60.0
    return 300.0


def faixa_score(score):
    """Rotulo do score por faixa: forte |s|>=65, moderado >=35, leve >=15, senao neutro."""
    if score is None:
        return "SEM DADO"
    a = abs(float(score))
    if a < 15:
        return "NEUTRO"
    forca = "FORTE" if a >= 65 else "MODERADO" if a >= 35 else "LEVE"
    return ("ALTA " if score > 0 else "BAIXA ") + forca


def rotulo_intraday(estrutural, intraday):
    """Rotulo do clima intraday; com sinal oposto ao estrutural vira REPIQUE (ex.: +18 = REPIQUE LEVE)."""
    if intraday is None:
        return "SEM DADO"
    texto = faixa_score(intraday)
    if (estrutural is not None and abs(float(intraday)) >= 15
            and float(estrutural) * float(intraday) < 0):
        forca = "FORTE" if abs(float(intraday)) >= 65 else "MODERADO" if abs(float(intraday)) >= 35 \
            else "LEVE"
        return "REPIQUE " + forca
    return texto


def rotulo_gex(gex):
    """NEGATIVO (AMPLIFICADOR) / POSITIVO (AMORTECEDOR) / SEM GEX."""
    if gex is None:
        return "SEM GEX"
    if gex < 0:
        return "NEGATIVO (AMPLIFICADOR)"
    if gex > 0:
        return "POSITIVO (AMORTECEDOR)"
    return "NULO"


def _er(con, ativo, agora_ms):
    """Eficiencia direcional (regimes.er) da serie do dia ate agora; None sem 15 pontos."""
    try:
        df = mr._serie_precos(con, ativo, mr._dia_idx(agora_ms), agora_ms)
        if df is None or len(df) < 15:
            return None
        return float(rg.er(df["last"].astype(float)))
    except Exception:
        return None


def _serie_compacta(con, ativo, agora_ms, passo_ms=300_000, maximo=160):
    """Serie do dia em baldes de 5 min para o grafico do cockpit -> {"t": [s], "p": [preco]}."""
    try:
        df = mr._serie_precos(con, ativo, mr._dia_idx(agora_ms), agora_ms)
    except Exception:
        return {"t": [], "p": []}
    if df is None or len(df) == 0:
        return {"t": [], "p": []}
    df = df.assign(b=(df["ts_ms"] // passo_ms) * passo_ms)
    g = df.groupby("b", sort=True).agg(ts_ms=("ts_ms", "last"), last=("last", "last")).reset_index()
    if len(g) > maximo:
        passo = int(math.ceil(len(g) / float(maximo)))
        g = g.iloc[::passo]
    return {"t": [int(x) // 1000 for x in g["ts_ms"]], "p": [float(x) for x in g["last"]]}


def _abertura_real(con, ativo, agora_ms):
    """(preco, ts) da primeira cotacao do dia na sessao regular; (None, None) sem dado."""
    dia = agora_ms // D
    ini = dia * D + 9 * 60 * 60_000
    fim = min(int(agora_ms), dia * D + 18 * 60 * 60_000)
    if fim <= ini:
        return None, None
    if ativo == "WDO$":
        sql = ("SELECT ts_ms, last FROM macro_cotacoes WHERE symbol=? AND ts_ms BETWEEN ? AND ? "
               "AND bid > 0 AND ask > bid AND last > 0 ORDER BY ts_ms LIMIT 1")
    else:
        sql = ("SELECT ts_ms, last FROM snapshots WHERE symbol=? AND ts_ms BETWEEN ? AND ? "
               "AND spread > 0 AND last > 0 ORDER BY ts_ms LIMIT 1")
    try:
        r = con.execute(sql, (ativo, ini, fim)).fetchone()
    except sqlite3.OperationalError:
        return None, None
    return (float(r[1]), int(r[0])) if r else (None, None)


def _ultimo_global(con, ativo, agora_ms):
    """(ts_ms, preco) da ultima cotacao global conhecida ate agora (global_cotacoes e, sem ela, global_barras)."""
    try:
        r = con.execute("SELECT ts_ms, ultimo FROM global_cotacoes WHERE ativo=? AND ts_ms<=? "
                        "ORDER BY ts_ms DESC LIMIT 1", (ativo, int(agora_ms))).fetchone()
        if r and r[1] is not None:
            return int(r[0]), float(r[1])
    except sqlite3.OperationalError:
        pass
    try:
        r = con.execute("SELECT ts_ms, close FROM global_barras WHERE ativo=? AND close>0 AND ts_ms<=? "
                        "ORDER BY ts_ms DESC LIMIT 1", (ativo, int(agora_ms))).fetchone()
        if r and r[1] is not None:
            return int(r[0]), float(r[1])
    except sqlite3.OperationalError:
        pass
    return None, None


# ------------------------------------------------------------------ pecas

class Pecas:
    """Cache das pecas com periodo proprio, erro isolado e atualizacao opcional em threads."""

    def __init__(self, db_path, seco=False):
        self.db_path = str(db_path)
        self.seco = seco
        self.dados = {}               # chave -> {"ts", "valor" (ultimo bom), "erro", "bom_ts", "tempo_ms"}
        self.em_curso = set()
        self.lock = threading.Lock()
        self.vies = 0.0
        self.validacao = None
        self._modelo = None
        self._modelo_lido = False
        self.ultimo_minuto_derivados = None
        self._estudo = None
        self._carregar_validacao_anterior()

    # ----- infraestrutura
    def _con(self):
        return sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=60)

    def _carregar_validacao_anterior(self):
        try:
            con = self._con()
            try:
                _ts, estado, _tempos = pv3.ler_estado(con)
            finally:
                con.close()
        except Exception:
            return
        val = ((estado or {}).get("cenario") or {}).get("validacao")
        if isinstance(val, dict) and val.get("dia_idx") is not None:
            self.validacao = val

    def _registrar(self, chave, agora, t0, valor, erro):
        with self.lock:
            antigo = self.dados.get(chave) or {}
            tempo_ms = (time.time() - t0) * 1000.0
            if erro is None:
                self.dados[chave] = {"ts": agora, "valor": valor, "erro": None, "bom_ts": agora,
                                     "tempo_ms": tempo_ms}
            else:
                self.dados[chave] = {"ts": agora, "valor": antigo.get("valor"), "erro": erro,
                                     "bom_ts": antigo.get("bom_ts"), "tempo_ms": tempo_ms}
            if chave == "cenario" and valor:
                self.vies = float(valor.get("_vies") or 0.0)

    def executar(self, chave, agora, fn):
        """Roda `fn(con, agora)` e guarda o resultado; erro fica registrado sem derrubar o ciclo."""
        t0 = time.time()
        valor, erro = None, None
        try:
            con = self._con()
            try:
                valor = fn(con, agora)
            finally:
                con.close()
        except Exception as e:
            erro = f"{type(e).__name__}: {e}"
        self._registrar(chave, agora, t0, valor, erro)
        return valor

    def talvez(self, chave, periodo, agora, fn):
        """Agenda a peca em thread se o periodo venceu e ela nao esta em curso."""
        with self.lock:
            if chave in self.em_curso:
                return False
            atual = self.dados.get(chave)
            if atual and agora - int(atual["ts"]) < int(periodo):
                return False
            self.em_curso.add(chave)

        def tarefa():
            try:
                self.executar(chave, agora, fn)
            finally:
                with self.lock:
                    self.em_curso.discard(chave)
        threading.Thread(target=tarefa, name=f"cockpit-{chave}", daemon=True).start()
        return True

    def peca(self, chave, agora=None):
        """Valor atual da peca; erro + ultimo valor bom com idade quando a ultima execucao falhou."""
        d = self.dados.get(chave)
        if not d:
            return {"erro": "aguardando primeira leitura"}
        if d.get("erro"):
            base = dict(d["valor"]) if isinstance(d["valor"], dict) else {}
            base["erro"] = d["erro"]
            if d.get("bom_ts") and agora is not None:
                base["idade_s"] = round(max(0.0, (int(agora) - int(d["bom_ts"])) / 1000.0), 1)
            return base
        v = d.get("valor")
        return dict(v) if isinstance(v, dict) else {"valor": v}

    def tempo(self, chave):
        d = self.dados.get(chave) or {}
        return round(float(d.get("tempo_ms") or 0.0), 1)

    def resumo(self):
        """(pecas com valor, pecas com erro) de um retrato consistente do cache (threads gravam)."""
        with self.lock:
            dados = dict(self.dados)
        return (sum(1 for x in dados.values() if x.get("valor") is not None),
                sum(1 for x in dados.values() if x.get("erro")))

    # ----- pecas
    def _modelo_regime(self):
        if self._modelo_lido:
            return self._modelo
        self._modelo_lido = True
        try:
            self._modelo = json.loads(ARQ_MODELO.read_text(encoding="utf-8"))
        except Exception:
            self._modelo = None
        return self._modelo

    def _validacao(self, con, agora):
        """Resumo do motor_cenario.avaliar, uma vez por dia e de novo depois das 18:00."""
        dia, minuto = agora // D, _minuto(agora)
        ant = self.validacao
        if (isinstance(ant, dict) and ant.get("dia_idx") == dia
                and (minuto < 18 * 60 or ant.get("depois_18"))):
            return ant
        with contextlib.redirect_stdout(io.StringIO()):
            res = mc.avaliar(con, 120)
        s = (res or {}).get("Score estrutural") or {}
        def _h(hz, chave):
            return (s.get(hz) or {}).get(chave)
        val = {"dia_idx": dia, "dia": _iso(dia), "depois_18": bool(minuto >= 18 * 60),
               "n": _h("dia", "n") or 0,
               "postos_dia": _h("dia", "postos"), "postos_seguinte": _h("seguinte", "postos"),
               "postos_5p": _h("5p", "postos"),
               "acerto_35_dia": _h("dia", "acerto"), "acerto_35_seguinte": _h("seguinte", "acerto"),
               "acerto_35_5p": _h("5p", "acerto"),
               "n_sinal_dia": _h("dia", "n_sinal"),
               "invertido": mc.SINAL_SCORE < 0, "invertido_desde": _iso(mc.INVERTIDO_DESDE)}
        fora = (res or {}).get("Score fora da amostra") or {}
        val["fora"] = {hz: {"n": (fora.get(hz) or {}).get("n") or 0, "postos": (fora.get(hz) or {}).get("postos"),
                            "acerto": (fora.get(hz) or {}).get("acerto"),
                            "n_sinal": (fora.get(hz) or {}).get("n_sinal") or 0}
                       for hz in ("dia", "seguinte", "5p")}
        self.validacao = val
        return val

    def validacao_piece(self, con, agora):
        """Peca da validacao do score (motor_cenario.avaliar); reaproveita o resumo do dia."""
        return self._validacao(con, agora)

    def cenario(self, con, agora):
        r = mc.cenario(con, agora)
        est, intra = r.get("score_estrutural"), r.get("score_intraday")
        pilares = {}
        for chave, p in (r.get("pilares") or {}).items():
            tat = p.get("tatico") or {}
            pilares[chave] = {
                "nome": p.get("nome"), "peso": p.get("peso"), "z": p.get("z"), "score": p.get("score"),
                "tatico": {"z": tat.get("z"), "score": tat.get("score")},
                "indicadores": [{"nome": i.get("nome"), "valor": i.get("valor"), "unidade": i.get("unidade"),
                                 "z": i.get("z"), "n": i.get("n")} for i in (p.get("indicadores") or [])],
                "faltou": p.get("faltou") or [],
            }
        gex = r.get("gex") or {}
        validacao = self.validacao
        if not (isinstance(validacao, dict) and validacao.get("dia_idx") == agora // D):
            validacao = {"dia_idx": agora // D, "erro": "validação ainda não medida hoje"}
        return {
            "score_estrutural": est, "score_intraday": intra,
            "rotulos": {"estrutural": faixa_score(est), "intraday": rotulo_intraday(est, intra)},
            "pilares": pilares,
            "arquetipo": r.get("arquetipo"),
            "briefing": r.get("briefing") or [],
            "fator_gex": r.get("fator_gex"),
            "gex": {"valor": gex.get("valor"), "rotulo": rotulo_gex(gex.get("valor")),
                    "posicao": gex.get("posicao"), "spot": gex.get("spot"), "fonte": gex.get("fonte")},
            "risco_brasil_bps": r.get("risco_brasil_bps"),
            "faltantes": r.get("faltantes") or [],
            "validacao": validacao,
            "_vies": est,
        }

    def abertura(self, con, agora):
        prev = aj.prever_hoje(con, agora)
        out = {}
        for ativo in ("WIN$", "WDO$"):
            p = (prev or {}).get(ativo)
            if not p:
                out[ativo] = {"erro": "sem historico de 20 pregoes com features da madrugada"}
                continue
            real, ts_real = _abertura_real(con, ativo, agora)
            sigma = p.get("sigma_erro")
            item = {
                "gap_previsto_pct": None if p.get("gap_previsto") is None else p["gap_previsto"] * 100.0,
                "abertura_prevista": p.get("abertura_prevista"),
                "fechamento_anterior": p.get("fechamento_anterior"),
                "abertura_real": real, "ts_abertura_real": ts_real,
                "sigma_erro": sigma,
                "desvio_sigma": (None if real is None or p.get("abertura_prevista") is None or not sigma
                                 else (real - p["abertura_prevista"]) / sigma),
                "distorcao": aj.distorcao_abertura(real, p.get("abertura_prevista"), sigma),
                "r2": p.get("r2"), "mae_pts": p.get("mae_pts"), "acerto_sinal": p.get("acerto_sinal"),
                "n_metricas": p.get("n_metricas"),
                "faltantes": p.get("faltantes") or [],
                "modelo": "Ridge (ES + cesta EM), janela de 120 pregoes",
            }
            out[ativo] = item
        return out

    def risco(self, con, agora):
        dia = agora // D
        linhas = rb.calcular_dia(con, dia) or []
        if not linhas:
            return {"dia": _iso(dia), "erro": "sem janela de 5 min do WDO$ hoje (betas DXY/EM ou preco)"}
        u = linhas[-1]
        return {"dia": _iso(dia), "ts_ms": u.get("ts_ms"), "eps_acum_pct": u.get("eps_acum_pct"),
                "incl_var_bps": u.get("incl_var_bps"), "r2": u.get("r2"),
                "beta_dxy": u.get("beta_dxy"), "beta_em": u.get("beta_em"),
                "rotulo": rb.rotulo(u.get("eps_acum_pct"), u.get("incl_var_bps")),
                "n_janelas": len(linhas),
                "modelo": "eps_BR = r_WDO - (beta_DXY r_DXY + beta_EM r_EM), 5 min, 20 pregoes"}

    def globais(self, con, agora):
        dia = agora // D
        ativos = []
        for ativo, nome in GLOBAIS:
            ts, ultimo = _ultimo_global(con, ativo, agora)
            ref = cg.referencia_pregao_anterior(con, ativo, dia)
            ativos.append({"ativo": ativo, "nome": nome, "ultimo": ultimo,
                           "ts_ms": ts, "referencia": ref,
                           "variacao_pct": cg.variacao(ultimo, ref) if ultimo is not None else None})
        return {"referencia": "último preço até 18:00 BRT do pregão anterior", "ativos": ativos}

    def regiao(self, con, ativo, agora):
        d = mr.regioes_detalhado(con, ativo, agora, self.vies)
        return {"preco": d.get("preco"), "vwap": d.get("vwap_dia"), "gamma_flip": d.get("gamma_flip"),
                "er": _er(con, ativo, agora), "origem_quantis": d.get("origem_quantis"),
                "atr15": d.get("atr15"), "faltando": d.get("faltando") or [],
                "zonas": d.get("zonas") or [], "serie": _serie_compacta(con, ativo, agora)}

    def gatilho(self, con, ativo, zona, agora):
        r = ge.estado_zona(con, ativo, zona, agora, zona.get("direcao"))
        return {"id_zona": zona.get("id_zona"), "status": r.get("status"), "detalhe": r.get("detalhe"),
                "metricas": r.get("metricas") or {}}

    def opcoes(self, con, agora):
        return oe.estruturas(con, agora, self.vies, "BOVA11")

    def quantis(self, con, agora):
        modelo = self._modelo_regime()
        if not modelo:
            return {"erro": "sem dados/modelo_regime_v3.json (rode modelo_regime_v3.py treinar)"}
        r = m3.prever(con, agora, modelo)
        if not r:
            return {"erro": "fora da janela 10:05-17:15 ou sem dados do WIN no dia"}
        return r

    def swing(self, con, agora):
        win = sw.zonas_swing(con, "WIN$", agora, self.vies)
        return {"zonas": win, "WIN$": {"zonas": win},
                "WDO$": {"zonas": sw.zonas_swing(con, "WDO$", agora, self.vies)},
                "invalidacao": sw.invalidacao(con, agora, self.vies)}

    def fluxo(self, con, agora):
        dia_iso = _iso(agora // D)
        now = nf.estimar(con, dia_iso)
        of = None
        try:
            r = con.execute("SELECT data_ref, saldo_mil, data_pub FROM fluxo_diario "
                            "WHERE tipo='Investidor Estrangeiro' AND saldo_mil IS NOT NULL "
                            "ORDER BY data_ref DESC LIMIT 5").fetchall()
            if r:
                of = {"ultimo": {"data_ref": str(r[0][0])[:10], "saldo_mi": r[0][1] / 1000.0,
                                 "data_pub": str(r[0][2])[:10] if r[0][2] else None},
                      "cinco_dias_mi": sum(x[1] for x in r) / 1000.0}
        except sqlite3.OperationalError:
            of = None
        return {"nowcast": now, "oficial": of, "dia": dia_iso}

    def correlacoes(self, con, agora):
        api = c3.correlacoes_v3_api(con, agora, "pregao")
        pares = []
        for p in api.get("principais") or []:
            pares.append({"y": p.get("y"), "x": p.get("x"), "nome": p.get("nome"),
                          "corr": p.get("corr_huber") if p.get("corr_huber") is not None else p.get("corr"),
                          "spearman": p.get("spearman"), "z_regime": p.get("z_regime"),
                          "q_valor": p.get("q_valor"), "alerta": bool(p.get("alerta")),
                          "n": p.get("n")})
        alertas = []
        for l in api.get("tabela") or []:
            if l.get("alerta"):
                alertas.append({"y": l.get("y"), "x": l.get("x"), "nome": l.get("nome"),
                                "corr": l.get("corr_huber") if l.get("corr_huber") is not None else l.get("corr"),
                                "z": l.get("z_regime") if l.get("z_regime") is not None else l.get("z"),
                                "q": l.get("q_valor")})
        return {"janela": api.get("janela"), "t0": api.get("t0"), "t1": api.get("t1"),
                "n_minutos": api.get("n_minutos"), "fdr_alpha": api.get("fdr_alpha"),
                "n_hipoteses": api.get("n_hipoteses"), "regime": api.get("regime"),
                "pares": pares, "alertas": alertas}

    def players(self, con, agora):
        dia = agora // D
        try:
            r = con.execute("SELECT ativo FROM tryd_saldo WHERE ativo LIKE 'WIN%' ORDER BY ts_ms DESC LIMIT 1"
                            ).fetchone()
        except sqlite3.OperationalError:
            r = None
        contrato = r[0] if r else None
        if not contrato:
            return {"erro": "sem contrato do WIN em tryd_saldo", "grupos": []}
        saldo = players.saldo_do_dia(con, dia, contrato, agora)
        if not saldo:
            return {"ativo": contrato, "dia": _iso(dia), "erro": "sem foto de saldo nem fita do WIN hoje",
                    "grupos": []}
        grupos = []
        for nome, serie in (saldo.get("series") or {}).items():
            valores = [v for v in (serie.get("saldo") or []) if v is not None]
            if valores:
                grupos.append({"grupo": nome, "saldo": valores[-1]})
        grupos.sort(key=lambda g: -abs(float(g["saldo"] or 0)))
        return {"ativo": contrato, "dia": _iso(dia), "fonte_saldo": saldo.get("fonte_saldo"),
                "captura_media": saldo.get("captura_media"), "unidade": players.unidade(contrato),
                "grupos": grupos, "t_ultimo": (saldo.get("t") or [None])[-1]}

    def saude(self, con, agora):
        # o supervisor SAUDE ja grava saude_coletores a cada ciclo; recalcular aqui (painel_v2.coleta) leva 45-110 s
        # no banco real, entao so recalcula quando a tabela falta ou esta velha
        gravados = ms.saude_api(con).get("coletores") or []
        verif = max((int(i.get("ts_verificacao_ms") or 0) for i in gravados), default=0)
        if gravados and agora - verif <= SAUDE_TABELA_MAX_MS:
            itens = [{k: v for k, v in i.items() if k != "ts_verificacao_ms"} for i in gravados]
            origem = "saude_coletores"
        else:
            itens = ms.avaliar(con, agora)
            origem = "calculada no cockpit"
        ts_global, _p = _ultimo_global(con, "ES", agora)
        dentro = _minuto(agora) >= 9 * 60 and _minuto(agora) <= 18 * 60 + 25
        itens.append({"coletor": "Global", "ts_ultimo_dado_ms": ts_global,
                      "status": ("OK" if ts_global and agora - ts_global <= 2 * 3600_000 else
                                 ("PARADO" if dentro else "OK")),
                      "metrica_qualidade": None,
                      "detalhe": None if ts_global else "sem cotacao global no banco"})
        por_nome = {i.get("coletor"): i for i in itens}
        mt5 = (por_nome.get("MT5 preco congelado") or {}).get("status") or "SEM DADO"
        fita = por_nome.get("Tryd fita captura") or {}
        fita_txt = (f"{fita.get('metrica_qualidade'):.0f}%" if fita.get("metrica_qualidade") is not None
                    else (fita.get("status") or "SEM DADO"))
        glob = (por_nome.get("Global") or {}).get("status") or "SEM DADO"
        piores = [i for i in itens if i.get("status") not in ("OK", None)]
        return {"resumo": f"MT5 {mt5} | Tryd fita {fita_txt} | Global {glob}",
                "itens": itens, "problemas": len(piores), "origem": origem}

    def calendario(self, con, agora):
        j = cm.janela_evento(con, agora)
        return {"proximo_evento": j.get("proximo"), "alerta_evento": j.get("alerta_evento")}

    # ----- montagem do estado
    def _pecas(self, agora):
        """Lista (chave, periodo, funcao) que devem ser (re)calculadas neste instante."""
        def reg(ativo):
            return lambda con, a: self.regiao(con, ativo, a)
        lista = [("validacao", PERIODOS["validacao"], self.validacao_piece),
                 ("cenario", PERIODOS["cenario"], self.cenario),
                 ("regioes:WIN$", PERIODOS["regioes:WIN$"], reg("WIN$")),
                 ("regioes:WDO$", PERIODOS["regioes:WDO$"], reg("WDO$")),
                 ("regioes:BOVA11", PERIODOS["regioes:BOVA11"], reg("BOVA11")),
                 ("opcoes", PERIODOS["opcoes"], self.opcoes),
                 ("swing", PERIODOS["swing"], self.swing),
                 ("risco", PERIODOS["risco"], self.risco),
                 ("correlacoes", PERIODOS["correlacoes"], self.correlacoes),
                 ("saude", PERIODOS["saude"], self.saude),
                 ("calendario", PERIODOS["calendario"], self.calendario),
                 ("players", PERIODOS["players"], self.players),
                 ("fluxo", PERIODOS["fluxo"], self.fluxo),
                 ("global", PERIODOS["global"], self.globais),
                 ("quantis", PERIODOS["quantis"], self.quantis)]
        m = _minuto(agora)
        # a abertura justa so e recalculada no pre-mercado e ate 09:15; fora disso vale a ultima
        if (7 * 60 <= m <= 9 * 60 + 15) or "abertura" not in self.dados:
            lista.append(("abertura", PERIODOS["abertura"], self.abertura))
        return lista

    def agendar(self, agora):
        """Dispara em threads as pecas vencidas e os gatilhos das zonas ativas (5 s)."""
        for chave, periodo, fn in self._pecas(agora):
            self.talvez(chave, periodo, agora, fn)
        with self.lock:
            reguas = {a: (self.dados.get(f"regioes:{a}") or {}).get("valor") for a in pv3.ATIVOS}
        for ativo, parte in reguas.items():
            for zona in ((parte or {}).get("zonas") or []):
                chave = f"gatilho:{ativo}:{zona.get('id_zona')}:{zona.get('direcao')}"
                self.talvez(chave, PERIODOS["gatilho"], agora,
                            lambda con, a, ativo=ativo, zona=zona: self.gatilho(con, ativo, zona, a))

    def atualizar_tudo(self, agora):
        """Roda todas as pecas em sequencia (modo --uma-vez/--seco), respeitando as dependencias."""
        for chave, periodo, fn in self._pecas(agora):
            self.executar(chave, agora, fn)
        with self.lock:
            reguas = {a: (self.dados.get(f"regioes:{a}") or {}).get("valor") for a in pv3.ATIVOS}
        for ativo, parte in reguas.items():
            for zona in ((parte or {}).get("zonas") or []):
                chave = f"gatilho:{ativo}:{zona.get('id_zona')}:{zona.get('direcao')}"
                self.executar(chave, agora,
                              lambda con, a, ativo=ativo, zona=zona: self.gatilho(con, ativo, zona, a))

    def montar(self, agora):
        """Monta o JSON de estado a partir das pecas em cache (sem recalcular)."""
        estado = {"ts_ms": int(agora)}
        estado["saude"] = self.peca("saude", agora)
        cal = self.peca("calendario", agora)
        estado["proximo_evento"] = cal.get("proximo_evento")
        estado["alerta_evento"] = cal.get("alerta_evento")
        estado["cenario"] = self.peca("cenario", agora)
        estado["cenario"].pop("_vies", None)
        val = self.peca("validacao", agora)
        if val.get("dia_idx") == agora // D:
            estado["cenario"]["validacao"] = val
        estado["abertura"] = self.peca("abertura", agora)
        estado["risco_brasil"] = self.peca("risco", agora)
        estado["global"] = self.peca("global", agora)
        estado["quantis"] = self.peca("quantis", agora)
        estado["opcoes"] = self.peca("opcoes", agora)
        estado["swing"] = self.peca("swing", agora)
        estado["correlacoes"] = self.peca("correlacoes", agora)
        regioes = {}
        for ativo in pv3.ATIVOS:
            parte = self.peca(f"regioes:{ativo}", agora)
            zonas = []
            for z in parte.get("zonas") or []:
                z = dict(z)
                z["gatilho"] = self.peca(f"gatilho:{ativo}:{z.get('id_zona')}:{z.get('direcao')}", agora)
                zonas.append(z)
            parte["zonas"] = zonas
            regioes[ativo] = parte
        estado["regioes"] = regioes
        pl = self.peca("players", agora)
        fl = self.peca("fluxo", agora)
        fl["placar"] = pl.get("grupos") or []
        fl["fonte_saldo"] = pl.get("fonte_saldo")
        fl["unidade"] = pl.get("unidade")
        estado["fluxo"] = fl
        estado["players"] = pl
        if self._estudo is None:
            self._estudo = pv3.estudo_regioes_resumo()
        estado["estudo_regioes"] = self._estudo
        with self.lock:
            tempos = dict(self.dados)
        estado["tempos"] = {k: round(float((tempos[k].get("tempo_ms") or 0.0)), 1)
                            for k in sorted(tempos)}
        return _limpo(estado)

    # ----- gravacao
    def gravar_estado(self, estado):
        corpo = json.dumps(estado, ensure_ascii=False, separators=(",", ":"))
        tempos = json.dumps(estado.get("tempos") or {}, ensure_ascii=False)
        db = banco.abrir(self.db_path, SCHEMA)
        try:
            db.execute("INSERT OR REPLACE INTO estado_cockpit_v3 (id, ts_ms, json, tempos_json) "
                       "VALUES (1,?,?,?)", (int(estado["ts_ms"]), corpo, tempos))
            db.commit()
        finally:
            db.close()
        return len(corpo.encode("utf-8"))

    def gravar_derivados(self, estado, agora):
        """Uma linha em cenario_scores e as zonas em regioes_operacionais por minuto."""
        minuto = int(agora) // 60_000
        if self.ultimo_minuto_derivados == minuto:
            return 0
        ce = estado.get("cenario") or {}
        pil = ce.get("pilares") or {}
        def ps(chave):
            v = (pil.get(chave) or {}).get("score")
            return None if v is None else float(v)
        arq = (ce.get("arquetipo") or {}).get("nome") or "Misto"
        resumo = " ".join(ce.get("briefing") or [])
        linhas_reg = []
        for ativo, parte in (estado.get("regioes") or {}).items():
            for z in (parte.get("zonas") or []):
                st = (z.get("gatilho") or {}).get("status")
                if z.get("id_zona") is None or any(z.get(k) is None for k in
                                                   ("preco_min", "preco_centro", "preco_max", "forca")):
                    continue
                linhas_reg.append((int(agora), ativo, "DAYTRADE", z["id_zona"], z.get("preco_min"),
                                   z.get("preco_centro"), z.get("preco_max"), z.get("forca"),
                                   z.get("alinhamento"), z.get("direcao"),
                                   json.dumps(z.get("componentes") or [], ensure_ascii=False),
                                   z.get("stop"), z.get("alvo_1"), z.get("alvo_2"), st))
        db = banco.abrir(self.db_path, mc.SCHEMA + mr.SCHEMA)
        try:
            if ce and (ce.get("score_estrutural") is not None or ce.get("score_intraday") is not None):
                db.execute(
                    "INSERT OR REPLACE INTO cenario_scores (ts_ms, score_estrutural, score_intraday, "
                    "pilar_macro_juros, pilar_fluxo_cambio, pilar_vol_opcoes, pilar_global_commod, "
                    "pilar_noticias, risco_brasil_bps, arquetipo_dia, resumo_narrativo) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (int(agora), float(ce.get("score_estrutural") or 0.0),
                     float(ce.get("score_intraday") or 0.0), ps("macro_juros"), ps("fluxo_cambio"),
                     ps("vol_opcoes"), ps("global_commod"), ps("noticias"),
                     ce.get("risco_brasil_bps"), arq, resumo))
            if linhas_reg:
                db.executemany(
                    "INSERT OR REPLACE INTO regioes_operacionais (ts_ms, ativo, horizonte, id_zona, preco_min, "
                    "preco_centro, preco_max, forca_score, alinhamento, direcao_operacao, componentes_json, "
                    "stop_sugerido, alvo_1, alvo_2, status_gatilho) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    linhas_reg)
            db.commit()
        finally:
            db.close()
        self.ultimo_minuto_derivados = minuto
        return len(linhas_reg)


# ------------------------------------------------------------------ execucao

def _tamanho(obj):
    return len(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _imprimir_tempos(p, estado):
    print("tempos por peca (ms):", flush=True)
    for chave in sorted(estado.get("tempos") or {}, key=lambda k: -float(estado["tempos"][k] or 0)):
        ms = float(estado["tempos"][chave] or 0)
        print(f"  {chave:34} {ms:9.1f}", flush=True)
    corpo = json.dumps(estado, ensure_ascii=False, separators=(",", ":"))
    print(f"JSON de estado: {len(corpo.encode('utf-8')):,} bytes "
          f"({len(corpo.encode('utf-8')) / 1024.0:,.1f} KB)", flush=True)


def uma_vez(db_path, seco=False, salvar=None, agora=None):
    """Calcula tudo uma vez, imprime os tempos e (fora do --seco) grava o estado."""
    desempenho.sem_modo_eficiencia()
    agora = int(agora if agora is not None else relogio.local_br_ms())
    p = Pecas(db_path, seco=seco)
    t0 = time.time()
    p.atualizar_tudo(agora)
    estado = p.montar(agora)
    total = time.time() - t0
    _imprimir_tempos(p, estado)
    print(f"total (sincrono): {total:.1f} s", flush=True)
    erros = [k for k, d in p.dados.items() if d.get("erro")]
    if erros:
        print("pecas com erro: " + ", ".join(f"{k} ({p.dados[k]['erro']})" for k in erros), flush=True)
    if salvar:
        Path(salvar).write_text(json.dumps(estado, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"JSON salvo em {salvar}", flush=True)
    if not seco:
        n = p.gravar_estado(estado)
        d = p.gravar_derivados(estado, agora)
        print(f"gravado em estado_cockpit_v3 ({n} bytes) e {d} zonas em regioes_operacionais", flush=True)
    return estado


def rodar(db_path, segundos=0.0, seco=False, salvar=None):
    """Laco do supervisor: agenda as pecas, monta e grava o estado a cada ciclo."""
    desempenho.sem_modo_eficiencia()
    p = Pecas(db_path, seco=seco)
    fim = time.time() + segundos if segundos and segundos > 0 else float("inf")
    print(f"Cockpit v3 ({db_path}); ciclo 5 s no pregao, 60 s das 07:00 as 09:00, 300 s fora"
          f"{' (SECO: nao grava)' if seco else ''}. Ctrl+C para parar.", flush=True)
    while time.time() < fim:
        agora = int(relogio.local_br_ms())
        d = date(1970, 1, 1) + timedelta(days=agora // D)
        try:
            pregao = cal.eh_pregao(d)
        except Exception:
            pregao = True
        try:
            p.agendar(agora)
            estado = p.montar(agora)
            if not seco:
                p.gravar_estado(estado)
                p.gravar_derivados(estado, agora)
            if salvar:
                Path(salvar).write_text(json.dumps(estado, ensure_ascii=False, indent=1), encoding="utf-8")
            prontas, erros = p.resumo()
            print(f"[{datetime.now().strftime('%H:%M:%S')}] estado com {prontas} pecas ({erros} com erro), "
                  f"{_tamanho(estado)} bytes", flush=True)
        except Exception as e:                     # o supervisor nunca morre por uma peca
            print(f"[{datetime.now().strftime('%H:%M:%S')}] ciclo falhou: {e.__class__.__name__}: {e}",
                  flush=True)
        time.sleep(intervalo_s(agora, pregao))


def main():
    ap = argparse.ArgumentParser(description="Atualizador do cockpit v3 (estado consolidado em JSON)")
    ap.add_argument("grupo", nargs="?", default="COCKPIT", help="nome do grupo no supervisor (informativo)")
    ap.add_argument("segundos", nargs="?", type=float, default=0.0, help="0 = sem limite")
    ap.add_argument("--db", default=str(Path(__file__).parent / "dados" / "book.db"))
    ap.add_argument("--uma-vez", action="store_true", help="calcula tudo uma vez e sai")
    ap.add_argument("--seco", action="store_true", help="nao grava nada; imprime os tempos de cada peca")
    ap.add_argument("--salvar-json", default=None, help="grava o JSON de estado nesse caminho (auditoria)")
    a = ap.parse_args()
    if a.uma_vez:
        uma_vez(a.db, seco=a.seco, salvar=a.salvar_json)
    else:
        rodar(a.db, a.segundos, seco=a.seco, salvar=a.salvar_json)


if __name__ == "__main__":
    main()
