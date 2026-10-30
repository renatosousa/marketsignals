"""Cliente DDE minimo (DDEML via ctypes, so leitura) para o Tryd/Profit: servico `profitchart`, topico `TT`.

As janelas de corretoras do Tryd ("Saldo por corretora" e "Negocios") so saem pelo DDE: no RTD a ferramenta nunca
existiu (30/10/2026: "Ferramenta Invalida" mesmo com a janela aberta; no Excel, `=profitchart|TT!WINZ26_SAL.BRK.0`
funciona). O item e `<ativo>_<ferramenta>.<campo>.<linha>`, p. ex. `WINZ26_SAL.QTD.3` ou `WINZ26_NEGS.NUM.0`, e
`<ferramenta>.INFO.ATV` devolve o ativo da janela (vazio quando nao ha janela exportando).

A leitura e um pedido unico (XTYP_REQUEST, sem assinatura), o mesmo que o Excel faz ao calcular: 3.250 itens em 0,5 s.
O texto vem em formato de planilha do Windows (virgula decimal); `valor` converte.

Uso: python dde_tryd.py [ITEM ...]   (sem itens, mostra INFO e as 3 primeiras linhas do saldo)
"""
import ctypes
import re
import sys
from ctypes import wintypes as w

SERVICO, TOPICO = "profitchart", "TT"
XTYP_REQUEST, CF_TEXT = 0x20B0, 1
_NUM = re.compile(r"^-?\d+(,\d+)?$")

_u = ctypes.WinDLL("user32") if sys.platform == "win32" else None
_CB = ctypes.WINFUNCTYPE(ctypes.c_void_p, w.UINT, w.UINT, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)


def _assinaturas():
    u, vp = _u, ctypes.c_void_p
    u.DdeInitializeW.argtypes, u.DdeInitializeW.restype = [ctypes.POINTER(w.DWORD), _CB, w.DWORD, w.DWORD], w.UINT
    u.DdeCreateStringHandleW.argtypes, u.DdeCreateStringHandleW.restype = [w.DWORD, w.LPCWSTR, ctypes.c_int], vp
    u.DdeFreeStringHandle.argtypes = [w.DWORD, vp]
    u.DdeConnect.argtypes, u.DdeConnect.restype = [w.DWORD, vp, vp, vp], vp
    u.DdeClientTransaction.argtypes = [vp, w.DWORD, vp, vp, w.UINT, w.UINT, w.DWORD, ctypes.POINTER(w.DWORD)]
    u.DdeClientTransaction.restype = vp
    u.DdeGetData.argtypes, u.DdeGetData.restype = [vp, vp, w.DWORD, w.DWORD], w.DWORD
    u.DdeFreeDataHandle.argtypes = [vp]
    u.DdeDisconnect.argtypes = [vp]
    u.DdeUninitialize.argtypes = [w.DWORD]
    u.DdeGetLastError.argtypes, u.DdeGetLastError.restype = [w.DWORD], w.UINT


if _u is not None:
    _assinaturas()


def valor(txt):
    """Texto do DDE -> int, float (virgula decimal) ou texto sem as pontas; None fica None."""
    if txt is None:
        return None
    t = txt.strip()
    if _NUM.match(t):
        return float(t.replace(",", ".")) if "," in t else int(t)
    return t


class Dde:
    """Conexao com um servico/topico DDE; deve ser usada na mesma thread que a criou."""

    def __init__(self, servico=SERVICO, topico=TOPICO, timeout_ms=3000):
        self.timeout, self.inst, self._hs = timeout_ms, w.DWORD(0), {}
        self._cb = _CB(lambda *a: None)  # cliente puro: nenhuma notificacao e tratada
        r = _u.DdeInitializeW(ctypes.byref(self.inst), self._cb, 0x10, 0)  # APPCMD_CLIENTONLY
        if r:
            raise OSError(f"DdeInitialize falhou (erro {r})")
        self.conv = _u.DdeConnect(self.inst.value, self._hsz(servico), self._hsz(topico), None)
        if not self.conv:
            erro = _u.DdeGetLastError(self.inst.value)
            _u.DdeUninitialize(self.inst.value)
            raise OSError(f"DDE {servico}|{topico} indisponivel (erro {erro}): Tryd/Profit fechado?")

    def _hsz(self, s):
        if s not in self._hs:
            self._hs[s] = _u.DdeCreateStringHandleW(self.inst.value, s, 1200)  # CP_WINUNICODE
        return self._hs[s]

    def ler(self, item):
        """Texto do item, ou None se o servidor nao responder (item inexistente ou servidor ocupado)."""
        res = w.DWORD(0)
        h = _u.DdeClientTransaction(None, 0, self.conv, self._hsz(item), CF_TEXT, XTYP_REQUEST, self.timeout,
                                    ctypes.byref(res))
        if not h:
            return None
        n = _u.DdeGetData(h, None, 0, 0)
        buf = ctypes.create_string_buffer(n)
        _u.DdeGetData(h, buf, n, 0)
        _u.DdeFreeDataHandle(h)
        return buf.raw.split(b"\x00")[0].decode("cp1252", "replace").rstrip("\r\n\t ")

    def fechar(self):
        _u.DdeDisconnect(self.conv)
        for h in self._hs.values():
            _u.DdeFreeStringHandle(self.inst.value, h)
        _u.DdeUninitialize(self.inst.value)


if __name__ == "__main__":
    d = Dde()
    itens = sys.argv[1:] or (["WINZ26_SAL.INFO.ATV", "WINZ26_NEGS.INFO.ATV"] +
                             [f"WINZ26_SAL.{c}.{i}" for i in range(3) for c in ("BRK", "QTD", "VOLC", "VOLV")])
    for it in itens:
        print(it, "->", repr(valor(d.ler(it))))
    d.fechar()
