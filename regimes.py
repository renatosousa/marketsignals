"""Metricas de regime e contexto macro a partir de barras de 1 min (tabela barras_1m).

Regime do WIN numa janela:
  rv   volatilidade realizada: raiz da soma dos log-retornos de 1 min ao quadrado, em %
  er   eficiencia (tendencia x vai-e-vem): |deslocamento| / soma dos |movimentos|, em barras de
       5 min; ~0 = lateral, ~1 = tendencia limpa
  ret  retorno da janela, em %

Horarios em Brasilia (convencao do banco). Tudo aqui usa so dados ate o instante de referencia,
para servir tanto ao estudo historico quanto ao painel ao vivo.
"""
import numpy as np
import pandas as pd

ABERTURA, INICIO, FIM, FECHAMENTO = "09:00", "09:05", "18:15", "18:25"
TOLERANCIA = pd.Timedelta("5min")


def carregar(con, symbol, coluna="close"):
    df = pd.read_sql(f"SELECT ts_ms, {coluna} FROM barras_1m WHERE symbol=? ORDER BY ts_ms", con, params=(symbol,))
    s = pd.Series(df[coluna].to_numpy(), index=pd.to_datetime(df["ts_ms"], unit="ms"), name=symbol)
    return s[~s.index.duplicated()]


def grade(dia, ini=ABERTURA, fim=FECHAMENTO):
    d = pd.Timestamp(dia).normalize()
    return pd.date_range(d + pd.Timedelta(ini + ":00"), d + pd.Timedelta(fim + ":00"), freq="1min")


def na_grade(s, g):
    """Preco na grade de 1 min, repetindo o ultimo por ate 5 min (minuto sem negocio)."""
    return s.reindex(g, method="ffill", tolerance=TOLERANCIA)


def preco_em(s, ts, max_idade="30min"):
    """Ultimo preco ate ts, se nao for mais velho que max_idade."""
    i = s.index.searchsorted(ts, side="right") - 1
    if i < 0 or ts - s.index[i] > pd.Timedelta(max_idade):
        return np.nan
    return s.iloc[i]


def lret(a, b):
    return np.log(b / a) * 100 if a and b and a > 0 and b > 0 else np.nan


def rv(p):
    """p: precos de 1 min (pode ter NaN). -> vol realizada em %."""
    r = np.log(p).diff().dropna()
    return float(np.sqrt((r ** 2).sum()) * 100) if len(r) >= 10 else np.nan


def er(p):
    q = p.dropna()
    if len(q) < 15:
        return np.nan
    c = q.iloc[::5]
    mov = c.diff().abs().sum()
    return float(abs(c.iloc[-1] - c.iloc[0]) / mov) if mov > 0 else np.nan


def ret(p):
    q = p.dropna()
    return lret(q.iloc[0], q.iloc[-1]) if len(q) >= 2 else np.nan


def corr_ret(a, b):
    ra, rb = np.log(a).diff(), np.log(b).diff()
    m = ra.notna() & rb.notna()
    if m.sum() < 20 or ra[m].std() == 0 or rb[m].std() == 0:
        return np.nan
    return float(np.corrcoef(ra[m], rb[m])[0, 1])


def regime_nome(rv_rel, er_valor):
    """Rotulo simples: vol relativa (vs. normal do horario) x eficiencia."""
    vol = "vol alta" if rv_rel > 1.25 else "vol baixa" if rv_rel < 0.8 else "vol normal"
    tipo = "tendencia" if er_valor >= 0.45 else "lateral" if er_valor <= 0.2 else "misto"
    return f"{vol}, {tipo}"
