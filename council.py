"""Council of Achrona: Raadsleden debatteren, de Koning leidt en stelt het Verdict op.

Start met `uv run council`. Schrijft trace.json na elke beurt en verdict.json aan het einde (root van de repo).

Wie doet wat:
  - Raadslid  (MODEL_KLEIN): leest via MCP alleen de Kennisbron van de eigen Rol en brengt bewijs met ID's.
                             Hij hervat zijn eigen gesprek: eerste beurt leest hij alles, daarna haalt hij gericht records op.
  - Koning    (MODEL_GROOT): leest geen Kennisbron. Kiest vóór elke beurt wie spreekt, met welke vraag, waarom, met welk
                             doel en welke eerdere beurt getoetst wordt (`kies_volgende`); houdt een stand en open vragen
                             bij, stopt zelf, en stelt het Verdict en de geschillen op.
  - Code: dwingt de spelregels af (Beurtenbudget, minstens 3 Rollen, één Raadslid per Rol) en controleert elk
          record-ID: het telt alleen als het Raadslid het in zijn eigen toolresultaten las.

De trace is het verslag voor de jury. Naast de verplichte velden staat er per beurt:
  routering  doel, toetst_beurten (nummers van eerdere beurten), beslist_door, stand, open_vragen
  toolcall   resultaat {records, ids}: welke records de call teruggaf
  bericht    id_controle {geldig, ongeldig, toolcalls}, duur_s, tokens
  elke beurt tijd (ISO-tijdstip)
"""
import asyncio
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

import backend
import controleer_trace
import koning
import raadsleden

ROOT = Path(__file__).parent
ROLLEN = ("archmage", "engineer", "guardian_keeper", "historian", "oracle", "beast_master")
BEURTENBUDGET = 10  # spelregel: maximaal 10 Raadslid-beurten (berichten van Raadsleden) per run
MIN_ROLLEN = 3  # spelregel: minstens 3 Rollen


def nu():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def controleer(leden):
    """Duidelijke melding in plaats van een traceback bij de meest voorkomende fouten."""
    nodig = ["MODEL_KLEIN", "MODEL_GROOT"] + (["OPENAI_BASE_URL", "OPENAI_API_KEY"] if backend.naam() == "openai" else [])
    mist = [v for v in nodig if not os.getenv(v)]
    if mist:
        sys.exit(f"In .env ontbreekt: {', '.join(mist)}. Kopieer .env.example naar .env en vul het in.")
    for lid in leden:
        if lid.ROL not in ROLLEN:
            sys.exit(f"raadsleden/: ROL = {lid.ROL!r} is ongeldig. Gebruik: {', '.join(ROLLEN)}.")
        pad = os.getenv(f"MCP_{lid.ROL.upper()}", "").strip()
        if not pad:
            sys.exit(f"MCP_{lid.ROL.upper()} ontbreekt in .env (bv. https://mcp.findingsolutions.be/{lid.ROL}/mcp).")
        if f"/{lid.ROL}/" not in pad:
            sys.exit(f"MCP_{lid.ROL.upper()} = {pad} is niet het pad van {lid.NAAM}: elk Raadslid leest alleen zijn eigen Kennisbron.")
    dubbel = {l.ROL for l in leden if [x.ROL for x in leden].count(l.ROL) > 1}
    if dubbel:
        sys.exit(f"Eén Raadslid per Rol: {', '.join(dubbel)} komt meer dan eens voor in raadsleden/.")
    if len(leden) < MIN_ROLLEN:
        sys.exit(f"Minstens {MIN_ROLLEN} Rollen nodig in raadsleden/.")


def debat_als_tekst(debat):
    """Wat Raadsleden en Koning van het debat zien: elke beurt (nummer = nr in de trace), de vraag en de ID-controle."""
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
            return {"rol": lid.ROL, "doel": "ingreep", "toetst_beurten": [], "beslist_door": "regel (vangnet)",
                    "vraag": "Wat zegt jouw Kennisbron over de oorzaak van de Desync: wie deed wat, wanneer, en welk patroon zie je in de tijd?",
                    "reden": f"{reden} Regel: {lid.NAAM} sprak nog niet en kan nieuw bewijs inbrengen."}
    return {"rol": None, "vraag": None, "doel": "stoppen", "toetst_beurten": [], "beslist_door": "regel (vangnet)",
            "reden": f"{reden} Regel: alle Rollen spraken al; het debat stopt."}


