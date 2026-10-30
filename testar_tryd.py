"""Teste do servidor de socket do Tryd (negocios com corretora compradora e vendedora). So leitura, so localhost.

O que se sabe (README de terceiros, github.com/MaironGallas/tryd_conexao_rtd, nao e documentacao oficial do Tryd): o
Tryd abre um socket TCP local (padrao 127.0.0.1:12002); o pedido de Times & Trades e "NEGS$S|<ATIVO>#" (ex.
NEGS$S|WINZ26#) e a resposta usa "|" entre campos, "@" entre registros e "#" no fim da mensagem; os campos do NEGS
seriam id, hora, preco, quantidade, corretora compradora, corretora vendedora e agressor. O executavel do Tryd 8.5
tem a classe TStechSocketDaemon e, na janela de Times & Trades, a configuracao "Export ... SAL / NEGS / RTD".

Este script NAO assume a porta: lista as portas em escuta do processo tryd.exe e usa a 12002 se estiver entre elas.
Responde as perguntas do teste: (1) o Tryd escuta? (2) o NEGS traz corretoras? (3) cada resposta e um retrato das
ultimas N negociacoes ou so o que chegou depois do pedido anterior? (4) os ids sao continuos (sem buracos)?

Uso: python testar_tryd.py [ATIVO] [--porta 12002] [--esperar SEGUNDOS] [--rodadas 3] [--intervalo 3] [--csv arq]
"""
import argparse
import csv
import re
import socket
import subprocess
import sys
import time

PORTA_PADRAO = 12002


def pid_tryd():
    r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq tryd.exe", "/FO", "CSV", "/NH"], capture_output=True, text=True)
    m = re.search(r'"tryd\.exe","(\d+)"', r.stdout, re.I)
    return int(m.group(1)) if m else None


def portas_em_escuta(pid):
    r = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True)
    out = []
    for linha in r.stdout.splitlines():
        p = linha.split()
        if len(p) >= 5 and p[3].upper() == "LISTENING" and p[4] == str(pid):
            out.append((p[1].rsplit(":", 1)[0], int(p[1].rsplit(":", 1)[1])))
    return sorted(set(out), key=lambda x: x[1])


def descobrir(esperar_s):
    """Espera ate esperar_s por uma porta em escuta do Tryd. -> (pid, [(endereco, porta)])"""
    fim = time.time() + esperar_s
    while True:
        pid = pid_tryd()
        portas = portas_em_escuta(pid) if pid else []
        if portas or time.time() >= fim:
            return pid, portas
        time.sleep(2)


def requisitar(porta, pedido, janela_s=4.0):
    """Envia o pedido e junta o que chegar por janela_s segundos. -> bytes"""
    with socket.create_connection(("127.0.0.1", porta), timeout=5) as s:
        s.sendall(pedido.encode("ascii"))
        s.settimeout(1.0)
        buf, fim = b"", time.time() + janela_s
        while time.time() < fim:
            try:
                d = s.recv(65536)
            except socket.timeout:
                if buf.endswith(b"#"):
                    break
                continue
            if not d:
                break
            buf += d
            if buf.endswith(b"#"):
                break
        return buf


def registros(buf):
    """Mensagem -> lista de registros (cada um e a lista de campos). '@' separa registros, '|' campos, '#' fecha."""
    txt = buf.decode("latin-1")
    corpo = txt.split("#")[0]
    campos = corpo.split("|")
    return txt, [r.split("|") for r in re.split(r"@", corpo)] if "@" in corpo else [campos]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("ativo", nargs="?", default="WINZ26")
    ap.add_argument("--porta", type=int)
    ap.add_argument("--esperar", type=float, default=0, help="espera ate N s o Tryd abrir uma porta em escuta")
    ap.add_argument("--rodadas", type=int, default=3)
    ap.add_argument("--intervalo", type=float, default=3.0)
    ap.add_argument("--csv", help="grava os registros recebidos (rodada, campos...) neste arquivo")
    a = ap.parse_args()

    pid, portas = descobrir(a.esperar)
    if not pid:
        print("Tryd (tryd.exe) nao esta aberto.")
        return 2
    print(f"Tryd PID {pid}; portas TCP em escuta: {portas or 'nenhuma'}")
    porta = a.porta or (PORTA_PADRAO if any(p == PORTA_PADRAO for _, p in portas) else (portas[0][1] if len(portas) == 1 else None))
    if porta is None:
        print("Sem porta para testar. Ative a exportacao/RTD na janela de Times & Trades do Tryd (ver a conversa) e rode de novo "
              "com --esperar 120." if not portas else f"Mais de uma porta em escuta; escolha com --porta: {portas}")
        return 1

    pedido = f"NEGS$S|{a.ativo}#"
    print(f"Pedido a 127.0.0.1:{porta}: {pedido!r}")
    anteriores, linhas_csv = None, []
    for k in range(1, a.rodadas + 1):
        t0 = time.time()
        buf = requisitar(porta, pedido)
        txt, regs = registros(buf)
        print(f"\n--- rodada {k}: {len(buf)} bytes em {time.time() - t0:.2f}s, {len(regs)} registros, "
              f"termina em '#': {buf.endswith(b'#')}")
        print("bruto (inicio):", repr(txt[:400]))
        if not buf:
            print("resposta vazia: o Tryd aceitou a conexao mas nao respondeu a esse pedido")
        for r in regs[:3] + (["..."] if len(regs) > 6 else []) + regs[-3:] if len(regs) > 3 else regs:
            print("  ", r)
        ids = [r[0] for r in regs if r and r[0].isdigit()]
        if ids:
            n = [int(x) for x in ids]
            buracos = sum(1 for x, y in zip(n, n[1:]) if y - x != 1 and x - y != 1)
            print(f"ids {min(n)}..{max(n)} ({len(n)} distintos: {len(set(n))}); saltos fora de +-1: {buracos}")
            if anteriores is not None:
                print(f"em comum com a rodada anterior: {len(set(n) & anteriores)}; novos: {len(set(n) - anteriores)}")
            anteriores = set(n)
        linhas_csv += [[k] + r for r in regs]
        if k < a.rodadas:
            time.sleep(a.intervalo)
    if a.csv:
        with open(a.csv, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(linhas_csv)
        print(f"\nregistros gravados em {a.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
