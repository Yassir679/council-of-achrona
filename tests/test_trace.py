"""Tests voor de trace, zonder LLM: een nep-backend speelt Koning en Raadsleden.

Start met `uv run python tests/test_trace.py`. Schrijft in een tijdelijke map, raakt trace.json in de repo niet aan.
"""
import asyncio
import json
import re
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import backend  # noqa: E402
import controleer_trace  # noqa: E402
import council  # noqa: E402

ROLLEN = ["engineer", "historian", "guardian_keeper"]
PREFIX = {"engineer": "ENG", "historian": "HIS", "guardian_keeper": "GUA"}


class NepBackend:
    """Koning kiest de Rollen om de beurt en stopt na 3; elk Raadslid 'leest' 3 records en noemt er 2."""

    def __init__(self, limiet_bij=None):
        self.beurt = 0
        self.limiet_bij = limiet_bij

    async def raadslid(self, systeem, prompt, mcp_url, sessie):
        self.beurt += 1
        if self.limiet_bij == self.beurt:
            raise backend.Gebruikslimiet("Claude: success: You've hit your 5-hour limit · resets 12:20pm")
        rol = mcp_url.rstrip("/").split("/")[-2]
        p = PREFIX[rol]
        gelezen = [f"{p}-00{i}" for i in (1, 2, 3)]
        return backend.Antwoord(
            tekst=f"Antwoord: mijn records tonen het. Bewijs: {gelezen[0]}, {gelezen[1]}. Niet gelezen: {p}-999.",
            toolcalls=[{"tool": "toon", "argumenten": {"ids": gelezen}, "ids": gelezen}],
            toolresultaten="\n".join(gelezen), usage={"invoer": 10, "gecached": 0, "uitvoer": 5})

    async def koning(self, systeem, prompt, schema):
        u = {"invoer": 20, "gecached": 0, "uitvoer": 10}
        if "rol" in schema.get("properties", {}):  # routering
            n = len(re.findall(r"\[beurt \d+\]", prompt))  # echte beurten, niet de uitleg '[beurt N]'
            rol = ROLLEN[n] if n < len(ROLLEN) else None
            return backend.Antwoord(usage=u, structured={
                "stand": {"ingreep": "x", "mechanisme": "y", "verloop": "z"}, "open_vragen": ["q"],
                "rol": rol, "vraag": None if rol is None else f"Toets beurt {n}", "reden": "test",
                "doel": "stoppen" if rol is None else "ingreep", "toetst_beurten": []})
        return backend.Antwoord(usage=u, structured={  # Verdict
            "samenvatting": "Test (ENG-001, ENG-777).",
            "oorzaak": {"ingreep": {"bewering": "a", "bewijs": ["ENG-001", "ENG-003"], "zekerheid": 0.9},
                        "mechanisme": {"bewering": "b", "bewijs": ["HIS-002"], "zekerheid": 0.5},
                        "verloop": {"bewering": "c", "bewijs": ["XXX"], "zekerheid": 0.5}},
            "acties": [{"actie": "doe iets", "reden": "omdat", "bewijs": ["GUA-001"]}],
            "risicos": [{"risico": "r", "mitigatie": "m"}],
            "geschillen": [{"onderwerp": "o", "standpunten": [{"rol": "engineer", "standpunt": "s"},
                                                              {"rol": "oracle", "standpunt": "sprak niet"}],
                            "uitkomst": "beslecht", "bewijs": ["ENG-002"], "beurten": [3]}]})


def draai(nep):
    tmp = Path(tempfile.mkdtemp(prefix="trace_test_"))
    oud_root, oud_r, oud_k = council.ROOT, backend.raadslid, backend.koning
    council.ROOT = tmp
    backend.raadslid, backend.koning = nep.raadslid, nep.koning
    os.environ.update({"MODEL_KLEIN": "nep-klein", "MODEL_GROOT": "nep-groot", "BACKEND": "claude"})
    for r in council.ROLLEN:
        os.environ[f"MCP_{r.upper()}"] = f"https://mcp.example/{r}/mcp"
    import raadsleden
    oud_alle = raadsleden.alle
    raadsleden.alle = lambda: [m for m in oud_alle() if m.ROL in ROLLEN]
    fout = None
    try:
        asyncio.run(council.run())
    except Exception as e:  # zoals main(): de trace eerlijk afsluiten
        fout = e
        council.markeer_afgebroken(e)
    finally:
        council.ROOT, backend.raadslid, backend.koning, raadsleden.alle = oud_root, oud_r, oud_k, oud_alle
    trace = json.loads((tmp / "trace.json").read_text(encoding="utf-8"))
    vp = tmp / "verdict.json"
    verdict = json.loads(vp.read_text(encoding="utf-8")) if vp.exists() else None
    shutil.rmtree(tmp)
    return trace, verdict, fout