def stop_herinnering(leden, gesproken, doelen):
    """Wil de Koning stoppen terwijl er nog werk ligt (Rol niet gehoord, geen herstelplan-beurt), dan krijgt hij
    één keer een herinnering. Hij beslist zelf: hij mag daarna nog steeds stoppen, met een reden."""
    over = BEURTENBUDGET - sum(gesproken.values())
    if over < 1:
        return None
    punten = []
    ongehoord = [l.NAAM for l in leden if not gesproken.get(l.ROL)]
    if ongehoord:
        punten.append(f"Nog niet gehoord: {', '.join(ongehoord)}. Hun Kennisbron kan een uniek stuk bevatten.")
    if "herstelplan" not in doelen:
        punten.append("Er was nog geen beurt met doel 'herstelplan': niemand is gevraagd hoe je de oorzaak veilig wegneemt "
                      "(bediening, voorschriften of waarschuwingen over herstel in hun bron).")
    if not punten:
        return None
    return ("\n\nJe wilde stoppen, maar let op (" + f"{over} beurten over" + "):\n- " + "\n- ".join(punten)
            + "\nKies een Rol voor een van deze punten, of stop alleen als je in je reden concreet uitlegt waarom geen enkele Rol dit nog kan beantwoorden.")


async def kies_volgende(leden, debat, gesproken, stand, open_vragen, tel, doelen=()):
    """De Koning leidt het debat: hij leest het debat en kiest wie spreekt (of stopt), met vraag, reden en doel.

    Geeft een dict {rol, vraag, reden, doel, toetst_beurten, beslist_door, stand, open_vragen}. De code bewaakt
    alleen de spelregels; grijpt het vangnet in, dan staat dat in `beslist_door` en in de reden.
    """
    rollen = [l.ROL for l in leden]
    bericht_nrs = {d["nr"] for d in debat}
    herinnerd = False
    try:
        prompt = koning.keuze_prompt(leden, debat_als_tekst(debat), gesproken, BEURTENBUDGET, stand, open_vragen)
        a = await backend.koning(koning.KONING_SYSTEEM, prompt, koning.keuze_schema(rollen))
        tel("groot", a.usage)
        k = a.structured
        herinnering = stop_herinnering(leden, gesproken, doelen) if k and k.get("rol") is None else None
        if herinnering:  # tweede kans; de Koning beslist zelf opnieuw
            a = await backend.koning(koning.KONING_SYSTEEM, prompt + herinnering, koning.keuze_schema(rollen))
            tel("groot", a.usage)
            k, herinnerd = a.structured, True
        stand, open_vragen = k.get("stand") or stand, k.get("open_vragen") or []
        keuze = {"rol": k.get("rol"), "vraag": (k.get("vraag") or "").strip() or None,
                 "reden": (k.get("reden") or "").strip() or "(geen reden opgegeven)",
                 "doel": k.get("doel") or ("stoppen" if k.get("rol") is None else "ingreep"),
                 "toetst_beurten": sorted({n for n in k.get("toetst_beurten") or [] if n in bericht_nrs}),
                 "beslist_door": "koning-agent (na herinnering)" if herinnerd else "koning-agent"}
    except backend.Gebruikslimiet:
        raise  # limiet op: de run stopt (zie main); geen routeringen door het vangnet die op een Koning lijken
    except Exception as e:
        print(f"  Koning-agent faalde: {type(e).__name__}: {e}")
        return {**regel_keuze(leden, gesproken, f"De Koning-agent gaf geen bruikbaar antwoord ({type(e).__name__})."),
                "stand": stand, "open_vragen": open_vragen}

    if keuze["rol"] is not None and keuze["rol"] not in rollen:
        keuze = regel_keuze(leden, gesproken, f"De Koning koos {keuze['rol']!r}, geen Raadslid.")
    elif keuze["rol"] is not None and not keuze["vraag"]:
        keuze["vraag"] = "Wat zegt jouw Kennisbron over de open vragen van de Koning?"
    elif keuze["rol"] is None:
        keuze["vraag"], keuze["doel"] = None, "stoppen"
        if sum(1 for r in gesproken if gesproken[r]) < MIN_ROLLEN:
            keuze = regel_keuze(leden, gesproken, f"De Koning wilde stoppen ({keuze['reden']}), maar er moeten minstens {MIN_ROLLEN} Rollen spreken.")
    return {**keuze, "stand": stand, "open_vragen": open_vragen}


