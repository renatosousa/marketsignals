"""Leitura do cockpit v3 (/v3): estado consolidado gravado por atualizador_cockpit.py.

O atualizador_cockpit.py calcula tudo em segundo plano e grava UMA linha em `estado_cockpit_v3`
(id=1, ts_ms, json, tempos_json). Este modulo nao recalcula nada: os endpoints do dashboard leem
essa linha, devolvem o JSON com `idade_s` e marcam `desatualizado` quando a linha nao existe ou
esta velha (> 120 s) em pregao. A meta do /api/v3/cockpit e < 15 ms (uma consulta e um json.loads).

  cockpit(con)                         -> estado completo + idade_s/desatualizado
  regioes(con, ativo, horizonte)       -> parte de `regioes` (DAYTRADE) ou `swing` (SWING)
  opcoes_estruturas(con, ativo)        -> parte de `opcoes` (sem recalcular a superficie)
  estudo_regioes_resumo()              -> numeros do estudo V3-9 (dados/estudo_regioes.csv)

Convencoes do projeto: ts_ms = hora de Brasilia codificada como epoch UTC; `desatualizado` usa o
relogio local (relogio.local_br_ms) e o calendario_b3 para saber se e pregao.
"""
import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import calendario_b3 as cal
import relogio

ATIVOS = ("WIN$", "WDO$", "BOVA11")
HORIZONTES = ("DAYTRADE", "SWING")
IDADE_MAX_S = 120.0
INICIO_PREGAO_MIN = 9 * 60
FIM_PREGAO_MIN = 18 * 60 + 25
ARQ_ESTUDO = Path(__file__).resolve().parent / "dados" / "estudo_regioes.csv"
TEXTO_ESTUDO_PADRAO = ("heurística não validada: no estudo de 37 pregões as zonas não superaram "
                       "nível aleatório")


def normalizar_ativo(ativo):
    """Aceita WIN/WIN$/WDO/WDO$/BOVA11 e devolve a chave fixa do estado (ValueError se desconhecido)."""
    chave = {"WIN": "WIN$", "WIN$": "WIN$", "WDO": "WDO$", "WDO$": "WDO$", "BOVA11": "BOVA11"}.get(
        str(ativo or "").upper())
    if chave is None:
        raise ValueError(f"ativo invalido ({'|'.join(ATIVOS)})")
    return chave


def normalizar_horizonte(horizonte):
    """Aceita DAYTRADE/SWING (maiusculo ou minusculo) e devolve a chave fixa (ValueError)."""
    h = str(horizonte or "").upper()
    if h not in HORIZONTES:
        raise ValueError(f"horizonte invalido ({'|'.join(HORIZONTES)})")
    return h


