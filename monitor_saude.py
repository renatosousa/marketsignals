"""Watchdog ativo da coleta (ESPEC_V3.md secao 3.2): avalia a saude de cada fonte, grava o estado e avisa.

Motivacao (17/11/2026): as 7 janelas de saldo do Tryd pararam de exportar em 13/11 18:29 e a fita seguiu; o
painel mostrava "Tryd saldo e fita: ok" porque usava o mais recente das duas. Ninguem foi avisado por 2 pregoes.
Aqui cada fonte de painel_v2.coleta e avaliada sozinha, mais tres checagens de qualidade:

  Tryd DDE saldo      % de celulas validas de tryd_saldo nos ultimos 5 min (qtd/qtdc/qtdv numericos e
                      |qtd - (qtdc - qtdv)| < 0,5); DEGRADADO abaixo de 95%.
  Tryd fita captura   captura dos ultimos 5 min por ativo (negocios lidos / ((max(num) - min(num)) / 10 + 1));
                      metrica = a menor; DEGRADADO abaixo de 60%.
  MT5 preco congelado WIN$ (snapshots.mid) sem mudar por mais de 90 s no pregao -> PARADO.

Tabelas (as mesmas da ESPEC secao 8.1):
  saude_coletores  coletor (PK), ts_ultimo_dado_ms, status ('OK'/'DEGRADADO'/'PARADO'), metrica_qualidade
                   (percentual 0 a 100, quando existe), detalhe, ts_verificacao_ms.
  saude_alertas    ts_ms, coletor, status, texto, enviado_telegram; PK (ts_ms, coletor). Grava so a transicao
                   de OK para DEGRADADO/PARADO, com cooldown de 15 min por coletor.

Telegram: so envia com TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID (ambiente ou arquivo .env da raiz, formato
CHAVE=valor) E com o processo iniciado com --telegram; sem isso, so loga. httpx com timeout de 10 s e erro de
rede nao derruba o monitor.

Uso (o mesmo padrao dos coletores, para o supervisor):
  python monitor_saude.py SAUDE 36000 [--db dados/book.db] [--intervalo 30] [--telegram]
  (segundos 0 = sem limite; fora do pregao avalia a cada 5 min)
"""
import argparse
import os
import sqlite3
import time
from datetime import date, timedelta
from pathlib import Path

import httpx

import banco
import calendario_b3 as cal
import desempenho
import fallback_tryd_fita as fb
import painel_v2
from relogio import local_br_ms

D = 86_400_000
RAIZ = Path(__file__).resolve().parent
COOLDOWN_MS = 15 * 60_000
JANELA_QUALIDADE_MS = 5 * 60_000
LIMIAR_DDE = 95.0            # % de celulas validas de tryd_saldo
LIMIAR_CAPTURA = 60.0        # % de captura da fita
CONGELADO_S = 90             # WIN$ sem variar por mais que isso no pregao
INICIO_PREGAO_MIN = 9 * 60
FIM_PREGAO_MIN = 18 * 60 + 25
INTERVALO_FORA_S = 300.0     # fora do pregao: avalia a cada 5 min
STATUS = ("OK", "DEGRADADO", "PARADO")

SCHEMA = """
CREATE TABLE IF NOT EXISTS saude_coletores (
    coletor TEXT PRIMARY KEY, ts_ultimo_dado_ms INTEGER, status TEXT NOT NULL,
    metrica_qualidade REAL, detalhe TEXT, ts_verificacao_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS saude_alertas (
    ts_ms INTEGER NOT NULL, coletor TEXT NOT NULL, status TEXT, texto TEXT,
    enviado_telegram INTEGER DEFAULT 0, PRIMARY KEY (ts_ms, coletor)
);
"""

_ATIVO_TELEGRAM = False       # ligado pelo --telegram no main
COLS_COLETORES = ("coletor", "ts_ultimo_dado_ms", "status", "metrica_qualidade", "detalhe", "ts_verificacao_ms")
COLS_ALERTAS = ("ts_ms", "coletor", "status", "texto", "enviado_telegram")


# ------------------------------------------------------------------ avaliacao

def _data(dia):
    return date(1970, 1, 1) + timedelta(days=int(dia))