def zuiver_verdict(ruw, geldig, rollen, bericht_nrs, gesproken_rollen):
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
        """Gewoon koppelteken; een niet-geverifieerd ID in vrije tekst gaat eruit (en staat in `verwijderd`)."""
        if not isinstance(s, str):
            return s

        def vervang(m):
            i = f"{m.group(1)}-{m.group(2)}"
            if i in geldig:
                return i
            verwijderd.append(f"{i} (in tekst)")
            return ""
        s = backend.ID.sub(vervang, s)
        s = re.sub(r"\(\s*[,;/ ]*\s*\)", "", s)  # lege haakjes na het weghalen
        s = re.sub(r"\(\s*[,;/]\s*", "(", s)
        s = re.sub(r"\s*[,;/]\s*\)", ")", s)
        return re.sub(r"\s{2,}", " ", s).replace(" .", ".").replace(" ,", ",").strip()

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
        standpunten = {}
        for s in g.get("standpunten") or []:
            if s.get("rol") in rollen and s.get("standpunt"):
                if s["rol"] in gesproken_rollen:
                    standpunten[s["rol"]] = tekst(s["standpunt"])
                else:  # een Rol die niet sprak heeft geen standpunt in dit debat
                    verwijderd.append(f"standpunt van {s['rol']} in geschil '{g.get('onderwerp')}' (sprak niet)")
        if not (g.get("onderwerp") and standpunten):
            continue
        b = filter_ids(g.get("bewijs"), "geschil")
        uitkomst = tekst(g.get("uitkomst")) or None
        if uitkomst and not b:
            uitkomst = None
        geschillen.append({"onderwerp": tekst(g["onderwerp"]), "standpunten": standpunten, "uitkomst": uitkomst, "bewijs": b,
                           "beurten": sorted({n for n in g.get("beurten") or [] if n in bericht_nrs})})
    return verdict, geschillen, verwijderd


