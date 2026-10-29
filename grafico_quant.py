"""Indicadores do grafico em tempo real do WIN (pagina /grafico do dashboard), calculados do book.db.

Fonte: snapshots do coletor_book (1 por segundo: bid/ask/ultimo negocio e agressao do intervalo, recalculada
pelos ticks) e o topo do livro na tabela niveis (dist=1). As barras saem do ultimo negocio amostrado a cada
segundo: maxima/minima podem perder um extremo que durou menos de 1 s.

O que vai no grafico (tudo causal: cada barra so usa dados ate ela):
  VWAP do dia +-1/2 desvios     desvio = desvio-padrao do preco ponderado pelo volume desde as 09:00.
  Perfil de volume do dia       volume agredido por faixa de preco (compra e venda), POC e area de valor (70%).
  Perfil composto (5 pregoes)   POC/area de valor dos 5 pregoes anteriores e as zonas de alto volume (HVN).
  Niveis do pregao anterior     maxima, minima, ultimo negocio, POC, VAH e VAL.
  Projecao do modelo de regime  abertura +-1 desvio previsto para o dia (rv_dia_prev) e ultimo +-1 desvio da
                                proxima hora (faixa_1h_pts); o modelo preve QUANTO oscila, nao a direcao.
  Stop ATR dinamico             "Dynamic Risk ATR Map" (github.com/geansm2/ntsl-indicator-library, reimplementado
                                pelas formulas do README): ATR 20 (EMA), K = clamp(1,5 x ATR/media100(ATR), 1, 3),
                                stop de compra = min(fech - K.ATR, minima10 - 0,25.ATR) e o simetrico de venda;
                                cor pelo momentum (fech - fech[10]) / ATR, neutro entre -0,10 e +0,10.
  Velas de tempo maior          15 min sobre 1 min; 60 min sobre 5 e 15 min (HTF_Candle_Sobreposto do mesmo repo).
  OFI                           order flow imbalance de Cont, Kukanov e Stoikov (2014) no topo do livro, somado por
                                barra e dividido pela profundidade media do topo (como o OFI do repo); sinal = MM5.
  Reversao a media (experim.)   MicrostructureMeanReversion do repo: gap = fech - MME10, AR(1) em 100 barras,
                                banda theta* = (phi + sqrt(phi^2 + 4 s2)) / 2 (Amaral 2026, nao verificado por nos),
                                desligada quando a meia-vida passa de 50 barras. Sem validacao fora da amostra.
  Movimento implicito           abertura +-1 desvio diario pela vol implicita ATM do BOVA11 (opcoes_resumo, vencimento
                                mais proximo com 5+ dias uteis): sigma_dia = IV / raiz(252), em % do WIN.
  Posicao em aberto (opcoes)    coberto+travado+descoberto por strike do vencimento mais proximo (B3 ou colagem
                                manual, a mais recente; dados de D-1), strikes convertidos para pontos do WIN pela
                                razao WIN/BOVA11 da ultima meia hora. Suporte/resistencia = maior descoberto em
                                put abaixo / call acima do spot; ima = strike de maior posicao total.
  GEX, DEX, gamma flip, walls   todos os vencimentos, gama/delta de Black-Scholes com a IV ao vivo de cada strike;
                                convencao usual (dealer comprado em call, vendido em put) porque a B3 nao diz quem
                                esta de cada lado. Gamma flip = spot onde o GEX total troca de sinal; call/put wall
                                1 e 2 = strikes de maior GEX de call/put. Detalhes em gex().
  Ativos relacionados           velas de BOVA11 (snapshots) e WDO$ (macro_cotacoes) no mesmo tempo grafico.
Fora: o Flow VWAP PDE do repo converge por construcao para o meio da faixa do dia (o proprio README mostra o limite)
e a definicao de t e do perfil inicial e ambigua; nao entra ate termos um motivo para acreditar nele.

Todos os ts seguem o banco (Brasilia codificada como epoch "UTC"); os da resposta vao em segundos.
"""
import math
import time

import numpy as np
import pandas as pd

