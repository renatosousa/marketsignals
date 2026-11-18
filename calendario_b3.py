"""Calendario oficial da B3/ANBIMA: Pascoa, feriados nacionais, dias uteis e navegacao entre pregoes.

Os feriados sao calculados (nao baixados), como em leiloes.py, que agora importa tudo daqui. A lista cobre os
feriados nacionais em que a ANBIMA nao negocia: fixos (01/01, 21/04, 01/05, 07/09, 12/10, 02/11, 15/11, 25/12),
moveis da Pascoa (Carnaval segunda e terca, Sexta-feira Santa e Corpus Christi) e a Consciencia Negra (20/11),
que so e feriado nacional desde 2024.

Convencoes:
  du(d0, d1)              dias uteis ANBIMA de d0 (inclusive) a d1 (exclusive), com feriados; aceita date, texto
                          ISO ou datetime64. Devolve int (pode ser negativo se d1 < d0).
  du_ate_vencimento(h,v)  dias uteis DEPOIS de h ate v inclusive (= busday_count(h+1, v+1, holidays=HOL)); 0 se
                          v <= h. E a contagem usada no prazo das opcoes (base 252).
  eh_pregao(d)            dia util ANBIMA que tambem tem sessao de negociacao: a B3 nao abre em 24/12 nem 31/12.
  pregao_anterior(d), proximo_pregao(d), somar_pregoes(d, n)  navegam pela mesma definicao de pregao.

Uso: python calendario_b3.py 2026   (imprime os feriados do ano e os pregoes do mes corrente)
"""
import sys
from datetime import date, datetime, timedelta

import numpy as np

INICIO_ANOS, FIM_ANOS = 2000, 2085
SEM_PREGAO = ((12, 24), (12, 31))  # vespera de Natal e de Ano Novo: feriado bancario, a B3 nao abre


def pascoa(ano):
    """Domingo de Pascoa (algoritmo de Butcher/Meeus, o mesmo do leiloes.py)."""
    a, b, c = ano % 19, ano // 100, ano % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    mes, dia = divmod(h + l - 7 * m + 114, 31)
    return date(ano, mes, dia + 1)


def feriados(ano):
    """Feriados nacionais que a ANBIMA nao negocia. Consciencia Negra (20/11) so vale desde 2024."""
    p = pascoa(ano)
    f = {date(ano, 1, 1), date(ano, 4, 21), date(ano, 5, 1), date(ano, 9, 7), date(ano, 10, 12), date(ano, 11, 2),
         date(ano, 11, 15), date(ano, 12, 25), p - timedelta(days=48), p - timedelta(days=47), p - timedelta(days=2),
         p + timedelta(days=60)}
    if ano >= 2024:
        f.add(date(ano, 11, 20))
    return f


HOL = np.array(sorted(d for ano in range(INICIO_ANOS, FIM_ANOS) for d in feriados(ano)), dtype="datetime64[D]")


def _data(d):
    """date a partir de datetime.date, datetime, texto ISO ('AAAA-MM-DD' ou com hora) ou numpy.datetime64."""
    if isinstance(d, np.datetime64):
        return d.astype("datetime64[D]").astype(date)
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    return date.fromisoformat(str(d)[:10])


def _np(d):
    return np.datetime64(_data(d), "D")


def du(d0, d1):
    """Dias uteis de d0 (inclusive) a d1 (exclusive), com os feriados ANBIMA: d0 util conta (d0, d1] ate d1 util."""
    return int(np.busday_count(_np(d0), _np(d1), holidays=HOL))


def du_ate_vencimento(hoje, venc):
    """Dias uteis depois de `hoje` ate `venc` inclusive (o proprio dia do vencimento conta); 0 se venc <= hoje."""
    hoje, venc = _data(hoje), _data(venc)
    if venc <= hoje:
        return 0
    return int(np.busday_count(_np(hoje + timedelta(days=1)), _np(venc + timedelta(days=1)), holidays=HOL))


def eh_pregao(d):
    """True se d e dia util ANBIMA e a B3 tem sessao (nao e 24/12 nem 31/12)."""
    d = _data(d)
    return d.weekday() < 5 and (d.month, d.day) not in SEM_PREGAO and d not in feriados(d.year)


def pregao_anterior(d):
    """Ultimo pregao estritamente anterior a d."""
    d = _data(d) - timedelta(days=1)
    while not eh_pregao(d):
        d -= timedelta(days=1)
    return d


def proximo_pregao(d):
    """Primeiro pregao estritamente posterior a d."""
    d = _data(d) + timedelta(days=1)
    while not eh_pregao(d):
        d += timedelta(days=1)
    return d


def somar_pregoes(d, n):
    """d deslocado em n pregoes (n > 0 para frente, n < 0 para tras, 0 = o proprio d)."""
    d = _data(d)
    passo = 1 if n > 0 else -1
    for _ in range(abs(int(n))):
        d += timedelta(days=passo)
        while not eh_pregao(d):
            d += timedelta(days=passo)
    return d


def pregoes(de, ate):
    """Gera os pregoes de `de` a `ate`, inclusive."""
    d = _data(de)
    while d <= _data(ate):
        if eh_pregao(d):
            yield d
        d += timedelta(days=1)


if __name__ == "__main__":
    ano = int(sys.argv[1]) if len(sys.argv) > 1 else date.today().year
    print(f"Pascoa {ano}: {pascoa(ano)}")
    for f in sorted(feriados(ano)):
        print(f"  {f} ({f.strftime('%A')})")
