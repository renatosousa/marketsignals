"""Motor de cenario da v3: 5 pilares, score estrutural/tatico, arquetipo do dia e briefing.

ESPEC_V3.md secoes 5.2 (scores), 5.3 (arquetipos) e 8.1 (tabela cenario_scores). Este modulo nao treina nada:
ele mede o que existe no banco, padroniza cada indicador de forma causal e compoe os pilares.

Regras de leitura
-----------------
- Causalidade estrita: o valor de um indicador no instante t usa somente observacoes com ts_ms <= t. O rotulo
  oficial do fluxo estrangeiro (data_pub) so entra quando a publicacao e anterior ao dia corrente; a posicao em
  aberto das opcoes e sempre D-1 (a B3 divulga a noite); o Focus usa apenas publicacoes com data <= dia; o leilao
  usa apenas datas estritamente anteriores ao dia avaliado (o resultado nao tem horario confiavel).
- Cada indicador vira z causal: (valor - media) / desvio da janela de 60 sessoes anteriores (estrutural) ou 20
  (tatico), truncado em [-3, +3]. Historico curto nao quebra nada: usa as observacoes disponiveis e, abaixo do
  minimo, o indicador fica ausente e entra na lista `faltou`.
- Sinal dos indicadores: positivo = construtivo para a bolsa brasileira (WIN sobe; WDO e DI caem). O campo `valor`
  de cada indicador e o numero medido na unidade natural (bps, %, R$ mi, razao) e o campo `z` ja vem orientado.
- Pilar = media dos z dos indicadores presentes. Composto = soma(peso_p * z_p) / soma(pesos presentes), com pesos
  25/25/20/20/10. O GEX nao entra na media: ele amplifica o z composto (negativo x 1,2; positivo x 0,85).
- Score = 100 * tanh(z / 1,5), em [-100, +100].

O que o modulo entrega
----------------------
  cenario(con, agora_ms)          dict com scores, pilares, fator GEX, risco Brasil, arquetipo e briefing.
  atualizar(db_path, agora_ms)    grava uma linha em cenario_scores (uso no coletor; aqui so no banco de teste).
  historico(con, dias=120)        score estrutural as 09:15 de cada pregao passado (causal).
  avaliar(con)                    fora da amostra: correlacao de postos e acerto de sinal do score e de cada pilar
                                  contra o retorno do WIN no proprio dia (09:15->fim), no dia seguinte e em 5
                                  pregoes. Nao ajusta pesos; apenas mede.

Nenhum indicador e obrigatorio. Fonte parada, tabela ausente e historico curto aparecem em `faltou`/`faltantes`.
"""
import argparse
import bisect
import math
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

import banco
import relogio

D = 86_400_000
SESSAO_FIM = 18 * 60 + 30          # minuto do ultimo preco liquido dos futuros
ABERTURA = 9 * 60
H0915 = 9 * 60 + 15

PESOS = {"macro_juros": 0.25, "fluxo_cambio": 0.25, "vol_opcoes": 0.20,
         "global_commod": 0.20, "noticias": 0.10}
NOMES_PILARES = {"macro_juros": "Macro e juros", "fluxo_cambio": "Fluxo e câmbio",
                 "vol_opcoes": "Vol e opções", "global_commod": "Global e commodities",
                 "noticias": "Notícias"}

