"""Bewijsdossier: maakt elk deel van het Verdict controleerbaar tot op de bronregel.

Start met `uv run bewijsdossier` (root van de repo) of `uv run bewijsdossier <map-met-trace-en-verdict>`.

Dit draait NA de run en hoort niet bij de Council: de Koning en de Raadsleden zien het nooit,
en trace.json en verdict.json blijven ongewijzigd. Het script schrijft `bewijsdossier.json`
(de viewer toont het in de tab Bewijs) en `bewijsdossier.md` (leesbaar op GitHub).

Per bewering in het Verdict (ingreep, mechanisme, verloop, elke actie, de voorspelling):
- de bewijs-ID's met de letterlijke recordtekst, opgehaald met `toon` op het MCP-pad van de
  Rol die het record bezit (zelfde pad als in de run; elke Rol alleen zijn eigen records);
- in welke toolcall van de run dat Raadslid het record las en in welke beurt het genoemd werd;
- hoeveel verschillende Rollen de bewering dragen (triangulatie: hoe meer onafhankelijke
  Kennisbronnen, hoe minder de bewering op het Belang van één Rol steunt).

Daarnaast de stabiliteit over eerdere runs in `archief/`: komen dezelfde dagen, tijden,
voorspelling en bewijs-ID's terug? Het script bevat zelf geen inhoud uit de Kennisbronnen:
alles wat het toont komt uit de run-bestanden of uit de MCP-server.
"""
import asyncio
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent
PREFIX = {"ARC": "archmage", "ENG": "engineer", "GUA": "guardian_keeper",
          "HIS": "historian", "ORA": "oracle", "BEA": "beast_master"}
NAAM = {"archmage": "Archmage", "engineer": "Engineer", "guardian_keeper": "Guardian Keeper",
        "historian": "Historian", "oracle": "Oracle", "beast_master": "Beast Master"}
ID_RE = re.compile(r"\b(ARC|ENG|GUA|HIS|ORA|BEA)[-\u2010-\u2015\u2212](\d{3})\b")
FEIT_RE = re.compile(r"\bdag\s+\d+\b|\b\d{1,2}:\d{2}\b|\b\d+(?:[.,]\d+)?\s?%", re.I)
SLOTS = ("ingreep", "mechanisme", "verloop")


def ids_in(tekst):
    return [f"{p}-{n}" for p, n in ID_RE.findall(str(tekst or ""))]


def rol_van(rid):
    return PREFIX.get(rid[:3])


# ---------- recordtekst uit de MCP-uitvoer van `toon` ----------

def ontleed_toon(tekst, gevraagd):
    """`toon` geeft per bestand een blok '## <bestand>': CSV (kopregel + rijen) of tekst met [ID]-koppen.
    Geeft {id: {"bestand", "tekst"}} terug, alleen voor de gevraagde ID's."""
    uit = {}
    gevraagd = set(gevraagd)
    blokken = re.split(r"^##\s+(.+?)\s*$", tekst, flags=re.M)
    # blokken = [voor, bestand1, inhoud1, bestand2, inhoud2, ...]
    for i in range(1, len(blokken) - 1, 2):
        bestand, inhoud = blokken[i].strip(), blokken[i + 1].strip("\n")
        regels = [r for r in inhoud.splitlines() if r.strip()]
        if bestand.lower().endswith(".csv") and regels:
            import csv
            rijen = list(csv.reader(regels))
            kop = rijen[0]
            for rij in rijen[1:]:
                if rij and rij[0] in gevraagd:
                    velden = [f"{k}: {v}" for k, v in zip(kop[1:], rij[1:]) if v.strip()]
                    uit[rij[0]] = {"bestand": bestand, "tekst": " · ".join(velden)}
            continue
        kopjes = list(re.finditer(r"^\[([A-Z]{3}-\d{3})\]\s*(.*)$", inhoud, flags=re.M))
        for j, m in enumerate(kopjes):
            if m.group(1) not in gevraagd:
                continue
            eind = kopjes[j + 1].start() if j + 1 < len(kopjes) else len(inhoud)
            romp = inhoud[m.end():eind].strip()
            uit[m.group(1)] = {"bestand": bestand, "kop": m.group(2).strip(), "tekst": romp}
        if not kopjes:  # ander formaat: zoek de regel met het ID
            for r in regels:
                for rid in gevraagd & set(ids_in(r)):
                    uit.setdefault(rid, {"bestand": bestand, "tekst": r.strip()})
    return uit