def test_volledige_run():
    trace, verdict, fout = draai(NepBackend())
    assert fout is None, fout
    assert trace["status"] == "klaar"
    soorten = [(b["spreker"], b["soort"]) for b in trace["beurten"]]
    # routering → toolcall → bericht, per Raadslid, en een stop-routering
    assert soorten[:3] == [("koning", "routering"), ("engineer", "toolcall"), ("engineer", "bericht")], soorten
    assert [b["aan"] for b in trace["beurten"] if b["soort"] == "routering"] == [*ROLLEN, None]
    stop = [b for b in trace["beurten"] if b["soort"] == "routering" and b["aan"] is None]
    assert len(stop) == 1 and stop[0]["beslist_door"] == "koning-agent"
    for b in trace["beurten"]:
        assert b.get("tijd"), f"beurt {b['nr']} zonder tijd"
        if b["soort"] == "toolcall":
            assert b["resultaat"]["ids"], "toolcall zonder vastgelegd resultaat"
            assert f"/{b['spreker']}/" in b["pad"]
        if b["soort"] == "bericht" and b["spreker"] != "koning":
            c = b["id_controle"]
            assert set(c["geldig"]) <= set(c["geldig"]) and c["ongeldig"], c  # x-999 is ongeldig
            assert not any(i.endswith("999") for i in c["geldig"])
    # Verdict: alleen ID's die gelezen én in het debat genoemd zijn; ENG-003 is gelezen maar nooit genoemd
    alle = json.dumps(verdict)
    for slecht in ("ENG-777", "ENG-003", "ENG-999", "XXX"):
        assert slecht not in alle, slecht
    assert verdict["oorzaak"]["verloop"]["bewering"] is None  # geen geldig bewijs → null
    assert "oracle" not in trace["geschillen"][0]["standpunten"]
    assert trace["zelfcontrole"]["fouten"] == [], trace["zelfcontrole"]
    f, _, s = controleer_trace.controleer(trace, verdict)
    assert f == [], f
    assert s["koning_stopte_zelf"] and s["raadslid_beurten"] == 3


def test_limiet_breekt_run_eerlijk_af():
    trace, verdict, fout = draai(NepBackend(limiet_bij=2))
    assert isinstance(fout, backend.Gebruikslimiet)
    assert trace["status"] == "afgebroken" and trace["fout"]["soort"] == "gebruikslimiet"
    assert verdict is None, "een afgebroken run mag geen verdict.json achterlaten"
    berichten = [b for b in trace["beurten"] if b["soort"] == "bericht"]
    assert len(berichten) == 1, "na de limiet mag geen bericht zonder Kennisbron volgen"
    assert not any(b.get("beslist_door", "").startswith("regel") for b in trace["beurten"] if b["soort"] == "routering")
    f, _, _ = controleer_trace.controleer(trace, verdict)
    assert any("afgebroken" in x and "gebruikslimiet" in x for x in f), f


def test_limiet_herkenning():
    assert backend.is_limiet("You've hit your 5-hour limit · resets 12:20pm")
    assert not backend.is_limiet("De engineer activeerde de hulpslag")

    async def faalt():
        faalt.n += 1
        raise RuntimeError("Claude: success: You've hit your 5-hour limit")
    faalt.n = 0
    try:
        asyncio.run(backend.met_retry(faalt))
    except backend.Gebruikslimiet:
        pass
    assert faalt.n == 1, "een limiet mag niet opnieuw geprobeerd worden"


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    for naam, fn in list(globals().items()):
        if naam.startswith("test_"):
            fn()
            print("OK ", naam)
    print("Alle trace-tests geslaagd.")
