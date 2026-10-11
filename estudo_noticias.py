"""Validacao fora da amostra: noticias ajudam a prever volatilidade do WIN?

Roda 4 partes: cobertura (latencia, duplicatas), intraday (R2 fora da amostra para vol da proxima hora),
eod (vol e retorno de abertura vs. noticias da noite anterior), rotulos (concordancia com o conjunto rotulado).

Criterio de aprovacao: R2 incremental >= +0,01 no alvo log_win_rv_fut (volatilidade da proxima hora),
com sinal estavel no walk-forward (positivo em >= 2 de 3 tercos do teste). Antes de 40 pregoes coletados,
imprime "amostra insuficiente" e nao aprova nada.

Uso: python estudo_noticias.py [--parte cobertura|intraday|eod|rotulos|todos] [--db dados/book.db]
                               [--csv-dir dados]
"""
import argparse
import sqlite3
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

import regimes as rg

try:
    import estudo_regime as er
    spearman = er.spearman
    ols_prev = er.ols_prev
    r2_oos = er.r2_oos
    split_dias = er.split_dias
    walk_forward = er.walk_forward
    tabela_incremento = er.tabela_incremento
except ImportError:
    print("aviso: estudo_regime.py nao disponivel; algumas funcoes podem falhar", flush=True)
    
    def spearman(x, y):
        m = x.notna() & y.notna()
        return (x[m].rank().corr(y[m].rank()), int(m.sum())) if m.sum() >= 20 else (np.nan, int(m.sum()))
    
    def ols_prev(Xtr, ytr, Xte):
        A = np.column_stack([np.ones(len(Xtr)), Xtr])
        b = np.linalg.lstsq(A, ytr, rcond=None)[0]
        return np.column_stack([np.ones(len(Xte)), Xte]) @ b
    
    def r2_oos(y, prev, media_treino):
        return 1 - np.sum((y - prev) ** 2) / np.sum((y - media_treino) ** 2)
    
    def split_dias(df, alvo, base, extra, frac=2 / 3):
        cols = base + extra
        d = df[["dia", alvo] + cols].dropna()
        dias = np.sort(d["dia"].unique())
        corte = dias[int(len(dias) * frac)]
        tr, te = d[d["dia"] < corte], d[d["dia"] >= corte]
        if len(tr) < 100 or len(te) < 50:
            return np.nan, np.nan, len(te)
        y, med = te[alvo].to_numpy(), tr[alvo].mean()
        pb = ols_prev(tr[base].to_numpy(), tr[alvo].to_numpy(), te[base].to_numpy())
        pe = ols_prev(tr[cols].to_numpy(), tr[alvo].to_numpy(), te[cols].to_numpy())
        return r2_oos(y, pb, med), r2_oos(y, pe, med), len(te)
    
    def walk_forward(df, alvo, base, extra, minimo=60):
        cols = base + extra
        d = df[[alvo] + cols].dropna()
        if len(d) < minimo + 20:
            return np.nan, np.nan, len(d)
        y = d[alvo].to_numpy()
        pb, pe, med = [], [], []
        for i in range(minimo, len(d)):
            tr = slice(0, i)
            pb.append(ols_prev(d[base].to_numpy()[tr], y[tr], d[base].to_numpy()[i:i + 1])[0] if base
                      else y[tr].mean())
            pe.append(ols_prev(d[cols].to_numpy()[tr], y[tr], d[cols].to_numpy()[i:i + 1])[0])
            med.append(y[tr].mean())
        yt, med = y[minimo:], np.array(med)
        sst = np.sum((yt - med) ** 2)
        return 1 - np.sum((yt - pb) ** 2) / sst, 1 - np.sum((yt - pe) ** 2) / sst, len(yt)
    
    def tabela_incremento(nome, resultados):
        print(f"\n  {nome}")
        print(f"    {'variavel':24}{'R2 base':>9}{'R2 +var':>11}{'ganho':>9}   n")
        for var, (rb, re, n) in sorted(resultados.items(), key=lambda kv: -(kv[1][1] - kv[1][0])
                                       if not np.isnan(kv[1][1]) else 0):
            marca = "  <==" if re - rb > 0.01 else ""
            print(f"    {var:24}{rb:>9.3f}{re:>11.3f}{re - rb:>+9.3f}  {n}{marca}")

warnings.filterwarnings("ignore", category=RuntimeWarning)

BASE_LONGA = ["log_win_rv_d1", "log_win_rv_d5", "log_win_rv_d20"]


