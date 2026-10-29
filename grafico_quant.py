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
Fora: o Flow VWAP PDE do repo converge por construcao para o meio da faixa do dia (o proprio README mostra o limite)
e a definicao de t e do perfil inicial e ambigua; nao entra ate termos um motivo para acreditar nele.

Todos os ts seguem o banco (Brasilia codificada como epoch "UTC"); os da resposta vao em segundos.
"""
import math

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


def _lista(x, d=2):
    return [None if v is None or (isinstance(v, float) and not math.isfinite(v)) else round(float(v), d) for v in x]


class Grafico:
    """Monta o JSON do /api/grafico. Pregoes passados ficam em cache (nao mudam); o do dia e relido a cada chamada."""

    def __init__(self):
        self.cache = {}

    def _seg(self, con, dia_ms, hoje_ms):
        if dia_ms == hoje_ms:
            return segundos(con, dia_ms)
        if dia_ms not in self.cache:
            if len(self.cache) > 12:
                self.cache.pop(min(self.cache))
            self.cache[dia_ms] = segundos(con, dia_ms)
        return self.cache[dia_ms]

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
        )
