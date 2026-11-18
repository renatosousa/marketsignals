"""Calendario economico: eventos de impacto alto/medio -> tabela calendario_eventos (ESPEC_V3 4.4).

Fontes:
  (a) nfs.faireconomy.media/ff_calendar_thisweek.json  (USD, EUR, CNY; datas em ET -> BRT)
  (b) servicodados.ibge.gov.br/api/v3/calendario      (IPCA, IPCA-15, PIB, PNAD, producao industrial)
  (c) dados/calendario_manual.csv                     (linhas confirmadas a mao, ex.: Copom)
  (d) eventos calculados com calendario_b3: vencimento WIN/IND, opcoes BOVA11, WDO/DOL e leiloes do Tesouro

Convencao de tempo: ts_ms = hora de Brasilia codificada como epoch UTC. A fonte (a) traz data com offset
(-05:00 no inverno dos EUA); convertemos para o epoch real e subtraimos 3 h. A fonte (b) traz a hora local
de Brasilia, entao basta interpreta-la como UTC (a convencao do banco).

Uso:
  python calendario_macro.py --atualizar [--db dados/book.db]
  python calendario_macro.py --de 2026-11-18 --ate 2026-11-28 [--impacto alto]
"""
import argparse
import csv
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

import banco
import calendario_b3

D = 86_400_000
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
URL_FF = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
URL_IBGE = "https://servicodados.ibge.gov.br/api/v3/calendario"
CSV_MANUAL = Path(__file__).parent / "dados" / "calendario_manual.csv"

SCHEMA = """
CREATE TABLE IF NOT EXISTS calendario_eventos (
    ts_ms INTEGER NOT NULL,
    pais TEXT NOT NULL,
    evento TEXT NOT NULL,
    impacto TEXT NOT NULL,
    fonte TEXT,
    previsto TEXT,
    anterior TEXT,
    PRIMARY KEY (ts_ms, pais, evento));
CREATE TABLE IF NOT EXISTS calendario_fontes (
    fonte TEXT PRIMARY KEY,
    dia TEXT NOT NULL,
    ts_ms INTEGER NOT NULL);
"""

PAIS_FF = {"USD": "EUA", "EUR": "EUR", "CNY": "CHN"}
IMPACTO_FF = {"High": "alto", "Medium": "medio", "Low": "baixo"}
_IMPACTOS = {"alto", "medio", "baixo"}


def _dia_idx(d):
    if isinstance(d, int):
        return d
    if isinstance(d, datetime):
        d = d.date()
    if isinstance(d, str):
        d = date.fromisoformat(d[:10])
    return (d - date(1970, 1, 1)).days


def _ms(d, hh=0, mm=0):
    """Data/hora de Brasilia -> ts_ms (epoch UTC da parede BRT)."""
    return _dia_idx(d) * D + (int(hh) * 60 + int(mm)) * 60_000


def _ms_de_datetime_brt(dt):
    """datetime naive tratado como hora de Brasilia -> ts_ms."""
    if dt.tzinfo is not None:
        return int((dt.timestamp() - 3 * 3600) * 1000)
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


def _evento(ts_ms, pais, evento, impacto, fonte, previsto=None, anterior=None):
    return {"ts_ms": int(ts_ms), "pais": pais, "evento": evento, "impacto": impacto.lower(),
            "fonte": fonte, "previsto": previsto, "anterior": anterior}


# ------------------------------------------------------------------ fontes (a) e (b)

def parse_ff(payload):
    """Lista do Forex Factory -> [evento]. So USD/EUR/CNY; impacto alto/medio/baixo."""
    out = []
    for item in payload or []:
        pais = PAIS_FF.get((item.get("country") or "").upper())
        if not pais:
            continue
        try:
            dt = datetime.fromisoformat(item["date"])
        except (KeyError, TypeError, ValueError):
            continue
        if dt.tzinfo is None:  # sem offset: assume ET (inverno -05:00)
            dt = dt.replace(tzinfo=timezone(timedelta(hours=-5)))
        ts_ms = int(dt.timestamp()) - 3 * 3600
        impacto = IMPACTO_FF.get(item.get("impact"), "baixo")
        out.append(_evento(ts_ms * 1000, pais, (item.get("title") or "").strip(), impacto, "forexfactory",
                           item.get("forecast") or None, item.get("previous") or None))
    return [e for e in out if e["evento"]]


