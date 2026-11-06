"""Opcoes SEMANAIS do BOVA11 pelo RTD do Profit -> as mesmas tabelas do coletor_opcoes.py (opcoes_cotacoes/_resumo).

A base de simbolos do MT5 da Genial parou de trazer as semanais em novembro/2026 (nenhuma serie vencendo entre 9 e
13/11; as de setembro e outubro vinham), e a grade de opcoes do Tryd nao exporta DDE. O RTD do Profit aceita qualquer
ticker sem janela aberta: `BOVAW166W2_B_0` com os campos ULT, OCP (oferta de compra), OVD (oferta de venda), NEG, QTT
e DAT (testado em 06/11/2026). O campo VEN (vencimento) nao e confiavel (ticker inexistente devolve uma data), entao o
vencimento sai do proprio ticker:

  BOVA + letra do mes (calls A..L, puts M..X) + strike inteiro + W<n>, n = qual sexta-feira do mes (1, 2, 4, 5; a 3a
  e o vencimento mensal, que o coletor_opcoes.py pega pelo MT5). Sexta feriado -> dia util anterior.

Tickers que o Profit nao conhece voltam com DAT 30/12/1899 e zeros: ficam de fora. A vol implicita, o spot implicito
pela paridade, o resumo (ATM, 25-delta) e o proxy de fluxo usam as funcoes do coletor_opcoes.py. So grava com o BOVA11
negociado HOJE no Profit (antes da abertura o RTD ainda mostra o pregao anterior).

O servidor RTD e compartilhado: os ids de topico comecam em TOPICO_BASE (ver coletor_profit_rtd.py). O Profit precisa
estar aberto e logado; sem ele o coletor espera.

Uso: python coletor_opcoes_rtd.py [SUBJACENTE] [SEGUNDOS] [--db dados/book.db] [--semanas 2] [--faixa 0.08]
                                  [--intervalo 2] [--intervalo-resumo 5] [--taxa 0.15]   (SEGUNDOS=0 ate Ctrl+C)
"""
import argparse
import math
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import comtypes
import comtypes.client

import banco
import coletor_opcoes as co
import coletor_profit_rtd as cp
from leiloes import feriados
from relogio import local_br_ms

CAMPOS = ("ULT", "OCP", "OVD", "NEG", "QTT", "DAT")
SPOT_CAMPOS = ("ULT", "OCP", "OVD", "HOR", "DAT")
DATA_VAZIA = "30/12/1899"   # DAT de ticker que o Profit nao conhece
REVARRER_S = 900            # spot andou ou virou a semana: assina strikes/vencimentos novos
PARADA_MAX = 30             # leituras seguidas com a hora do BOVA11 parada, no pregao = RTD travado (reconecta)
DIAS_A_FRENTE = 21          # procura semanais ate 3 semanas a frente


def util_anterior(d):
    """d, ou o dia util anterior se d cair em feriado/fim de semana."""
    while d.weekday() >= 5 or d in feriados(d.year):
        d -= timedelta(days=1)
    return d


def semanais(hoje, n):
    """Os n proximos vencimentos semanais depois de hoje: [(vencimento, mes, semana)]. Pula a 3a sexta (mensal)."""
    out, d = [], hoje + timedelta(days=(4 - hoje.weekday()) % 7)  # proxima sexta (ou hoje, se sexta)
    while d <= hoje + timedelta(days=DIAS_A_FRENTE) and len(out) < n:
        semana = (d.day - 1) // 7 + 1
        venc = util_anterior(d)
        if semana != 3 and venc > hoje:
            out.append((venc, d.month, semana))
        d += timedelta(days=7)
    return out


def ticker(prefixo, tipo, mes, strike, semana):
    letra = chr(ord("A" if tipo == "C" else "M") + mes - 1)
    return f"{prefixo}{letra}{strike}W{semana}"


def listar_series(subjacente, spot, hoje, n_semanas, faixa):
    """{ticker: (vencimento, tipo, strike)} das semanais com strike inteiro dentro de +-faixa do spot."""
    prefixo = subjacente[:4]
    lo, hi = math.ceil(spot * (1 - faixa)), math.floor(spot * (1 + faixa))
    return {ticker(prefixo, tipo, mes, k, sem): (venc, tipo, float(k))
            for venc, mes, sem in semanais(hoje, n_semanas) for tipo in ("C", "P") for k in range(lo, hi + 1)}


