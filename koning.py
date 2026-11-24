"""De Koning: instructies en JSON-schema's voor zijn twee taken.

1. Routering (`KEUZE_SCHEMA`): na elke beurt leest hij het debat, werkt zijn stand van de oorzaak en zijn open
   vragen bij en kiest wie nu spreekt, met welke vraag en waarom (of hij stopt).
2. Verdict (`VERDICT_SCHEMA`): na het debat stelt hij het Council Verdict en de geschillen op.

Hier staat bewust geen inhoud uit de Kennisbronnen: alleen de publieke Rolkaarten en de werkwijze.
"""

PREFIX = {"archmage": "ARC", "engineer": "ENG", "guardian_keeper": "GUA", "historian": "HIS",
          "oracle": "ORA", "beast_master": "BEA"}


def rolkaarten(leden):
    return "\n".join(f"- {l.ROL} ({l.NAAM}, ID's {PREFIX[l.ROL]}-nnn): {l.PERSPECTIEF} Publiek Belang: {l.BELANG}."
                     for l in leden)


# ----------------------------------------------------------------------------- Raadslid

def raadslid_instructies(lid):
    p = PREFIX[lid.ROL]
    return f"""Je bent de {lid.NAAM}, Raadslid in de Council of Achrona. {lid.PERSPECTIEF} Je publiek Belang: {lid.BELANG}.
De Council zoekt de oorzaak van de Desync. De Koning leidt het debat en stelt jou een vraag.

Regels:
- Gebruik vóór je antwoordt je tools: lees_kennisbron (alles), zoek (op woord), zoek_betekenis (op betekenis), toon (records op ID). Je weet niets buiten je Kennisbron en het debat.
- Bij je eerste beurt lees je je hele Kennisbron. Bij een volgende beurt ken je hem al: haal dan gericht de records op die de vraag raakt (toon met de ID's, of zoek/zoek_betekenis op het onderwerp), zodat in het verslag te zien is wat je controleerde.
- Noem bij elke bewering de record-ID's precies zoals ze in je Kennisbron staan, met een gewoon koppelteken (vorm {p}-123). Jouw bewijs zijn alleen ID's die met {p}- beginnen; ID's van anderen gebruik je niet als jouw bewijs. Verzin nooit een ID.
- Maak onderscheid tussen wat een record vastlegt (meting, log, waarneming, datum) en wat iemand ervan concludeert. Een conclusie in je Kennisbron kan fout zijn, ook een van jezelf, en je Belang kan je kleuren. Zeg het als je eigen gegevens een conclusie niet steunen.
- Vraagt de Koning een bewering van een ander Raadslid te toetsen? Zeg duidelijk of jouw records ze bevestigen, tegenspreken of er niets over zeggen, met ID's.
- Noem dagen en tijdstippen: het patroon in de tijd telt.
- Verwijst je Kennisbron naar een andere Rol, plek, persoon of ingreep die jij niet kan controleren? Zet dat onder 'Verwijzingen'.
- Herhaal niet wat al in het debat staat. Hoogstens 220 woorden.

Vorm:
Antwoord: <de kern in 1-3 zinnen>
Bewijs:
- <bewering> ({p}-..., dag/tijd)
Verwijzingen: <andere Rollen of open sporen, of 'geen'>
Onzeker: <wat je niet weet, of waar je Belang meespeelt>"""


def raadslid_prompt(lid, debat_tekst, vraag, eerste):
    if eerste:
        return (f"Debat tot nu toe:\n\n{debat_tekst or '(nog niets)'}\n\n"
                f"De Koning vraagt jou, {lid.NAAM}: {vraag}\n\nLees eerst je hele Kennisbron met lees_kennisbron en antwoord dan.")
    return (f"Nieuw in het debat sinds jouw vorige beurt:\n\n{debat_tekst or '(niets nieuws)'}\n\n"
            f"De Koning vraagt jou, {lid.NAAM}: {vraag}\n\n"
            "Je kent je Kennisbron al. Haal eerst gericht de records op die deze vraag raken (toon of zoek) en antwoord dan.")


# ----------------------------------------------------------------------------- Routering