def _classificar_ibge(alias):
    """(evento, impacto) de um item do calendario do IBGE, ou None se nao for um dos indicadores pedidos."""
    a = (alias or "").lower().strip()
    if a == "indice-nacional-de-precos-ao-consumidor-amplo-15":
        return "IPCA-15", "alto"
    if a == "indice-nacional-de-precos-ao-consumidor-amplo":
        return "IPCA", "alto"
    if a == "contas-nacionais-trimestrais":
        return "PIB", "alto"
    if a.startswith("pesquisa-nacional-por-amostra-de-domicilios-continua"):
        return "PNAD Continua", "medio"
    if a == "pesquisa-industrial-mensal-producao-fisica-brasil":
        return "Producao industrial", "medio"
    return None


def parse_ibge(payload):
    """Resposta do calendario do IBGE -> [evento] dos indicadores pedidos (horario local de Brasilia)."""
    out = []
    for item in (payload or {}).get("items") or []:
        classe = _classificar_ibge(item.get("alias_produto"))
        if not classe:
            continue
        try:
            dt = datetime.strptime(item["data_divulgacao"], "%d/%m/%Y %H:%M:%S")
        except (KeyError, TypeError, ValueError):
            continue
        out.append(_evento(_ms_de_datetime_brt(dt), "BRA", classe[0], classe[1], "ibge"))
    return out


def ler_manual(caminho=CSV_MANUAL):
    """Le dados/calendario_manual.csv (data,hora,pais,evento,impacto,previsto,anterior,obs).

    Linhas com obs 'confirmar' entram normalmente, mas o texto fica registrado no campo evento? Nao:
    mantemos o evento intacto e apenas usamos obs para validar que a linha e manual. O arquivo e curto
    e o campo obs serve de anotacao humana; nao vai para a tabela."""
    caminho = Path(caminho)
    if not caminho.exists():
        return []
    out = []
    with caminho.open(encoding="utf-8-sig", newline="") as f:
        for linha in csv.DictReader(f):
            try:
                d = date.fromisoformat((linha.get("data") or "").strip())
                h, m = (linha.get("hora") or "00:00").split(":")[:2]
                impacto = (linha.get("impacto") or "medio").strip().lower()
                if impacto not in _IMPACTOS:
                    impacto = "medio"
                evento = (linha.get("evento") or "").strip()
                if not evento:
                    continue
                out.append(_evento(_ms(d, int(h), int(m)), (linha.get("pais") or "BRA").strip(),
                                  evento, impacto, "manual", (linha.get("previsto") or "").strip() or None,
                                  (linha.get("anterior") or "").strip() or None))
            except (ValueError, TypeError):
                continue
    return out


# ------------------------------------------------------------------ fonte (d): calculados

def quarta_mais_proxima_do_15(ano, mes):
    """Quarta-feira mais proxima do dia 15 (no maximo 3 dias antes/depois)."""
    d = date(ano, mes, 15)
    delta = (2 - d.weekday()) % 7          # proxima quarta
    if delta > 3:
        delta -= 7                          # a de tras esta mais perto
    return d + timedelta(days=delta)


def terceira_sexta(ano, mes):
    d = date(ano, mes, 1)
    sextas = [d + timedelta(days=k) for k in range(31) if (d + timedelta(days=k)).month == mes]
    sextas = [x for x in sextas if x.weekday() == 4]
    return sextas[2]


def primeiro_pregao(ano, mes):
    d = date(ano, mes, 1)
    while d.month == mes:
        if calendario_b3.eh_pregao(d):
            return d
        d += timedelta(days=1)
    return None


def _pregiao_ou_antes(d):
    """Se d nao for pregao, anda para tras ate o pregao imediatamente anterior."""
    d = calendario_b3._data(d)
    while not calendario_b3.eh_pregao(d):
        d -= timedelta(days=1)
    return d