DIA_MS = 86_400_000
H = lambda hh, mm=0: (hh * 60 + mm) * 60_000
SESSAO = (H(9), H(18, 30))
SIMBOLO = "WIN$"
RTD_PROFIT = "WINFUT_F_0"
TF_HTF = {1: 15, 5: 60, 15: 60}
PASSO_PERFIL, PASSO_COMPOSTO, DIAS_COMPOSTO = 25.0, 50.0, 5
AREA_VALOR = 0.70
# Dynamic Risk ATR (padroes do README do repo)
ATR_N, ATR_BASE, K_BASE, K_MIN, K_MAX, ESTRUT_N, ESTRUT_BUF, MOM_N, MOM_NEUTRO = 20, 100, 1.5, 1.0, 3.0, 10, 0.25, 10, 0.10
# reversao a media: meio spread do WIN (2,5 pts) + 1 tick de deslizamento como custo phi
OU_MME, OU_JANELA, OU_MEIA_VIDA_MAX, OU_PHI = 10, 100, 50.0, 2.5 + 5.0
OFI_SINAL = 5
PAUSA_OFI_MS = 10_000
# opcoes: vencimento mais proximo com pelo menos 5 dias uteis (a IV ATM da semana do vencimento e ruidosa),
# strikes a +-10% do spot, posicao de no maximo 7 dias corridos antes do pregao do grafico
OPC_DU_MIN, OPC_FAIXA, OPC_DIAS_MAX, OPC_N = 5, 0.10, 7, 3
RAZAO_JANELA_MS = 30 * 60_000
GEX_FAIXA, GEX_JANELA_IV_MS, TAXA_PADRAO = 0.25, 60 * 60_000, 0.13
# ativo -> (tabela, janela do pregao): o BOVA11 so negocia das 10:00 as 17:00 (antes disso o ultimo e o de ontem)
RELACIONADOS = {"BOVA11": ("snapshots", (H(10), H(17))), "WDO$": ("macro_cotacoes", SESSAO)}


def dias_com_dados(con, dia_ms, n, sym=SIMBOLO):
    """Ate n pregoes com snapshots, de dia_ms (meia-noite) para tras, mais recente primeiro."""
    out, d = [], dia_ms
    for _ in range(n * 3 + 10):
        if con.execute("SELECT 1 FROM snapshots WHERE symbol=? AND ts_ms BETWEEN ? AND ? LIMIT 1",
                       (sym, d + SESSAO[0], d + SESSAO[1])).fetchone():
            out.append(d)
            if len(out) >= n:
                break
        d -= DIA_MS
    return out


def ofi_eventos(b, a, qb, qa):
    """Contribuicao de cada atualizacao do topo ao OFI (Cont, Kukanov e Stoikov 2014): entra o tamanho novo do
    lado que melhorou ou ficou, sai o antigo do lado que piorou ou ficou. A primeira posicao vem NaN."""
    pb, pa, pqb, pqa = (np.r_[np.nan, x[:-1]] for x in (b, a, qb, qa))
    return (np.where(b >= pb, qb, 0) - np.where(b <= pb, pqb, 0)
            - np.where(a <= pa, qa, 0) + np.where(a >= pa, pqa, 0))


def segundos(con, dia_ms, sym=SIMBOLO):
    """Snapshots do pregao com livro valido + topo do livro (qb/qa) + OFI por segundo (e)."""
    s = pd.read_sql("SELECT ts_ms, sessao, bid, ask, last, vol_compra_agr vc, vol_venda_agr vv FROM snapshots "
                    "WHERE symbol=? AND ts_ms BETWEEN ? AND ? ORDER BY ts_ms",
                    con, params=(sym, dia_ms + SESSAO[0], dia_ms + SESSAO[1]))
    s = s[(s.bid > 0) & (s.ask > s.bid) & (s.last > 0)].reset_index(drop=True)  # fora do leilao de abertura
    if s.empty:
        return s
    s[["vc", "vv"]] = s[["vc", "vv"]].fillna(0.0)
    ses = list(s.sessao.unique())
    # o indice de niveis comeca por sessao: sem ela a consulta varre a tabela inteira
    n = pd.read_sql(f"SELECT ts_ms, lado, volume FROM niveis WHERE sessao IN ({','.join('?' * len(ses))}) "
                    "AND symbol=? AND ts_ms BETWEEN ? AND ? AND dist=1",
                    con, params=(*ses, sym, int(s.ts_ms.iloc[0]), int(s.ts_ms.iloc[-1])))
    if len(n):
        q = n.pivot_table(index="ts_ms", columns="lado", values="volume", aggfunc="first")
        s = s.join(q.rename(columns={"COMPRA": "qb", "VENDA": "qa"})[["qb", "qa"]], on="ts_ms")
    for c in ("qb", "qa"):
        if c not in s:
            s[c] = np.nan
    e = ofi_eventos(*(s[c].to_numpy(float) for c in ("bid", "ask", "qb", "qa")))
    quebra = np.r_[True, (np.diff(s.ts_ms.to_numpy()) > PAUSA_OFI_MS) | (s.sessao.to_numpy()[1:] != s.sessao.to_numpy()[:-1])]
    e[quebra | np.isnan(e)] = 0.0
    s["e"] = e
    s["prof"] = (s.qb + s.qa).astype(float)
    return s.drop(columns="sessao")


