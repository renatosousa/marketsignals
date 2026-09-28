"""Regime do WIN ao vivo -> tabela regime_live (1 linha por minuto), para o dashboard e o Jev.

Na partida (08:55, pelo supervisor) atualiza as barras recentes e treina o modelo do dia
(modelo_regime.py). Depois, a cada minuto, le do banco o WIN (snapshots), o dolar e o DI
(macro_cotacoes) e o VIX (IBKR, aceito com 15 min de atraso por ser medida lenta) e grava:

  rv_60 / rv_norm / rv_rel  vol da ultima hora (%), normal do horario e a razao
  er_60, rotulo             eficiencia da ultima hora e rotulo (vol x tendencia/lateral)
  rv_prev_1h, faixa_1h_pts  vol prevista p/ a proxima hora e o equivalente em pontos (1 desvio)
  rv_dia_prev, rv_dia_real  vol prevista p/ o dia (na abertura) e a realizada ate agora
  vix, di29_var_bps, wdo_rv_rel, alertas (JSON)

Uso: python coletor_regime.py [GRUPO] [SEGUNDOS] [--db dados/book.db] [--intervalo 60] [--sem-treino]
"""
import argparse
import json
import sqlite3
import time
from datetime import datetime

import numpy as np
import pandas as pd

import banco
import modelo_regime as mr
import regimes as rg
from relogio import local_br_ms

SCHEMA = """
CREATE TABLE IF NOT EXISTS regime_live (
    ts_ms INTEGER NOT NULL, sessao TEXT NOT NULL, preco REAL,
    rv_60 REAL, rv_norm REAL, rv_rel REAL, er_60 REAL, rotulo TEXT,
    rv_prev_1h REAL, faixa_1h_pts REAL, rv_dia_prev REAL, rv_dia_real REAL,
    vix REAL, vix_idade_min REAL, di29_var_bps REAL, wdo_rv_rel REAL, alertas TEXT, modelo TEXT,
    PRIMARY KEY (sessao, ts_ms)
);
CREATE INDEX IF NOT EXISTS ix_regime_ts ON regime_live (ts_ms);
"""
VIX_ALTO = 20.0     # no estudo, acima disso o WIN oscila ~5% alem do proprio historico
ATRASO_IBKR_MIN = 15  # atraso dos dados da IBKR sem assinatura (indices Cboe)


def minutos(db, symbol, tabela, col, t0, t1, filtro=""):
    """Precos de 1 min (ultimo de cada minuto) de uma tabela ao vivo."""
    q = f"SELECT ts_ms, {col} FROM {tabela} WHERE symbol=? AND ts_ms BETWEEN ? AND ? {filtro} ORDER BY ts_ms"
    df = pd.read_sql(q, db, params=(symbol, t0, t1))
    if df.empty:
        return pd.Series(dtype=float)
    s = pd.Series(df[col].to_numpy(), index=pd.to_datetime(df["ts_ms"], unit="ms"))
    return s.resample("1min", label="left").last()


def ultimo_valor(db, sql, *p):
    r = db.execute(sql, p).fetchone()
    return (r[0], r[1]) if r and r[0] is not None else (None, None)


