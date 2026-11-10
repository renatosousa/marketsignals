"""Tira o processo atual do modo de eficiencia (EcoQoS) do Windows 11.

10/11/2026: o Windows poe em modo de eficiencia os processos de segundo plano sem janela -- exatamente como a tarefa
agendada (iniciar_coleta.ps1 / vigiar_dashboard.ps1) sobe o dashboard e os coletores. No Core 7 240H (nucleos P e E)
o dashboard ficava ~5x mais lento: /api/players do WIN 9,4 s (1,7 s fora do modo), /api/dados 2 s (0,3 s). Medido
ligando o modo num processo novo (1,7 -> 9,3 s) e desligando no que estava no ar (9,4 -> 1,7 s).

O ajuste e por processo (SetProcessInformation / ProcessPowerThrottling), nao mexe em configuracao do sistema, e precisa
rodar DENTRO do interpretador: o python.exe do .venv e um lancador que abre o Python de verdade como processo filho.
Por isso o dashboard chama sem_modo_eficiencia() no main e o supervisor roda cada coletor via executar.py.
"""
import ctypes
import sys

_PROCESS_POWER_THROTTLING = 4  # PROCESS_INFORMATION_CLASS.ProcessPowerThrottling
_EXECUTION_SPEED = 0x1         # PROCESS_POWER_THROTTLING_EXECUTION_SPEED


class _Estado(ctypes.Structure):
    _fields_ = [("Version", ctypes.c_ulong), ("ControlMask", ctypes.c_ulong), ("StateMask", ctypes.c_ulong)]


def sem_modo_eficiencia():
    """Pede ao Windows para nunca aplicar o EcoQoS a este processo. -> True se aplicado (False fora do Windows)."""
    if sys.platform != "win32":
        return False
    k = ctypes.windll.kernel32
    k.GetCurrentProcess.restype = ctypes.c_void_p
    k.SetProcessInformation.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong]
    estado = _Estado(1, _EXECUTION_SPEED, 0)  # controla a velocidade de execucao e a deixa SEM throttling
    return bool(k.SetProcessInformation(k.GetCurrentProcess(), _PROCESS_POWER_THROTTLING, ctypes.byref(estado),
                                        ctypes.sizeof(estado)))