def barras(s, tf_min):
    """OHLC do ultimo negocio, volume agredido, delta, OFI e profundidade media do topo por barra."""
    tf = tf_min * 60_000
    g = s.assign(t=s.ts_ms // tf * tf, v=s.vc + s.vv, d=s.vc - s.vv).groupby("t")
    return g.agg(o=("last", "first"), h=("last", "max"), l=("last", "min"), c=("last", "last"), v=("v", "sum"),
                 delta=("d", "sum"), ofi=("e", "sum"), prof=("prof", "mean")).reset_index()


def vwap(s):
    """VWAP acumulado do dia e desvio-padrao ponderado por volume, por segundo (precos centrados p/ precisao)."""
    v = (s.vc + s.vv).to_numpy(float)
    x = s["last"].to_numpy(float) - float(s["last"].iloc[0])
    cv, cx, cx2 = np.cumsum(v), np.cumsum(v * x), np.cumsum(v * x * x)
    with np.errstate(invalid="ignore", divide="ignore"):
        m = cx / cv
        sd = np.sqrt(np.maximum(cx2 / cv - m * m, 0.0))
    return m + float(s["last"].iloc[0]), sd


def perfil(s, passo):
    """Volume agredido por faixa de preco (compra/venda), POC e area de valor (expande para o vizinho maior)."""
    if s.empty or (s.vc + s.vv).sum() <= 0:
        return None
    p = (s["last"] / passo).round() * passo
    g = s.groupby(p).agg(vc=("vc", "sum"), vv=("vv", "sum"))
    g = g.reindex(np.arange(g.index.min(), g.index.max() + passo / 2, passo), fill_value=0.0)
    vol = (g.vc + g.vv).to_numpy()
    k = int(vol.argmax())
    e = dd = k
    acc, tot = vol[k], vol.sum()
    while acc < AREA_VALOR * tot and (e > 0 or dd < len(vol) - 1):
        ve = vol[e - 1] if e > 0 else -1
        vd = vol[dd + 1] if dd < len(vol) - 1 else -1
        if ve >= vd:
            e -= 1
            acc += vol[e]
        else:
            dd += 1
            acc += vol[dd]
    preco = g.index.to_numpy()
    return dict(passo=passo, preco=preco.tolist(), vc=g.vc.tolist(), vv=g.vv.tolist(), poc=float(preco[k]),
                vah=float(preco[dd] + passo / 2), val=float(preco[e] - passo / 2))


def zonas_hvn(pf, n=4, vizinhos=3, minimo=1.3, borda=0.7):
    """Picos do perfil suavizado (maximo local em +-vizinhos faixas e acima de minimo x media) e a faixa em volta
    do pico enquanto o volume suavizado fica acima de borda x pico. -> lista de {lo, hi, pico, vol_rel}."""
    bruto = np.add(pf["vc"], pf["vv"])
    vol = pd.Series(bruto).rolling(3, center=True, min_periods=1).mean().to_numpy()
    preco, passo, media = np.array(pf["preco"]), pf["passo"], vol.mean()
    picos = [i for i in range(len(vol))
             if vol[i] >= minimo * media and vol[i] == vol[max(0, i - vizinhos):i + vizinhos + 1].max()]
    out, usados = [], set()
    for i in sorted(picos, key=lambda i: -vol[i]):
        if i in usados or len(out) >= n:  # empate na suavizacao: picos vizinhos da mesma zona
            continue
        a = z = i
        while a > 0 and vol[a - 1] >= borda * vol[i]:
            a -= 1
        while z < len(vol) - 1 and vol[z + 1] >= borda * vol[i]:
            z += 1
        usados.update(range(a, z + 1))
        out.append(dict(lo=float(preco[a] - passo / 2), hi=float(preco[z] + passo / 2),
                        pico=float(preco[a + int(bruto[a:z + 1].argmax())]),
                        vol_rel=float(vol[i] / media)))
    return out


def atr_dinamico(b):
    """Stops de compra/venda do Dynamic Risk ATR Map e o momentum em ATR (sobre as barras de varios pregoes)."""
    c1 = b.c.shift(1)
    tr = pd.concat([b.h - b.l, (b.h - c1).abs(), (b.l - c1).abs()], axis=1).max(axis=1)
    atr = tr.ewm(span=ATR_N, adjust=False).mean()
    base = atr.rolling(ATR_BASE, min_periods=ATR_N).mean()
    d = atr * (K_BASE * atr / base).clip(K_MIN, K_MAX)
    buf = ESTRUT_BUF * atr
    stop_c = np.minimum(b.c - d, b.l.rolling(ESTRUT_N).min() - buf)
    stop_v = np.maximum(b.c + d, b.h.rolling(ESTRUT_N).max() + buf)
    mom = (b.c - b.c.shift(MOM_N)) / atr
    ok = base.notna() & b.l.rolling(ESTRUT_N).min().notna()
    return stop_c.where(ok), stop_v.where(ok), mom.where(ok), atr


def reversao_media(b):
    """gap = fech - MME10; AR(1) movel em OU_JANELA barras -> banda theta* e meia-vida (em barras)."""
    gap = b.c - b.c.ewm(span=OU_MME, adjust=False).mean()
    x, y, n = gap.shift(1), gap, OU_JANELA
    r = lambda z: z.rolling(n, min_periods=n).mean()
    mx, my = r(x), r(y)
    cov, vx, vy = r(x * y) - mx * my, r(x * x) - mx * mx, r(y * y) - my * my
    a = cov / vx
    se2 = (vy - a * cov).clip(lower=0) * n / (n - 2)
    valido = (a > 0) & (a < 0.9999)
    s2 = (se2 / (1 - a * a)).where(valido)
    meia = (math.log(2) / -np.log(a.where(valido)))
    theta = 0.5 * (OU_PHI + np.sqrt(OU_PHI ** 2 + 4 * s2))
    return gap, theta.where(meia <= OU_MEIA_VIDA_MAX), meia


def ultimos(con, sym, tabela, dia_ms, janela=SESSAO):
    """ts_ms e ultimo negocio de outro ativo no pregao (BOVA11 dos snapshots, WDO$ do coletor macro)."""
    filtro = "bid > 0 AND ask > bid AND last > 0"  # livro cruzado = leilao: o ultimo ainda e o de antes
    return pd.read_sql(f"SELECT ts_ms, last FROM {tabela} WHERE symbol=? AND ts_ms BETWEEN ? AND ? AND {filtro} "
                       "ORDER BY ts_ms", con, params=(sym, dia_ms + janela[0], dia_ms + janela[1]))


def velas(x, tf_min, t):
    """OHLC do ultimo negocio por barra, alinhado aos tempos t (ms) do WIN; barra sem negocio vem NaN."""
    tf = tf_min * 60_000
    g = x.assign(t=x.ts_ms // tf * tf).groupby("t")["last"]
    return pd.DataFrame({"o": g.first(), "h": g.max(), "l": g.min(), "c": g.last()}).reindex(t)


def razao(win, outro, janela_ms=RAZAO_JANELA_MS):
    """Mediana de WIN / outro nos segundos em comum da ultima meia hora (converte strike do BOVA11 em pontos)."""
    if win.empty or outro.empty:
        return None
    m = pd.merge_asof(win[["ts_ms", "last"]], outro.rename(columns={"last": "o"}), on="ts_ms",
                      tolerance=2_000, direction="backward").dropna()
    m = m[m.ts_ms >= m.ts_ms.iloc[-1] - janela_ms] if len(m) else m
    return float((m["last"] / m.o).median()) if len(m) else None


def _iso(v):
    """Vencimento em AAAA-MM-DD (a colagem manual traz DD/MM/AAAA; 'nao informado' vira None)."""
    v = str(v or "")
    if len(v) == 10 and v[2] == "/":
        return f"{v[6:]}-{v[3:5]}-{v[:2]}"
    return v if len(v) == 10 and v[4] == "-" else None


def posicoes(con, dia_iso):
    """Posicao em aberto das opcoes do BOVA11 conhecida no pregao dia_iso, sem olhar o futuro: a mais recente entre
    o download da B3 (b3_posicoes_series; a posicao do pregao X sai a noite, entao vale de X+1 em diante) e a
    colagem manual (opcoes_manual_series, vale do dia da colagem em diante). A idade fica para quem chama.
    -> dict(fonte, ref, series=DataFrame[venc, tipo, strike, oi, descoberto, iv]) ou None"""
    cands = []
    try:
        r = con.execute("SELECT max(sessao) FROM b3_posicoes_series WHERE subjacente='BOVA11' AND sessao < ?",
                        (dia_iso,)).fetchone()[0]
        if r:
            cands.append(("b3", r, "SELECT vencimento, tipo, strike, coberto, travado, descoberto, vol_impl_pct iv "
                                   "FROM b3_posicoes_series WHERE subjacente='BOVA11' AND sessao=?", r))
    except Exception:  # tabela ainda nao criada (posicoes_b3_csv.py nunca rodou)
        pass
    r = con.execute("SELECT max(capturado_em) FROM opcoes_manual_series WHERE subjacente='BOVA11' "
                    "AND substr(capturado_em, 1, 10) <= ?", (dia_iso,)).fetchone()[0]
    if r:
        cands.append(("manual", r[:10], "SELECT vencimento, tipo, strike, coberto, travado, descoberto, "
                                        "vol_impl_pct iv FROM opcoes_manual_series WHERE subjacente='BOVA11' "
                                        "AND capturado_em=?", r))
    if not cands:
        return None
    fonte, ref, sql, chave = max(cands, key=lambda x: x[1])
    s = pd.read_sql(sql, con, params=(chave,))
    s["venc"] = s.vencimento.map(_iso)
    s["oi"] = s[["coberto", "travado", "descoberto"]].sum(axis=1, min_count=1)
    s = s[s.venc.notna() & (s.venc >= dia_iso) & (s.oi > 0) & s.tipo.isin(["C", "P"])]
    return dict(fonte=fonte, ref=ref,
                series=s[["venc", "tipo", "strike", "oi", "descoberto", "iv"]].reset_index(drop=True))


def em_pontos(k, k_win):
    return round(float(k) * k_win / 5) * 5  # WIN anda de 5 em 5


def niveis_opcoes(pos, spot, k_win, n=OPC_N, faixa=OPC_FAIXA):
    """Posicao em aberto por strike do vencimento mais proximo (strikes a +-faixa do spot).
    -> dict(fonte, ref, vencimento, razao, strikes=[dict(k, pts, call, put)], suporte, resistencia, ima) ou None"""
    if not pos or not spot or not k_win:
        return None
    s = pos["series"]
    s = s[s.strike.between(spot * (1 - faixa), spot * (1 + faixa))]
    if s.empty:
        return None
    s = s[s.venc == s.venc.min()]
    tab = s.pivot_table(index="strike", columns="tipo", values=["oi", "descoberto"], aggfunc="sum").fillna(0.0)
    col = lambda campo, tipo: tab[(campo, tipo)] if (campo, tipo) in tab else pd.Series(0.0, tab.index)
    call, put = col("oi", "C"), col("oi", "P")
    pts = lambda k: em_pontos(k, k_win)
    topo = lambda x: [dict(k=float(k), pts=pts(k), qtd=float(v)) for k, v in x[x > 0].nlargest(n).items()]
    desc_p, desc_c = col("descoberto", "P"), col("descoberto", "C")
    return dict(fonte=pos["fonte"], ref=pos["ref"], vencimento=s.venc.iloc[0], razao=round(k_win, 3),
                strikes=[dict(k=float(k), pts=pts(k), call=float(call[k]), put=float(put[k])) for k in tab.index],
                suporte=topo(desc_p[desc_p.index <= spot]), resistencia=topo(desc_c[desc_c.index >= spot]),
                ima=pts((call + put).idxmax()))


_normal_cdf = np.frompyfunc(lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2))), 1, 1)


