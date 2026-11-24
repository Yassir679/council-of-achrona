"""Strenge controle van trace.json + verdict.json, bovenop `uv run validate`. Start met `uv run controleer [map]`.

Controleert wat de jury in de trace naleest:
  1. Vóór elke Raadslid-beurt een routering naar precies die Rol, met vraag en reden.
  2. Maximaal 10 Raadslid-beurten; minstens 3 verschillende Rollen spraken.
  3. Elk Raadslid las alleen de eigen Kennisbron: elke toolcall gebruikt het MCP-pad van de eigen Rol.
  4. Elk Raadslid deed minstens één toolcall vóór zijn bericht.
  5. De Koning deed geen enkele toolcall en las geen bestand.
  6. Geen menselijke tussenkomst (spreker "mens").
  7. Elk ID in het Verdict komt uit een toolresultaat van de Rol met dat prefix (geen verzonnen bewijs).
  8. Elk ID dat een Raadslid als eigen bewijs noemt, zat in zijn eigen toolresultaten.
  9. Verwijzingen kloppen: toetst_beurten en geschillen.beurten wijzen naar eerdere Raadslid-berichten.
 10. Tijdstippen lopen op; nummering is 1..n zonder gaten; run_id van trace en Verdict is gelijk; status klaar.
 11. Elk ID in het Verdict werd in het debat door een Raadslid genoemd (de Koning kent alleen het debat).
 12. Elke toolcall legt vast wat hij teruggaf (`resultaat`); een afgebroken run wordt als fout gemeld met de reden.
"""
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent
ROLLEN = ("archmage", "engineer", "guardian_keeper", "historian", "oracle", "beast_master")
PREFIX = {"ARC": "archmage", "ENG": "engineer", "GUA": "guardian_keeper", "HIS": "historian", "ORA": "oracle", "BEA": "beast_master"}
ID = re.compile(r"\b(ARC|ENG|GUA|HIS|ORA|BEA)-([0-9]{3})\b")
BEURTENBUDGET = 10


def ids(x):
    return {f"{m.group(1)}-{m.group(2)}" for m in ID.finditer(json.dumps(x, ensure_ascii=False))}