def _em_pregao(agora_ms):
    """True em dia de pregao entre 09:00 e 18:25 (hora de Brasilia na convencao do banco)."""
    d = date(1970, 1, 1) + timedelta(days=int(agora_ms) // 86_400_000)
    try:
        if not cal.eh_pregao(d):
            return False
    except Exception:
        pass
    minuto = (int(agora_ms) % 86_400_000) // 60_000
    return INICIO_PREGAO_MIN <= minuto <= FIM_PREGAO_MIN


def ler_estado(con):
    """(ts_ms, dict, tempos) da linha unica; (None, None, {}) se a tabela/linha nao existir."""
    try:
        r = con.execute("SELECT ts_ms, json, tempos_json FROM estado_cockpit_v3 WHERE id=1").fetchone()
    except sqlite3.OperationalError:
        return None, None, {}
    if not r:
        return None, None, {}
    try:
        estado = json.loads(r[1])
    except (TypeError, ValueError):
        return int(r[0]), None, {}
    try:
        tempos = json.loads(r[2] or "{}")
    except (TypeError, ValueError):
        tempos = {}
    return int(r[0]), estado, tempos


def cockpit(con, agora_ms=None):
    """Estado completo do cockpit. Sem linha ou linha velha em pregao -> `desatualizado: True`.

    O JSON e devolvido como foi gravado (mais idade_s e desatualizado) para o endpoint responder
    em poucos milissegundos; nenhuma peca e recalculada aqui.
    """
    agora = int(agora_ms if agora_ms is not None else relogio.local_br_ms())
    ts, estado, tempos = ler_estado(con)
    if estado is None:
        return {"ts_ms": ts, "vazio": True, "idade_s": None, "desatualizado": True,
                "motivo": "sem linha em estado_cockpit_v3: o atualizador_cockpit.py nao rodou",
                "tempos": tempos}
    idade = max(0.0, (agora - ts) / 1000.0) if ts is not None else None
    desatualizado = bool(idade is None or (idade > IDADE_MAX_S and _em_pregao(agora)))
    estado = dict(estado)
    estado["ts_ms"] = ts if ts is not None else estado.get("ts_ms")
    estado["idade_s"] = round(idade, 1) if idade is not None else None
    estado["desatualizado"] = desatualizado
    if idade is not None and idade > IDADE_MAX_S:
        estado["motivo_idade"] = (f"estado de {idade:.0f} s atras"
                                  + (" (pregao: o atualizador pode estar parado)" if desatualizado else
                                     " (fora do pregao, normal)"))
    if tempos:
        estado["tempos"] = tempos
    return estado


def regioes(con, ativo, horizonte="DAYTRADE", agora_ms=None):
    """Zonas e contexto do ativo no horizonte pedido, direto do estado (sem recalcular).

    -> dict do estado (`regioes[ativo]` ou `swing[ativo]`) mais `desatualizado`/`idade_s`; quando
    o horizonte nao tem o ativo, devolve `zonas: []` e o motivo. `agora_ms` existe para os testes
    (o endpoint usa o relogio local).
    """
    ativo = normalizar_ativo(ativo)
    horizonte = normalizar_horizonte(horizonte)
    estado = cockpit(con, agora_ms)
    base = {k: estado.get(k) for k in ("ts_ms", "idade_s", "desatualizado", "vazio", "motivo")}
    if horizonte == "DAYTRADE":
        parte = (estado.get("regioes") or {}).get(ativo) or {}
        if not parte:
            return {**base, "ativo": ativo, "horizonte": horizonte, "zonas": [],
                    "motivo": f"sem reguas de {ativo} no estado"}
        return {**base, "ativo": ativo, "horizonte": horizonte, **parte}
    parte = (estado.get("swing") or {}).get(ativo) or {}
    if not parte:
        return {**base, "ativo": ativo, "horizonte": horizonte, "zonas": [],
                "invalidacao": (estado.get("swing") or {}).get("invalidacao") or [],
                "motivo": f"sem zonas de swing de {ativo} no estado (o cockpit calcula WIN$ e WDO$)"}
    return {**base, "ativo": ativo, "horizonte": horizonte, **parte,
            "invalidacao": (estado.get("swing") or {}).get("invalidacao") or []}


def opcoes_estruturas(con, ativo="BOVA11", agora_ms=None):
    """Diagnostico da superficie e estruturas calculadas, direto do estado (sem recalcular)."""
    ativo = str(ativo or "BOVA11").upper()
    if ativo != "BOVA11":
        raise ValueError("ativo invalido (BOVA11)")
    estado = cockpit(con, agora_ms)
    base = {k: estado.get(k) for k in ("ts_ms", "idade_s", "desatualizado", "vazio", "motivo")}
    parte = estado.get("opcoes") or {}
    if not parte:
        return {**base, "ativo": ativo, "motivo": "sem estruturas de opcoes no estado",
                "diagnostico": {}, "a_favor": [], "contra": []}
    return {**base, "ativo": ativo, **parte}


def _milhar(v):
    """Inteiro com ponto de milhar pt-BR (ex.: -1.067)."""
    txt = f"{abs(int(round(float(v)))):,}".replace(",", ".")
    return ("-" if float(v) < 0 else "") + txt


def estudo_regioes_resumo():
    """Numeros do estudo V3-9 (dados/estudo_regioes.csv) para a nota da regua.

    O CSV guarda uma linha por faixa x alinhamento x filtro x horizonte; a nota usa o horizonte
    de 15 min sem filtro de confirmacao e compara a zona com o nivel aleatorio de mesma distancia.
    Sem CSV, vale o texto fixo do estudo (37 pregoes, 24/09 a 17/11/2026).
    """
    out = {"n_pregoes": 37, "csv": False, "texto": TEXTO_ESTUDO_PADRAO, "detalhe": None, "faixas": {}}
    try:
        import pandas as pd
        df = pd.read_csv(ARQ_ESTUDO)
    except Exception:
        return out
    try:
        sub = df[(df["horizonte"] == 15) & (df["filtro"] == "sem_filtro")]
        faixas = {}
        for faixa in ("35-59", "60-79", "80+"):
            g = sub[sub["faixa"] == faixa]
            if g.empty:
                continue
            def _soma(col):
                v = g[col].dropna()
                return float(v.sum()) if len(v) else None
            faixas[faixa] = {"n_toques": int(g["n_toques"].sum()),
                             "zona_ret": _soma("zona_ret"), "rand_ret": _soma("rand_ret"),
                             "diff_ret": _soma("diff_ret")}
        if not faixas:
            return out
        out["csv"] = True
        out["faixas"] = faixas
        partes = []
        for faixa in ("60-79", "80+"):
            f = faixas.get(faixa)
            if f and f["diff_ret"] is not None:
                txt = _milhar(f["diff_ret"])
                partes.append(f"{faixa}: {'+' if f['diff_ret'] > 0 else ''}{txt} pts contra o aleatório")
        out["detalhe"] = ("CSV de dados/estudo_regioes.csv (15 min, sem filtro): " + "; ".join(partes)
                          if partes else None)
    except Exception:
        pass
    return out