def bs_gama_delta(call, s, k, t, r, iv):
    """Gama e delta de Black-Scholes, vetorizados (s pode ser uma coluna para varrer uma grade de spots)."""
    d1 = (np.log(s / k) + (r + iv * iv / 2) * t) / (iv * np.sqrt(t))
    gama = np.exp(-d1 * d1 / 2) / math.sqrt(2 * math.pi) / (s * iv * np.sqrt(t))
    nd1 = np.asarray(_normal_cdf(d1), dtype=float)
    return gama, np.where(call, nd1, nd1 - 1.0)


def ivs_series(con, dia_ms, desde=None, janela_ms=GEX_JANELA_IV_MS):
    """Ultima IV de cada serie (venc, tipo, strike) em opcoes_cotacoes: as linhas depois de `desde` (ms) ou, sem ele,
    a ultima hora de cada vencimento. O dia inteiro tem ~1 milhao de linhas (ler tudo levava 13 s); o Grafico le a
    ultima hora uma vez e depois so o que chegou. -> DataFrame[venc, tipo, strike, iv, ts_ms]"""
    # o indice e (subjacente, vencimento, ts_ms): sem o vencimento a consulta varre a tabela, entao os vencimentos do
    # dia vem do opcoes_resumo (mesmo coletor) e a janela termina na ultima cotacao de cada um
    vencs = [v for (v,) in con.execute("SELECT DISTINCT vencimento FROM opcoes_resumo WHERE subjacente='BOVA11' "
                                       "AND ts_ms BETWEEN ? AND ?", (dia_ms, dia_ms + DIA_MS - 1))]
    partes = []
    for v in vencs:
        fim = con.execute("SELECT max(ts_ms) FROM opcoes_cotacoes WHERE subjacente='BOVA11' AND vencimento=? "
                          "AND ts_ms BETWEEN ? AND ?", (v, dia_ms, dia_ms + DIA_MS - 1)).fetchone()[0]
        if fim is not None:
            ini = desde if desde is not None else fim - janela_ms
            partes.append(pd.read_sql("SELECT ts_ms, vencimento venc, tipo, strike, iv FROM opcoes_cotacoes "
                                      "WHERE subjacente='BOVA11' AND vencimento=? AND ts_ms > ? AND ts_ms <= ? "
                                      "AND iv > 0 ORDER BY ts_ms", con, params=(v, ini, fim)))
    x = pd.concat(partes, ignore_index=True) if partes else pd.DataFrame(columns=["ts_ms", "venc", "tipo", "strike", "iv"])
    return x.groupby(["venc", "tipo", "strike"]).agg(iv=("iv", "last"), ts_ms=("ts_ms", "last")).reset_index()