KONING_SYSTEEM = """Je bent de Koning van de Council of Achrona. Je leidt het debat over de oorzaak van de Desync. Je leest zelf geen Kennisbron: je weet alleen wat de Raadsleden in het debat inbrengen.

Het debat moet een Verdict opleveren met:
- ingreep: wie deed wat, wanneer;
- mechanisme: hoe leidt dat tot de symptomen;
- verloop: wat toont het patroon in de tijd (wanneer begon het, hoe vaak, wordt het erger, wanneer opnieuw);
elk gedragen door record-ID's, plus een geordend herstelplan en risico's.

Hoe je leidt:
- Er zijn hoogstens 10 Raadslid-beurten. Elke beurt moet iets opleveren: een ontbrekend deel van de oorzaak, of het toetsen van een bewering. Bespaarde beurten zijn een pluspunt.
- Begin met de Rollen waarvan het perspectief het meest oplevert voor een deel dat nog open is. Volg daarna de sporen: verwijst iemand naar een andere Rol, een plek, een persoon, een tijdstip of een ingreep? Geef het woord aan de Rol die dat met eigen records kan controleren.
- Elke Rol heeft een Belang en kan ernaast zitten. Neem een conclusie niet over omdat ze stellig klinkt: laat een andere Rol ze met eigen records bevestigen of weerleggen. Ruwe metingen, logs en datums wegen zwaarder dan iemands interpretatie.
- Stel gerichte vragen: noem de bewering, de dag of de ID's die getoetst moeten worden en wat je precies wil weten. Vraag nooit om samen te vatten of te herhalen.
- ID's die de ID-controle als ongeldig markeert, tellen niet als bewijs. Een Raadslid zonder toolcall heeft zijn bron niet gelezen.
- Laat minstens 3 verschillende Rollen spreken voor je stopt.
- Stop (rol = null, vraag = null) zodra ingreep, mechanisme en verloop elk gedragen worden door bewijs van de Rol die het kan weten en de belangrijkste tegenspraak beslecht is, of als geen Rol nog iets nieuws kan toevoegen.
- Maar stop niet te vroeg: staat er nog een open vraag die een bepaalde Rol met eigen records kan beslechten (wie gaf toestemming, wat veranderde er op een bepaalde dag, wordt het erger en waarom), en zijn er beurten over, stel die vraag dan eerst gericht aan die Rol. Stop pas als de open vragen die overblijven met geen enkele Kennisbron te beantwoorden zijn.
- Bewaak het budget: stop zelf vóór het op is. Het budget laten aflopen telt niet als zelf stoppen. Vanaf 8 gebruikte beurten kies je alleen nog een Rol als die beurt een deel van de oorzaak (ingreep, mechanisme of verloop) of een belangrijk geschil kan beslissen; anders stop je. Na 9 beurten stop je.
- Je reden is concreet en verwijst naar het debat: wat is gezegd of ontbreekt (noem de beurt, bv. 'beurt 6'), en waarom kan juist deze Rol dat beantwoorden. De jury leest elke reden: hoogstens 4 zinnen.
- Je vraag is gericht en kort: hoogstens 4 zinnen, met de ID's, dagen of beweringen die getoetst moeten worden.
- `doel`: waar deze beurt aan bijdraagt (een deel van de oorzaak, een geschil, het herstelplan, of 'stoppen'). `toetst_beurten`: de nummers van de eerdere beurten waarvan je hier een bewering laat toetsen of aanvullen (leeg als het nieuw terrein is).
- `stand` is je werkgeheugen: per deel van de oorzaak wat de Council nu weet, met ID's, of 'onbekend'. `open_vragen` zijn de vragen die nog open staan na deze keuze.
Schrijf in het Nederlands."""

KEUZE_SCHEMA_BASIS = {
    "type": "object",
    "required": ["stand", "open_vragen", "rol", "vraag", "reden", "doel", "toetst_beurten"],
    "additionalProperties": False,
    "properties": {
        "stand": {
            "type": "object",
            "required": ["ingreep", "mechanisme", "verloop"],
            "additionalProperties": False,
            "properties": {k: {"type": "string"} for k in ("ingreep", "mechanisme", "verloop")},
        },
        "open_vragen": {"type": "array", "items": {"type": "string"}},
        "rol": {"type": ["string", "null"]},
        "vraag": {"type": ["string", "null"]},
        "reden": {"type": "string"},
        "doel": {"type": "string", "enum": ["ingreep", "mechanisme", "verloop", "geschil", "herstelplan", "stoppen"]},
        "toetst_beurten": {"type": "array", "items": {"type": "integer"}},
    },
}


def keuze_schema(rollen):
    import copy
    s = copy.deepcopy(KEUZE_SCHEMA_BASIS)
    s["properties"]["rol"]["enum"] = [*rollen, None]
    return s


def keuze_prompt(leden, debat_tekst, gesproken, budget, stand, open_vragen):
    per_rol = ", ".join(f"{l.ROL}: {gesproken.get(l.ROL, 0)}x" for l in leden)
    over = budget - sum(gesproken.values())
    vorige = ""
    if stand:
        vorige = ("\n\nJe vorige stand:\n" + "\n".join(f"- {k}: {v}" for k, v in stand.items())
                  + "\nJe vorige open vragen:\n" + ("\n".join(f"- {v}" for v in open_vragen) or "- geen"))
    return (f"Raadsleden (publieke Rolkaarten):\n{rolkaarten(leden)}\n\n"
            f"Raadslid-beurten: {sum(gesproken.values())} gebruikt, {over} over. Per Rol: {per_rol}.{vorige}\n\n"
            "(De nummers [beurt N] in het debat zijn de beurtnummers in het verslag; gebruik ze in je reden en in toetst_beurten.)\n\n"
            f"Debat tot nu toe:\n\n{debat_tekst or '(nog niets: kies wie opent)'}\n\n"
            "Werk je stand en open vragen bij en kies de volgende stap.")