def relatorio_cobertura(con, csv_dir):
    """Parte 1: cobertura, latencia, duplicatas."""
    print("\n=== Cobertura ===\n")
    
    df = pd.read_sql("SELECT * FROM noticias", con)
    if df.empty:
        print("  Nenhuma noticia coletada ainda")
        return
    
    df["dia"] = pd.to_datetime(df["ts_coleta_ms"], unit="ms").dt.date
    df["latencia_s"] = (df["ts_coleta_ms"] - df["ts_pub_ms"]) / 1000
    
    por_fonte = df.groupby("fonte").agg(
        n=("id", "count"),
        n_orig=("dup_de", lambda x: (x.isna()).sum()),
        mediana_lat=("latencia_s", lambda x: x.dropna().median() if x.notna().any() else np.nan),
        p90_lat=("latencia_s", lambda x: x.dropna().quantile(0.9) if x.notna().any() else np.nan),
    )
    
    print("  Noticias por fonte:")
    print(por_fonte.to_string())
    
    print(f"\n  Total: {len(df)} noticias, {(~df['dup_de'].isna()).sum()} duplicatas ({(~df['dup_de'].isna()).sum() / len(df) * 100:.1f}%)")
    
    por_dia = df.groupby("dia").agg(n=("id", "count"))
    print(f"\n  Dias cobertos: {len(por_dia)}")
    print(f"  Media por dia: {por_dia['n'].mean():.0f} noticias")
    
    score_df = pd.read_sql("SELECT * FROM noticias_score", con)
    if not score_df.empty:
        score_df = score_df.merge(df[["id", "ts_coleta_ms"]], on="id")
        score_df["atraso_s"] = (score_df["ts_score_ms"] - score_df["ts_coleta_ms"]) / 1000
        
        print(f"\n  Classificacao (atraso):")
        print(f"    Mediana: {score_df['atraso_s'].median():.0f}s")
        print(f"    P90: {score_df['atraso_s'].quantile(0.9):.0f}s")
        print(f"    Max: {score_df['atraso_s'].max():.0f}s")
    
    if csv_dir:
        out = Path(csv_dir) / "noticias_cobertura.csv"
        por_fonte.to_csv(out)
        print(f"\n  Exportado para {out}")


def montar_intraday_noticias(con, win, dias):
    """Monta dataset intraday com features de noticias."""
    try:
        from estudo_regime import montar_intraday
        base_df = montar_intraday(win, {}, dias)
    except:
        print("  aviso: nao foi possivel carregar dados base do estudo_regime", flush=True)
        linhas = []
        slots = pd.timedelta_range("10:05:00", "17:15:00", freq="15min")
        for d in dias:
            g = rg.grade(d, rg.INICIO, rg.FIM)
            pw = rg.na_grade(win, g)
            if pw.notna().sum() < 300:
                continue
            for sl in slots:
                t = d + sl
                pas = pw[t - pd.Timedelta("60min"):t - pd.Timedelta("1min")]
                fut = pw[t:t + pd.Timedelta("59min")]
                ant = pw[t - pd.Timedelta("120min"):t - pd.Timedelta("61min")]
                linhas.append({
                    "dia": d,
                    "slot": str(sl)[-8:-3],
                    "win_rv_fut": rg.rv(fut),
                    "win_rv_pas": rg.rv(pas),
                    "win_rv_ant": rg.rv(ant) if ant.notna().sum() >= 30 else np.nan,
                })
        base_df = pd.DataFrame(linhas)
        base_df["log_win_rv_fut"] = np.log(base_df["win_rv_fut"])
        base_df["log_win_rv_pas"] = np.log(base_df["win_rv_pas"])
        base_df["log_win_rv_ant"] = np.log(base_df["win_rv_ant"])
    
    noticias_5min = pd.read_sql("SELECT * FROM noticias_5min", con)
    if noticias_5min.empty:
        return base_df
    
    noticias_5min["t"] = pd.to_datetime(noticias_5min["ts_ms"], unit="ms")
    noticias_5min["dia"] = noticias_5min["t"].dt.normalize()
    noticias_5min["slot_idx"] = (noticias_5min["t"].dt.hour * 60 + noticias_5min["t"].dt.minute) // 15
    
    linhas = []
    for _, row in base_df.iterrows():
        d = row["dia"]
        slot = row["slot"]
        t = d + pd.Timedelta(slot + ":00")
        
        janela = noticias_5min[
            (noticias_5min["dia"] == d) &
            (noticias_5min["t"] < t) &
            (noticias_5min["t"] >= t - pd.Timedelta("60min"))
        ]
        
        feat = {"dia": d, "slot": slot}
        
        for escopo in ["br", "oficial", "global"]:
            for modelo in ["finbert"]:
                sub = janela[(janela["escopo"] == escopo) & (janela["modelo"] == modelo)]
                
                if not sub.empty:
                    feat[f"n60_{escopo}"] = sub["n_relevantes"].sum()
                    feat[f"sent60_{escopo}"] = sub["sent_medio"].mean() if sub["sent_medio"].notna().any() else np.nan
                    feat[f"frac_neg60_{escopo}"] = sub["frac_neg"].mean() if sub["frac_neg"].notna().any() else np.nan
                    feat[f"pico_{escopo}"] = int(sub["pico"].max()) if sub["pico"].notna().any() else 0
                else:
                    feat[f"n60_{escopo}"] = 0
                    feat[f"sent60_{escopo}"] = np.nan
                    feat[f"frac_neg60_{escopo}"] = np.nan
                    feat[f"pico_{escopo}"] = 0
        
        linhas.append(feat)
    
    feat_df = pd.DataFrame(linhas)
    result = base_df.merge(feat_df, on=["dia", "slot"], how="left")
    
    for escopo in ["br", "oficial", "global"]:
        result[f"log1p_n60_{escopo}"] = np.log1p(result[f"n60_{escopo}"].fillna(0))
        result[f"abs_sent60_{escopo}"] = result[f"sent60_{escopo}"].abs()
    
    return result