async def haal_records(per_rol, paden):
    from mcp import ClientSession
    try:
        from mcp.client.streamable_http import streamablehttp_client
    except ImportError:  # nieuwere mcp-versies
        from mcp.client.streamable_http import streamable_http_client as streamablehttp_client

    async def een_rol(rol, lijst):
        try:
            async with streamablehttp_client(paden[rol]) as (r, w, _):
                async with ClientSession(r, w) as s:
                    await s.initialize()
                    res = await s.call_tool("toon", {"ids": sorted(lijst)})
                    tekst = "\n".join(getattr(c, "text", "") for c in res.content)
                    return rol, ontleed_toon(tekst, lijst), None
        except Exception as e:  # netwerkfout: dossier zonder tekst, maar wel met de rest
            return rol, {}, f"{type(e).__name__}: {e}"

    uit = await asyncio.gather(*(een_rol(r, l) for r, l in per_rol.items() if paden.get(r)))
    records, fouten = {}, {}
    for rol, recs, fout in uit:
        records.update(recs)
        if fout:
            fouten[rol] = fout
    return records, fouten


# ---------- herkomst in de trace ----------

def herkomst(trace):
    gelezen, genoemd = {}, {}
    for b in trace.get("beurten", []):
        if b.get("soort") == "toolcall":
            for rid in (b.get("resultaat") or {}).get("ids", []):
                gelezen.setdefault(rid, []).append({"beurt": b["nr"], "rol": b["spreker"], "tool": b.get("tool")})
        elif b.get("soort") == "bericht" and b.get("spreker") in NAAM:
            for rid in dict.fromkeys(ids_in(b.get("tekst"))):
                genoemd.setdefault(rid, []).append({"beurt": b["nr"], "rol": b["spreker"]})
    return gelezen, genoemd


def beweringen(verdict):
    uit = []
    o = verdict.get("oorzaak") or {}
    for slot in SLOTS:
        d = o.get(slot) or {}
        uit.append({"sleutel": slot, "soort": "oorzaak", "titel": slot.capitalize(),
                    "bewering": d.get("bewering"), "bewijs": d.get("bewijs") or [], "zekerheid": d.get("zekerheid")})
    for i, a in enumerate(verdict.get("acties") or [], 1):
        uit.append({"sleutel": f"actie-{i}", "soort": "actie", "titel": f"Actie {i}",
                    "bewering": a.get("actie"), "reden": a.get("reden"), "bewijs": a.get("bewijs") or []})
    v = verdict.get("voorspelling")
    if v:
        uit.append({"sleutel": "voorspelling", "soort": "voorspelling", "titel": f"Voorspelling dag {v.get('dag')}",
                    "bewering": v.get("bewering"), "bewijs": v.get("bewijs") or []})
    return uit


# ---------- stabiliteit over eerdere runs ----------

def feiten(tekst):
    return {re.sub(r"\s+", " ", f.lower()) for f in FEIT_RE.findall(str(tekst or ""))}