JANELA_ESTRUTURAL = 60
JANELA_TATICA = 20
MIN_HIST = {"estrutural": 20, "tatico": 10}
# o VRP usa a vol realizada oficial do BOVA11, que so tem 7 sessoes consolidadas no banco real
MIN_ITEM = {("vol_opcoes", "VRP = IV ATM - vol realizada 20d"): 5}
Z_MAX = 3.0
GEX_NEG = 1.2
GEX_POS = 0.85
IDADE_MAX_DIAS = 7                 # valor pontual mais velho que isso conta como fonte parada
DI_CURVA = ("DI1F27", "DI1F28", "DI1F29", "DI1F31", "DI1F33", "DI1F35")
DAPS = ("DAPK29", "DAPQ28", "DAPK33")
TAU_NOTICIA = {"estrutural": 3 * D, "tatico": 4 * 3_600_000}
JANELA_NOTICIA = {"estrutural": 10 * D, "tatico": D}
MIN_NOTICIAS = 20
DU_OPCAO_MIN = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS cenario_scores (
    ts_ms INTEGER NOT NULL PRIMARY KEY,
    score_estrutural REAL NOT NULL,
    score_intraday REAL NOT NULL,
    pilar_macro_juros REAL,
    pilar_fluxo_cambio REAL,
    pilar_vol_opcoes REAL,
    pilar_global_commod REAL,
    pilar_noticias REAL,
    risco_brasil_bps REAL,
    arquetipo_dia TEXT NOT NULL,
    resumo_narrativo TEXT
);"""

TEXTO_ARQUETIPO = {
    "Dia de evento binário": ("Operar leve antes do dado. Aguardar a varredura de liquidez pós-evento e a "
                              "reancoragem na VWAP do evento antes de aumentar tamanho."),
    "Exaustão": ("Alerta máximo: tendência madura e esticada. Reduzir tamanho em rompimentos e habilitar setups "
                 "contra a tendência só com confirmação de absorção (Spring/UTAD)."),
    "Repique contra a tendência": ("Aguardar a exaustão do movimento de abertura nas zonas de suporte/resistência "
                                   "institucional para entrar a favor da tendência estrutural com ótimo risco/retorno."),
    "Tendência alinhada": ("Priorizar operações a favor da tendência em pullbacks curtos (VWAP/VAH/VAL) e "
                           "rompimentos confirmados. Evitar contra-tendência precoce."),
    "Compressão": ("Mercado travado por hedging de dealers: contra-tendência nas extremidades (±1,5 sigma, "
                   "VAH/VAL) e venda de volatilidade em opções."),
    "Misto": ("Sem viés dominante: operar apenas os níveis, com tamanho reduzido, até o cenário definir direção."),
}


# ------------------------------------------------------------------ utilidades

def _dia(ts):
    return int(ts) // D


def _iso(dia):
    return (date(1970, 1, 1) + timedelta(days=int(dia))).isoformat()


def _data(iso):
    return date.fromisoformat(str(iso)[:10])


def _dia_idx(d):
    if isinstance(d, int):
        return d
    if isinstance(d, datetime):
        d = d.date()
    if isinstance(d, str):
        d = _data(d)
    return (d - date(1970, 1, 1)).days


def _minuto(ts):
    return (int(ts) % D) // 60_000


def _ts(dia, minuto):
    return int(dia) * D + int(minuto) * 60_000


def _ind(bruto, unidade, sinal):
    """Indicador medido: `bruto` na unidade natural, `sinal` orienta para bolsa (+1/-1)."""
    if bruto is None or not np.isfinite(bruto):
        return None
    return {"bruto": float(bruto), "unidade": unidade, "sinal": int(sinal)}


def _orientado(v):
    return None if v is None else v["sinal"] * v["bruto"]


def _z(valor, hist, minimo):
    """z causal do valor orientado contra o historico orientado; None se faltar amostra ou dispersao."""
    vals = [h for h in hist if h is not None and np.isfinite(h)]
    if valor is None or not np.isfinite(valor) or len(vals) < int(minimo):
        return None
    mu = float(np.mean(vals))
    sd = float(np.std(vals, ddof=1)) if len(vals) > 1 else 0.0
    if not np.isfinite(sd) or sd <= 1e-12:
        return None
    return float(np.clip((valor - mu) / sd, -Z_MAX, Z_MAX))


def _score_z(z):
    """Score do z: 100 * tanh(z / 1,5). None continua None."""
    if z is None:
        return None
    return float(100.0 * math.tanh(float(z) / 1.5))


def _fator_gex(gex_total):
    """Amplificador do composto: GEX negativo aumenta a magnitude do vies, positivo reduz."""
    if gex_total is None or not np.isfinite(gex_total) or gex_total == 0:
        return 1.0
    return GEX_NEG if gex_total < 0 else GEX_POS


def _fmt(x, nd=2, sinal=False):
    """Numero em pt-BR (ponto de milhar, virgula decimal)."""
    if x is None or not np.isfinite(x):
        return "sem dado"
    if round(float(x), nd) == 0:
        x = 0.0
    txt = f"{float(x):,.{nd}f}"
    txt = txt.replace(",", "X").replace(".", ",").replace("X", ".")
    if sinal and x > 0:
        txt = "+" + txt
    return txt


def _fmt_pct(x, nd=2, sinal=True):
    return "sem dado" if x is None or not np.isfinite(x) else _fmt(x, nd, sinal) + "%"


def _fmt_brl_mi(x):
    """R$ mi -> R$ bi quando grande, para leitura direta."""
    if x is None or not np.isfinite(x):
        return "sem dado"
    sinal = "-" if x < 0 else ""
    y = abs(float(x))
    if y >= 1000:
        return f"{sinal}R$ {_fmt(y / 1000.0, 2)} bi"
    return f"{sinal}R$ {_fmt(y, 0)} mi"


# ------------------------------------------------------------------ acesso a dados

def _tem(con, tabela):
    try:
        return bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (tabela,)).fetchone())
    except sqlite3.OperationalError:
        return False


def _sessoes(con, ini_ms=None, fim_ms=None):
    """Pregoes pelo WIN$ (sessoes com >= 100 barras) + fechamento_diario; fallback global_barras."""
    ini, fim = ini_ms or 0, fim_ms or 9 * 10 ** 15
    dias = set()
    sql = "SELECT ts_ms/86400000 d FROM barras_1m WHERE symbol=? AND ts_ms>=? AND ts_ms<? GROUP BY d " \
          "HAVING count(*)>=100 ORDER BY d"
    for sym in ("WIN$", "WDO$", "IND$"):
        try:
            rows = con.execute(sql, (sym, ini, fim)).fetchall()
        except sqlite3.OperationalError:
            rows = []
        if len(rows) >= 10:
            dias.update(int(r[0]) for r in rows)
            break
    try:
        rows = con.execute("SELECT DISTINCT dia_idx FROM fechamento_diario WHERE ativo='WIN$' AND dia_idx>=? "
                           "AND dia_idx<?", (ini // D, fim // D)).fetchall()
        dias.update(int(r[0]) for r in rows)
    except sqlite3.OperationalError:
        pass
    if len(dias) >= 10:
        return sorted(dias)
    try:
        rows = con.execute("SELECT DISTINCT ts_ms/86400000 d FROM global_barras WHERE ts_ms>=? AND ts_ms<? "
                           "ORDER BY d", (ini, fim)).fetchall()
        return [int(r[0]) for r in rows]
    except sqlite3.OperationalError:
        return []


class _Dados:
    """Series e caches de uma avaliacao. `dia_ref` limita o que pode ser lido (ts < (dia_ref+1)*D)."""

    def __init__(self, con, dia_ref):
        self.con = con
        self.dia_ref = int(dia_ref)
        self.ini_ms = (self.dia_ref - 420) * D
        self.fim_ms = (self.dia_ref + 1) * D
        self._serie = {}
        self._diario = {}
        self._cache = {}
        self._tem = {}
        self.sessoes = _sessoes(con, self.ini_ms, self.fim_ms)

    # ----- estrutura
    def existe(self, tabela):
        if tabela not in self._tem:
            self._tem[tabela] = _tem(self.con, tabela)
        return self._tem[tabela]

    def anteriores(self, dia):
        return self.sessoes[:bisect.bisect_left(self.sessoes, int(dia))]

    def ref_dia(self, dia, modo):
        """Sessao de referencia: 5 pregoes atras (estrutural) ou a anterior (tatico)."""
        ant = self.anteriores(dia)
        n = 5 if modo == "estrutural" else 1
        return ant[-n] if len(ant) >= n else None

    # ----- series intradiarias
    def serie(self, simbolo):
        """(ts, valor) ordenados do simbolo: barras_1m.close, completado pelo macro_cotacoes.mid mais recente;
        BOVA11 pelo spot de opcoes_resumo."""
        if simbolo in self._serie:
            return self._serie[simbolo]
        mapa = {}
        if simbolo == "BOVA11" and self.existe("opcoes_resumo"):
            try:
                rows = self.con.execute(
                    "SELECT ts_ms, spot FROM opcoes_resumo WHERE subjacente='BOVA11' AND spot>0 "
                    "AND ts_ms>=? AND ts_ms<? ORDER BY ts_ms", (self.ini_ms, self.fim_ms)).fetchall()
            except sqlite3.OperationalError:
                rows = []
            for t, v in rows:
                if t is not None and v is not None:
                    mapa[int(t)] = float(v)
        else:
            try:
                rows = self.con.execute(
                    "SELECT ts_ms, close FROM barras_1m WHERE symbol=? AND close>0 AND ts_ms>=? AND ts_ms<? "
                    "ORDER BY ts_ms", (simbolo, self.ini_ms, self.fim_ms)).fetchall()
            except sqlite3.OperationalError:
                rows = []
            for t, v in rows:
                if t is not None and v is not None:
                    mapa[int(t)] = float(v)
            # macro cobre o que as barras nao tem, inclusive o pregao mais recente quando a fita parou
            corte = (max(mapa) + 1) if mapa else self.ini_ms
            if corte < self.fim_ms:
                try:
                    rows = self.con.execute(
                        "SELECT ts_ms, mid FROM macro_cotacoes WHERE symbol=? AND mid>0 AND ts_ms>=? AND ts_ms<? "
                        "ORDER BY ts_ms", (simbolo, corte, self.fim_ms)).fetchall()
                except sqlite3.OperationalError:
                    rows = []
                for t, v in rows:
                    if t is not None and v is not None:
                        mapa.setdefault(int(t), float(v))
        itens = sorted(mapa.items())
        self._serie[simbolo] = (np.asarray([t for t, _v in itens], dtype=np.int64),
                                np.asarray([v for _t, v in itens], dtype=float))
        return self._serie[simbolo]

    def serie_global(self, ativo):
        """(ts, close) do global_barras (5m preferido ao 60m no mesmo instante)."""
        chave = "g:" + ativo
        if chave in self._serie:
            return self._serie[chave]
        try:
            rows = self.con.execute(
                "SELECT ts_ms, close, intervalo FROM global_barras WHERE ativo=? AND close>0 AND ts_ms>=? "
                "AND ts_ms<? AND intervalo IN ('5m','60m') ORDER BY ts_ms, intervalo DESC",
                (ativo, self.ini_ms, self.fim_ms)).fetchall()
        except sqlite3.OperationalError:
            rows = []
        mapa = {}
        for t, c, _i in rows:
            if t is not None and c is not None:
                mapa[int(t)] = float(c)       # '5m' chega depois de '60m' no mesmo ts e sobrescreve
        itens = sorted(mapa.items())
        self._serie[chave] = (np.asarray([t for t, _v in itens], dtype=np.int64),
                              np.asarray([v for _t, v in itens], dtype=float))
        return self._serie[chave]

    def diario(self, simbolo):
        """{dia: {close, vwap, ts}} do simbolo, com vwap ponderado pelo volume das barras."""
        if simbolo in self._diario:
            return self._diario[simbolo]
        ts, val = self.serie(simbolo)
        vol = None
        if simbolo != "BOVA11" and len(ts):
            try:
                rows = self.con.execute(
                    "SELECT ts_ms, volume FROM barras_1m WHERE symbol=? AND ts_ms>=? AND ts_ms<? ORDER BY ts_ms",
                    (simbolo, self.ini_ms, self.fim_ms)).fetchall()
                if rows:
                    vol = {int(t): (float(v) if v and v > 0 else 0.0) for t, v in rows}
            except sqlite3.OperationalError:
                vol = None
        out = {}
        for t, v in zip(ts, val):
            dia = int(t) // D
            minuto = (int(t) % D) // 60_000
            if minuto > SESSAO_FIM:
                continue
            v_vol = vol.get(int(t), 0.0) if vol else 0.0
            if dia not in out:
                out[dia] = {"close": float(v), "ts": int(t), "pv": float(v) * v_vol, "vol": v_vol}
            else:
                e = out[dia]
                e["close"], e["ts"] = float(v), int(t)
                e["pv"] += float(v) * v_vol
                e["vol"] += v_vol
        for e in out.values():
            e["vwap"] = e["pv"] / e["vol"] if e["vol"] > 0 else e["close"]
        # fechamento consolidado (fonte oficial da variacao do dia seguinte) sobrescreve/complete os dias
        try:
            rows = self.con.execute(
                "SELECT dia_idx, ultimo_liquido, vwap_dia FROM fechamento_diario "
                "WHERE ativo=? AND ultimo_liquido IS NOT NULL", (simbolo,)).fetchall()
        except sqlite3.OperationalError:
            rows = []
        for dia, close, vwap in rows:
            dia = int(dia)
            if not (self.ini_ms // D <= dia <= self.dia_ref):
                continue
            out[dia] = {"close": float(close), "ts": _ts(dia, SESSAO_FIM),
                        "vwap": float(vwap) if vwap else float(close), "pv": 0.0, "vol": 0.0}
        self._diario[simbolo] = out
        return out

    # ----- leituras pontuais
    def ultimo(self, simbolo, ts, idade_max_dias=IDADE_MAX_DIAS):
        ts_arr, val = self.serie(simbolo)
        i = int(np.searchsorted(ts_arr, int(ts), side="right")) - 1
        if i < 0:
            return None, None
        t = int(ts_arr[i])
        if (int(ts) - t) > idade_max_dias * D:
            return None, None
        return t, float(val[i])

    def valor_em(self, simbolo, ts, idade_max_dias=IDADE_MAX_DIAS):
        return self.ultimo(simbolo, ts, idade_max_dias)[1]

    def valor_no_dia(self, simbolo, dia, minuto=SESSAO_FIM):
        """Ultimo valor do proprio dia ate `minuto`; None se o dia nao tem dado."""
        ts_arr, val = self.serie(simbolo)
        i = int(np.searchsorted(ts_arr, _ts(dia, minuto), side="right")) - 1
        if i < 0 or int(ts_arr[i]) // D != int(dia):
            return None
        return float(val[i])

    def fechamento(self, simbolo, dia):
        e = self.diario(simbolo).get(int(dia)) if dia is not None else None
        return e["close"] if e else None

    def vwap(self, simbolo, dia):
        e = self.diario(simbolo).get(int(dia)) if dia is not None else None
        return e["vwap"] if e else None

    def ts_fechamento(self, simbolo, dia):
        e = self.diario(simbolo).get(int(dia)) if dia is not None else None
        return e["ts"] if e else None

    def idade_h(self, ativo, ts):
        """Idade em horas da ultima cotacao global do ativo (None sem cotacao)."""
        t, _v = self.ultimo_global(ativo, ts)
        return None if t is None else (int(ts) - int(t)) / 3_600_000.0

    def ultimo_global(self, ativo, ts):
        ts_arr, val = self.serie_global(ativo)
        i = int(np.searchsorted(ts_arr, int(ts), side="right")) - 1
        if i < 0:
            return None, None
        return int(ts_arr[i]), float(val[i])

    def valor_global(self, ativo, ts):
        return self.ultimo_global(ativo, ts)[1]

    def cache(self, chave, fn):
        if chave not in self._cache:
            self._cache[chave] = fn()
        return self._cache[chave]


# ------------------------------------------------------------------ indicadores

def _f_di(simbolo):
    def f(d, ts, modo):
        ref = d.ref_dia(_dia(ts), modo)
        a = d.valor_em(simbolo, ts)
        b = d.fechamento(simbolo, ref)
        if a is None or b is None:
            return None
        return _ind((a - b) * 100.0, "bps", -1)
    return f


def _di_curto(d, ts, ref):
    """(DI1F33 - DI1F28) e a referencia; usa DI1F27 se o F28 faltar."""
    for curto in ("DI1F28", "DI1F27"):
        a33 = d.valor_em("DI1F33", ts)
        b = d.valor_em(curto, ts)
        r33 = d.fechamento("DI1F33", ref)
        rb = d.fechamento(curto, ref)
        if None not in (a33, b, r33, rb):
            return (a33 - b), (r33 - rb)
    return None, None


def _f_inclinacao(d, ts, modo):
    ref = d.ref_dia(_dia(ts), modo)
    if ref is None:
        return None
    atual, base = _di_curto(d, ts, ref)
    if atual is None:
        return None
    return _ind((atual - base) * 100.0, "bps", -1)


def _breakeven(d, ts):
    """Inflacao implicita (% a.a.) no instante ts pela curva DI x DAP (curvas.breakeven_dap)."""
    import curvas
    taxas = {}
    for s in DI_CURVA:
        v = d.valor_em(s, ts, 3)
        if v is not None:
            taxas[s] = v
    if len(taxas) < 2:
        return None
    for dap in DAPS:
        v = d.valor_em(dap, ts, 3)
        if v is None:
            continue
        try:
            be, _di, _du = curvas.breakeven_dap(_data(_iso(_dia(ts))), taxas, dap, v)
        except Exception:
            be = None
        if be is not None:
            return float(be)
    return None


def _f_implicita(d, ts, modo):
    ref = d.ref_dia(_dia(ts), modo)
    if ref is None:
        return None
    atual = _breakeven(d, ts)
    base = _breakeven(d, _ts(ref, SESSAO_FIM))
    if atual is None or base is None:
        return None
    return _ind((atual - base) * 100.0, "bps", -1)


def _focus_rows(d):
    """[(data_iso, mediana)] do IPCA do ano corrente, so publicacoes no banco."""
    def carregar():
        if not d.existe("focus_ipca"):
            return []
        try:
            rows = d.con.execute("SELECT data, data_ref, mediana FROM focus_ipca WHERE mediana IS NOT NULL "
                                 "ORDER BY data").fetchall()
        except sqlite3.OperationalError:
            return []
        return [(str(r[0])[:10], int(r[1]), float(r[2])) for r in rows]
    return d.cache("focus", carregar)


def _f_focus(d, ts, modo):
    dia = _dia(ts)
    iso, ano = _iso(dia), _data(_iso(dia)).year
    rows = [r for r in _focus_rows(d) if r[0] <= iso and r[1] == ano]
    if len(rows) < 2:
        return None
    ult_data, _ref, ult = rows[-1]
    corte = (_data(ult_data) - timedelta(days=28)).isoformat()
    ant = [r for r in rows if r[0] <= corte]
    if not ant:
        if (_data(ult_data) - _data(rows[0][0])).days < 21:
            return None
        ant = [rows[0]]
    return _ind(ult - ant[-1][2], "p.p.", -1)


def _leiloes(d):
    """[(data_iso, concessao_media_bps)] dos leiloes do Tesouro, ordenado por data."""
    def carregar():
        if not (d.existe("leilao_resultados") and d.existe("leilao_metricas")):
            return []
        try:
            rows = d.con.execute(
                "SELECT r.data, avg(COALESCE(m.concessao_bps, m.curva_bps)) v "
                "FROM leilao_resultados r JOIN leilao_metricas m ON m.resultado_id=r.id "
                "WHERE COALESCE(m.concessao_bps, m.curva_bps) IS NOT NULL "
                "GROUP BY r.data ORDER BY r.data").fetchall()
        except sqlite3.OperationalError:
            return []
        return [(str(r[0])[:10], float(r[1])) for r in rows if r[1] is not None]
    return d.cache("leiloes", carregar)


def _f_leilao(d, ts, modo):
    dia = _iso(_dia(ts))
    rows = _leiloes(d)
    ant = [r for r in rows if r[0] < dia]   # resultado sem horario: usa so data estritamente anterior
    if not ant:
        return None
    return _ind(ant[-1][1], "bps", -1)


def _fluxo(d):
    """[(data_ref, saldo_mil, data_pub)] do estrangeiro, ordenado por data_ref."""
    def carregar():
        if not d.existe("fluxo_diario"):
            return []
        try:
            rows = d.con.execute(
                "SELECT data_ref, saldo_mil, data_pub FROM fluxo_diario WHERE tipo='Investidor Estrangeiro' "
                "AND saldo_mil IS NOT NULL ORDER BY data_ref").fetchall()
        except sqlite3.OperationalError:
            return []
        return [(str(r[0])[:10], float(r[1]), str(r[2])[:10] if r[2] else None) for r in rows]
    return d.cache("fluxo", carregar)


def _f_fluxo(n):
    def f(d, ts, modo):
        iso = _iso(_dia(ts))
        rows = [r for r in _fluxo(d) if r[1] is not None and r[2] is not None and r[2] < iso and r[0] <= iso]
        if len(rows) < n:
            return None
        return _ind(sum(r[1] for r in rows[-n:]) / 1000.0, "R$ mi", +1)
    return f


def _nowcasts(d):
    def carregar():
        try:
            import nowcast_fluxo_v2 as nf
            return {l["dia"]: float(l["estimativa_mi"]) for l in nf.historico(d.con)}
        except Exception:
            return {}
    return d.cache("nowcast", carregar)


def _f_nowcast(d, ts, modo):
    if modo != "tatico":
        return None
    v = _nowcasts(d).get(_iso(_dia(ts)))
    if v is None:
        # o nowcast de hoje pode nao estar no historico (dia sem oficial/tryd ainda)
        try:
            import nowcast_fluxo_v2 as nf
            r = nf.estimar(d.con, _iso(_dia(ts)))
        except Exception:
            r = None
        v = None if not r else r.get("estimativa_mi")
    return _ind(v, "R$ mi", +1)


def _f_wdo_vwap(n):
    def f(d, ts, modo):
        dia = _dia(ts)
        atual = d.valor_em("WDO$", ts)
        ant = d.anteriores(dia)[-n:]
        if atual is None or len(ant) < n:
            return None
        vw = [d.vwap("WDO$", x) for x in ant]
        vw = [v for v in vw if v is not None and v > 0]
        if len(vw) < max(3, n // 2):
            return None
        vwap = float(np.mean(vw))
        return _ind((atual / vwap - 1.0) * 100.0, "%", -1)
    return f


def _eps_dia(d, dia):
    def calcular():
        try:
            import risco_brasil as rb
            return rb.calcular_dia(d.con, int(dia)) or []
        except Exception:
            return []
    return d.cache(f"eps:{int(dia)}", calcular)


def _f_eps(d, ts, modo):
    if modo != "tatico":
        return None
    dia = _dia(ts)
    linhas = [r for r in _eps_dia(d, dia) if int(r.get("ts_ms", 0)) <= int(ts)]
    if not linhas:
        return None
    return _ind(linhas[-1].get("eps_acum_pct"), "%", -1)


def _opcao_row(d, ts, coluna):
    """Ultima linha do vencimento mais proximo (>= 5 dias uteis) com a coluna preenchida."""
    if not d.existe("opcoes_resumo"):
        return None
    ini = int(ts) - 5 * D
    try:
        rows = d.con.execute(
            f"SELECT {coluna}, ts_ms, t_anos FROM opcoes_resumo WHERE subjacente='BOVA11' AND ts_ms<=? "
            f"AND ts_ms>=? AND {coluna} IS NOT NULL AND t_anos>=? ORDER BY ts_ms DESC, t_anos ASC LIMIT 1",
            (int(ts), ini, DU_OPCAO_MIN / 252.0)).fetchone()
    except sqlite3.OperationalError:
        return None
    return rows


def _f_rr25(d, ts, modo):
    # opcoes_resumo.rr25 = IV call25 - IV put25: puts caras deixam o RR25 negativo, que ja e o sinal
    # pessimista para bolsa (nao inverter)
    r = _opcao_row(d, ts, "rr25")
    return None if not r else _ind(float(r[0]), "razão", +1)


def _rv_diaria_bova(d):
    """{dia: vol realizada em % no dia} do BOVA11 pelo fechamento consolidado (fonte oficial)."""
    def carregar():
        if not d.existe("fechamento_diario"):
            return {}
        try:
            rows = d.con.execute(
                "SELECT dia_idx, vol_realizada_dia FROM fechamento_diario WHERE ativo='BOVA11' "
                "AND vol_realizada_dia IS NOT NULL ORDER BY dia_idx").fetchall()
        except sqlite3.OperationalError:
            return {}
        return {int(r[0]): float(r[1]) for r in rows}
    return d.cache("rv_bova", carregar)


def _f_vrp(d, ts, modo):
    r = _opcao_row(d, ts, "atm_iv")
    if not r:
        return None
    iv = float(r[0]) * 100.0
    dia = _dia(ts)
    ant = d.anteriores(dia)[-20:]
    rv = _rv_diaria_bova(d)
    vals = [rv[x] for x in ant if x in rv]
    if len(vals) < 5:
        return None
    rv20 = float(np.mean(vals)) * math.sqrt(252.0)
    return _ind(iv - rv20, "p.p.", -1)


def _retorno_global(d, ativo, ts, modo):
    """Retorno desde 18:00 de D-1 (tatico) ou em 5 pregoes (estrutural), sempre com ts <= agora."""
    dia = _dia(ts)
    agora = d.valor_global(ativo, ts)
    ant = d.anteriores(dia)
    if agora is None or not ant:
        return None
    if modo == "estrutural":
        if len(ant) < 5:
            return None
        base_ts = _ts(ant[-5], _minuto(ts))
    else:
        base_ts = _ts(ant[-1], 18 * 60)
    base = d.valor_global(ativo, base_ts)
    if base is None or base <= 0:
        return None
    return agora / base - 1.0


def _f_global(nome, sinal=+1, media=()):
    def f(d, ts, modo):
        if media:
            vals = [_retorno_global(d, a, ts, modo) for a in media]
            vals = [v for v in vals if v is not None]
            if not vals:
                return None
            bruto = float(np.mean(vals))
        else:
            bruto = _retorno_global(d, nome, ts, modo)
        if bruto is None:
            return None
        return _ind(bruto * 100.0, "%", sinal)
    return f


def _f_commodities(d, ts, modo):
    brent = _retorno_global(d, "BRENT", ts, modo)
    minerio = _retorno_global(d, "MINERIO", ts, modo)
    if brent is None and minerio is None:
        return None
    if minerio is None:
        bruto = brent
    elif brent is None:
        bruto = minerio
    else:
        bruto = 0.5 * brent + 0.5 * minerio
    return _ind(bruto * 100.0, "%", +1)


def _f_vix(d, ts, modo):
    v9 = d.valor_global("VIX9D", ts)
    vix = d.valor_global("VIX", ts)
    if not v9 or not vix or vix <= 0:
        return None
    return _ind((v9 / vix - 1.0) * 100.0, "%", -1)


def _f_noticias(d, ts, modo):
    if not (d.existe("noticias") and d.existe("noticias_score")):
        return None
    tau = TAU_NOTICIA[modo]
    ini = int(ts) - JANELA_NOTICIA[modo]
    try:
        rows = d.con.execute(
            "SELECT n.ts_coleta_ms, avg(s.sentimento), max(s.relevancia) FROM noticias n "
            "JOIN noticias_score s ON s.id=n.id WHERE n.ts_coleta_ms<=? AND n.ts_coleta_ms>=? "
            "AND n.dup_de IS NULL AND s.sentimento IS NOT NULL GROUP BY n.id, n.ts_coleta_ms",
            (int(ts), ini)).fetchall()
    except sqlite3.OperationalError:
        return None
    if len(rows) < MIN_NOTICIAS:
        return None
    soma_w = soma = 0.0
    for t, sent, rel in rows:
        w = math.exp(-(int(ts) - int(t)) / float(tau)) * (float(rel) if rel is not None else 0.5)
        if w <= 0:
            continue
        soma_w += w
        soma += w * float(sent)
    if soma_w <= 0:
        return None
    return _ind(soma / soma_w, "sentimento", +1)


def _itens_pilar():
    """Definicao dos pilares: (chave do pilar, [(nome, funcao, modos permitidos)])."""
    apenas_tatico = ("tatico",)
    ambos = ("estrutural", "tatico")
    return {
        "macro_juros": [
            ("DI1F29 (bps, invertido)", _f_di("DI1F29"), ambos),
            ("DI1F33 (bps, invertido)", _f_di("DI1F33"), ambos),
            ("Inclinação 33-28 (bps, invertida)", _f_inclinacao, ambos),
            ("Inflação implícita (bps, invertida)", _f_implicita, ambos),
            ("Focus IPCA do ano em 4 semanas (p.p., invertido)", _f_focus, ambos),
            ("Concessão do último leilão (bps, invertida)", _f_leilao, ambos),
        ],
        "fluxo_cambio": [
            ("Fluxo estrangeiro 5 pregões", _f_fluxo(5), ambos),
            ("Fluxo estrangeiro 20 pregões", _f_fluxo(20), ambos),
            ("Nowcast de fluxo do dia", _f_nowcast, apenas_tatico),
            ("WDO$ contra VWAP 5 pregões", _f_wdo_vwap(5), ambos),
            ("WDO$ contra VWAP 20 pregões", _f_wdo_vwap(20), ambos),
            ("eps_BR acumulado do dia", _f_eps, apenas_tatico),
        ],
        "vol_opcoes": [
            ("RR25 do BOVA11 (call-put)", _f_rr25, ambos),
            ("VRP = IV ATM - vol realizada 20d", _f_vrp, ambos),
        ],
        "global_commod": [
            ("Ásia (N225/HSI/CSI300)", _f_global("", +1, ("N225", "HSI", "CSI300")), ambos),
            ("Europa (STOXX50/DAX/FTSE)", _f_global("", +1, ("STOXX50", "DAX", "FTSE")), ambos),
            ("ES", _f_global("ES"), ambos),
            ("NQ", _f_global("NQ"), ambos),
            ("Commodities (Brent + minério)", _f_commodities, ambos),
            ("DXY (invertido)", _f_global("DXY", -1), ambos),
            ("VIX9D/VIX - 1 (invertido)", _f_vix, ambos),
        ],
        "noticias": [
            ("Sentimento x relevância ponderado no tempo", _f_noticias, ambos),
        ],
    }


# ------------------------------------------------------------------ avaliacao dos pilares

def _composto(pilares):
    """Media dos z dos pilares presentes ponderada pelos pesos; renormaliza pelo peso presente."""
    presentes = [(p["z"], PESOS.get(k, 0.0)) for k, p in pilares.items()
                 if p.get("z") is not None and PESOS.get(k, 0.0) > 0]
    if not presentes:
        return None, 0.0
    total = sum(w for _z_, w in presentes)
    if total <= 0:
        return None, 0.0
    return sum(z * w for z, w in presentes) / total, total


def _avaliar_modo(d, agora_ms, modo):
    """Pilares do modo ('estrutural' ou 'tatico') e o z composto sem o fator GEX."""
    dia = _dia(agora_ms)
    janela = JANELA_ESTRUTURAL if modo == "estrutural" else JANELA_TATICA
    minimo = MIN_HIST[modo]
    hist_dias = d.anteriores(dia)[-janela:]
    minuto_agora = _minuto(agora_ms)
    # antes da abertura nao existe serie intraday do dia: o historico tatico usa o fechamento
    minuto_hist = SESSAO_FIM if minuto_agora < ABERTURA else min(minuto_agora, SESSAO_FIM)
    pilares = {}
    for chave, itens in _itens_pilar().items():
        indicadores, faltou, zs = [], [], []
        for nome, fn, modos in itens:
            if modo not in modos:      # indicador intradiario nao entra no score estrutural
                continue
            erro = None
            try:
                atual = fn(d, agora_ms, modo)
            except Exception as e:      # fonte esquisita nao quebra o painel
                atual = None
                erro = type(e).__name__
            if atual is None:
                faltou.append(f"{nome}: erro ao ler ({erro})" if erro else f"{nome}: sem dado")
                continue
            hist = []
            for x in hist_dias:
                ts_h = _ts(x, SESSAO_FIM) if modo == "estrutural" else _ts(x, minuto_hist)
                try:
                    hist.append(_orientado(fn(d, ts_h, modo)))
                except Exception:
                    hist.append(None)
            validos = sum(1 for h in hist if h is not None and np.isfinite(h))
            z = _z(_orientado(atual), hist, MIN_ITEM.get((chave, nome), minimo))
            if z is None:
                if validos < MIN_ITEM.get((chave, nome), minimo):
                    faltou.append(f"{nome}: histórico curto ({validos} obs)")
                else:
                    faltou.append(f"{nome}: sem dispersão no histórico")
                continue
            indicadores.append({"nome": nome, "valor": atual["bruto"], "unidade": atual["unidade"],
                                "z": z, "n": validos})
            zs.append(z)
        z_p = float(np.mean(zs)) if zs else None
        pilares[chave] = {"z": z_p, "score": _score_z(z_p), "indicadores": indicadores, "faltou": faltou,
                          "peso": PESOS[chave]}
    z_comp, peso_presente = _composto(pilares)
    return {"dia": dia, "modo": modo, "pilares": pilares, "z_composto": z_comp,
            "pesos_presentes": peso_presente,
            "faltantes": [f"{NOMES_PILARES[k]}: {x}" for k, p in pilares.items() for x in p["faltou"]]}


# ------------------------------------------------------------------ GEX

def _posicoes_bova(d, dia_iso):
    """Posicao em aberto do BOVA11 (D-1) via grafico_quant.posicoes; fallback direto na b3_posicoes_series."""
    import grafico_quant as gq
    try:
        pos = gq.posicoes(d.con, dia_iso)
    except sqlite3.OperationalError:
        pos = None
    if pos and not pos["series"].empty:
        return pos
    if not d.existe("b3_posicoes_series"):
        return None
    try:
        ref = d.con.execute("SELECT max(sessao) FROM b3_posicoes_series WHERE subjacente='BOVA11' "
                            "AND sessao < ?", (dia_iso,)).fetchone()[0]
        if not ref:
            return None
        import pandas as pd
        s = pd.read_sql("SELECT vencimento, tipo, strike, coberto, travado, descoberto, vol_impl_pct iv "
                        "FROM b3_posicoes_series WHERE subjacente='BOVA11' AND sessao=?", d.con, params=(ref,))
    except Exception:
        return None
    s["venc"] = s.vencimento.map(gq._iso)
    s["oi"] = s[["coberto", "travado", "descoberto"]].sum(axis=1, min_count=1)
    s = s[s.venc.notna() & (s.venc >= dia_iso) & (s.oi > 0) & s.tipo.isin(["C", "P"])]
    if s.empty:
        return None
    s = s.assign(mult=1.0, fator=1.0)
    return {"fonte": "b3", "ref": ref, "series": s[gq.COLS_POS].reset_index(drop=True)}


def _spot_bova(d, ts):
    """Spot do BOVA11 conhecido em ts (opcoes_resumo); fallback fechamento de D-1."""
    if d.existe("opcoes_resumo"):
        try:
            r = d.con.execute("SELECT spot FROM opcoes_resumo WHERE subjacente='BOVA11' AND spot>0 AND ts_ms<=? "
                              "AND ts_ms>=? ORDER BY ts_ms DESC LIMIT 1", (int(ts), int(ts) - 5 * D)).fetchone()
            if r:
                return float(r[0])
        except sqlite3.OperationalError:
            pass
    ref = d.ref_dia(_dia(ts), "tatico")
    return d.fechamento("BOVA11", ref)


def _gex(d, ts):
    """GEX total do BOVA11 com a posicao conhecida em t (D-1). None se nao houver posicao/spot."""
    import grafico_quant as gq
    dia_iso = _iso(_dia(ts))
    pos = _posicoes_bova(d, dia_iso)
    if not pos or pos["series"].empty:
        return None
    spot = _spot_bova(d, ts)
    if spot is None:
        return None
    taxa = None
    if d.existe("opcoes_resumo"):
        try:
            r = d.con.execute("SELECT taxa FROM opcoes_resumo WHERE subjacente='BOVA11' AND ts_ms<=? AND taxa>0 "
                              "ORDER BY ts_ms DESC LIMIT 1", (int(ts),)).fetchone()
            taxa = float(r[0]) if r else None
        except sqlite3.OperationalError:
            taxa = None
    gx = gq.gex(pos, spot, dia_iso, gq.TAXA_PADRAO if taxa is None else taxa, None, None)
    if not gx:
        return None
    return {"gex_total": float(gx["gex_total"]), "dex_total": float(gx["dex_total"]), "flip": gx.get("flip"),
            "spot": float(spot), "posicao": pos["ref"], "fonte": pos["fonte"]}


# ------------------------------------------------------------------ arquetipo

def _classificar_arquetipo(s_macro, s_intra, gex, evento, r5, q05, q95, iv_pct, vol_rel):
    """Precedencia da ESPEC 5.3, na ordem do pedido. Devolve o nome do arquetipo."""
    def sinal(x):
        return 0 if x is None else (1 if x > 0 else (-1 if x < 0 else 0))
    fora = (r5 is not None and q05 is not None and q95 is not None and (r5 < q05 or r5 > q95))
    if evento:
        return "Dia de evento binário"
    if (s_macro is not None and abs(s_macro) >= 65 and fora
            and iv_pct is not None and iv_pct >= 85):
        return "Exaustão"
    if (s_macro is not None and abs(s_macro) >= 40 and s_intra is not None
            and sinal(s_macro) != 0 and sinal(s_intra) != 0 and sinal(s_intra) != sinal(s_macro)):
        return "Repique contra a tendência"
    if (s_macro is not None and abs(s_macro) >= 35 and s_intra is not None
            and sinal(s_macro) != 0 and sinal(s_macro) == sinal(s_intra)):
        return "Tendência alinhada"
    if gex is not None and gex < 0 and s_macro is not None and abs(s_macro) >= 25:
        return "Tendência alinhada"
    sem_gex_vol_baixa = gex is None and vol_rel is not None and vol_rel < 0.8
    if s_macro is not None and abs(s_macro) < 25 and not evento and ((gex is not None and gex > 0)
                                                                     or sem_gex_vol_baixa):
        return "Compressão"
    return "Misto"


def _evento_hoje(d, ts):
    """Evento de impacto alto hoje ainda por vir, ou janela de evento ativa."""
    dia = _dia(ts)
    try:
        r = d.con.execute("SELECT ts_ms, pais, evento FROM calendario_eventos WHERE impacto='alto' AND ts_ms>? "
                          "AND ts_ms<? ORDER BY ts_ms LIMIT 1", (int(ts), (dia + 1) * D)).fetchone()
    except sqlite3.OperationalError:
        r = None
    if r:
        return {"tipo": "por vir", "ts_ms": int(r[0]), "pais": r[1], "evento": r[2]}
    try:
        import calendario_macro
        jan = calendario_macro.janela_evento(d.con, int(ts))
    except Exception:
        jan = None
    if jan and jan.get("alerta_evento"):
        e = jan["alerta_evento"]
        return {"tipo": "janela ativa", "ts_ms": int(e["ts_ms"]), "pais": e["pais"], "evento": e["evento"]}
    return None


def _arquetipo(d, ts, s_macro, s_intra, gex):
    """Arquetipo do dia + condicoes medidas (transparencia do que foi usado)."""
    dia = _dia(ts)
    ant = [x for x in d.sessoes if x < dia]
    pares = [(x, d.fechamento("WIN$", x)) for x in ant]
    pares = [(x, c) for x, c in pares if c is not None]
    r5 = q05 = q95 = None
    t_obs, v_obs = d.ultimo("WIN$", ts)
    f_ult = d.fechamento("WIN$", ant[-1]) if ant else None
    if f_ult is not None and (t_obs is None or int(t_obs) // D < ant[-1]):
        atual = f_ult
    else:
        atual = v_obs
    if atual is not None and len(pares) >= 6:
        base = pares[-5][1]
        if base:
            r5 = atual / base - 1.0
        hist = [pares[j][1] / pares[j - 5][1] - 1.0 for j in range(5, len(pares))]
        hist = [h for h in hist[-120:] if np.isfinite(h)]
        if len(hist) >= 20:
            q05, q95 = (float(x) for x in np.quantile(hist, [0.05, 0.95]))
    iv_pct = None
    if d.existe("opcoes_resumo"):
        try:
            rows = d.con.execute(
                "SELECT ts_ms/86400000 d, max(ts_ms) FROM opcoes_resumo WHERE subjacente='BOVA11' "
                "AND atm_iv IS NOT NULL AND ts_ms<=? GROUP BY d ORDER BY d DESC LIMIT 61", (int(ts),)).fetchall()
            vals = []
            for _dia_row, x in rows[1:]:
                v = d.con.execute("SELECT atm_iv FROM opcoes_resumo WHERE subjacente='BOVA11' AND ts_ms=? "
                                  "AND atm_iv IS NOT NULL ORDER BY t_anos ASC LIMIT 1", (x,)).fetchone()
                if v:
                    vals.append(float(v[0]))
            if len(vals) >= 20:
                atual = _opcao_row(d, ts, "atm_iv")
                if atual:
                    iv_pct = 100.0 * sum(1 for v in vals if v < float(atual[0])) / len(vals)
        except sqlite3.OperationalError:
            iv_pct = None
    vol_rel = None
    try:
        r = d.con.execute("SELECT rv_rel FROM regime_live WHERE ts_ms<=? ORDER BY ts_ms DESC LIMIT 1",
                          (int(ts),)).fetchone()
        if r and r[0] is not None:
            vol_rel = float(r[0])
    except sqlite3.OperationalError:
        pass
    if vol_rel is None:
        rv = _rv_diaria_bova(d)
        vals = [rv[x] for x in d.anteriores(dia)[-20:] if x in rv]
        atual = rv.get(dia)
        if atual and len(vals) >= 5 and np.mean(vals) > 0:
            vol_rel = float(atual / np.mean(vals))
    evento = _evento_hoje(d, ts)
    gex_total = gex["gex_total"] if gex else None
    nome = _classificar_arquetipo(s_macro, s_intra, gex_total, evento, r5, q05, q95, iv_pct, vol_rel)
    condicoes = {"score_estrutural": s_macro, "score_intraday": s_intra, "gex": gex_total,
                 "evento": evento, "retorno_5d": r5, "q05_5d": q05, "q95_5d": q95,
                 "iv_percentil": iv_pct, "vol_relativa": vol_rel}
    return {"nome": nome, "orientacao": TEXTO_ARQUETIPO[nome], "condicoes": condicoes}


# ------------------------------------------------------------------ briefing

def _abertura_prevista(d, ts):
    """Previsao da abertura justa (abertura_justa.prever_hoje), com o que existe ate ts."""
    def calcular():
        try:
            import abertura_justa as aj
            r = aj.prever_hoje(d.con, int(ts)).get("WIN$")
            if not r:
                return None
            return {"abertura": r.get("abertura_prevista"), "fechamento": r.get("fechamento_anterior"),
                    "faltantes": r.get("faltantes") or []}
        except Exception:
            return None
    return d.cache("abertura", calcular)


def _briefing(d, ts, est, tat, gex, arq, risco_bps):
    dia = _dia(ts)
    ant = d.anteriores(dia)
    d1 = ant[-1] if ant else None
    d2 = ant[-2] if len(ant) > 1 else None
    win1, win2 = d.fechamento("WIN$", d1), d.fechamento("WIN$", d2)
    wdo1, wdo2 = d.fechamento("WDO$", d1), d.fechamento("WDO$", d2)
    var_win = (win1 / win2 - 1.0) * 100.0 if win1 and win2 else None
    var_wdo = (wdo1 / wdo2 - 1.0) * 100.0 if wdo1 and wdo2 else None
    # linha 1: fechamento de D-1 e madrugada
    madruga = []
    for rotulo, ativos in (("Asia", ("N225", "HSI", "CSI300")), ("Europa", ("STOXX50", "DAX", "FTSE"))):
        vals = [_retorno_global(d, a, ts, "tatico") for a in ativos]
        vals = [v for v in vals if v is not None]
        madruga.append((rotulo, float(np.mean(vals)) * 100.0 if vals else None))
    es = _retorno_global(d, "ES", ts, "tatico")
    ab = _abertura_prevista(d, ts)
    if ab and ab.get("abertura") and ab.get("fechamento"):
        var_ab = (ab["abertura"] / ab["fechamento"] - 1.0) * 100.0
        txt_ab = f"abertura justa prevista em {_fmt(ab['abertura'], 0)} pts ({_fmt_pct(var_ab)})"
    else:
        txt_ab = "abertura justa indisponível"
    l1 = (f"WIN fechou em {_fmt(win1, 0)} pts ({_fmt_pct(var_win)}) e WDO em {_fmt(wdo1, 1)} "
          f"({_fmt_pct(var_wdo)}). Madrugada: Ásia {_fmt_pct(madruga[0][1])}, Europa "
          f"{_fmt_pct(madruga[1][1])}, ES {_fmt_pct(None if es is None else es * 100)}; {txt_ab}.")
    # linha 2: fluxo estrangeiro
    fluxo = _fluxo(d)
    iso = _iso(dia)
    conhecidos = [r for r in fluxo if r[2] is not None and r[2] < iso and r[0] <= iso]
    ult = conhecidos[-1] if conhecidos else None
    acum5 = sum(r[1] for r in conhecidos[-5:]) / 1000.0 if len(conhecidos) >= 5 else None
    now = _nowcasts(d).get(iso)
    l2 = ("Fluxo estrangeiro: último oficial " +
          (f"{_fmt_brl_mi(ult[1] / 1000.0)} em {_fmt_data(ult[0])} (publicado em {_fmt_data(ult[2])})" if ult
           else "sem dado") +
          f"; 5 pregões {_fmt_brl_mi(acum5)}; nowcast de hoje {_fmt_brl_mi(now)}.")
    # linha 3: juros e inflacao
    di29 = d.valor_em("DI1F29", ts)
    ref = d.ref_dia(dia, "tatico")
    di29_ref = d.fechamento("DI1F29", ref)
    var29 = (di29 - di29_ref) * 100.0 if di29 is not None and di29_ref is not None else None
    incl, incl_ref = _di_curto(d, ts, ref) if ref is not None else (None, None)
    var_incl = (incl - incl_ref) * 100.0 if incl is not None and incl_ref is not None else None
    inc = _breakeven(d, ts)
    inc_ref = _breakeven(d, _ts(ref, SESSAO_FIM)) if ref is not None else None
    var_inc = (inc - inc_ref) * 100.0 if inc is not None and inc_ref is not None else None
    risco_txt = "sem dado" if risco_bps is None else _fmt(risco_bps, 0, True) + " bps"
    l3 = (f"Juros: DI1F29 {_fmt_pct(di29, 2, False)} ({_fmt(var29, 1, True)} bps), inclinação 33-28 "
          f"{_fmt(None if incl is None else incl * 100, 1)} bps ({_fmt(var_incl, 1, True)} bps), inflação implícita "
          f"{_fmt_pct(inc, 2, False)} ({_fmt(var_inc, 1, True)} bps); risco Brasil {risco_txt}.")
    # linha 4: vol e opcoes
    iv = _opcao_row(d, ts, "atm_iv")
    ivv = float(iv[0]) * 100.0 if iv else None
    rv = _rv_diaria_bova(d)
    vals = [rv[x] for x in d.anteriores(dia)[-20:] if x in rv]
    rv20 = float(np.mean(vals)) * math.sqrt(252.0) if vals else None
    vrp = (ivv - rv20) if (ivv is not None and rv20 is not None) else None
    rr = _opcao_row(d, ts, "rr25")
    gex_txt = "sem GEX" if not gex else f"GEX {_fmt(gex['gex_total'] / 1e6, 0)} mi"
    l4 = (f"Vol e opções: IV ATM {_fmt_pct(ivv, 2, False)} contra realizada de 20 pregões {_fmt_pct(rv20, 2, False)} "
          f"({len(vals)} obs, VRP {_fmt(vrp, 1, True)} p.p.), RR25 {_fmt(None if not rr else rr[0], 4, True)}, "
          f"{gex_txt}; arquétipo: {arq['nome']} (score estrutural {_fmt(est['score'], 0, True)}).")
    return [l1, l2, l3, l4]


def _fmt_data(iso):
    if not iso:
        return "sem data"
    try:
        return _data(iso).strftime("%d/%m")
    except Exception:
        return "sem data"


# ------------------------------------------------------------------ API principal

def cenario(con, agora_ms):
    """Cenario completo no instante `agora_ms` (somente leitura).

    Devolve dict com score_estrutural, score_intraday, pilares (cada um com z/score/indicadores/faltou do modo
    estrutural e a versao tatica em `tatico`), fator_gex, gex, risco_brasil_bps, arquetipo, briefing (4 linhas),
    faltantes e ts_ms. Score None significa que nenhum pilar tinha dado suficiente.
    """
    ts = int(agora_ms)
    d = _Dados(con, _dia(ts))
    est = _avaliar_modo(d, ts, "estrutural")
    tat = _avaliar_modo(d, ts, "tatico")
    gex = _gex(d, ts)
    fator = _fator_gex(None if gex is None else gex["gex_total"])
    z_est = None if est["z_composto"] is None else est["z_composto"] * fator
    z_tat = None if tat["z_composto"] is None else tat["z_composto"] * fator
    score_est, score_tat = _score_z(z_est), _score_z(z_tat)
    # risco Brasil: eps acumulado do dia (tatico), em bps
    eps = _f_eps(d, ts, "tatico")
    risco = float(eps["bruto"]) * 100.0 if eps is not None else None
    arq = _arquetipo(d, ts, score_est, score_tat, gex)
    briefing = _briefing(d, ts, {"score": score_est}, tat, gex, arq, risco)
    pilares = {}
    for chave, p in est["pilares"].items():
        pt = tat["pilares"][chave]
        pilares[chave] = {"nome": NOMES_PILARES[chave], "peso": p["peso"], "z": p["z"], "score": p["score"],
                          "indicadores": p["indicadores"], "faltou": p["faltou"],
                          "tatico": {"z": pt["z"], "score": pt["score"], "indicadores": pt["indicadores"],
                                     "faltou": pt["faltou"]}}
    faltantes = list(dict.fromkeys(est["faltantes"] + tat["faltantes"]))
    if gex is None:
        faltantes.append("GEX: sem posição em aberto/spot do BOVA11")
    for ativo in ("ES", "N225", "STOXX50"):
        idade = d.idade_h(ativo, ts)
        if idade is not None and idade > 6:
            faltantes.append(f"Global: última cotação de {ativo} há {_fmt(idade, 1)} h (possível fonte parada)")
    return {
        "ts_ms": ts,
        "score_estrutural": score_est,
        "score_intraday": score_tat,
        "pilares": pilares,
        "fator_gex": fator,
        "gex": None if gex is None else {"valor": gex["gex_total"], "posicao": gex["posicao"],
                                         "fonte": gex["fonte"], "spot": gex["spot"]},
        "risco_brasil_bps": risco,
        "arquetipo": arq,
        "briefing": briefing,
        "faltantes": faltantes,
    }


def atualizar(db_path, agora_ms=None):
    """Calcula e grava uma linha em cenario_scores. Devolve o dict do cenario gravado.

    Colunas NOT NULL (score e arquetipo) recebem 0,0 quando nenhum pilar tem dado; o resumo narrativo guarda as
    quatro linhas do briefing, que registram o que faltou.
    """
    ts = int(agora_ms) if agora_ms is not None else int(relogio.local_br_ms())
    ro = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    try:
        r = cenario(ro, ts)
    finally:
        ro.close()
    con = banco.abrir(db_path, SCHEMA)
    try:
        pil = r["pilares"]
        con.execute(
            "INSERT OR REPLACE INTO cenario_scores (ts_ms, score_estrutural, score_intraday, pilar_macro_juros, "
            "pilar_fluxo_cambio, pilar_vol_opcoes, pilar_global_commod, pilar_noticias, risco_brasil_bps, "
            "arquetipo_dia, resumo_narrativo) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (ts, _ou_zero(r["score_estrutural"]), _ou_zero(r["score_intraday"]),
             _pilar_score(pil, "macro_juros"), _pilar_score(pil, "fluxo_cambio"),
             _pilar_score(pil, "vol_opcoes"), _pilar_score(pil, "global_commod"),
             _pilar_score(pil, "noticias"), r["risco_brasil_bps"], r["arquetipo"]["nome"],
             " ".join(r["briefing"])))
        con.commit()
    finally:
        con.close()
    return r


def _ou_zero(x):
    return 0.0 if x is None else float(x)


def _pilar_score(pilares, chave):
    p = pilares.get(chave) or {}
    return None if p.get("score") is None else float(p["score"])


def historico(con, dias=120):
    """Score estrutural as 09:15 de cada um dos ultimos `dias` pregoes (causal).

    Cada linha usa apenas dados com ts <= 09:15 daquele pregao. Devolve lista de dicts com dia, ts_ms, score,
    z composto, fator GEX e z de cada pilar.
    """
    dias = max(0, int(dias))
    todas = _sessoes(con, None, None)
    if not todas:
        return []
    d = _Dados(con, todas[-1] + 1)
    alvo = [x for x in todas if x <= todas[-1]][-dias:] if dias else []
    out = []
    for dia in alvo:
        ts = _ts(dia, H0915)
        est = _avaliar_modo(d, ts, "estrutural")
        gex = _gex(d, ts)
        fator = _fator_gex(None if gex is None else gex["gex_total"])
        z = None if est["z_composto"] is None else est["z_composto"] * fator
        out.append({"dia": _iso(dia), "ts_ms": ts, "score_estrutural": _score_z(z), "z_composto": z,
                    "fator_gex": fator,
                    "pilares": {k: p["z"] for k, p in est["pilares"].items()}})
    return out


def _spearman(a, b):
    """Correlacao de postos (Pearson dos ranks medios); None se n < 4 ou sem dispersao."""
    if len(a) < 4:
        return None
    ra, rb = _ranks(a), _ranks(b)
    if np.std(ra) <= 0 or np.std(rb) <= 0:
        return None
    return float(np.corrcoef(ra, rb)[0, 1])


def _ranks(x):
    x = np.asarray(x, dtype=float)
    ordem = np.argsort(x, kind="mergesort")
    ranks = np.empty(len(x), dtype=float)
    i = 0
    while i < len(x):
        j = i
        while j + 1 < len(x) and x[ordem[j + 1]] == x[ordem[i]]:
            j += 1
        ranks[ordem[i:j + 1]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return ranks


def avaliar(con, dias=120):
    """Mede fora da amostra a relacao do score estrutural (09:15) com o retorno do WIN.

    Horizontes: proprio dia (09:15->fechamento), dia seguinte (fechamento->fechamento) e 5 pregoes
    (fechamento->fechamento). Reporta n, correlacao de postos e acerto de sinal quando |score| >= 35, para o
    composto e para cada pilar sozinho. Nao ajusta pesos: so mede o que a ESPEC propos.
    """
    hist = historico(con, dias)
    if not hist:
        print("sem historico de sessoes do WIN$")
        return {"n": 0}
    d = _Dados(con, _dia(hist[-1]["ts_ms"]) + 1)
    sess = d.sessoes
    pos = {x: i for i, x in enumerate(sess)}
    linhas = []
    for h in hist:
        dia = _dia(h["ts_ms"])
        i = pos.get(dia)
        if i is None:
            continue
        p915 = d.valor_no_dia("WIN$", dia, H0915)
        c0 = d.fechamento("WIN$", dia)
        c1 = d.fechamento("WIN$", sess[i + 1]) if i + 1 < len(sess) else None
        c5 = d.fechamento("WIN$", sess[i + 5]) if i + 5 < len(sess) else None
        r = {}
        if p915 and c0:
            r["dia"] = c0 / p915 - 1.0
        if c0 and c1:
            r["seguinte"] = c1 / c0 - 1.0
        if c0 and c5:
            r["5p"] = c5 / c0 - 1.0
        if r:
            linhas.append((h, r))
    if not linhas:
        print("sem retorno do WIN para cruzar com o score")
        return {"n": 0}
    grupos = [("Score estrutural", lambda h: h["score_estrutural"], 35.0)]
    for chave, nome in NOMES_PILARES.items():
        grupos.append((f"Pilar {nome}", lambda h, k=chave: (None if h["pilares"].get(k) is None
                                                             else _score_z(h["pilares"][k])), 35.0))
    res = {}
    print(f"Pregões avaliados: {len(linhas)} (score às 09:15, causal; retorno do WIN)")
    print(f"{'grupo':28}{'horizonte':11}{'n':>5}{'postos':>9}{'acerto |score|>=35':>21}")
    for nome, fn, limiar in grupos:
        res[nome] = {}
        for hz in ("dia", "seguinte", "5p"):
            xs, ys = [], []
            for h, r in linhas:
                s = fn(h)
                if s is not None and hz in r:
                    xs.append(float(s))
                    ys.append(float(r[hz]))
            if not xs:
                res[nome][hz] = {"n": 0, "postos": None, "acerto": None, "n_sinal": 0}
                print(f"{nome:28}{hz:11}{0:>5}{'-':>9}{'-':>21}")
                continue
            sp = _spearman(xs, ys)
            pares = [(s, y) for s, y in zip(xs, ys) if abs(s) >= limiar]
            acerto = (sum(1 for s, y in pares if (s > 0) == (y > 0)) / len(pares)) if pares else None
            res[nome][hz] = {"n": len(xs), "postos": sp, "acerto": acerto, "n_sinal": len(pares)}
            ac = "-" if acerto is None else f"{acerto * 100:.0f}% ({len(pares)})"
            print(f"{nome:28}{hz:11}{len(xs):>5}{'-' if sp is None else f'{sp:+.2f}':>9}{ac:>21}")
    print("\npostos = Spearman; acerto = fração de sinais certos entre |score| >= 35 (n entre parênteses).")
    print("Sem ajuste de pesos: a tabela mostra se os pesos da ESPEC têm sinal nesta amostra.")
    return res


# ------------------------------------------------------------------ CLI

def main():
    ap = argparse.ArgumentParser(description="Motor de cenario v3: 5 pilares, arquetipo e briefing")
    ap.add_argument("comando", choices=("cenario", "historico", "avaliar", "atualizar"))
    ap.add_argument("--db", default=str(Path(__file__).parent / "dados" / "book.db"))
    ap.add_argument("--agora", help="epoch ms (hora BRT codificada como UTC); padrao = agora")
    ap.add_argument("--dias", type=int, default=120)
    a = ap.parse_args()
    if a.comando == "atualizar":
        ts = int(a.agora) if a.agora else None
        r = atualizar(a.db, ts)
        print(f"cenario gravado: estrutural {_fmt(r['score_estrutural'], 0, True)} / "
              f"intraday {_fmt(r['score_intraday'], 0, True)} / {r['arquetipo']['nome']}")
        return
    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True, timeout=30)
    try:
        if a.comando == "historico":
            for l in historico(con, a.dias):
                print(f"{l['dia']}  estrutural {_fmt(l['score_estrutural'], 0, True):>6}  "
                      f"gex x{l['fator_gex']:.2f}")
            return
        if a.comando == "avaliar":
            avaliar(con, a.dias)
            return
        r = cenario(con, int(a.agora) if a.agora else int(relogio.local_br_ms()))
        imprimir(r)
    finally:
        con.close()


def imprimir(r):
    """Impressao legivel do cenario (usada na CLI e nos testes manuais)."""
    print(f"ts_ms={r['ts_ms']}  estrutural={_fmt(r['score_estrutural'], 0, True)}  "
          f"intraday={_fmt(r['score_intraday'], 0, True)}  fator_gex=x{r['fator_gex']:.2f}")
    for chave, p in r["pilares"].items():
        z_txt = "-" if p["z"] is None else f"{p['z']:+.2f}"
        zt = p.get("tatico", {}).get("z")
        zt_txt = "-" if zt is None else f"{zt:+.2f}"
        print(f"  {p['nome']:22} z={z_txt:>6} score={_fmt(p['score'], 0, True):>6}  "
              f"(tatico z={zt_txt:>6})  ({len(p['indicadores'])} indicadores)")
        for i in p["indicadores"]:
            nd = 0 if abs(i["valor"]) >= 1000 else 4
            print(f"      {i['nome']:52} {_fmt(i['valor'], nd, True):>14} {i['unidade']:>10}  z={i['z']:+.2f}")
        for x in p["faltou"]:
            print(f"      faltou: {x}")
    print(f"  risco Brasil: {_fmt(r['risco_brasil_bps'], 0, True)} bps; GEX: {r['gex']}")
    print(f"  arquetipo: {r['arquetipo']['nome']} - {r['arquetipo']['orientacao']}")
    for l in r["briefing"]:
        print("  " + l)
    if r["faltantes"]:
        print("  faltantes gerais:")
        for x in r["faltantes"]:
            print("    - " + x)


if __name__ == "__main__":
    main()
