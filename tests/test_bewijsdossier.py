"""Tests voor bewijsdossier.py zonder netwerk en zonder Kennisbronnen (verzonnen molen-records).

Start: `uv run python tests/test_bewijsdossier.py` of `uv run python -m unittest discover -s tests`.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bewijsdossier as bd  # noqa: E402

TOON = """## logboek.csv

id,dag,tijd,onderdeel,actie
ENG-901,3,06:00,wiek,vervangen
ENG-902,4,07:30,as,gesmeerd

## dagboek.md

[ENG-910] dag 3
De nieuwe wiek draait sneller.
Tweede regel.

[ENG-911] dag 5
Niets bijzonders.
"""


def trace_en_verdict():
    trace = {"run_id": "r1", "status": "klaar", "beurten": [
        {"nr": 1, "spreker": "koning", "soort": "routering", "aan": "engineer"},
        {"nr": 2, "spreker": "engineer", "soort": "toolcall", "tool": "lees_kennisbron",
         "resultaat": {"records": 3, "ids": ["ENG-901", "ENG-910", "ENG-911"]}},
        {"nr": 3, "spreker": "engineer", "soort": "bericht", "tekst": "Wiek vervangen (ENG-901, ENG\u2011910)."},
        {"nr": 4, "spreker": "koning", "soort": "routering", "aan": "oracle"},
        {"nr": 5, "spreker": "oracle", "soort": "toolcall", "tool": "lees_kennisbron",
         "resultaat": {"records": 1, "ids": ["ORA-907"]}},
        {"nr": 6, "spreker": "oracle", "soort": "bericht", "tekst": "Wind op dag 3 (ORA-907), zie ENG-901."},
    ]}
    verdict = {"run_id": "r1", "oorzaak": {
        "ingreep": {"bewering": "Op dag 3 om 06:00 werd de wiek vervangen.", "bewijs": ["ENG-901", "ENG-910"], "zekerheid": 0.9},
        "mechanisme": {"bewering": "De wiek draait sneller.", "bewijs": ["ENG-910", "ORA-907"]},
        "verloop": {"bewering": None, "bewijs": []}},
        "acties": [{"actie": "Oude wiek terug.", "reden": "r", "bewijs": ["ENG-902"]}],
        "voorspelling": {"dag": 7, "bewering": "dag 7", "bewijs": ["ORA-907"]}}
    return trace, verdict


class TestBewijsdossier(unittest.TestCase):
    def test_ontleed_csv_en_markdown(self):
        r = bd.ontleed_toon(TOON, ["ENG-901", "ENG-910", "ENG-911"])
        self.assertEqual(r["ENG-901"]["bestand"], "logboek.csv")
        self.assertIn("tijd: 06:00", r["ENG-901"]["tekst"])
        self.assertNotIn("ENG-902", r)  # niet gevraagd
        self.assertEqual(r["ENG-910"]["kop"], "dag 3")
        self.assertIn("Tweede regel.", r["ENG-910"]["tekst"])
        self.assertNotIn("Niets bijzonders", r["ENG-910"]["tekst"])  # stopt bij het volgende record

    def test_herkomst_normaliseert_koppeltekens(self):
        trace, _ = trace_en_verdict()
        gelezen, genoemd = bd.herkomst(trace)
        self.assertEqual([g["beurt"] for g in gelezen["ENG-901"]], [2])
        self.assertEqual([g["beurt"] for g in genoemd["ENG-910"]], [3])  # ENG\u2011910 telt als ENG-910
        self.assertEqual({g["rol"] for g in genoemd["ENG-901"]}, {"engineer", "oracle"})

    def test_bouw_triangulatie_en_eigen_bron(self):
        trace, verdict = trace_en_verdict()
        records = bd.ontleed_toon(TOON, ["ENG-901", "ENG-910", "ENG-902"])
        with tempfile.TemporaryDirectory() as tmp:
            d = bd.bouw(Path(tmp), Path(tmp) / "archief", records, {}, trace, verdict)
        per = {b["sleutel"]: b for b in d["beweringen"]}
        self.assertEqual(per["ingreep"]["aantal_rollen"], 1)
        self.assertEqual(per["mechanisme"]["rollen"], ["engineer", "oracle"])
        eng2 = per["actie-1"]["bewijs"][0]
        self.assertFalse(eng2["uit_eigen_bron"])  # record bestaat, maar het Raadslid las het nooit
        self.assertIn("ORA-907", d["totaal"]["zonder_tekst"])  # geen tekst opgehaald voor de oracle
        self.assertIsNone(d["stabiliteit"])  # geen archief

    def test_stabiliteit(self):
        trace, verdict = trace_en_verdict()
        with tempfile.TemporaryDirectory() as tmp:
            arch = Path(tmp)
            for i, (dag, tijd) in enumerate([(7, "06:00"), (7, "08:00"), (9, "06:00")], 1):
                v = json.loads(json.dumps(verdict))
                v["run_id"] = f"r{i}"
                v["voorspelling"]["dag"] = dag
                v["oorzaak"]["ingreep"]["bewering"] = f"Op dag 3 om {tijd} werd de wiek vervangen."
                (arch / f"run{i}").mkdir()
                (arch / f"run{i}" / "verdict.json").write_text(json.dumps(v), encoding="utf-8")
                (arch / f"run{i}" / "trace.json").write_text(json.dumps({"run_id": f"r{i}", "status": "klaar"}), encoding="utf-8")
            (arch / "run9").mkdir()  # afgebroken run telt niet mee
            (arch / "run9" / "trace.json").write_text(json.dumps({"run_id": "x", "status": "afgebroken"}), encoding="utf-8")
            s = bd.stabiliteit(verdict, arch)
        assert s is not None
        self.assertEqual(s["aantal_runs"], 3)
        self.assertEqual(s["voorspelling"], {"dag": 7, "eens": 2, "van": 3})
        self.assertEqual(s["slots"]["ingreep"]["kernfeiten"], {"06:00": 2, "dag 3": 3})
        self.assertEqual(s["slots"]["mechanisme"]["soort"], "ids")  # geen dagen/tijden: vergelijk ID's
        self.assertEqual(s["slots"]["mechanisme"]["kernfeiten"], {"ENG-910": 3, "ORA-907": 3})


if __name__ == "__main__":
    unittest.main(verbosity=2)
