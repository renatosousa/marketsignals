"""Parser + analise da tabela de opcoes colada manualmente no dashboard (formato da tela de
opcoes de uma corretora: ticker, vencimento, strike, IV, gregas, e posicao em aberto por tipo
-- Coberto/Travado/Descoberto/Titulares/Lancadores, dado que a B3 publica mas que se mostrou
pouco confiavel de raspar via HTTP simples; ver posicoes_b3.py).

Aceita o texto exatamente como copiado da tela (colunas separadas por tab, cabecalho na 1a
linha). O mapeamento de colunas e por nome (normalizado), tolerante a pequenas variacoes.

Uso como biblioteca:
    from opcoes_manual import parsear, resumo, salvar
    df = parsear(texto_colado)
    r = resumo(df)
    salvar(db, df, r)
"""
import re
import unicodedata
from datetime import datetime

import numpy as np
import pandas as pd

SCHEMA = """
CREATE TABLE IF NOT EXISTS opcoes_manual_series (
    capturado_em TEXT NOT NULL, subjacente TEXT NOT NULL, symbol TEXT NOT NULL,
    vencimento TEXT, dias_uteis INTEGER, tipo TEXT, strike REAL, situacao TEXT, dist_pct REAL,
    ultimo REAL, var_pct REAL, num_neg INTEGER, vol_financeiro REAL, vol_impl_pct REAL,
    delta REAL, gamma REAL, theta_cifra REAL, theta_pct REAL, vega REAL,
    coberto REAL, travado REAL, descoberto REAL, titulares REAL, lancadores REAL,
    PRIMARY KEY (capturado_em, symbol)
);
CREATE TABLE IF NOT EXISTS opcoes_manual_resumo (
    capturado_em TEXT NOT NULL, subjacente TEXT NOT NULL, vencimento TEXT NOT NULL,
    atm_iv REAL, rr25 REAL, strike_ima REAL, concentracao_ima REAL,
    suporte REAL, resistencia REAL, PRIMARY KEY (capturado_em, subjacente, vencimento)
);
"""

# nome normalizado (sem acento/pontuacao, minusculo) -> campo interno
ALIAS = {
    "ticker": "symbol", "vencimento": "vencimento", "diasuteis": "dias_uteis", "tipo": "tipo",
    "strike": "strike", "aiotm": "situacao", "distdostrike": "dist_pct", "dist": "dist_pct",
    "ultimo": "ultimo", "var": "var_pct", "numdeneg": "num_neg", "volfinanceiro": "vol_financeiro",
    "volimpl": "vol_impl_pct", "delta": "delta", "gamma": "gamma", "theta": "theta_cifra",
    "vega": "vega", "coberto": "coberto", "travado": "travado", "descob": "descoberto",
    "tit": "titulares", "lanc": "lancadores",
}
NUM = {"ultimo", "var_pct", "dist_pct", "vol_financeiro", "vol_impl_pct", "delta", "gamma",
      "theta_cifra", "theta_pct", "vega", "coberto", "travado", "descoberto", "titulares",
      "lancadores", "num_neg", "dias_uteis", "strike"}