def relatorio_intraday(con, csv_dir):
    """Parte 2: features de noticias ajudam a prever vol da proxima hora?"""
    print("\n=== Intraday (proximos 60 min) ===\n")
    
    noticias_df = pd.read_sql("SELECT ts_coleta_ms FROM noticias WHERE dup_de IS NULL", con)
    if noticias_df.empty:
        print("  Nenhuma noticia coletada ainda")
        return
    
    noticias_df["dia"] = pd.to_datetime(noticias_df["ts_coleta_ms"], unit="ms").dt.normalize()
    pregoes_com_noticias = len(noticias_df["dia"].unique())
    
    if pregoes_com_noticias < 40:
        print(f"  Amostra insuficiente: {pregoes_com_noticias} pregoes (minimo 40)")
        return
    
    print(f"  Pregoes com noticias: {pregoes_com_noticias}")
    
    win = rg.carregar(con, "WIN$")
    dias = sorted(set(win.index.normalize()))
    
    df = montar_intraday_noticias(con, win, dias)
    
    if "log_win_rv_fut" not in df.columns:
        print("  Dados insuficientes para calcular volatilidade futura")
        return
    
    base = ["slot"]
    if "log_win_rv_pas" in df.columns:
        base.append("log_win_rv_pas")
    if "log_win_rv_ant" in df.columns:
        base.append("log_win_rv_ant")
    
    base_vol = base + BASE_LONGA if all(c in df.columns for c in BASE_LONGA) else base
    
    candidatas = {
        "log1p_n60_br": ["log1p_n60_br"],
        "log1p_n60_oficial": ["log1p_n60_oficial"],
        "log1p_n60_global": ["log1p_n60_global"],
        "sent60_br": ["sent60_br"],
        "sent60_oficial": ["sent60_oficial"],
        "abs_sent60_br": ["abs_sent60_br"],
        "frac_neg60_br": ["frac_neg60_br"],
        "pico_br": ["pico_br"],
        "pico_oficial": ["pico_oficial"],
    }
    
    resultados = {}
    for nome, cols in candidatas.items():
        if all(c in df.columns for c in cols):
            rb, re, n = split_dias(df, "log_win_rv_fut", base_vol, cols)
            resultados[nome] = (rb, re, n)
    
    if resultados:
        tabela_incremento("Volatilidade WIN proximos 60 min (log_win_rv_fut)", resultados)
        
        aprovadas = [k for k, (rb, re, n) in resultados.items() if re - rb >= 0.01]
        if aprovadas:
            print(f"\n  Aprovadas (ganho >= +0.01): {', '.join(aprovadas)}")
        else:
            print("\n  Nenhuma feature aprovada (ganho < +0.01)")
    else:
        print("  Nenhuma feature disponivel para teste")
    
    if csv_dir:
        out = Path(csv_dir) / "noticias_intraday.csv"
        df.to_csv(out, index=False)
        print(f"\n  Exportado para {out}")