def ivs_por_strike(series):
    """Media da IV de call e put em cada (venc, strike): o sorriso usado no GEX."""
    return series.groupby(["venc", "strike"]).iv.mean().reset_index()


def gex(pos, spot, dia_iso, r, ivs=None, iv_padrao=None, n_wall=2, faixa=GEX_FAIXA):
    """GEX, DEX, gamma flip e walls da posicao em aberto (todos os vencimentos, strikes a +-faixa do spot).

    Convencao usual, porque a B3 nao diz quem esta comprado: os dealers estao comprados nas calls e vendidos nas
    puts. GEX = soma(gama x OI x S^2 x 1%), call + e put -, em R$ por 1% de movimento do BOVA11. DEX = soma(delta x
    OI x S), call + e put -: o delta de quem detem as opcoes, em R$. 1 opcao de BOVA11 = 1 cota.
    IV de cada serie: o sorriso ao vivo do vencimento (opcoes_cotacoes, interpolado no strike; fora da grade, a
    IV da ponta), senao a IV da propria fonte, senao iv_padrao (ATM). Prazo = dias uteis ate o vencimento / 252,
    no minimo meio dia. Gamma flip = spot em que o GEX total troca de sinal, recalculando o gama numa grade de
    spots com as IVs fixas (a troca mais perto do spot). Walls = os n_wall strikes de maior GEX de call / de put.
    -> dict(gex_total, dex_total, flip, call_walls, put_walls, strikes=[dict(k, gex, dex)]) ou None"""
    if not pos or not spot:
        return None
    s = pos["series"]
    s = s[s.strike.between(spot * (1 - faixa), spot * (1 + faixa))].copy()
    if s.empty:
        return None
    iv = s.iv.astype(float).copy()
    if ivs is not None and len(ivs):
        for v, g in ivs.groupby("venc"):
            g = g.sort_values("strike")
            m = (s.venc == v).to_numpy()
            iv[m] = np.interp(s.strike[m].astype(float), g.strike.astype(float), g.iv.astype(float))
    iv = iv.where(iv > 0)
    s["iv_uso"] = iv.fillna(iv_padrao if iv_padrao else iv.median())
    s = s[s.iv_uso > 0]
    if s.empty:
        return None
    du = np.busday_count(np.datetime64(dia_iso), s.venc.to_numpy(dtype="datetime64[D]"))
    t = np.maximum(du, 0.5) / 252
    call = (s.tipo == "C").to_numpy()
    sinal = np.where(call, 1.0, -1.0)
    oi, k, v = s.oi.to_numpy(float), s.strike.to_numpy(float), s.iv_uso.to_numpy(float)
    gama, delta = bs_gama_delta(call, spot, k, t, r, v)
    s["gex"] = sinal * gama * oi * spot * spot * 0.01
    s["dex"] = delta * oi * spot
    por = s.groupby("strike").agg(gex=("gex", "sum"), dex=("dex", "sum"))
    gex_c = s[call].groupby("strike").gex.sum()
    gex_p = s[~call].groupby("strike").gex.sum()

    grade = spot * np.linspace(1 - faixa / 2, 1 + faixa / 2, 241)
    g2, _ = bs_gama_delta(call, grade[:, None], k, t, r, v)
    total = (sinal * g2 * oi).sum(axis=1) * grade * grade * 0.01
    troca = np.nonzero(np.diff(np.sign(total)) != 0)[0]
    flip = None
    if len(troca):
        i = troca[np.argmin(np.abs(grade[troca] - spot))]
        flip = float(grade[i] - total[i] * (grade[i + 1] - grade[i]) / (total[i + 1] - total[i]))
    wall = lambda x: [dict(k=float(kk), gex=float(x[kk])) for kk in x[x != 0].abs().nlargest(n_wall).index]
    return dict(gex_total=float(s.gex.sum()), dex_total=float(s.dex.sum()), flip=flip,
                call_walls=wall(gex_c), put_walls=wall(gex_p),
                strikes=[dict(k=float(kk), gex=float(x.gex), dex=float(x.dex)) for kk, x in por.iterrows()])