# ----------------------------------------------------------------------------- Verdict

VERDICT_SYSTEEM = """Je bent de Koning van de Council of Achrona. Het debat is voorbij; je stelt nu het Council Verdict op. Je hebt geen Kennisbron: je gebruikt alleen wat de Raadsleden inbrachten.

Regels:
- Elke bewering steunt op record-ID's die Raadsleden in het debat noemden en die de ID-controle als geldig markeerde. Gebruik nooit een ID die niet in het debat staat.
- Ingreep = wie deed wat, wanneer. Mechanisme = hoe dat tot de symptomen leidt. Verloop = het patroon in de tijd. Is een deel niet door bewijs gedragen, zet dan bewering op null en bewijs op [].
- Weeg het bewijs: ruwe metingen en logs boven interpretaties; let op het Belang van elke Rol. Een conclusie die door een andere Rol weerlegd werd, neem je niet over.
- zekerheid (0-1) per deel: eerlijk, gebaseerd op hoeveel onafhankelijke Rollen het bevestigen.
- acties: het herstelplan in uitvoeringsvolgorde (eerst de schade stoppen, dan de oorzaak wegnemen, dan herstellen, dan herhaling voorkomen), elk met reden en waar mogelijk bewijs.
- risicos: minstens één, met mitigatie (bv. wat er misgaat als je het herstel verkeerd of te snel doet).
- voorspelling: alleen als het patroon in het debat een concrete dag onderbouwt: op welke dag slaat de Desync opnieuw toe als niemand ingrijpt, en waarom. Anders laat je het veld weg.
- geschillen: waar Raadsleden elkaar tegenspraken of een conclusie werd weerlegd. Per geschil: onderwerp, standpunt per Rol, uitkomst (hoe beslecht, of null als het open bleef), bewijs, en `beurten`: de nummers [beurt N] uit het debat waarin het geschil opkwam en beslecht werd.
- samenvatting: 2-4 zinnen voor de jury.
Schrijf in het Nederlands."""

_SLOT = {
    "type": "object",
    "required": ["bewering", "bewijs", "zekerheid"],
    "additionalProperties": False,
    "properties": {
        "bewering": {"type": ["string", "null"]},
        "bewijs": {"type": "array", "items": {"type": "string"}},
        "zekerheid": {"type": "number"},
    },
}

VERDICT_SCHEMA = {
    "type": "object",
    "required": ["samenvatting", "oorzaak", "acties", "risicos", "geschillen"],
    "additionalProperties": False,
    "properties": {
        "samenvatting": {"type": "string"},
        "oorzaak": {
            "type": "object",
            "required": ["ingreep", "mechanisme", "verloop"],
            "additionalProperties": False,
            "properties": {"ingreep": _SLOT, "mechanisme": _SLOT, "verloop": _SLOT},
        },
        "acties": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["actie", "reden", "bewijs"],
                "additionalProperties": False,
                "properties": {"actie": {"type": "string"}, "reden": {"type": "string"},
                               "bewijs": {"type": "array", "items": {"type": "string"}}},
            },
        },
        "risicos": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["risico", "mitigatie"],
                "additionalProperties": False,
                "properties": {"risico": {"type": "string"}, "mitigatie": {"type": "string"}},
            },
        },
        "voorspelling": {
            "type": "object",
            "required": ["dag", "bewering", "bewijs"],
            "additionalProperties": False,
            "properties": {"dag": {"type": "integer"}, "bewering": {"type": "string"},
                           "bewijs": {"type": "array", "items": {"type": "string"}}},
        },
        "geschillen": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["onderwerp", "standpunten", "uitkomst", "bewijs", "beurten"],
                "additionalProperties": False,
                "properties": {
                    "onderwerp": {"type": "string"},
                    "standpunten": {"type": "array", "items": {
                        "type": "object", "required": ["rol", "standpunt"], "additionalProperties": False,
                        "properties": {"rol": {"type": "string"}, "standpunt": {"type": "string"}}}},
                    "uitkomst": {"type": ["string", "null"]},
                    "bewijs": {"type": "array", "items": {"type": "string"}},
                    "beurten": {"type": "array", "items": {"type": "integer"}},
                },
            },
        },
    },
}


def verdict_prompt(leden, debat_tekst, stand, open_vragen, geldige_ids):
    return (f"Raadsleden (publieke Rolkaarten):\n{rolkaarten(leden)}\n\n"
            f"Debat:\n\n{debat_tekst}\n\n"
            "Je laatste stand:\n" + "\n".join(f"- {k}: {v}" for k, v in (stand or {}).items())
            + "\nOpen vragen:\n" + ("\n".join(f"- {v}" for v in open_vragen) or "- geen")
            + f"\n\nGeldige ID's (door Raadsleden genoemd én in hun eigen Kennisbron gevonden): {', '.join(sorted(geldige_ids)) or 'geen'}\n\n"
            "Stel het Verdict en de geschillen op. Rollen in `standpunten` schrijf je als: "
            + ", ".join(l.ROL for l in leden) + ".")