def stabiliteit(verdict, archief):
    runs = []
    for map_ in sorted(archief.glob("run*"), key=lambda p: int(re.sub(r"\D", "", p.name) or 0)):
        try:
            v = json.loads((map_ / "verdict.json").read_text(encoding="utf-8"))
            t = json.loads((map_ / "trace.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if v.get("run_id") != t.get("run_id") or t.get("status", "klaar") != "klaar":
            continue
        runs.append((map_.name, v))
    if not runs:
        return None
    slots = {}
    for slot in SLOTS:
        nu = (verdict.get("oorzaak") or {}).get(slot) or {}
        kern = feiten(nu.get("bewering"))
        ids_nu = set(nu.get("bewijs") or [])
        per_run = []
        for naam, v in runs:
            d = (v.get("oorzaak") or {}).get(slot) or {}
            tekst = d.get("bewering") or ""
            ids_v = set(d.get("bewijs") or [])
            per_run.append({
                "run": naam, "run_id": v.get("run_id"), "zekerheid": d.get("zekerheid"),
                "kernfeiten_terug": sorted(f for f in kern if f in feiten(tekst)),
                "ids_gedeeld": len(ids_nu & ids_v), "ids_run": len(ids_v),
            })
        per_feit = {f: sum(f in feiten(((v.get("oorzaak") or {}).get(slot) or {}).get("bewering")) for _, v in runs)
                    for f in sorted(kern)}
        soort = "feiten"
        if not per_feit:  # bewering zonder dagen/tijden: vergelijk dan de bewijs-ID's van dit deel
            soort = "ids"
            per_feit = {i: sum(i in (((v.get("oorzaak") or {}).get(slot) or {}).get("bewijs") or []) for _, v in runs)
                        for i in sorted(ids_nu)}
        slots[slot] = {"soort": soort, "kernfeiten": per_feit, "runs": per_run}
    vp = [(n, (v.get("voorspelling") or {}).get("dag")) for n, v in runs]
    dag_nu = (verdict.get("voorspelling") or {}).get("dag")
    return {
        "aantal_runs": len(runs),
        "runs": [{"run": n, "run_id": v.get("run_id"), "dag": d} for (n, v), (_, d) in zip(runs, vp)],
        "voorspelling": {"dag": dag_nu, "eens": sum(d == dag_nu for _, d in vp), "van": len(vp)},
        "slots": slots,
    }


# ---------- hoofdlijn ----------

def bouw(map_, archief, records, fouten, trace, verdict):
    gelezen, genoemd = herkomst(trace)
    items = []
    for b in beweringen(verdict):
        bewijs = []
        for rid in b["bewijs"]:
            rol = rol_van(rid)
            rec = records.get(rid)
            g_eigen = [x for x in gelezen.get(rid, []) if x["rol"] == rol]
            bewijs.append({
                "id": rid, "rol": rol, "record": rec,
                "gelezen_in": g_eigen[:3], "genoemd_in": genoemd.get(rid, [])[:6],
                "bestaat": rec is not None, "uit_eigen_bron": bool(g_eigen),
            })
        rollen = sorted({x["rol"] for x in bewijs if x["rol"]})
        b.update({"bewijs": bewijs, "rollen": rollen, "aantal_rollen": len(rollen)})
        items.append(b)
    alle = {x["id"] for b in items for x in b["bewijs"]}
    zonder_tekst = sorted(i for b in items for x in b["bewijs"] if not x["bestaat"] for i in [x["id"]])
    return {
        "run_id": verdict.get("run_id"),
        "gemaakt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "uitleg": "Na de run gemaakt door bewijsdossier.py; geen deel van de Council. Recordtekst via `toon` op het MCP-pad van de Rol die het record bezit.",
        "totaal": {
            "beweringen": len(items), "bewijs_ids": len(alle),
            "met_recordtekst": len(alle) - len(set(zonder_tekst)),
            "uit_eigen_bron": len({x["id"] for b in items for x in b["bewijs"] if x["uit_eigen_bron"]}),
            "oorzaak_rollen": {b["sleutel"]: b["aantal_rollen"] for b in items if b["soort"] == "oorzaak"},
            "zonder_tekst": sorted(set(zonder_tekst)), "mcp_fouten": fouten,
        },
        "beweringen": items,
        "stabiliteit": stabiliteit(verdict, archief),
    }


def markdown(d):
    r = [f"# Bewijsdossier · run `{d['run_id']}`", "",
         f"_{d['uitleg']}_  Gemaakt: {d['gemaakt']}.", ""]
    t = d["totaal"]
    r += [f"**{t['beweringen']} beweringen · {t['bewijs_ids']} bewijs-ID's · {t['met_recordtekst']} met letterlijke recordtekst · "
          f"{t['uit_eigen_bron']} gelezen door het Raadslid van de eigen Rol.**", ""]
    s = d.get("stabiliteit")
    if s:
        vp = s["voorspelling"]
        r += ["## Stabiliteit over runs", "",
              f"- Voorspelling **dag {vp['dag']}** in **{vp['eens']} van {vp['van']}** volledige runs.", ""]
        for slot, info in s["slots"].items():
            kf = ", ".join(f"`{f}` {n}/{s['aantal_runs']}" for f, n in info["kernfeiten"].items()) or "geen"
            wat = "bewijs-ID's" if info.get("soort") == "ids" else "kernfeiten"
            r.append(f"- **{slot.capitalize()}**: {wat} terug in eerdere runs: {kf}")
        r.append("")
    for b in d["beweringen"]:
        z = f" · zekerheid {b['zekerheid']}" if b.get("zekerheid") is not None else ""
        r += [f"## {b['titel']}{z} · gedragen door {b['aantal_rollen']} Rol(len): {', '.join(NAAM.get(x, x) for x in b['rollen'])}", "",
              f"> {b['bewering']}", ""]
        for x in b["bewijs"]:
            rec = x["record"] or {}
            herk = []
            if x["gelezen_in"]:
                herk.append("gelezen in beurt " + ", ".join(str(g["beurt"]) for g in x["gelezen_in"]))
            if x["genoemd_in"]:
                herk.append("genoemd in beurt " + ", ".join(str(g["beurt"]) for g in x["genoemd_in"]))
            tekst = (rec.get("kop", "") + " " + rec.get("tekst", "")).strip().replace("\n", " ") or "_(geen recordtekst opgehaald)_"
            if len(tekst) > 400:
                tekst = tekst[:400] + "…"
            r.append(f"- **{x['id']}** ({NAAM.get(x['rol'], x['rol'])}, `{rec.get('bestand', '?')}`): {tekst}  \n  <sub>{' · '.join(herk)}</sub>")
        r.append("")
    return "\n".join(r)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    map_ = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT
    load_dotenv(ROOT / ".env")
    trace = json.loads((map_ / "trace.json").read_text(encoding="utf-8"))
    verdict = json.loads((map_ / "verdict.json").read_text(encoding="utf-8"))
    if trace.get("run_id") != verdict.get("run_id"):
        sys.exit("trace.json en verdict.json komen niet uit dezelfde run.")
    paden = (trace.get("opzet") or {}).get("kennisbronnen") or {}
    paden = {rol: os.getenv(f"MCP_{rol.upper()}", "").strip() or (paden.get(rol) if isinstance(paden, dict) else "") for rol in NAAM}
    per_rol = {}
    for b in beweringen(verdict):
        for rid in b["bewijs"]:
            if rol_van(rid):
                per_rol.setdefault(rol_van(rid), set()).add(rid)
    records, fouten = asyncio.run(haal_records(per_rol, paden))
    d = bouw(map_, ROOT / "archief", records, fouten, trace, verdict)
    (map_ / "bewijsdossier.json").write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    (map_ / "bewijsdossier.md").write_text(markdown(d), encoding="utf-8")
    t = d["totaal"]
    print(f"Bewijsdossier voor run {d['run_id']}: {t['beweringen']} beweringen, {t['bewijs_ids']} bewijs-ID's, "
          f"{t['met_recordtekst']} met recordtekst, {t['uit_eigen_bron']} gelezen door de eigen Rol.")
    print("Rollen per deel van de oorzaak:", ", ".join(f"{k} {v}" for k, v in t["oorzaak_rollen"].items()))
    if d["stabiliteit"]:
        vp = d["stabiliteit"]["voorspelling"]
        print(f"Voorspelling dag {vp['dag']} in {vp['eens']}/{vp['van']} runs uit archief/.")
    if t["zonder_tekst"] or fouten:
        print("LET OP: geen recordtekst voor", t["zonder_tekst"], fouten)
    print(f"Geschreven: {map_ / 'bewijsdossier.json'} en bewijsdossier.md")


if __name__ == "__main__":
    main()