def num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return 0.0
    return x if math.isfinite(x) else 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("subjacente", nargs="?", default="BOVA11")
    ap.add_argument("segundos", nargs="?", type=float, default=60.0)
    ap.add_argument("--db", default="dados/book.db")
    ap.add_argument("--intervalo", type=float, default=2.0, help="leitura do RTD (s)")
    ap.add_argument("--intervalo-resumo", type=float, default=5.0, help="resumo por vencimento (s)")
    ap.add_argument("--semanas", type=int, default=2, help="quantos vencimentos semanais a frente")
    ap.add_argument("--faixa", type=float, default=0.08, help="strikes ate +-faixa do spot")
    ap.add_argument("--taxa", type=float, default=None, help="taxa a.a. (default: Selic meta do BCB)")
    args = ap.parse_args()
    sub, chave_spot = args.subjacente, f"{args.subjacente}_B_0"

    if args.taxa is not None:
        r, origem_taxa = math.log(1 + args.taxa), f"informada {args.taxa:.2%}"
    else:
        r, origem_taxa = co.taxa_selic(0.15)

    db = banco.abrir(args.db, co.SCHEMA)
    comtypes.CoInitialize()
    sessao = datetime.now().strftime("%Y%m%d_%H%M%S") + "_rtd"
    fim = time.time() + args.segundos if args.segundos > 0 else float("inf")
    rtd, avisou, hora_ant, parada = None, False, None, 0
    series, assinadas, ultima_varredura = {}, set(), 0.0
    anterior, ultimo_iv, fluxo = {}, {}, {}
    prox_resumo, pend_cot, pend_res, n_cot, n_res = 0.0, [], [], 0, 0
    print(f"Semanais de {sub} pelo RTD do Profit -> {args.db} (sessao {sessao}, taxa {origem_taxa}). "
          f"Ctrl+C para parar.", flush=True)

    def desconectar():
        nonlocal rtd, assinadas, series
        if rtd is not None:
            rtd.fechar()
        rtd, assinadas, series = None, set(), {}

    try:
        while time.time() < fim:
            if rtd is None:
                if not cp.profit_aberto():
                    if not avisou:
                        print(f"[{datetime.now():%H:%M:%S}] Profit fechado: aguardando", flush=True)
                        avisou = True
                    time.sleep(30)
                    continue
                try:
                    rtd = cp.Rtd([chave_spot], list(SPOT_CAMPOS))
                    avisou, hora_ant, parada, ultima_varredura = False, None, 0, 0.0
                    print(f"[{datetime.now():%H:%M:%S}] conectado ao RTD do Profit", flush=True)
                except Exception as e:
                    print(f"[{datetime.now():%H:%M:%S}] aviso: RTD indisponivel ({e}); tento em 60 s", flush=True)
                    time.sleep(60)
                    continue

            t_loop = time.time()
            try:
                v = rtd.ler()
            except Exception as e:  # Profit fechado/reiniciado no meio do dia
                print(f"[{datetime.now():%H:%M:%S}] aviso: leitura do RTD falhou ({e}); reconectando", flush=True)
                desconectar()
                continue

            agora_ms = int(local_br_ms())
            agora = datetime.fromtimestamp(agora_ms / 1000, tz=timezone.utc).replace(tzinfo=None)  # hora BR
            hoje = agora.date()
            spot_dat, hora = v.get((chave_spot, "DAT")), v.get((chave_spot, "HOR"))
            bid_s, ask_s = num(v.get((chave_spot, "OCP"))), num(v.get((chave_spot, "OVD")))
            spot = num(v.get((chave_spot, "ULT"))) or ((bid_s + ask_s) / 2 if bid_s and ask_s else 0.0)
            do_dia = spot > 0 and spot_dat == hoje.strftime("%d/%m/%Y")

            parada = parada + 1 if do_dia and hora is not None and hora == hora_ant else 0
            hora_ant = hora
            if parada >= PARADA_MAX and co.HORA_ABERTURA <= agora.hour < co.HORA_FECHAMENTO:
                print(f"[{agora:%H:%M:%S}] aviso: RTD parado em {hora} ha {parada} leituras; reconectando", flush=True)
                desconectar()
                continue

            if do_dia and (time.time() - ultima_varredura >= REVARRER_S or not series):
                novas = listar_series(sub, spot, hoje, args.semanas, args.faixa)
                for nome in novas.keys() - assinadas:
                    for c in CAMPOS:
                        rtd.conectar(f"{nome}_B_0", c)
                    assinadas.add(nome)
                series, ultima_varredura = novas, time.time()
                print(f"[{agora:%H:%M:%S}] {len(series)} series | semanais "
                      f"{sorted({str(x) for x, _, _ in series.values()})} | spot {spot}", flush=True)

            if do_dia and series:
                # cotacao valida: o Profit conhece o ticker e ha oferta dos dois lados ou ultimo
                ticks = {}
                for nome in series:
                    q = {c: v.get((f"{nome}_B_0", c)) for c in CAMPOS}
                    if q["DAT"] in (None, DATA_VAZIA):
                        continue
                    o = SimpleNamespace(bid=num(q["OCP"]), ask=num(q["OVD"]), last=num(q["ULT"]),
                                        neg=int(num(q["NEG"])), qtd=num(q["QTT"]))
                    if o.bid > 0 or o.ask > 0 or o.last > 0:
                        ticks[nome] = o
                s_impl = co.spot_implicito(series, ticks, spot, agora, r)

                linhas = []
                for nome, o in ticks.items():
                    venc, tipo, k = series[nome]
                    s_venc = s_impl.get(venc, spot)
                    atual = (o.bid, o.ask, o.last, o.neg, o.qtd)
                    ant = anterior.get(nome)
                    anterior[nome] = atual
                    if ant is not None and atual == ant:
                        continue
                    last_vol = o.qtd - ant[4] if ant is not None and o.qtd >= ant[4] else 0.0
                    # proxy de fluxo como no coletor_opcoes: negocios novos no preco da oferta anterior
                    if ant is not None and o.neg > ant[3] and o.last > 0:
                        lado = ("compra" if ant[1] > 0 and o.last >= ant[1] else
                                "venda" if ant[0] > 0 and o.last <= ant[0] else None)
                        if lado:
                            f = fluxo.setdefault(venc, {})
                            ch = f"{'call' if tipo == 'C' else 'put'}_{lado}"
                            f["neg_" + ch] = f.get("neg_" + ch, 0) + (o.neg - ant[3])
                            f["vol_" + ch] = f.get("vol_" + ch, 0.0) + last_vol
                    iv = dl = None
                    t = co.prazo_anos(agora, venc)
                    if o.bid > 0 and o.ask > 0 and t > 0:
                        mid = (o.bid + o.ask) / 2
                        if (o.ask - o.bid) / mid <= co.SPREAD_MAX_REL:
                            iv = co.vol_implicita(tipo, mid, s_venc, k, t, r)
                            if iv is not None:
                                dl = co.bs_delta(tipo, s_venc, k, t, r, iv)
                    ultimo_iv[nome] = (iv, dl)
                    linhas.append((agora_ms, sessao, sub, nome, str(venc), tipo, k,
                                   o.bid, o.ask, o.last, last_vol, s_venc, iv, dl))
                pend_cot += linhas

                if time.time() >= prox_resumo:
                    prox_resumo = time.time() + args.intervalo_resumo
                    por_venc = {}
                    for nome in ticks:
                        venc, tipo, k = series[nome]
                        iv, dl = ultimo_iv.get(nome, (None, None))
                        por_venc.setdefault(venc, []).append(dict(tipo=tipo, strike=k, iv=iv, delta=dl))
                    for venc, cot in por_venc.items():
                        t = co.prazo_anos(agora, venc)
                        fwd, atm, ivc, ivp, n_iv = co.resumo_vencimento(cot, s_impl.get(venc, spot), t, r)
                        rr = ivc - ivp if ivc is not None and ivp is not None else None
                        bf = (ivc + ivp) / 2 - atm if rr is not None and atm is not None else None
                        f = fluxo.pop(venc, {})
                        pend_res.append(
                            (agora_ms, sessao, sub, str(venc), t, spot, fwd, r, atm, ivc, ivp, rr, bf, len(cot), n_iv,
                             *(f.get(f"neg_{x}", 0) for x in ("call_compra", "call_venda", "put_compra", "put_venda")),
                             *(f.get(f"vol_{x}", 0.0) for x in ("call_compra", "call_venda", "put_compra", "put_venda"))))

            if pend_cot or pend_res:
                try:
                    co.gravar(db, pend_cot, pend_res)
                    n_cot, n_res = n_cot + len(pend_cot), n_res + len(pend_res)
                    pend_cot, pend_res = [], []
                except sqlite3.OperationalError as e:  # banco ocupado: tenta no proximo ciclo
                    db.rollback()
                    print(f"aviso: gravacao adiada ({e}); {len(pend_cot) + len(pend_res)} linhas pendentes",
                          flush=True)

            prox = t_loop + args.intervalo
            while time.time() < prox:  # o servidor so atualiza com mensagens do Windows sendo processadas
                comtypes.client.PumpEvents(min(0.5, max(0.01, prox - time.time())))
    except KeyboardInterrupt:
        pass
    finally:
        if rtd is not None:
            rtd.fechar()
        if pend_cot or pend_res:
            co.gravar(db, pend_cot, pend_res)
            n_cot, n_res = n_cot + len(pend_cot), n_res + len(pend_res)
        db.close()
    print(f"Linhas de cotacao: {n_cot} | resumos: {n_res}", flush=True)


if __name__ == "__main__":
    main()