def eventos_calculados(de, ate):
    """Vencimentos e leiloes calculados no intervalo [de, ate] usando calendario_b3."""
    de, ate = calendario_b3._data(de), calendario_b3._data(ate)
    out = []
    ano, mes = de.year, de.month
    while (ano, mes) <= (ate.year, ate.month):
        if mes % 2 == 0:
            d = _pregiao_ou_antes(quarta_mais_proxima_do_15(ano, mes))
            if de <= d <= ate:
                out.append(_evento(_ms(d, 17, 55), "BRA", "Vencimento WIN/IND", "medio", "calculado"))
        d = _pregiao_ou_antes(terceira_sexta(ano, mes))
        if de <= d <= ate:
            out.append(_evento(_ms(d, 17, 55), "BRA", "Vencimento opcoes BOVA11", "medio", "calculado"))
        d = primeiro_pregao(ano, mes)
        if d and de <= d <= ate:
            out.append(_evento(_ms(d, 18, 0), "BRA", "Vencimento WDO/DOL", "medio", "calculado"))
        mes += 1
        if mes > 12:
            mes, ano = 1, ano + 1
    for d in calendario_b3.pregoes(de, ate):
        if d.weekday() in (1, 3):  # terca e quinta
            out.append(_evento(_ms(d, 11, 0), "BRA", "Leilao do Tesouro (11:00-11:30)", "medio", "calculado"))
    return out


# ------------------------------------------------------------------ banco e atualizacao

def inserir(con, eventos):
    """INSERT OR REPLACE em calendario_eventos. -> numero de linhas recebidas."""
    linhas = [(e["ts_ms"], e["pais"], e["evento"], e["impacto"], e["fonte"], e["previsto"], e["anterior"])
              for e in eventos]
    if linhas:
        con.executemany("INSERT OR REPLACE INTO calendario_eventos "
                        "(ts_ms, pais, evento, impacto, fonte, previsto, anterior) VALUES (?,?,?,?,?,?,?)", linhas)
        con.commit()
    return len(linhas)


def _ja_atualizado(con, fonte, dia_iso):
    try:
        row = con.execute("SELECT dia FROM calendario_fontes WHERE fonte=?", (fonte,)).fetchone()
    except sqlite3.OperationalError:
        return False
    return bool(row and row[0] == dia_iso)


def _marcar(con, fonte, dia_iso):
    con.execute("INSERT OR REPLACE INTO calendario_fontes VALUES (?,?,?)", (fonte, dia_iso, _ms(dia_iso, 12)))
    con.commit()


def _baixar_json(http, url, params=None):
    r = http.get(url, headers=UA, params=params, timeout=(5, 30))
    if r.status_code == 429:
        raise RuntimeError("429 rate limit")
    r.raise_for_status()
    return r.json()


def atualizar_fontes(db_path="dados/book.db", hoje=None, online=True, forcar=False, http=None):
    """Atualiza as fontes (a) e (b) 1x por dia, mais manual e calculados. Chamado na partida do coletor_global.

    `online=False` (teste) pula as fontes de rede. Uma fonte que falha nao impede as outras.
    -> {"forexfactory": n|None, "ibge": n|None, "manual": n, "calculado": n, "erros": {...}}"""
    hoje = hoje or date.today()
    dia_iso = hoje.isoformat()
    resultado = {"forexfactory": None, "ibge": None, "manual": 0, "calculado": 0, "erros": {}}
    db = banco.abrir(str(db_path), SCHEMA)
    try:
        http = http or requests.Session()
        if online and (forcar or not _ja_atualizado(db, "forexfactory", dia_iso)):
            try:
                resultado["forexfactory"] = inserir(db, parse_ff(_baixar_json(http, URL_FF)))
                _marcar(db, "forexfactory", dia_iso)
            except Exception as e:
                resultado["erros"]["forexfactory"] = f"{e.__class__.__name__}: {e}"
        if online and (forcar or not _ja_atualizado(db, "ibge", dia_iso)):
            try:
                de = (hoje - timedelta(days=7)).isoformat()
                ate = (hoje + timedelta(days=120)).isoformat()
                payload = _baixar_json(http, URL_IBGE, {"de": de, "ate": ate})
                resultado["ibge"] = inserir(db, parse_ibge(payload))
                _marcar(db, "ibge", dia_iso)
            except Exception as e:
                resultado["erros"]["ibge"] = f"{e.__class__.__name__}: {e}"
        resultado["manual"] = inserir(db, ler_manual())
        resultado["calculado"] = inserir(db, eventos_calculados(hoje - timedelta(days=7),
                                                                hoje + timedelta(days=120)))
    finally:
        db.close()
    return resultado