def _dentro_pregao(agora_ms):
    """Pregao = calendario_b3.eh_pregao e 09:00-18:25 (a janela da coleta)."""
    agora_ms = int(agora_ms)
    return (cal.eh_pregao(_data(agora_ms // D))
            and INICIO_PREGAO_MIN <= (agora_ms % D) // 60_000 <= FIM_PREGAO_MIN)


def _item(coletor, ts, status, metrica=None, detalhe=None):
    return {"coletor": coletor, "ts_ultimo_dado_ms": ts, "status": status,
            "metrica_qualidade": metrica, "detalhe": detalhe}


def _celula_valida(qtd, qtdc, qtdv):
    """Celula do DDE valida: os tres numeros e |qtd - (qtdc - qtdv)| < 0,5 (a regra de players.saldo_por_grupo)."""
    if not all(isinstance(x, (int, float)) for x in (qtd, qtdc, qtdv)):
        return False
    return abs(qtd - (qtdc - qtdv)) < 0.5


def _tryd_dde_saldo(con, agora_ms, dentro):
    """% de fotos de tryd_saldo validas nos ultimos 5 min (todos os ativos)."""
    try:
        linhas = con.execute("SELECT qtd, qtdc, qtdv FROM tryd_saldo WHERE ts_ms>=? AND ts_ms<=?",
                             (agora_ms - JANELA_QUALIDADE_MS, agora_ms)).fetchall()
    except sqlite3.OperationalError:
        linhas = []
    if not linhas:
        return _item("Tryd DDE saldo", None, "PARADO" if dentro else "OK", None,
                     "sem fotos do saldo nos últimos 5 min")
    invalidas = sum(1 for q, c, v in linhas if not _celula_valida(q, c, v))
    pct = 100.0 * (len(linhas) - invalidas) / len(linhas)
    status = "OK" if pct >= LIMIAR_DDE else "DEGRADADO"
    detalhe = None if status == "OK" else (f"{invalidas} de {len(linhas)} fotos com célula inválida "
                                           f"({pct:.1f}% válidas)".replace(".", ","))
    return _item("Tryd DDE saldo", agora_ms, status, round(pct, 2), detalhe)


def _tryd_fita_captura(con, agora_ms, dentro):
    """Menor captura da fita nos ultimos 5 min entre os ativos com negocio."""
    try:
        linhas = con.execute("SELECT ativo, count(*), min(num), max(num) FROM tryd_fita "
                             "WHERE ts_ms>=? AND ts_ms<=? GROUP BY ativo",
                             (agora_ms - JANELA_QUALIDADE_MS, agora_ms)).fetchall()
    except sqlite3.OperationalError:
        linhas = []
    if not linhas:
        return _item("Tryd fita captura", None, "PARADO" if dentro else "OK", None,
                     "sem negócios na fita nos últimos 5 min")
    caps = {a: fb.captura(n, lo, hi) for a, n, lo, hi in linhas}
    ativo = min(caps, key=caps.get)
    pct = 100.0 * caps[ativo]
    status = "OK" if pct >= LIMIAR_CAPTURA else "DEGRADADO"
    detalhe = None if status == "OK" else (f"captura de {pct:.0f}% no {ativo} "
                                           f"(a menor dos {len(caps)} ativos)")
    return _item("Tryd fita captura", agora_ms, status, round(pct, 2), detalhe)


def _win_congelado(con, agora_ms, dentro):
    """WIN$ com o mid igual por mais de CONGELADO_S segundos no pregao (queda de conexao: conexao.py)."""
    try:
        r = con.execute("SELECT ts_ms, mid FROM snapshots WHERE symbol='WIN$' AND ts_ms<=? "
                        "ORDER BY ts_ms DESC LIMIT 1", (agora_ms,)).fetchone()
    except sqlite3.OperationalError:
        r = None
    if not r or r[1] is None:
        return _item("MT5 preco congelado", None, "PARADO" if dentro else "OK", None, "sem snapshot do WIN$")
    ts, mid = int(r[0]), r[1]
    try:
        antigas = con.execute("SELECT ts_ms, mid FROM snapshots WHERE symbol='WIN$' AND ts_ms<=? "
                              "ORDER BY ts_ms DESC LIMIT 900", (ts,)).fetchall()
    except sqlite3.OperationalError:
        antigas = []
    desde = None
    for t, m in antigas:
        if m != mid:
            desde = t
            break
    if desde is None and antigas:
        desde = antigas[-1][0]
    congelado_s = (ts - desde) / 1000 if desde is not None else 0.0
    if dentro and congelado_s > CONGELADO_S:
        return _item("MT5 preco congelado", ts, "PARADO", None,
                     f"WIN$ sem variar há {congelado_s:.0f} s")
    return _item("MT5 preco congelado", ts, "OK", None,
                 f"WIN$ sem variar há {congelado_s:.0f} s" if congelado_s > CONGELADO_S else None)


def avaliar(con_ro, agora_ms):
    """Saude de cada fonte (uma entrada por fonte de painel_v2.coleta, mais 'Tryd DDE saldo', 'Tryd fita
    captura' e 'MT5 preco congelado'). -> [{"coletor", "ts_ultimo_dado_ms", "status", "metrica_qualidade",
    "detalhe"}] com status 'OK'/'DEGRADADO'/'PARADO'. A conexao e somente leitura."""
    agora_ms = int(agora_ms)
    dentro = _dentro_pregao(agora_ms)
    itens = []
    for f in painel_v2.coleta(con_ro, agora_ms)["fontes"]:
        estado = f["estado"]
        if estado == "ok":
            status, detalhe = "OK", f.get("detalhe")
        elif estado == "atrasado":
            status, detalhe = "DEGRADADO", f.get("detalhe") or "atrasado"
        elif estado == "parado":
            status, detalhe = "PARADO", f.get("detalhe") or "sem leitura"
        elif estado == "fora do horario":
            status, detalhe = "OK", "fora do horário"
        else:                                   # "sem dados": so e falha dentro do horario de coleta
            status = "PARADO" if dentro else "OK"
            detalhe = f.get("detalhe") or "sem dados"
        itens.append(_item(f["nome"], f.get("ts_ms"), status, None, detalhe))
    itens.append(_tryd_dde_saldo(con_ro, agora_ms, dentro))
    itens.append(_tryd_fita_captura(con_ro, agora_ms, dentro))
    itens.append(_win_congelado(con_ro, agora_ms, dentro))
    return itens


# ------------------------------------------------------------------ gravacao e alertas

def gravar(con_rw, itens, agora_ms):
    """INSERT OR REPLACE em saude_coletores e, para cada coletor que PASSOU de OK para DEGRADADO/PARADO, um
    alerta em saude_alertas (cooldown de 15 min por coletor; sem linha anterior conta como OK, porque a
    primeira avaliacao ja deve avisar de uma falha em curso). -> alertas novos [{"coletor", "status", "texto"}]
    (quem chama decide notificar)."""
    agora_ms = int(agora_ms)
    novos = []
    for it in itens:
        antiga = con_rw.execute("SELECT status FROM saude_coletores WHERE coletor=?",
                                (it["coletor"],)).fetchone()
        anterior = antiga[0] if antiga else "OK"
        con_rw.execute("INSERT OR REPLACE INTO saude_coletores (coletor, ts_ultimo_dado_ms, status, "
                       "metrica_qualidade, detalhe, ts_verificacao_ms) VALUES (?,?,?,?,?,?)",
                       (it["coletor"], it["ts_ultimo_dado_ms"], it["status"], it["metrica_qualidade"],
                        it["detalhe"], agora_ms))
        if it["status"] not in ("DEGRADADO", "PARADO") or anterior != "OK":
            continue
        ultimo = con_rw.execute("SELECT max(ts_ms) FROM saude_alertas WHERE coletor=?",
                                (it["coletor"],)).fetchone()
        if ultimo and ultimo[0] is not None and agora_ms - ultimo[0] < COOLDOWN_MS:
            continue
        texto = f"{it['coletor']}: {it['status']}" + (f" - {it['detalhe']}" if it["detalhe"] else "")
        con_rw.execute("INSERT OR REPLACE INTO saude_alertas (ts_ms, coletor, status, texto, enviado_telegram) "
                       "VALUES (?,?,?,?,0)", (agora_ms, it["coletor"], it["status"], texto))
        novos.append({"coletor": it["coletor"], "status": it["status"], "texto": texto})
    con_rw.commit()
    return novos


def config_telegram(env=None):
    """(token, chat) de TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID: variaveis de ambiente, completadas pelo .env da
    raiz (CHAVE=valor; o arquivo nunca sobrescreve o ambiente). -> (None, None) quando algum falta."""
    env = dict(os.environ) if env is None else dict(env)
    token, chat = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        try:
            linhas = (RAIZ / ".env").read_text(encoding="utf-8").splitlines()
        except OSError:
            linhas = []
        for linha in linhas:
            linha = linha.strip()
            if not linha or linha.startswith("#") or "=" not in linha:
                continue
            chave, valor = linha.split("=", 1)
            chave, valor = chave.strip(), valor.strip().strip('"').strip("'")
            if chave == "TELEGRAM_BOT_TOKEN" and not token:
                token = valor
            elif chave == "TELEGRAM_CHAT_ID" and not chat:
                chat = valor
    return token or None, chat or None


def notificar(texto, ativo=None, http_post=None):
    """Manda `texto` pelo Telegram so quando o processo foi iniciado com --telegram E ha token e chat (ambiente
    ou .env). Sem isso, ou em erro de rede, loga e devolve False: o monitor nunca cai por causa do alerta.
    `ativo` e `http_post` existem para os testes (nao chamam a rede)."""
    ligado = _ATIVO_TELEGRAM if ativo is None else bool(ativo)
    token, chat = config_telegram()
    if not (ligado and token and chat):
        motivo = "--telegram ausente" if not ligado else "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID ausentes"
        print(f"[saude] sem envio ({motivo}): {texto}", flush=True)
        return False
    post = http_post or httpx.post
    try:
        r = post(f"https://api.telegram.org/bot{token}/sendMessage",
                 json={"chat_id": chat, "text": texto[:4000]}, timeout=10)
        ok = getattr(r, "status_code", 0) == 200
        if not ok:
            print(f"[saude] Telegram respondeu {getattr(r, 'status_code', '?')}: {texto}", flush=True)
        return bool(ok)
    except Exception as e:
        print(f"[saude] Telegram falhou ({e.__class__.__name__}: {e}); seguindo sem o alerta", flush=True)
        return False


# ------------------------------------------------------------------ leitura (dashboard)

def saude_api(con):
    """Payload do GET /api/saude (o dashboard so le): {"coletores": [...], "alertas": [ultimos 20]}. As tabelas
    podem ainda nao existir -> listas vazias."""
    try:
        coletores = [dict(zip(COLS_COLETORES, r)) for r in con.execute(
            "SELECT coletor, ts_ultimo_dado_ms, status, metrica_qualidade, detalhe, ts_verificacao_ms "
            "FROM saude_coletores ORDER BY coletor")]
    except sqlite3.OperationalError:
        coletores = []
    try:
        alertas = [dict(zip(COLS_ALERTAS, r)) for r in con.execute(
            "SELECT ts_ms, coletor, status, texto, enviado_telegram FROM saude_alertas "
            "ORDER BY ts_ms DESC LIMIT 20")]
    except sqlite3.OperationalError:
        alertas = []
    return {"coletores": coletores, "alertas": alertas}


# ------------------------------------------------------------------ CLI

def main():
    ap = argparse.ArgumentParser(description="Watchdog da coleta: saude das fontes e alerta no Telegram.")
    ap.add_argument("grupo", nargs="?", default="SAUDE", help="nome do grupo no supervisor (informativo)")
    ap.add_argument("segundos", nargs="?", type=float, default=0.0, help="0 = sem limite")
    ap.add_argument("--db", default="dados/book.db")
    ap.add_argument("--intervalo", type=float, default=30.0, help="s entre verificacoes no pregao")
    ap.add_argument("--telegram", action="store_true",
                    help="liga o envio pelo Telegram (exige TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID no ambiente ou .env)")
    args = ap.parse_args()

    global _ATIVO_TELEGRAM
    _ATIVO_TELEGRAM = bool(args.telegram)
    desempenho.sem_modo_eficiencia()      # o supervisor sobe sem janela: no modo de eficiencia fica ~5x mais lento
    fim = time.time() + args.segundos if args.segundos > 0 else float("inf")
    print(f"Monitor de saude ({args.grupo}) -> {args.db}; a cada {args.intervalo:.0f} s no pregao, "
          f"{INTERVALO_FORA_S:.0f} s fora; Telegram {'ligado' if args.telegram else 'desligado'}. "
          f"Ctrl+C para parar.", flush=True)

    while time.time() < fim:
        agora = int(local_br_ms())
        con_ro = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=10)
        try:
            itens = avaliar(con_ro, agora)
        except Exception as e:                      # banco ocupado/tabela sumida: tenta no proximo ciclo
            print(f"[{time.strftime('%H:%M:%S')}] avaliacao falhou ({e.__class__.__name__}: {e})", flush=True)
            itens = []
        finally:
            con_ro.close()
        if itens:
            db = banco.abrir(args.db, SCHEMA)       # rw so para gravar, transacao curta (gravar faz o commit)
            try:
                novos = gravar(db, itens, agora)
                for a in novos:
                    if notificar(a["texto"]):
                        db.execute("UPDATE saude_alertas SET enviado_telegram=1 WHERE ts_ms=? AND coletor=?",
                                   (agora, a["coletor"]))
                db.commit()
            finally:
                db.close()
            problemas = [i for i in itens if i["status"] != "OK"]
            print(f"[{time.strftime('%H:%M:%S')}] {len(itens)} coletores, {len(problemas)} com problema, "
                  f"{len(novos)} alerta(s) novo(s)", flush=True)
            for i in problemas:
                print(f"    {i['status']:<8} {i['coletor']}" + (f" ({i['detalhe']})" if i["detalhe"] else ""),
                      flush=True)
        espera = args.intervalo if _dentro_pregao(agora) else INTERVALO_FORA_S
        time.sleep(max(0.0, min(espera, fim - time.time())))


if __name__ == "__main__":
    main()