def _norm(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _num_br(v):
    """'1.234,56' -> 1234.56; '-8,22' -> -8.22; '' -> NaN."""
    v = (v or "").strip().replace("R$", "").strip()
    if not v or v in ("-", "—"):
        return np.nan
    v = v.replace(".", "").replace(",", ".")
    try:
        return float(v)
    except ValueError:
        return np.nan


def parsear(texto, subjacente="BOVA11"):
    """Texto colado (cabecalho na 1a linha nao vazia, colunas por TAB). -> DataFrame."""
    linhas = [l for l in texto.strip("\n").split("\n") if l.strip()]
    if not linhas:
        raise ValueError("texto vazio")
    cab = linhas[0].split("\t")
    # theta aparece 2x (Theta ($) e Theta (%)): o 2o mapeamento de "theta" vira theta_pct
    campos, visto_theta = [], False
    for h in cab:
        n = _norm(h)
        alvo = next((v for k, v in ALIAS.items() if n.startswith(k)), None)
        if alvo == "theta_cifra" and visto_theta:
            alvo = "theta_pct"
        if alvo == "theta_cifra":
            visto_theta = True
        campos.append(alvo)
    if "symbol" not in campos:
        raise ValueError("cabecalho nao reconhecido (coluna 'Ticker' nao encontrada); confira se "
                          "colou a tabela inteira, com o cabecalho, separada por TAB")

    registros = []
    for l in linhas[1:]:
        cel = l.split("\t")
        if len(cel) < len(campos) - 3:  # linha claramente incompleta
            continue
        reg = {}
        for campo, valor in zip(campos, cel):
            if campo is None:
                continue
            reg[campo] = _num_br(valor) if campo in NUM else valor.strip()
        if reg.get("symbol"):
            registros.append(reg)
    if not registros:
        raise ValueError("nenhuma linha de dados reconhecida")

    df = pd.DataFrame(registros)
    df["subjacente"] = subjacente
    df["tipo"] = df["tipo"].str.upper().str[0].map({"C": "C", "P": "P"}).fillna(df.get("tipo"))
    if "vol_impl_pct" in df:
        df["vol_impl_pct"] = df["vol_impl_pct"] / 100
    if "var_pct" in df:
        df["var_pct"] = df["var_pct"] / 100
    if "dist_pct" in df:
        df["dist_pct"] = df["dist_pct"] / 100
    return df


def _mais_proximo(df, alvo_delta):
    d = df.dropna(subset=["delta", "vol_impl_pct"])
    if d.empty:
        return None
    i = (d["delta"] - alvo_delta).abs().idxmin()
    return d.loc[i] if abs(d.loc[i, "delta"] - alvo_delta) <= 0.10 else None


def resumo(df):
    """Por vencimento: ATM IV, RR25, strike de maior concentracao (imã), suporte/resistencia
    (strikes com maior posicao a descoberto do lado put/call, respectivamente)."""
    out = {}
    for venc, g in df.groupby("vencimento"):
        c, p = g[g.tipo == "C"], g[g.tipo == "P"]
        # ATM: strike com |dist_pct| minimo, media da IV de call e put nesse strike
        if g["dist_pct"].notna().any():
            k_atm = g.loc[g["dist_pct"].abs().idxmin(), "strike"]
            atm_iv = g.loc[g["strike"] == k_atm, "vol_impl_pct"].mean()
        else:
            atm_iv = None
        c25, p25 = _mais_proximo(c, 0.25), _mais_proximo(p, -0.25)
        rr25 = (c25["vol_impl_pct"] - p25["vol_impl_pct"]) if c25 is not None and p25 is not None else None

        g = g.copy()
        g["oi_total"] = g[["coberto", "travado", "descoberto"]].sum(axis=1, min_count=1)
        por_strike = g.groupby("strike")["oi_total"].sum().dropna()
        strike_ima = por_strike.idxmax() if len(por_strike) else None
        concentracao = (por_strike.max() / por_strike.drop(strike_ima).max()
                        if strike_ima is not None and len(por_strike) > 1 and por_strike.drop(strike_ima).max()
                        else None)

        suporte = p.loc[p["descoberto"].idxmax(), "strike"] if p["descoberto"].notna().any() else None
        resistencia = c.loc[c["descoberto"].idxmax(), "strike"] if c["descoberto"].notna().any() else None

        out[venc] = dict(atm_iv=none_ou(atm_iv), rr25=none_ou(rr25), strike_ima=none_ou(strike_ima),
                         concentracao_ima=none_ou(concentracao), suporte=none_ou(suporte),
                         resistencia=none_ou(resistencia),
                         por_strike=[dict(strike=float(k), call=float(g.loc[(g.strike == k) & (g.tipo == "C"),
                                                                              "oi_total"].sum()),
                                          put=float(g.loc[(g.strike == k) & (g.tipo == "P"), "oi_total"].sum()))
                                    for k in sorted(por_strike.index)])
    return out


def none_ou(v):
    return None if v is None or (isinstance(v, float) and np.isnan(v)) else (
        float(v) if isinstance(v, (int, float, np.floating)) else v)


def salvar(db, df, resumos, subjacente="BOVA11"):
    ts = datetime.now().isoformat(timespec="seconds")
    cols = ["symbol", "vencimento", "dias_uteis", "tipo", "strike", "situacao", "dist_pct", "ultimo",
           "var_pct", "num_neg", "vol_financeiro", "vol_impl_pct", "delta", "gamma", "theta_cifra",
           "theta_pct", "vega", "coberto", "travado", "descoberto", "titulares", "lancadores"]
    linhas = [(ts, subjacente, *(r.get(c) for c in cols)) for r in df.to_dict("records")]
    db.executemany(f"INSERT OR REPLACE INTO opcoes_manual_series VALUES (?,?,{','.join('?' * len(cols))})",
                   linhas)
    db.executemany("INSERT OR REPLACE INTO opcoes_manual_resumo VALUES (?,?,?,?,?,?,?,?,?)",
                   [(ts, subjacente, venc, r["atm_iv"], r["rr25"], r["strike_ima"], r["concentracao_ima"],
                     r["suporte"], r["resistencia"]) for venc, r in resumos.items()])
    db.commit()
    return ts
