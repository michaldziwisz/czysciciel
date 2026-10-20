"""Wycinanie oddechów i mlaśnięć z przerw mowy: geometria i decyzje bez modeli."""
import importlib.util
from pathlib import Path
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[1]


def load_worker():
    spec = importlib.util.spec_from_file_location("worker_luki", ROOT / "worker.py")
    w = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(w)
    w.np = np
    w.log = lambda *_: None
    w.progress = lambda *_: None
    return w


class KandydaciZLukTests(unittest.TestCase):
    """Sygnał o znanej prawdzie: mowa (VAD=1), przerwa z odgłosem, mowa."""

    def setUp(self):
        self.w = load_worker()
        self.sr = self.w.SR
        self.hop = self.w.VAD_WIN / self.sr
        rng = np.random.default_rng(0)
        y = rng.normal(0, 1e-4, int(6 * self.sr)).astype(np.float32)   # tło
        y[int(1.0 * self.sr):int(2.0 * self.sr)] += 0.3 * np.sin(np.arange(self.sr) * 0.2)
        y[int(4.0 * self.sr):int(5.0 * self.sr)] += 0.3 * np.sin(np.arange(self.sr) * 0.2)
        # odgłos w przerwie 2.0-4.0 s: 2.6-3.0 s
        y[int(2.6 * self.sr):int(3.0 * self.sr)] += rng.normal(0, 0.05, int(0.4 * self.sr))
        self.y = y
        n = int(len(y) / self.w.VAD_WIN)
        t = np.arange(n) * self.hop
        self.pv = (((t >= 1.0) & (t < 2.0)) | ((t >= 4.0) & (t < 5.0))).astype(np.float32)

    def test_odglos_w_przerwie_jest_kandydatem(self):
        k = self.w.kandydaci_z_luk(self.y, self.pv)
        hit = [c for c in k if c["a"] < 3.0 and c["b"] > 2.6]
        self.assertEqual(len(hit), 1, k)
        self.assertAlmostEqual(hit[0]["a"], 2.6, delta=0.03)
        self.assertAlmostEqual(hit[0]["b"], 3.0, delta=0.03)

    def test_ciecie_nigdy_nie_wchodzi_w_mowe(self):
        for c in self.w.kandydaci_z_luk(self.y, self.pv):
            for a, b in ((1.0, 2.0), (4.0, 5.0)):
                self.assertFalse(c["a"] < b and c["b"] > a, (c, a, b))

    def test_cisza_bez_odglosu_nie_daje_kandydatow(self):
        y = self.y.copy()
        y[int(2.6 * self.sr):int(3.0 * self.sr)] = 0.0
        k = self.w.kandydaci_z_luk(y, self.pv)
        self.assertFalse([c for c in k if c["a"] < 3.0 and c["b"] > 2.6], k)

    def test_odglos_przyklejony_do_mowy_jest_odsuniety_od_granicy(self):
        y = self.y.copy()
        rng = np.random.default_rng(1)
        y[int(2.0 * self.sr):int(2.3 * self.sr)] += rng.normal(0, 0.05, int(0.3 * self.sr))
        k = self.w.kandydaci_z_luk(y, self.pv)
        for c in k:
            if c["a"] < 2.3:
                self.assertGreaterEqual(c["a"], 2.0 + self.w.LUKI_EDGE - 1e-3, c)


class KlasyfikacjaTests(unittest.TestCase):
    def setUp(self):
        self.w = load_worker()

    def c(self, **kw):
        base = {"p_mowa": 0.01, "p_kat": {"chrzakniecia": 0.0, "oddechy": 0.0, "mlasniecia": 0.0},
                "top": "Silence", "resp": 0.0, "dur": 0.3}
        base.update(kw)
        return base

    def test_respiro_rozpoznaje_oddech(self):
        self.assertEqual(self.w.klasyfikuj_luke(self.c(resp=0.8)), "oddech")

    def test_krotki_oddech_tylko_z_respiro_odrzucony(self):
        self.assertIsNone(self.w.klasyfikuj_luke(self.c(resp=0.8, dur=0.1)))

    def test_ast_rozpoznaje_oddech(self):
        self.assertEqual(self.w.klasyfikuj_luke(
            self.c(p_kat={"chrzakniecia": 0.0, "oddechy": 0.4, "mlasniecia": 0.0})), "oddech")

    def test_mowa_nigdy_nie_jest_cieta(self):
        self.assertIsNone(self.w.klasyfikuj_luke(self.c(resp=0.9, p_mowa=0.5)))

    def test_dzwiek_programu_nie_jest_ciety(self):
        self.assertIsNone(self.w.klasyfikuj_luke(self.c(resp=0.9, top="Beep, bleep")))

    def test_chrzakniecie_w_przerwie_nie_jest_oddechem(self):
        self.assertIsNone(self.w.klasyfikuj_luke(
            self.c(resp=0.9, p_kat={"chrzakniecia": 0.5, "oddechy": 0.5, "mlasniecia": 0.0})))

    def test_klik(self):
        self.assertEqual(self.w.klasyfikuj_luke(self.c(dur=0.1, top="Tick")), "klik")
        self.assertIsNone(self.w.klasyfikuj_luke(self.c(dur=0.4, top="Tick")))


class RespiroBezTorchaudioTests(unittest.TestCase):
    def test_worker_nie_importuje_torchaudio(self):
        kod = [l for l in (ROOT / "worker.py").read_text(encoding="utf-8").splitlines()
               if l.strip().startswith(("import ", "from "))]
        self.assertFalse([l for l in kod if "torchaudio" in l], kod)

    def test_bootstrap_przypina_model_suma_kontrolna(self):
        spec = importlib.util.spec_from_file_location("bootstrap_luki", ROOT / "bootstrap.py")
        b = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(b)
        self.assertIn("/70e01c60c2f582c41092730680f2894ab24d6467/", b.RESPIRO_URL)
        self.assertEqual(len(b.RESPIRO_SHA256), 64)
        self.assertTrue(b._paths()["respiro"].endswith("respiro-en.pt"))


if __name__ == "__main__":
    unittest.main()