def implicito(con, dia_ms, abertura, du_min=OPC_DU_MIN):
    """Desvio diario implicito pela IV ATM do BOVA11 na ultima leitura do pregao (vencimento mais proximo com
    du_min+ dias uteis). -> dict(iv, vencimento, sig_dia_pts, sig_venc_pts) ou None"""
    ts = con.execute("SELECT max(ts_ms) FROM opcoes_resumo WHERE subjacente='BOVA11' AND ts_ms BETWEEN ? AND ? "
                     "AND atm_iv IS NOT NULL", (dia_ms + SESSAO[0], dia_ms + SESSAO[1])).fetchone()[0]
    if ts is None:
        return None
    r = con.execute("SELECT vencimento, t_anos, atm_iv FROM opcoes_resumo WHERE subjacente='BOVA11' AND ts_ms=? "
                    "AND atm_iv IS NOT NULL AND t_anos >= ? ORDER BY vencimento LIMIT 1",
                    (ts, du_min / 252)).fetchone()
    if not r:
        return None
    venc, t_anos, iv = r
    return dict(iv=iv, vencimento=venc, t=ts / 1000, sig_dia_pts=abertura * iv / math.sqrt(252),
                sig_venc_pts=abertura * iv * math.sqrt(t_anos))


def _lista(x, d=2):
    return [None if v is None or (isinstance(v, float) and not math.isfinite(v)) else round(float(v), d) for v in x]