def _evento_dict(row):
    return dict(zip(("ts_ms", "pais", "evento", "impacto", "fonte", "previsto", "anterior"), row))


def janela_evento(con, agora_ms):
    """Evento de alto impacto entre -15 e +10 min (alerta_evento) e o proximo evento alto depois da janela.

    -> {"alerta_evento": dict|None, "proximo": dict|None}. Janela fechada nos dois extremos."""
    agora_ms = int(agora_ms)
    cols = "ts_ms, pais, evento, impacto, fonte, previsto, anterior"
    try:
        alertas = con.execute(
            f"SELECT {cols} FROM calendario_eventos WHERE impacto='alto' AND ts_ms BETWEEN ? AND ? "
            "ORDER BY ts_ms", (agora_ms - 15 * 60_000, agora_ms + 10 * 60_000)).fetchall()
        proximo = con.execute(
            f"SELECT {cols} FROM calendario_eventos WHERE impacto='alto' AND ts_ms>? ORDER BY ts_ms LIMIT 1",
            (agora_ms + 10 * 60_000,)).fetchone()
    except sqlite3.OperationalError:
        return {"alerta_evento": None, "proximo": None}
    alerta = None
    if alertas:  # o mais proximo do instante atual
        alerta = min((_evento_dict(r) for r in alertas), key=lambda e: abs(e["ts_ms"] - agora_ms))
    return {"alerta_evento": alerta, "proximo": _evento_dict(proximo) if proximo else None}


def listar(db_path, de, ate, impacto=None):
    """Eventos no intervalo [de, ate], opcionalmente filtrando por impacto. Somente leitura."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    try:
        sql = ("SELECT ts_ms, pais, evento, impacto, fonte, previsto, anterior FROM calendario_eventos "
               "WHERE ts_ms>=? AND ts_ms<=?")
        args = [_ms(de), _ms(ate, 23, 59)]
        if impacto:
            sql += " AND impacto=?"
            args.append(impacto)
        sql += " ORDER BY ts_ms"
        return [_evento_dict(r) for r in con.execute(sql, args).fetchall()]
    finally:
        con.close()


def main():
    ap = argparse.ArgumentParser(description="Calendario economico (Forex Factory, IBGE, manual e calculado)")
    ap.add_argument("--db", default="dados/book.db")
    ap.add_argument("--atualizar", action="store_true")
    ap.add_argument("--de")
    ap.add_argument("--ate")
    ap.add_argument("--impacto", choices=sorted(_IMPACTOS))
    args = ap.parse_args()
    if args.atualizar:
        print(atualizar_fontes(args.db, forcar=True))
        return
    de = args.de or date.today().isoformat()
    ate = args.ate or (date.today() + timedelta(days=10)).isoformat()
    eventos = listar(args.db, de, ate, args.impacto)
    for e in eventos:
        quando = (date(1970, 1, 1) + timedelta(days=e["ts_ms"] // D)).isoformat()
        hh = (e["ts_ms"] % D) // 3_600_000
        mm = ((e["ts_ms"] % D) // 60_000) % 60
        print(f"{quando} {hh:02d}:{mm:02d} {e['pais']:3s} {e['impacto']:5s} {e['evento']} ({e['fonte']})")
    print(f"{len(eventos)} evento(s) de {de} a {ate}")


if __name__ == "__main__":
    main()
