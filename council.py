"""Council of Achrona: Raadsleden debatteren, de Koning leidt en stelt het Verdict op.

Start met `uv run council`. Schrijft trace.json na elke beurt en verdict.json aan het einde (root van de repo).

Wie doet wat:
  - Raadslid  (MODEL_KLEIN): leest via MCP alleen de Kennisbron van de eigen Rol en brengt bewijs met ID's.
  - Koning    (MODEL_GROOT): leest geen Kennisbron. Kiest vóór elke beurt wie spreekt, met welke vraag en waarom
                             (`kies_volgende`), houdt een stand en open vragen bij, stopt zelf, en stelt het Verdict op.
  - Code: dwingt de spelregels af (Beurtenbudget, minstens 3 Rollen, één Raadslid per Rol) en controleert elk
          record-ID: het telt alleen als het Raadslid het in zijn eigen Kennisbron las (toolresultaten van zijn beurten).
"""
import asyncio
import json
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

import backend
import koning
import raadsleden

ROOT = Path(__file__).parent
ROLLEN = ("archmage", "engineer", "guardian_keeper", "historian", "oracle", "beast_master")
BEURTENBUDGET = 10  # spelregel: maximaal 10 Raadslid-beurten (berichten van Raadsleden) per run
MIN_ROLLEN = 3  # spelregel: minstens 3 Rollen


def controleer(leden):
    """Duidelijke melding in plaats van een traceback bij de meest voorkomende fouten."""
    nodig = ["MODEL_KLEIN", "MODEL_GROOT"] + (["OPENAI_BASE_URL", "OPENAI_API_KEY"] if backend.naam() == "openai" else [])
    mist = [v for v in nodig if not os.getenv(v)]
    if mist:
        sys.exit(f"In .env ontbreekt: {', '.join(mist)}. Kopieer .env.example naar .env en vul het in.")
    for lid in leden:
        if lid.ROL not in ROLLEN:
            sys.exit(f"raadsleden/: ROL = {lid.ROL!r} is ongeldig. Gebruik: {', '.join(ROLLEN)}.")
        if not os.getenv(f"MCP_{lid.ROL.upper()}", "").strip():
            sys.exit(f"MCP_{lid.ROL.upper()} ontbreekt in .env (bv. https://mcp.findingsolutions.be/{lid.ROL}/mcp).")
    dubbel = {l.ROL for l in leden if [x.ROL for x in leden].count(l.ROL) > 1}
    if dubbel:
        sys.exit(f"Eén Raadslid per Rol: {', '.join(dubbel)} komt meer dan eens voor in raadsleden/.")
    if len(leden) < MIN_ROLLEN:
        sys.exit(f"Minstens {MIN_ROLLEN} Rollen nodig in raadsleden/.")


def debat_als_tekst(debat):
    """Wat Raadsleden en Koning van het debat zien: elke beurt met de vraag van de Koning en de ID-controle."""
    delen = []
    for d in debat:
        controle = f"ID-controle: geldig {', '.join(d['geldig']) or 'geen'}"
        if d["ongeldig"]:
            controle += f"; ONGELDIG (niet in de eigen Kennisbron gelezen) {', '.join(d['ongeldig'])}"
        if not d["toolcalls"]:
            controle += "; LET OP: geen toolcall, Kennisbron niet gelezen"
        delen.append(f"[beurt {d['nr']}] {d['naam']} (vraag van de Koning: {d['vraag']})\n{d['tekst']}\n({controle})")
    return "\n\n".join(delen)


def regel_keuze(leden, gesproken, reden):
    """Vangnet als de Koning-agent geen geldige keuze geeft: de eerste Rol die nog niet sprak."""
    for lid in leden:
        if not gesproken.get(lid.ROL):
            return (lid.ROL, "Wat zegt jouw Kennisbron over de oorzaak van de Desync: wie deed wat, wanneer, en welk patroon zie je in de tijd?",
                    f"{reden} Regel: {lid.NAAM} sprak nog niet en kan nieuw bewijs inbrengen.")
    return None, None, f"{reden} Regel: alle Rollen spraken al; het debat stopt."