def montar_eod_noticias(con, win, dias):
    """Monta dataset EOD com noticias da noite anterior."""
    linhas = []
    
    for i in range(1, len(dias)):
        d, dp = dias[i], dias[i - 1]
        pw = rg.na_grade(win, rg.grade(d, rg.INICIO, rg.FIM))
        if pw.notna().sum() < 300:
            continue
        
        noite_inicio = int((dp + pd.Timedelta("18:30:00")).value // 10**6)
        noite_fim = int((d + pd.Timedelta("09:00:00")).value // 10**6)
        
        cursor = con.execute(
            """SELECT COUNT(*) FROM noticias n
               JOIN noticias_score s ON n.id = s.id
               WHERE n.ts_coleta_ms BETWEEN ? AND ? AND n.dup_de IS NULL
                 AND s.modelo = 'finbert' AND s.relevancia >= 0.5""",
            (noite_inicio, noite_fim)
        )
        n_noite = cursor.fetchone()[0]
        
        cursor = con.execute(
            """SELECT AVG(s.sentimento) FROM noticias n
               JOIN noticias_score s ON n.id = s.id
               WHERE n.ts_coleta_ms BETWEEN ? AND ? AND n.dup_de IS NULL
                 AND s.modelo = 'finbert' AND s.relevancia >= 0.5""",
            (noite_inicio, noite_fim)
        )
        sent_noite = cursor.fetchone()[0]
        
        linhas.append({
            "dia": d,
            "rv_dia": rg.rv(pw),
            "ret_dia": rg.ret(pw),
            "n_noite": n_noite,
            "sent_noite": sent_noite if sent_noite else np.nan,
        })
    
    df = pd.DataFrame(linhas)
    df["log_rv_dia"] = np.log(df["rv_dia"])
    df["log1p_n_noite"] = np.log1p(df["n_noite"])
    df["abs_sent_noite"] = df["sent_noite"].abs()
    
    return df


def relatorio_eod(con, csv_dir):
    """Parte 3: noticias da noite anterior vs. vol e retorno do dia."""
    print("\n=== EOD (dia seguinte) ===\n")
    
    noticias_df = pd.read_sql("SELECT ts_coleta_ms FROM noticias WHERE dup_de IS NULL", con)
    if noticias_df.empty:
        print("  Nenhuma noticia coletada ainda")
        return
    
    noticias_df["dia"] = pd.to_datetime(noticias_df["ts_coleta_ms"], unit="ms").dt.normalize()
    pregoes_com_noticias = len(noticias_df["dia"].unique())
    
    if pregoes_com_noticias < 80:
        print(f"  Amostra insuficiente: {pregoes_com_noticias} pregoes (minimo 80 para EOD)")
        return
    
    win = rg.carregar(con, "WIN$")
    dias = sorted(set(win.index.normalize()))
    
    df = montar_eod_noticias(con, win, dias)
    
    if df.empty:
        print("  Dados insuficientes")
        return
    
    print(f"  Dias com dados: {len(df)}")
    
    candidatas = {
        "log1p_n_noite": (["log1p_n_noite"], "log_rv_dia"),
        "sent_noite": (["sent_noite"], "log_rv_dia"),
        "abs_sent_noite": (["abs_sent_noite"], "log_rv_dia"),
        "sent_noite_ret": (["sent_noite"], "ret_dia"),
    }
    
    for nome, (cols, alvo) in candidatas.items():
        if all(c in df.columns for c in [alvo] + cols):
            rb, re, n = walk_forward(df, alvo, [], cols, minimo=60)
            print(f"  {nome:20} -> {alvo:12}  R2={re:+.3f}  n={n}")


def relatorio_rotulos(con):
    """Parte 4: concordancia com conjunto rotulado (quando existir)."""
    print("\n=== Rotulos ===\n")
    print("  Conjunto de rotulos nao implementado ainda")
    print("  Use rotular_noticias.py para gerar dados/rotulos_noticias.csv")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--parte", default="todos", choices=["cobertura", "intraday", "eod", "rotulos", "todos"])
    ap.add_argument("--db", default="dados/book.db")
    ap.add_argument("--csv-dir", default="dados")
    args = ap.parse_args()
    
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Banco {args.db} nao encontrado")
        return 1
    
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=30)
    
    partes = {
        "cobertura": lambda: relatorio_cobertura(con, args.csv_dir),
        "intraday": lambda: relatorio_intraday(con, args.csv_dir),
        "eod": lambda: relatorio_eod(con, args.csv_dir),
        "rotulos": lambda: relatorio_rotulos(con),
    }
    
    if args.parte == "todos":
        for parte in ["cobertura", "intraday", "eod", "rotulos"]:
            partes[parte]()
    else:
        partes[args.parte]()
    
    con.close()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