def estado(db, modelo, agora_ms):
    t = pd.Timestamp(agora_ms, unit="ms").floor("1min")
    hoje = t.normalize()
    ini = int((hoje + pd.Timedelta(rg.INICIO + ":00")).value // 10**6)
    win = minutos(db, "WIN$", "snapshots", "mid", ini, agora_ms, "AND spread > 0")
    if win.dropna().empty:
        return None
    g = rg.grade(hoje, rg.INICIO, rg.FIM)
    pw = rg.na_grade(win.dropna(), g[g < t])
    preco = float(win.dropna().iloc[-1])
    pas = pw[t - pd.Timedelta("60min"):]
    ant = pw[t - pd.Timedelta("120min"):t - pd.Timedelta("61min")]
    valido = lambda v: None if v is None or (isinstance(v, float) and np.isnan(v)) else v
    rv_60 = valido(rg.rv(pas)) if pas.notna().sum() >= 45 else None
    rv_ant = valido(rg.rv(ant)) if ant.notna().sum() >= 30 else None
    er_60 = valido(rg.er(pas)) if rv_60 else None
    sl = mr.slot_de(t) or mr.SLOTS[0]
    rv_norm = modelo["normal_win"][sl]
    rv_rel = rv_60 / rv_norm if rv_60 else None

    # VIX: o mais recente gravado pela IBKR (atrasado serve); senao, ultima barra historica
    r = db.execute("SELECT mid, ts_ms, grupo FROM macro_cotacoes WHERE symbol='IB:VIX' "
                   "ORDER BY ts_ms DESC LIMIT 1").fetchone()
    vix, ts_vix, atraso = (r[0], r[1], ATRASO_IBKR_MIN if r[2] == "internacional_atrasado" else 0) if r else (None,) * 3
    if vix is None or agora_ms - ts_vix > 3 * 86400_000:
        vix, ts_vix = ultimo_valor(db, "SELECT close, ts_ms FROM barras_1m WHERE symbol='IB:VIX' "
                                       "ORDER BY ts_ms DESC LIMIT 1")
        atraso = 0
    # ts do coletor e a hora de CHEGADA; dado atrasado da IBKR ja chega ~15 min velho
    vix_idade = (agora_ms - ts_vix) / 60000 + atraso if ts_vix else None

    # DI1F29 hoje x fechamento anterior (pontos-base)
    di_hoje, _ = ultimo_valor(db, "SELECT mid, ts_ms FROM macro_cotacoes WHERE symbol='DI1F29' AND ts_ms >= ? "
                                  "ORDER BY ts_ms DESC LIMIT 1", ini)
    di_ant, _ = ultimo_valor(db, "SELECT close, ts_ms FROM barras_1m WHERE symbol='DI1F29' AND ts_ms < ? "
                                 "ORDER BY ts_ms DESC LIMIT 1", int(hoje.value // 10**6))
    di_var = (di_hoje - di_ant) * 100 if di_hoje is not None and di_ant is not None else None

    # dolar: vol da ultima hora x normal do horario
    wdo = minutos(db, "WDO$", "macro_cotacoes", "mid", int((t - pd.Timedelta("61min")).value // 10**6), agora_ms)
    wdo_rv = rg.rv(rg.na_grade(wdo.dropna(), pas.index)) if len(wdo.dropna()) >= 30 else None
    wdo_rel = wdo_rv / modelo["normal_wdo"][sl] if wdo_rv else None

    rv_prev = mr.prever_hora(modelo, t, rv_60, rv_ant, vix)
    rv_dia_real = rg.rv(pw)

    alertas = []
    if vix and vix >= VIX_ALTO:
        alertas.append(dict(nivel="serio", texto=f"VIX {vix:.1f} (acima de {VIX_ALTO:.0f}): mercado externo em "
                                                   "aversao a risco; vol do WIN tende a ficar acima do historico"))
    if di_var is not None and abs(di_var) >= modelo["di29_abs_pct"]["80"]:
        nivel = "serio" if abs(di_var) >= modelo["di29_abs_pct"]["95"] else "atencao"
        alertas.append(dict(nivel=nivel, texto=f"DI1F29 {di_var:+.0f} bps no dia: movimento de juros atipico "
                                                f"(maior que {'95' if nivel == 'serio' else '80'}% dos dias)"))
    if wdo_rel and wdo_rel >= 1.5:
        alertas.append(dict(nivel="atencao", texto=f"Dolar oscilando {wdo_rel:.1f}x o normal do horario"))
    if rv_rel and rv_rel >= 1.5:
        alertas.append(dict(nivel="atencao", texto=f"WIN oscilando {rv_rel:.1f}x o normal do horario"))
    if rv_prev and rv_60 and rv_prev / rv_60 >= 1.2:
        alertas.append(dict(nivel="atencao", texto="Modelo espera volatilidade maior na proxima hora"))

    return dict(ts_ms=int(t.value // 10**6), preco=preco, rv_60=rv_60, rv_norm=rv_norm, rv_rel=rv_rel, er_60=er_60,
                rotulo=rg.regime_nome(rv_rel, er_60) if rv_rel and er_60 is not None else "aguardando 1a hora",
                rv_prev_1h=rv_prev, faixa_1h_pts=rv_prev / 100 * preco if rv_prev else None,
                rv_dia_prev=mr.prever_dia(modelo, t), rv_dia_real=rv_dia_real, vix=vix, vix_idade_min=vix_idade,
                di29_var_bps=di_var, wdo_rv_rel=wdo_rel, alertas=json.dumps(alertas, ensure_ascii=False),
                modelo=modelo["treinado_em"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("grupo", nargs="?", default="REGIME")
    ap.add_argument("segundos", nargs="?", type=float, default=60.0)
    ap.add_argument("--db", default="dados/book.db")
    ap.add_argument("--intervalo", type=float, default=60.0)
    ap.add_argument("--sem-treino", action="store_true")
    args = ap.parse_args()

    modelo = mr.carregar_modelo()
    hoje = datetime.now().strftime("%Y-%m-%d")
    if not args.sem_treino and (modelo is None or not modelo["treinado_em"].startswith(hoje)):
        print("treinando o modelo do dia...", flush=True)
        mr.atualizar_barras(args.db)
        con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
        modelo = mr.treinar(con)
        con.close()
        mr.ARQ.write_text(json.dumps(modelo, ensure_ascii=False, indent=1), encoding="utf-8")
    if modelo is None:
        raise SystemExit("sem modelo: rode modelo_regime.py")
    print(f"modelo de {modelo['treinado_em']} ({modelo['pregoes']} pregoes)", flush=True)

    db = banco.abrir(args.db, SCHEMA)
    sessao = datetime.now().strftime("%Y%m%d_%H%M%S")
    fim = time.time() + args.segundos if args.segundos > 0 else float("inf")
    n = 0
    while time.time() < fim:
        try:
            e = estado(db, modelo, int(local_br_ms()))
            if e:
                db.execute(f"INSERT OR REPLACE INTO regime_live ({','.join(['sessao'] + list(e))}) "
                           f"VALUES ({','.join('?' * (len(e) + 1))})", [sessao] + list(e.values()))
                db.commit()
                n += 1
                print(f"[{pd.Timestamp(e['ts_ms'], unit='ms'):%H:%M}] {e['rotulo']} | proxima hora "
                      f"{'%.0f pts' % e['faixa_1h_pts'] if e['faixa_1h_pts'] else '-'} | "
                      f"{len(json.loads(e['alertas']))} alertas", flush=True)
        except sqlite3.OperationalError as ex:  # banco ocupado: tenta no proximo minuto
            db.rollback()
            print(f"aviso: {ex}", flush=True)
        time.sleep(max(1.0, args.intervalo - time.time() % args.intervalo))  # alinha ao minuto
    db.close()
    print(f"linhas gravadas: {n}", flush=True)


if __name__ == "__main__":
    main()