async def run():
    load_dotenv(ROOT / ".env")
    leden = raadsleden.alle()
    controleer(leden)
    per_rol = {l.ROL: l for l in leden}
    paden = {l.ROL: os.environ[f"MCP_{l.ROL.upper()}"].strip() for l in leden}
    run_id = f"{nu()}_{uuid.uuid4().hex[:8]}"
    beurten, verbruik = [], {}
    trace = {"run_id": run_id, "status": "bezig", "raadsleden": [l.ROL for l in leden], "beurten": beurten,
             "geschillen": [], "verbruik": verbruik,
             "opzet": {"backend": backend.naam(), "model_raadsleden": os.environ["MODEL_KLEIN"],
                       "model_koning": os.environ["MODEL_GROOT"], "beurtenbudget": BEURTENBUDGET,
                       "kennisbron_per_raadslid": paden,  # elk Raadslid krijgt alleen deze ene MCP-server
                       "tools_koning": [],  # de Koning heeft geen enkele tool en geen Kennisbron
                       "gestart": nu()}}

    def bewaar(naam, data):
        (ROOT / naam).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def beurt(ronde, spreker, soort, **velden):
        nr = len(beurten) + 1
        beurten.append({"nr": nr, "ronde": ronde, "spreker": spreker, "soort": soort, **velden, "tijd": nu()})
        bewaar("trace.json", trace)  # na elke beurt: de viewer (`uv run viewer`) toont het debat live
        return nr

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
    sessies = {l.ROL: {} for l in leden}  # elk Raadslid hervat zijn eigen gesprek
    gezien_tot = {l.ROL: 0 for l in leden}  # tot waar in het debat dit Raadslid al bij was
    gelezen = {l.ROL: set() for l in leden}  # ID's die elk Raadslid in zijn eigen toolresultaten zag
    geldig_genoemd = set()  # ID's die een Raadslid noemde én in zijn eigen Kennisbron las
    stand, open_vragen = None, []
    doelen = []  # doel van elke routering tot nu toe (voor de stop-herinnering)

    while True:
        n = sum(gesproken.values())
        ronde = n + 1  # ronde = nummer van de Raadslid-beurt die volgt
        if n >= BEURTENBUDGET:
            k = {"rol": None, "vraag": None, "doel": "stoppen", "toetst_beurten": [], "beslist_door": "spelregel",
                 "reden": f"Beurtenbudget op ({BEURTENBUDGET} Raadslid-beurten).", "stand": stand, "open_vragen": open_vragen}
        else:
            trace["aan_het_woord"] = "koning"
            bewaar("trace.json", trace)
            k = await kies_volgende(leden, debat, gesproken, stand, open_vragen, tel, doelen)
        stand, open_vragen, rol = k["stand"], k["open_vragen"], k["rol"]
        doelen.append(k["doel"])
        beurt(ronde if rol else n, "koning", "routering", aan=rol, vraag=k["vraag"], reden=k["reden"],
              doel=k["doel"], toetst_beurten=k["toetst_beurten"], beslist_door=k["beslist_door"],
              open_vragen=open_vragen, stand=stand)
        print(f"\n[Koning · {k['doel']}{' · toetst ' + str(k['toetst_beurten']) if k['toetst_beurten'] else ''}] "
              f"{'stopt' if rol is None else '→ ' + per_rol[rol].NAAM}: {k['reden']}")
        if rol is None:
            break

        lid, mcp_url = per_rol[rol], paden[rol]
        eerste = gesproken[rol] == 0
        nieuw = debat if eerste else [d for d in debat if d["nr"] > gezien_tot[rol]]
        trace["aan_het_woord"] = rol
        bewaar("trace.json", trace)
        t0 = time.monotonic()
        # Faalt een Raadslid (ook na de retries), dan stopt de run: een 'bericht' zonder Kennisbron zou in de
        # trace als een echte beurt tellen. main() zet de trace dan op status 'afgebroken' met de reden.
        a = await backend.raadslid(koning.raadslid_instructies(lid),
                                   koning.raadslid_prompt(lid, debat_als_tekst(nieuw), k["vraag"], eerste),
                                   mcp_url, sessies[rol])
        duur = round(time.monotonic() - t0, 1)
        tel("klein", a.usage)
        for c in a.toolcalls:
            beurt(ronde, rol, "toolcall", tool=c["tool"], argumenten=c["argumenten"], pad=mcp_url,
                  resultaat={"records": len(c["ids"]), "ids": c["ids"]})
        gelezen[rol] |= set(backend.ids_in(a.toolresultaten))
        eigen = koning.PREFIX[rol] + "-"
        genoemd = [i for i in backend.ids_in(a.tekst) if i.startswith(eigen)]  # ID's van anderen zijn citaten
        geldig = [i for i in genoemd if i in gelezen[rol]]
        ongeldig = [i for i in genoemd if i not in gelezen[rol]]
        geldig_genoemd |= set(geldig)
        gesproken[rol] += 1
        nr = beurt(ronde, rol, "bericht", tekst=a.tekst,
                   id_controle={"geldig": geldig, "ongeldig": ongeldig, "toolcalls": len(a.toolcalls)},
                   duur_s=duur, tokens=a.usage or {})
        debat.append({"nr": nr, "rol": rol, "naam": lid.NAAM, "vraag": k["vraag"], "tekst": a.tekst,
                      "geldig": geldig, "ongeldig": ongeldig, "toolcalls": len(a.toolcalls)})
        gezien_tot[rol] = nr
        calls = ", ".join(f"{c['tool']}({json.dumps(c['argumenten'], ensure_ascii=False)})→{len(c['ids'])}" for c in a.toolcalls)
        print(f"\n[beurt {nr}] {lid.NAAM} · {calls or 'GEEN toolcall'} · geldig: {', '.join(geldig) or '-'}"
              f"{'; ONGELDIG: ' + ', '.join(ongeldig) if ongeldig else ''} · {duur}s\n{a.tekst}")

    # De Koning heeft geen Kennisbron: hij stelt het Verdict op uit het debat alleen.
    trace["aan_het_woord"] = "koning"
    bewaar("trace.json", trace)
    print("\n[Koning] stelt het Verdict op ...")
    a = await backend.koning(koning.VERDICT_SYSTEEM,
                             koning.verdict_prompt(leden, debat_als_tekst(debat), stand, open_vragen, geldig_genoemd),
                             koning.VERDICT_SCHEMA)
    tel("groot", a.usage)
    verdict, geschillen, verwijderd = zuiver_verdict(a.structured or {}, geldig_genoemd, [l.ROL for l in leden],
                                                     {d["nr"] for d in debat}, {r for r, x in gesproken.items() if x})
    verdict = {"run_id": run_id, **verdict}
    trace["geschillen"] = geschillen
    trace["verdict_controle"] = {"geldige_ids": sorted(geldig_genoemd), "verwijderd": verwijderd}
    o = verdict["oorzaak"]
    beurt(sum(gesproken.values()) + 1, "koning", "bericht",
          tekst="Verdict: " + verdict["samenvatting"] + "\n" + "\n".join(
              f"- {k_}: {o[k_]['bewering'] or 'onbekend'} ({', '.join(o[k_]['bewijs']) or 'geen bewijs'})" for k_ in o))
    trace["status"], trace["aan_het_woord"] = "klaar", None
    trace["opzet"]["klaar"] = nu()
    # Zelfcontrole: dezelfde strenge controle als `uv run controleer`, op de trace zoals ze nu is.
    fouten, let_op, samenvatting = controleer_trace.controleer(trace, verdict)
    trace["zelfcontrole"] = {"fouten": fouten, "let_op": let_op, "samenvatting": samenvatting}
    bewaar("trace.json", trace)
    bewaar("verdict.json", verdict)
    print(f"\nZelfcontrole van de trace: {'GROEN' if not fouten else 'ROOD: ' + '; '.join(fouten)}")

    print(f"\n{json.dumps(verdict, indent=2, ensure_ascii=False)}")
    if verwijderd:
        print(f"\nID-controle verwijderde: {', '.join(verwijderd)}")
    print(f"\nKlaar: verdict.json en trace.json geschreven (run_id {run_id}).")
    for model, v in verbruik.items():
        print(f"Verbruik {model}: {v['beurten']} beurten, {v['invoer']} tokens in ({v['gecached']} gecached), {v['uitvoer']} tokens uit")


def markeer_afgebroken(e):
    """Stopt de run door een fout, dan zegt de trace dat eerlijk: status 'afgebroken', de reden, en geen Verdict."""
    pad = ROOT / "trace.json"
    try:
        trace = json.loads(pad.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if trace.get("status") != "bezig":
        return
    trace["status"], trace["aan_het_woord"] = "afgebroken", None
    trace["fout"] = {"soort": "gebruikslimiet" if isinstance(e, backend.Gebruikslimiet) else type(e).__name__,
                     "melding": str(e)[:500], "tijd": nu()}
    pad.write_text(json.dumps(trace, indent=2, ensure_ascii=False), encoding="utf-8")
    (ROOT / "verdict.json").unlink(missing_ok=True)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        asyncio.run(run())
    except (Exception, KeyboardInterrupt) as e:
        markeer_afgebroken(e)
        soort = "Gebruikslimiet bereikt" if isinstance(e, backend.Gebruikslimiet) else f"{type(e).__name__}"
        sys.exit(f"\nRun afgebroken ({soort}): {str(e)[:300]}\ntrace.json staat op status 'afgebroken'; er is geen verdict.json.")


if __name__ == "__main__":
    main()
