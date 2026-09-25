"""Coletor da grade de opcoes de um ativo (default BOVA11) -> SQLite.

A cada --intervalo (1 s) le a cotacao (bid/ask/ultimo) das series dos proximos vencimentos com
strike perto do spot; grava a serie so quando a cotacao muda. A cada --intervalo-resumo (5 s)
grava, por vencimento, um resumo com vol implicita ATM, skew 25-delta e um proxy de fluxo.

NAO usa copy_ticks (no terminal da Genial isso trava o MT5 para todos os processos).
O MT5 tambem nao informa volume do dia, numero de negocios nem contratos em aberto das opcoes,
entao o fluxo e um PROXY: negocio detectado quando o ultimo preco/volume muda entre duas
leituras; agressor = compra se saiu no ask (ou acima), venda se saiu no bid (ou abaixo). Varios
negocios entre duas leituras contam como um.

Vol implicita: Black-Scholes, mid do book, prazo em dias uteis/252 (feriados ignorados),
taxa = Selic meta do BCB (ou --taxa). O subjacente de cada vencimento e o spot implicito pela
paridade put-call (coluna spot em opcoes_cotacoes), para o skew nao herdar erro de carrego.

Tabelas (ts_ms = hora do servidor MT5 em ms, mesma base do coletor_book):
  opcoes_cotacoes  ts_ms, sessao, subjacente, symbol, vencimento, tipo, strike, bid, ask, last,
                   last_vol, spot, iv, delta          (1 linha por mudanca de cotacao)
  opcoes_resumo    ts_ms, sessao, subjacente, vencimento, t_anos, spot, forward, taxa, atm_iv,
                   iv_call25, iv_put25, rr25, bf25, n_series, n_iv, e o proxy de fluxo
                   (neg_/vol_ call/put compra/venda) acumulado desde o resumo anterior

Uso: python coletor_opcoes.py [SUBJACENTE] [SEGUNDOS] [--db dados/book.db] [--vencimentos 3]
                              [--faixa 0.08] [--taxa 0.15]     (SEGUNDOS=0 roda ate Ctrl+C)
"""
import argparse
import json
import math
import sqlite3
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import MetaTrader5 as mt5
import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS opcoes_cotacoes (
    ts_ms INTEGER NOT NULL, sessao TEXT NOT NULL, subjacente TEXT NOT NULL, symbol TEXT NOT NULL,
    vencimento TEXT NOT NULL, tipo TEXT NOT NULL, strike REAL NOT NULL,
    bid REAL, ask REAL, last REAL, last_vol REAL, spot REAL, iv REAL, delta REAL
);
CREATE INDEX IF NOT EXISTS ix_opc_cot ON opcoes_cotacoes (subjacente, vencimento, ts_ms);
CREATE TABLE IF NOT EXISTS opcoes_resumo (
    ts_ms INTEGER NOT NULL, sessao TEXT NOT NULL, subjacente TEXT NOT NULL, vencimento TEXT NOT NULL,
    t_anos REAL, spot REAL, forward REAL, taxa REAL,
    atm_iv REAL, iv_call25 REAL, iv_put25 REAL, rr25 REAL, bf25 REAL, n_series INTEGER, n_iv INTEGER,
    neg_call_compra INTEGER, neg_call_venda INTEGER, neg_put_compra INTEGER, neg_put_venda INTEGER,
    vol_call_compra REAL, vol_call_venda REAL, vol_put_compra REAL, vol_put_venda REAL,
    PRIMARY KEY (sessao, subjacente, vencimento, ts_ms)
);
CREATE INDEX IF NOT EXISTS ix_opc_res_ts ON opcoes_resumo (subjacente, ts_ms);
"""

HORA_ABERTURA, HORA_FECHAMENTO = 10, 17  # pregao de opcoes (aprox.), para a fracao do dia no prazo
SPREAD_MAX_REL = 0.30   # spread/mid acima disso: preco pouco informativo, nao calcula IV
MIN_SERIES_VENC = 20    # vencimentos com menos series que isso sao ignorados (longos, iliquidos)
REVARRER_S = 900        # refaz a lista de series (spot andou, series novas) a cada 15 min


# ---------------------------------------------------------------- Black-Scholes
def _ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_preco(tipo, s, k, t, r, vol):
    d1 = (math.log(s / k) + (r + 0.5 * vol * vol) * t) / (vol * math.sqrt(t))
    d2 = d1 - vol * math.sqrt(t)
    if tipo == "C":
        return s * _ncdf(d1) - k * math.exp(-r * t) * _ncdf(d2)
    return k * math.exp(-r * t) * _ncdf(-d2) - s * _ncdf(-d1)


def bs_delta(tipo, s, k, t, r, vol):
    d1 = (math.log(s / k) + (r + 0.5 * vol * vol) * t) / (vol * math.sqrt(t))
    return _ncdf(d1) if tipo == "C" else _ncdf(d1) - 1.0


def vol_implicita(tipo, preco, s, k, t, r):
    """Bissecao em [1%, 300%]. None se o preco esta fora dos limites de nao-arbitragem."""
    intrinseco = max(0.0, s - k * math.exp(-r * t)) if tipo == "C" else max(0.0, k * math.exp(-r * t) - s)
    if t <= 0 or preco <= intrinseco + 1e-6:
        return None
    lo, hi = 0.01, 3.0
    if not (bs_preco(tipo, s, k, t, r, lo) <= preco <= bs_preco(tipo, s, k, t, r, hi)):
        return None
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if bs_preco(tipo, s, k, t, r, mid) > preco:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


# ---------------------------------------------------------------- auxiliares
def taxa_selic(padrao):
    """Selic meta (% a.a., serie SGS 432 do BCB) como taxa continua. Cai para o padrao se falhar."""
    try:
        url = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.432/dados/ultimos/1?formato=json"
        with urllib.request.urlopen(url, timeout=10) as r:
            selic = float(json.load(r)[0]["valor"].replace(",", ".")) / 100
        return math.log(1 + selic), f"Selic meta BCB {selic:.2%}"
    except Exception as e:  # sem internet / API fora: segue com o padrao
        return math.log(1 + padrao), f"padrao {padrao:.2%} (BCB indisponivel: {e.__class__.__name__})"


def prazo_anos(agora, venc):
    """Dias uteis ate o fim do pregao do vencimento / 252. Conta a fracao restante de hoje."""
    h = agora.hour + agora.minute / 60
    frac_hoje = min(max((HORA_FECHAMENTO - h) / (HORA_FECHAMENTO - HORA_ABERTURA), 0.0), 1.0)
    if agora.weekday() >= 5:
        frac_hoje = 0.0
    hoje = agora.date()
    dias = int(np.busday_count(hoje + timedelta(days=1), venc + timedelta(days=1))) if venc > hoje else 0
    return (frac_hoje + dias) / 252


def listar_series(subjacente, spot, hoje, n_venc, faixa):
    """Series vigentes dos n_venc proximos vencimentos (apos hoje) com |K/spot - 1| <= faixa."""
    prefixo = subjacente[:4]
    vig = [s for s in (mt5.symbols_get(f"{prefixo}*") or [])
           if s.option_strike > 0 and s.expiration_time > 0]
    por_venc = {}
    for s in vig:
        v = datetime.fromtimestamp(s.expiration_time, tz=timezone.utc).date()
        if v > hoje:
            por_venc.setdefault(v, []).append(s)
    vencs = sorted(v for v, ss in por_venc.items() if len(ss) >= MIN_SERIES_VENC)[:n_venc]
    series = {}
    for v in vencs:
        for s in por_venc[v]:
            if abs(s.option_strike / spot - 1) <= faixa:
                tipo = "C" if s.option_right == mt5.SYMBOL_OPTION_RIGHT_CALL else "P"
                series[s.name] = (v, tipo, s.option_strike)
    return series


def spot_implicito(series, ticks, spot, agora, r):
    """Por vencimento, S* = C - P + K e^{-rT} (paridade put-call) nos 3 strikes mais perto do spot
    com call e put de book valido (mediana). Tira o viés de carrego/aluguel do skew; sem par valido
    o vencimento fica de fora e usa o spot."""
    pares = {}
    for nome, o in ticks.items():
        venc, tipo, k = series[nome]
        if o.bid > 0 and o.ask > 0 and (o.ask - o.bid) / ((o.ask + o.bid) / 2) <= SPREAD_MAX_REL:
            pares.setdefault((venc, k), {})[tipo] = (o.bid + o.ask) / 2
    por_venc = {}
    for (venc, k), p in pares.items():
        if "C" in p and "P" in p:
            por_venc.setdefault(venc, []).append((abs(k - spot), k, p["C"], p["P"]))
    s_impl = {}
    for venc, lst in por_venc.items():
        t = prazo_anos(agora, venc)
        est = sorted(c - p + k * math.exp(-r * t) for _, k, c, p in sorted(lst)[:3])
        s_impl[venc] = est[len(est) // 2]
    return s_impl


def mais_proximo(lista, alvo, chave):
    return min(lista, key=lambda x: abs(chave(x) - alvo)) if lista else None


def resumo_vencimento(cot, spot, t, r):
    """cot: lista de dicts (tipo, strike, iv, delta). -> atm_iv, iv_call25, iv_put25."""
    fwd = spot * math.exp(r * t)
    com_iv = [c for c in cot if c["iv"] is not None]
    # ATM: media das IVs de call e put no strike mais proximo do forward que tenha as duas (ou so uma)
    atm = None
    strikes = sorted({c["strike"] for c in com_iv}, key=lambda k: abs(k - fwd))
    for k in strikes[:3]:
        ivs = [c["iv"] for c in com_iv if c["strike"] == k]
        if ivs:
            atm = sum(ivs) / len(ivs)
            break
    c25 = mais_proximo([c for c in com_iv if c["tipo"] == "C"], 0.25, lambda c: c["delta"])
    p25 = mais_proximo([c for c in com_iv if c["tipo"] == "P"], -0.25, lambda c: c["delta"])
    # so aceita se o delta achado estiver razoavelmente perto de 25
    ivc = c25["iv"] if c25 and abs(c25["delta"] - 0.25) <= 0.10 else None
    ivp = p25["iv"] if p25 and abs(p25["delta"] + 0.25) <= 0.10 else None
    return fwd, atm, ivc, ivp, len(com_iv)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("subjacente", nargs="?", default="BOVA11")
    ap.add_argument("segundos", nargs="?", type=float, default=60.0)
    ap.add_argument("--db", default="dados/book.db")
    ap.add_argument("--intervalo", type=float, default=1.0, help="leitura das cotacoes (s)")
    ap.add_argument("--intervalo-resumo", type=float, default=5.0, help="resumo por vencimento (s)")
    ap.add_argument("--vencimentos", type=int, default=3)
    ap.add_argument("--faixa", type=float, default=0.08, help="strikes ate +-faixa do spot")
    ap.add_argument("--taxa", type=float, default=None, help="taxa a.a. (default: Selic meta do BCB)")
    args = ap.parse_args()
    sub = args.subjacente

    if args.taxa is not None:
        r, origem_taxa = math.log(1 + args.taxa), f"informada {args.taxa:.2%}"
    else:
        r, origem_taxa = taxa_selic(0.15)

    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(args.db, timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.executescript(SCHEMA)

    if not mt5.initialize():
        raise SystemExit(f"initialize falhou: {mt5.last_error()}")
    mt5.symbol_select(sub, True)

    sessao = datetime.now().strftime("%Y%m%d_%H%M%S")
    offset = float("-inf")
    series, ultima_varredura = {}, 0.0
    anterior = {}     # symbol -> (bid, ask, last, last_vol, time_msc)
    ultimo_iv = {}    # symbol -> (iv, delta)
    fluxo = {}        # vencimento -> contadores desde o ultimo resumo
    prox_resumo = 0.0
    n_cot = n_res = 0
    fim = time.time() + args.segundos if args.segundos > 0 else float("inf")
    print(f"Coletando opcoes de {sub} -> {args.db} (sessao {sessao}, taxa {origem_taxa}). Ctrl+C para parar.",
          flush=True)

    try:
        while time.time() < fim:
            t_loop = time.time()
            tk = mt5.symbol_info_tick(sub)
            if tk is None or tk.time_msc == 0:
                time.sleep(1)
                continue
            local = time.time() * 1000
            offset = max(offset, tk.time_msc - local)
            agora_ms = int(local + offset)
            agora = datetime.fromtimestamp(agora_ms / 1000, tz=timezone.utc).replace(tzinfo=None)  # hora BR
            spot = tk.last or (tk.bid + tk.ask) / 2

            if time.time() - ultima_varredura >= REVARRER_S or not series:
                novas = listar_series(sub, spot, agora.date(), args.vencimentos, args.faixa)
                for nome in novas.keys() - series.keys():
                    mt5.symbol_select(nome, True)
                series, ultima_varredura = novas, time.time()
                vencs = sorted({v for v, _, _ in series.values()})
                print(f"[{agora:%H:%M:%S}] {len(series)} series | vencimentos {[str(v) for v in vencs]} "
                      f"| spot {spot}", flush=True)

            ticks = {nome: mt5.symbol_info_tick(nome) for nome in series}
            ticks = {n: o for n, o in ticks.items() if o is not None and o.time_msc}
            s_impl = spot_implicito(series, ticks, spot, agora, r)

            linhas = []
            for nome, o in ticks.items():
                venc, tipo, k = series[nome]
                s_venc = s_impl.get(venc, spot)
                atual = (o.bid, o.ask, o.last, o.volume_real or float(o.volume), o.time_msc)
                ant = anterior.get(nome)
                anterior[nome] = atual
                if ant is not None and atual[:4] == ant[:4]:
                    continue
                # proxy de negocio: ultimo preco ou volume do ultimo negocio mudou
                if ant is not None and o.last > 0 and (o.last != ant[2] or atual[3] != ant[3]):
                    bid_ant, ask_ant = ant[0], ant[1]
                    lado = ("compra" if ask_ant > 0 and o.last >= ask_ant else
                            "venda" if bid_ant > 0 and o.last <= bid_ant else None)
                    if lado:
                        f = fluxo.setdefault(venc, {})
                        chave = f"{'call' if tipo == 'C' else 'put'}_{lado}"
                        f["neg_" + chave] = f.get("neg_" + chave, 0) + 1
                        f["vol_" + chave] = f.get("vol_" + chave, 0.0) + atual[3]
                # IV pelo mid, se o book da opcao for informativo
                iv = dl = None
                t = prazo_anos(agora, venc)
                if o.bid > 0 and o.ask > 0 and t > 0:
                    mid = (o.bid + o.ask) / 2
                    if (o.ask - o.bid) / mid <= SPREAD_MAX_REL:
                        iv = vol_implicita(tipo, mid, s_venc, k, t, r)
                        if iv is not None:
                            dl = bs_delta(tipo, s_venc, k, t, r, iv)
                ultimo_iv[nome] = (iv, dl)
                linhas.append((agora_ms, sessao, sub, nome, str(venc), tipo, k,
                               o.bid, o.ask, o.last, atual[3], s_venc, iv, dl))
            if linhas:
                db.executemany("INSERT INTO opcoes_cotacoes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", linhas)
                n_cot += len(linhas)

            if time.time() >= prox_resumo:
                prox_resumo = time.time() + args.intervalo_resumo
                por_venc = {}
                for nome, (venc, tipo, k) in series.items():
                    iv, dl = ultimo_iv.get(nome, (None, None))
                    por_venc.setdefault(venc, []).append(dict(tipo=tipo, strike=k, iv=iv, delta=dl))
                for venc, cot in por_venc.items():
                    t = prazo_anos(agora, venc)
                    fwd, atm, ivc, ivp, n_iv = resumo_vencimento(cot, s_impl.get(venc, spot), t, r)
                    rr = ivc - ivp if ivc is not None and ivp is not None else None
                    bf = (ivc + ivp) / 2 - atm if rr is not None and atm is not None else None
                    f = fluxo.pop(venc, {})
                    db.execute(
                        "INSERT OR REPLACE INTO opcoes_resumo VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (agora_ms, sessao, sub, str(venc), t, spot, fwd, r, atm, ivc, ivp, rr, bf, len(cot), n_iv,
                         *(f.get(f"neg_{x}", 0) for x in ("call_compra", "call_venda", "put_compra", "put_venda")),
                         *(f.get(f"vol_{x}", 0.0) for x in ("call_compra", "call_venda", "put_compra", "put_venda"))))
                    n_res += 1
                db.commit()

            time.sleep(max(0.0, args.intervalo - (time.time() - t_loop)))
    except KeyboardInterrupt:
        pass
    finally:
        mt5.shutdown()
        db.commit()
        db.close()
    print(f"Linhas de cotacao: {n_cot} | resumos: {n_res}", flush=True)


if __name__ == "__main__":
    main()