def controleer(trace, verdict):
    fout, let = [], []
    beurten = trace.get("beurten") or []
    # Oudere traces leggen niet vast wat een toolcall teruggaf: dan kan regel 7/8 niet getoetst worden.
    met_resultaat = any("resultaat" in b for b in beurten if b.get("soort") == "toolcall")
    if not met_resultaat:
        let.append("toolcalls zonder 'resultaat': herkomst van ID's (regel 7 en 8) niet te toetsen")

    # 10. nummering, tijd, run_id, status
    if [b.get("nr") for b in beurten] != list(range(1, len(beurten) + 1)):
        fout.append("nummering van de beurten is niet 1..n zonder gaten")
    tijden = [b.get("tijd") for b in beurten if b.get("tijd")]
    if tijden and tijden != sorted(tijden, key=datetime.fromisoformat):
        fout.append("tijdstippen lopen niet op")
    if verdict and verdict.get("run_id") != trace.get("run_id"):
        fout.append("run_id van trace.json en verdict.json verschilt")
    if trace.get("status") != "klaar":
        reden = f" ({trace['fout'].get('soort')}: {trace['fout'].get('melding', '')[:120]})" if trace.get("fout") else ""
        fout.append(f"trace-status is {trace.get('status')!r}, niet 'klaar': de run is niet afgerond{reden}")

    toegewezen, raadslid_berichten, gesproken = None, [], set()
    bron_ids = {r: set() for r in ROLLEN}  # wat elk Raadslid via zijn tools terugkreeg
    calls_sinds_routering = 0
    genoemd_in_debat = set()  # ID's die Raadsleden in hun berichten noemden: het enige dat de Koning kent
    for b in beurten:
        s, soort = b.get("spreker"), b.get("soort")
        if s == "mens":
            fout.append(f"beurt {b['nr']}: menselijke tussenkomst (telt niet mee voor de Council-score)")
        if soort == "routering":
            toegewezen, calls_sinds_routering = b.get("aan"), 0
            if b.get("aan") is not None and not (b.get("vraag") and b.get("reden")):
                fout.append(f"beurt {b['nr']}: routering zonder vraag of reden")
            for n in b.get("toetst_beurten") or []:
                if n not in raadslid_berichten:
                    fout.append(f"beurt {b['nr']}: toetst_beurten verwijst naar {n}, geen eerder Raadslid-bericht")
            if b.get("beslist_door", "koning-agent") != "koning-agent":
                let.append(f"beurt {b['nr']}: routering door {b['beslist_door']}, niet door de Koning-agent")
        elif soort in ("toolcall", "bestand"):
            if s == "koning":
                fout.append(f"beurt {b['nr']}: de Koning raadpleegt een Kennisbron")
            elif soort == "toolcall" and f"/{s}/" not in (b.get("pad") or ""):
                fout.append(f"beurt {b['nr']}: {s} gebruikt het pad {b.get('pad')} van een andere Rol")
            elif soort == "bestand" and not str(b.get("bestand", "")).startswith(f"{s}/"):
                fout.append(f"beurt {b['nr']}: {s} leest het bestand {b.get('bestand')} van een andere Rol")
            if soort == "toolcall" and met_resultaat and "resultaat" not in b:
                fout.append(f"beurt {b['nr']}: toolcall zonder vastgelegd resultaat")
            if s in bron_ids:
                bron_ids[s] |= set((b.get("resultaat") or {}).get("ids") or [])
            calls_sinds_routering += 1
        elif soort == "bericht" and s in ROLLEN:
            if toegewezen != s:
                fout.append(f"beurt {b['nr']}: bericht van {s} zonder routering naar {s} ervoor")
            if calls_sinds_routering == 0:
                fout.append(f"beurt {b['nr']}: {s} sprak zonder eerst zijn Kennisbron te raadplegen")
            genoemd_in_debat |= ids(b.get("tekst", ""))
            if (b.get("id_controle") or {}).get("ongeldig"):
                let.append(f"beurt {b['nr']}: {s} noemde ID's die de ID-controle afkeurde: {', '.join(b['id_controle']['ongeldig'])}")
            eigen = {i for i in ids(b.get("tekst", "")) if PREFIX[i[:3]] == s}
            # Al afgekeurd door de ID-controle van de run (en dus uit het Verdict geweerd)? Dan is het een LET OP
            # hierboven; een FOUT is het alleen als de run het niet zelf opmerkte.
            niet_gelezen = sorted(eigen - bron_ids[s] - set((b.get("id_controle") or {}).get("ongeldig") or []))
            if niet_gelezen and met_resultaat:
                fout.append(f"beurt {b['nr']}: {s} noemt ID's die niet in zijn eigen toolresultaten zaten: {', '.join(niet_gelezen)}")
            toegewezen = None
            raadslid_berichten.append(b["nr"])
            gesproken.add(s)

    # 2. budget en aantal Rollen
    if len(raadslid_berichten) > BEURTENBUDGET:
        fout.append(f"{len(raadslid_berichten)} Raadslid-beurten, het Beurtenbudget is {BEURTENBUDGET}")
    if len(gesproken) < 3:
        fout.append(f"maar {len(gesproken)} Rollen spraken (minimum 3)")
    stop = [b for b in beurten if b.get("soort") == "routering" and b.get("aan") is None]
    if not stop:
        let.append("geen routering met aan: null: de Koning stopte niet zelf")
    elif len(raadslid_berichten) < BEURTENBUDGET:
        pass  # de Koning stopte zelf vóór het budget op was (Extra uitdaging)

    # 7. bewijs in het Verdict
    if verdict and met_resultaat:
        for i in sorted(ids(verdict)):
            if i not in bron_ids[PREFIX[i[:3]]]:
                fout.append(f"verdict.json: {i} komt in geen enkel toolresultaat van {PREFIX[i[:3]]} voor")
            elif i not in genoemd_in_debat:
                fout.append(f"verdict.json: {i} werd in het debat door geen Raadslid genoemd (de Koning kan het niet kennen)")

    # 9. geschillen
    for g in trace.get("geschillen") or []:
        for n in g.get("beurten") or []:
            if n not in raadslid_berichten:
                fout.append(f"geschil '{g.get('onderwerp')}': beurt {n} is geen Raadslid-bericht")
        for r in g.get("standpunten") or {}:
            if r not in gesproken:
                fout.append(f"geschil '{g.get('onderwerp')}': {r} sprak niet in het debat")
    if not trace.get("geschillen"):
        let.append("geen geschillen in de trace")

    samenvatting = {
        "raadslid_beurten": len(raadslid_berichten), "rollen": sorted(gesproken),
        "toolcalls": sum(1 for b in beurten if b.get("soort") == "toolcall"),
        "gerichte_toolcalls": sum(1 for b in beurten if b.get("soort") == "toolcall" and b.get("tool") != "lees_kennisbron"),
        "koning_stopte_zelf": bool(stop) and stop[-1].get("beslist_door", "koning-agent") == "koning-agent",
        "geschillen": len(trace.get("geschillen") or []),
        "routeringen_met_toetst_beurten": sum(1 for b in beurten if b.get("soort") == "routering" and b.get("toetst_beurten")),
    }
    return fout, let, samenvatting


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    map_ = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT
    trace = json.loads((map_ / "trace.json").read_text(encoding="utf-8"))
    vp = map_ / "verdict.json"
    verdict = json.loads(vp.read_text(encoding="utf-8")) if vp.exists() else None
    fout, let, s = controleer(trace, verdict)
    for f in fout:
        print("FOUT ", f)
    for l in let:
        print("LET OP", l)
    print(json.dumps(s, ensure_ascii=False))
    print("GROEN: de trace volgt alle spelregels." if not fout else f"ROOD: {len(fout)} fout(en).")
    sys.exit(1 if fout else 0)


if __name__ == "__main__":
    main()