class Grafico:
    """Monta o JSON do /api/grafico. Pregoes passados ficam em cache (nao mudam); o do dia e relido a cada chamada."""

    def __init__(self):
        self.cache = {}
        self.cache_opc = {}
        self.cache_iv = {}

    def _seg(self, con, dia_ms, hoje_ms):
        if dia_ms == hoje_ms:
            return segundos(con, dia_ms)
        if dia_ms not in self.cache:
            if len(self.cache) > 12:
                self.cache.pop(min(self.cache))
            self.cache[dia_ms] = segundos(con, dia_ms)
        return self.cache[dia_ms]

    def _ivs(self, con, dia_ms):
        """IV de cada serie, acumulada: a ultima hora na primeira chamada do dia, depois so as linhas novas."""
        ant = self.cache_iv.get(dia_ms)
        novo = ivs_series(con, dia_ms, desde=int(ant.ts_ms.max()) if ant is not None and len(ant) else None)
        if ant is not None and len(ant):
            novo = (pd.concat([ant, novo], ignore_index=True).sort_values("ts_ms")
                    .groupby(["venc", "tipo", "strike"]).last().reset_index())
        if dia_ms not in self.cache_iv and len(self.cache_iv) > 12:
            self.cache_iv.pop(next(iter(self.cache_iv)))
        self.cache_iv[dia_ms] = novo
        return novo

    def _opcoes(self, con, dia_ms, hoje_ms):
        """Posicao em aberto valida no pregao + IVs do fim do pregao (o dia de hoje fica 60 s em cache)."""
        chave = ("opc", dia_ms)
        hit = self.cache_opc.get(chave)
        if hit and (dia_ms != hoje_ms or time.time() - hit[0] < 60):
            return hit[1]
        dia_iso = pd.Timestamp(dia_ms, unit="ms").date().isoformat()
        pos = posicoes(con, dia_iso)
        velha = pos is not None and (pd.Timestamp(dia_iso) - pd.Timestamp(pos["ref"])).days > OPC_DIAS_MAX
        ivs = ivs_por_strike(self._ivs(con, dia_ms)) if pos is not None and not velha else None
        r = con.execute("SELECT taxa FROM opcoes_resumo WHERE subjacente='BOVA11' AND ts_ms <= ? AND taxa > 0 "
                        "ORDER BY ts_ms DESC LIMIT 1", (dia_ms + DIA_MS,)).fetchone()
        res = (None if velha else pos, ivs, r[0] if r else TAXA_PADRAO,
               dict(fonte=pos["fonte"], ref=pos["ref"]) if pos else None)
        if len(self.cache_opc) > 12:
            self.cache_opc.pop(next(iter(self.cache_opc)))
        self.cache_opc[chave] = (time.time(), res)
        return res

    def montar(self, con, dia_ms, tf, hoje_ms):
        dias = dias_com_dados(con, dia_ms, DIAS_COMPOSTO + 1)
        if not dias or dias[0] != dia_ms:
            return dict(erro="sem snapshots do WIN nesse dia", dia=dia_ms / 1000)
        seg = {d: self._seg(con, d, hoje_ms) for d in dias}
        s = seg[dia_ms]
        # barras de varios pregoes para aquecer ATR (100+ barras) e a AR(1); no grafico vai so o dia
        todas = pd.concat([barras(seg[d], tf) for d in reversed(dias) if len(seg[d])], ignore_index=True)
        stop_c, stop_v, mom, atr = atr_dinamico(todas)
        gap, theta, meia = reversao_media(todas)
        hoje = (todas.t >= dia_ms).to_numpy()
        b = todas[hoje].reset_index(drop=True)

        vw, sd = vwap(s)
        fim_barra = s.ts_ms // (tf * 60_000) * (tf * 60_000)
        por_barra = pd.DataFrame({"t": fim_barra, "vw": vw, "sd": sd}).groupby("t").last().reindex(b.t)
        saldo = b.delta.cumsum()
        prof = b.prof.where(b.prof > 0)
        ofi_norm = b.ofi / prof
        prof_rtd = pd.read_sql("SELECT ts_ms, agr_saldo FROM profit_rtd WHERE symbol=? AND ts_ms BETWEEN ? AND ? "
                               "ORDER BY ts_ms", con, params=(RTD_PROFIT, dia_ms + SESSAO[0], dia_ms + SESSAO[1]))
        prof_rtd = (prof_rtd.assign(t=prof_rtd.ts_ms // (tf * 60_000) * (tf * 60_000)).groupby("t").agr_saldo.last()
                    .reindex(b.t).to_numpy(dtype=float))

        htf_min = TF_HTF[tf]
        hb = barras(s, htf_min)
        pf = perfil(s, PASSO_PERFIL)
        anteriores = [d for d in dias if d != dia_ms]
        comp = None
        if anteriores:
            sc = pd.concat([seg[d] for d in anteriores[:DIAS_COMPOSTO]], ignore_index=True)
            pc = perfil(sc, PASSO_COMPOSTO)
            if pc:
                comp = dict(dias=len(anteriores[:DIAS_COMPOSTO]), poc=pc["poc"], vah=pc["vah"], val=pc["val"],
                            hvn=zonas_hvn(pc))
        niveis = [dict(nome="Abertura", preco=float(s["last"].iloc[0]), grupo="dia")]
        if anteriores and len(seg[anteriores[0]]):
            sa = seg[anteriores[0]]
            pa = perfil(sa, PASSO_PERFIL)
            niveis += [dict(nome="Máx. ant.", preco=float(sa["last"].max()), grupo="anterior"),
                       dict(nome="Mín. ant.", preco=float(sa["last"].min()), grupo="anterior"),
                       dict(nome="Último ant.", preco=float(sa["last"].iloc[-1]), grupo="anterior")]
            if pa:
                niveis += [dict(nome="POC ant.", preco=pa["poc"], grupo="anterior"),
                           dict(nome="VAH ant.", preco=pa["vah"], grupo="anterior"),
                           dict(nome="VAL ant.", preco=pa["val"], grupo="anterior")]
        if comp:
            niveis += [dict(nome=f"POC {comp['dias']}d", preco=comp["poc"], grupo="composto")]

        # ultima previsao valida (a ultima linha do dia pode vir sem faixa_1h_pts, depois do pregao)
        reg = con.execute("SELECT ts_ms, rv_dia_prev, faixa_1h_pts FROM regime_live WHERE ts_ms BETWEEN ? AND ? "
                          "AND faixa_1h_pts IS NOT NULL ORDER BY ts_ms DESC LIMIT 1",
                          (dia_ms, dia_ms + DIA_MS - 1)).fetchone()
        abertura = float(s["last"].iloc[0])
        projecao = None
        if reg:
            projecao = dict(t=reg[0] / 1000, abertura=abertura,
                            sig_dia_pts=reg[1] / 100 * abertura if reg[1] else None,
                            ref=float(s["last"].iloc[-1]), faixa_1h_pts=reg[2])

        rel, k_win, spot = {}, None, None
        for sym, (tabela, janela) in RELACIONADOS.items():
            x = ultimos(con, sym, tabela, dia_ms, janela)
            if x.empty:
                continue
            v = velas(x, tf, b.t)
            rel[sym.rstrip("$")] = {k: _lista(v[k].to_numpy()) for k in "ohlc"} | dict(base=float(x["last"].iloc[0]))
            if sym == "BOVA11":
                k_win, spot = razao(s, x), float(x["last"].iloc[-1])
        dia_iso = pd.Timestamp(dia_ms, unit="ms").date().isoformat()
        pos, ivs, taxa, pos_ultima = self._opcoes(con, dia_ms, hoje_ms)
        opcoes = niveis_opcoes(pos, spot, k_win)
        impl = implicito(con, dia_ms, abertura)
        gx = gex(pos, spot, dia_iso, taxa, ivs, impl["iv"] if impl else None) if k_win else None
        if gx:  # strikes do BOVA11 -> pontos do WIN
            for w in gx["call_walls"] + gx["put_walls"] + gx["strikes"]:
                w["pts"] = em_pontos(w["k"], k_win)
            gx["flip_pts"] = em_pontos(gx["flip"], k_win) if gx["flip"] else None
            gx["spot"], gx["fonte"], gx["ref"] = spot, pos["fonte"], pos["ref"]

        u = len(todas) - 1
        a_ult = float(atr.iloc[u]) if u >= 0 and math.isfinite(atr.iloc[u]) else None
        return dict(
            dia=dia_ms / 1000, tf=tf, htf=htf_min, ultimo_ts=float(s.ts_ms.iloc[-1]) / 1000,
            t=(b.t / 1000).tolist(), o=_lista(b.o), h=_lista(b.h), l=_lista(b.l), c=_lista(b.c), v=_lista(b.v, 0),
            delta=_lista(b.delta, 0),
            vwap=_lista(por_barra.vw.to_numpy()), vwap_sd=_lista(por_barra.sd.to_numpy()),
            stop_c=_lista(stop_c[hoje]), stop_v=_lista(stop_v[hoje]), mom=_lista(mom[hoje], 3),
            atr=a_ult, atr_proj=dict(ref=float(b.c.iloc[-1]), atr=a_ult) if a_ult else None,
            saldo=_lista(saldo, 0), saldo_profit=_lista(prof_rtd, 0),
            ofi=_lista(ofi_norm, 3), ofi_sinal=_lista(ofi_norm.rolling(OFI_SINAL, min_periods=1).mean(), 3),
            ofi_acum=_lista(b.ofi.cumsum(), 0),
            ou_gap=_lista(gap[hoje]), ou_theta=_lista(theta[hoje]), ou_meia_vida=_lista(meia[hoje], 1),
            velas_htf=dict(t=(hb.t / 1000).tolist(), o=_lista(hb.o), h=_lista(hb.h), l=_lista(hb.l), c=_lista(hb.c)),
            perfil=pf, composto=comp, niveis=niveis, projecao=projecao,
            implicito=impl, opcoes=opcoes, gex=gx, relacionados=rel, opcoes_ultima=pos_ultima,
            base_win=abertura,
        )