async def kies_volgende(leden, debat, gesproken, stand, open_vragen, tel):
    """De Koning leidt het debat: hij leest het debat en kiest (rol, vraag, reden), of stopt met rol None.

    Geeft (rol, vraag, reden, stand, open_vragen) terug. De code bewaakt alleen de spelregels.
    """
    rollen = [l.ROL for l in leden]
    try:
        a = await backend.koning(koning.KONING_SYSTEEM,
                                 koning.keuze_prompt(leden, debat_als_tekst(debat), gesproken, BEURTENBUDGET, stand, open_vragen),
                                 koning.keuze_schema(rollen))
        tel("groot", a.usage)
        k = a.structured
        rol, vraag, reden = k.get("rol"), k.get("vraag"), (k.get("reden") or "").strip()
        stand, open_vragen = k.get("stand") or stand, k.get("open_vragen") or []
    except Exception as e:
        print(f"  Koning-agent faalde: {type(e).__name__}: {e}")
        return (*regel_keuze(leden, gesproken, "De Koning-agent gaf geen antwoord."), stand, open_vragen)

    if rol is not None and rol not in rollen:
        return (*regel_keuze(leden, gesproken, f"De Koning koos {rol!r}, geen Raadslid."), stand, open_vragen)
    if rol is not None and not (vraag or "").strip():
        vraag = "Wat zegt jouw Kennisbron over de open vragen van de Koning?"
    if rol is None and sum(1 for r in gesproken if gesproken[r]) < MIN_ROLLEN:
        return (*regel_keuze(leden, gesproken, f"De Koning wilde stoppen ({reden}), maar er moeten minstens {MIN_ROLLEN} Rollen spreken."),
                stand, open_vragen)
    return rol, (vraag.strip() if vraag else None), reden or "(geen reden opgegeven)", stand, open_vragen


def zuiver_verdict(ruw, geldig, rollen):
    """Houdt alleen geverifieerde ID's over en maakt het Verdict schema-proof. Geeft (verdict, geschillen, verwijderd)."""
    verwijderd = []

    def filter_ids(ids, waar):
        uit = []
        for i in backend.ids_in(" ".join(map(str, ids or []))):
            if i in geldig:
                uit.append(i)
            else:
                verwijderd.append(f"{i} ({waar})")
        return uit

    def tekst(s):
        return backend.ID.sub(lambda m: f"{m.group(1)}-{m.group(2)}", s) if isinstance(s, str) else s

    oorzaak = {}
    for slot in ("ingreep", "mechanisme", "verloop"):
        s = (ruw.get("oorzaak") or {}).get(slot) or {}
        bewijs = filter_ids(s.get("bewijs"), slot)
        bewering = tekst(s.get("bewering")) or None
        if bewering and not bewijs:
            verwijderd.append(f"bewering '{slot}' (geen geldig bewijs, op null gezet)")
            bewering = None
        deel = {"bewering": bewering, "bewijs": bewijs if bewering else []}
        if isinstance(s.get("zekerheid"), (int, float)) and bewering:
            deel["zekerheid"] = round(min(1.0, max(0.0, float(s["zekerheid"]))), 2)
        oorzaak[slot] = deel

    acties = []
    for a in ruw.get("acties") or []:
        if not (a.get("actie") and a.get("reden")):
            continue
        item = {"actie": tekst(a["actie"]), "reden": tekst(a["reden"])}
        b = filter_ids(a.get("bewijs"), "actie")
        if b:
            item["bewijs"] = b
        acties.append(item)

    risicos = [{"risico": tekst(r["risico"]), "mitigatie": tekst(r["mitigatie"])}
               for r in ruw.get("risicos") or [] if r.get("risico") and r.get("mitigatie")]

    verdict = {"samenvatting": tekst(ruw.get("samenvatting") or ""), "oorzaak": oorzaak, "acties": acties, "risicos": risicos}
    v = ruw.get("voorspelling")
    if isinstance(v, dict) and v.get("bewering") and isinstance(v.get("dag"), int) and v["dag"] >= 1:
        b = filter_ids(v.get("bewijs"), "voorspelling")
        if b:
            verdict["voorspelling"] = {"dag": v["dag"], "bewering": tekst(v["bewering"]), "bewijs": b}
        else:
            verwijderd.append("voorspelling (geen geldig bewijs)")

    geschillen = []
    for g in ruw.get("geschillen") or []:
        standpunten = {s["rol"]: tekst(s["standpunt"]) for s in g.get("standpunten") or []
                       if s.get("rol") in rollen and s.get("standpunt")}
        if not (g.get("onderwerp") and standpunten):
            continue
        b = filter_ids(g.get("bewijs"), "geschil")
        uitkomst = tekst(g.get("uitkomst")) or None
        if uitkomst and not b:
            uitkomst = None
        geschillen.append({"onderwerp": tekst(g["onderwerp"]), "standpunten": standpunten, "uitkomst": uitkomst, "bewijs": b})
    return verdict, geschillen, verwijderd


