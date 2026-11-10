"""Testes do desempenho.py / executar.py (modo de eficiencia do Windows).

  python -m unittest test_desempenho -v
"""
import ctypes
import subprocess
import sys
import unittest
from pathlib import Path

import desempenho

AQUI = Path(__file__).parent


class TestDesempenho(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "so no Windows")
    def test_desliga_o_modo_de_eficiencia_do_processo(self):
        self.assertTrue(desempenho.sem_modo_eficiencia())
        k = ctypes.windll.kernel32
        k.GetCurrentProcess.restype = ctypes.c_void_p
        k.GetProcessInformation.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong]
        e = desempenho._Estado(1, 0, 0)
        self.assertTrue(k.GetProcessInformation(k.GetCurrentProcess(), 4, ctypes.byref(e), ctypes.sizeof(e)))
        self.assertEqual((e.ControlMask, e.StateMask), (1, 0))

    def test_executar_repassa_os_argumentos_ao_script(self):
        r = subprocess.run([sys.executable, str(AQUI / "executar.py"), "-c"], capture_output=True, text=True, cwd=AQUI)
        self.assertNotEqual(r.returncode, 0)  # "-c" nao e script: o erro vem do runpy, com o argumento repassado
        s = AQUI / "dados" / "_teste_executar.py"
        s.parent.mkdir(exist_ok=True)
        s.write_text("import sys\nprint(__name__, sys.argv[1:])\n", encoding="utf-8")
        try:
            r = subprocess.run([sys.executable, str(AQUI / "executar.py"), str(s), "TRYD", "60"], capture_output=True,
                               text=True, cwd=AQUI)
        finally:
            s.unlink()
        self.assertEqual(r.stdout.strip(), "__main__ ['TRYD', '60']")


if __name__ == "__main__":
    unittest.main()