async def run():
    load_dotenv(ROOT / ".env")
    leden = raadsleden.alle()
    controleer(leden)
    per_rol = {l.ROL: l for l in leden}
    run_id = f"{datetime.now().astimezone().isoformat(timespec='seconds')}_{uuid.uuid4().hex[:8]}"
    beurten, verbruik = [], {}
    trace = {"run_id": run_id, "status": "bezig", "raadsleden": [l.ROL for l in leden], "beurten": beurten,
             "geschillen": [], "verbruik": verbruik,
             "opzet": {"backend": backend.naam(), "model_klein": os.environ["MODEL_KLEIN"],
                       "model_groot": os.environ["MODEL_GROOT"], "beurtenbudget": BEURTENBUDGET}}

    def bewaar(naam, data):
        (ROOT / naam).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def beurt(ronde, spreker, soort, **velden):
        beurten.append({"nr": len(beurten) + 1, "ronde": ronde, "spreker": spreker, "soort": soort, **velden})
        bewaar("trace.json", trace)  # na elke beurt: de viewer (`uv run viewer`) toont het debat live

    def tel(model, usage):
        if not usage:
            return
        v = verbruik.setdefault(model, {"beurten": 0, "invoer": 0, "gecached": 0, "uitvoer": 0})
        v["beurten"] += 1
        for k in ("invoer", "gecached", "uitvoer"):
            v[k] += usage.get(k, 0)

    (ROOT / "verdict.json").unlink(missing_ok=True)  # geen Verdict van een vorige run laten staan
    bewaar("trace.json", trace)
    print(f"Run {run_id} · backend {backend.naam()} · Raadsleden: {', '.join(l.NAAM for l in leden)}")

    debat, gesproken = [], {l.ROL: 0 for l in leden}
    gelezen = {l.ROL: set() for l in leden}  # ID's die elk Raadslid in zijn eigen toolresultaten zag
    geldig_genoemd = set()  # ID's die een Raadslid noemde én in zijn eigen Kennisbron las
    stand, open_vragen = None, []

    while True:
        n = sum(gesproken.values())
        ronde = n + 1  # ronde = nummer van de Raadslid-beurt die volgt
        if n >= BEURTENBUDGET:
            rol, vraag, reden = None, None, f"Beurtenbudget op ({BEURTENBUDGET} Raadslid-beurten)."
        else:
            trace["aan_het_woord"] = "koning"
            bewaar("trace.json", trace)
            rol, vraag, reden, stand, open_vragen = await kies_volgende(leden, debat, gesproken, stand, open_vragen, tel)
        beurt(ronde if rol else n, "koning", "routering", aan=rol, vraag=vraag, reden=reden,
              open_vragen=open_vragen, stand=stand)
        print(f"\n[Koning] {'stopt' if rol is None else '→ ' + per_rol[rol].NAAM}: {reden}")
        if rol is None:
            break

        lid, mcp_url = per_rol[rol], os.environ[f"MCP_{rol.upper()}"].strip()
        trace["aan_het_woord"] = rol
        bewaar("trace.json", trace)
        try:
            a = await backend.raadslid(koning.raadslid_instructies(lid),
                                       koning.raadslid_prompt(lid, debat_als_tekst(debat), vraag), mcp_url)
        except Exception as e:
            a = backend.Antwoord(tekst=f"(Geen antwoord: technische fout {type(e).__name__}: {str(e)[:200]})")
        tel("klein", a.usage)
        for tool, args in a.toolcalls:
            beurt(ronde, rol, "toolcall", tool=tool, argumenten=args, pad=mcp_url)
        gelezen[rol] |= set(backend.ids_in(a.toolresultaten))
        eigen = koning.PREFIX[rol] + "-"
        genoemd = [i for i in backend.ids_in(a.tekst) if i.startswith(eigen)]  # ID's van anderen zijn citaten
        geldig = [i for i in genoemd if i in gelezen[rol]]
        ongeldig = [i for i in genoemd if i not in gelezen[rol]]
        geldig_genoemd |= set(geldig)
        gesproken[rol] += 1
        beurt(ronde, rol, "bericht", tekst=a.tekst,
              id_controle={"geldig": geldig, "ongeldig": ongeldig, "toolcalls": len(a.toolcalls)})
        debat.append({"nr": ronde, "rol": rol, "naam": lid.NAAM, "vraag": vraag, "tekst": a.tekst,
                      "geldig": geldig, "ongeldig": ongeldig, "toolcalls": len(a.toolcalls)})
        print(f"\n[beurt {ronde}] {lid.NAAM} ({len(a.toolcalls)} toolcalls, geldig: {', '.join(geldig) or '-'}"
              f"{'; ONGELDIG: ' + ', '.join(ongeldig) if ongeldig else ''}):\n{a.tekst}")

    # De Koning heeft geen Kennisbron: hij stelt het Verdict op uit het debat alleen.
    trace["aan_het_woord"] = "koning"
    bewaar("trace.json", trace)
    print("\n[Koning] stelt het Verdict op ...")
    a = await backend.koning(koning.VERDICT_SYSTEEM,
                             koning.verdict_prompt(leden, debat_als_tekst(debat), stand, open_vragen, geldig_genoemd),
                             koning.VERDICT_SCHEMA)
    tel("groot", a.usage)
    verdict, geschillen, verwijderd = zuiver_verdict(a.structured or {}, geldig_genoemd, [l.ROL for l in leden])
    verdict = {"run_id": run_id, **verdict}
    trace["geschillen"] = geschillen
    trace["verdict_controle"] = {"geldige_ids": sorted(geldig_genoemd), "verwijderd": verwijderd}
    o = verdict["oorzaak"]
    beurt(sum(gesproken.values()) + 1, "koning", "bericht",
          tekst="Verdict: " + verdict["samenvatting"] + "\n" + "\n".join(
              f"- {k}: {o[k]['bewering'] or 'onbekend'} ({', '.join(o[k]['bewijs']) or 'geen bewijs'})" for k in o))
    trace["status"], trace["aan_het_woord"] = "klaar", None
    bewaar("trace.json", trace)
    bewaar("verdict.json", verdict)

    print(f"\n{json.dumps(verdict, indent=2, ensure_ascii=False)}")
    if verwijderd:
        print(f"\nID-controle verwijderde: {', '.join(verwijderd)}")
    print(f"\nKlaar: verdict.json en trace.json geschreven (run_id {run_id}).")
    for model, v in verbruik.items():
        print(f"Verbruik {model}: {v['beurten']} beurten, {v['invoer']} tokens in ({v['gecached']} gecached), {v['uitvoer']} tokens uit")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(run())


if __name__ == "__main__":
    main()
