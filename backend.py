"""LLM-backends voor de Council.

BACKEND=claude  (standaard) Claude-abonnement via de Claude Agent SDK (de `claude` CLI moet ingelogd zijn).
BACKEND=openai  een OpenAI-compatibel endpoint (Roboest-endpoint, FreeLLMAPI, Ollama) via OPENAI_BASE_URL/OPENAI_API_KEY.

Beide backends geven hetzelfde terug (Antwoord), zodat council.py dezelfde trace schrijft.
Een Raadslid krijgt alleen de MCP-server van zijn eigen Rol; de Koning krijgt geen enkele tool.
"""
import asyncio
import json
import os
import re
import tempfile
from dataclasses import dataclass, field

TOOLS = ("lees_kennisbron", "zoek", "zoek_betekenis", "toon")
TOOL_PREFIX = "mcp__kennisbron__"
# Ingebouwde Claude Code tools die niemand in de Council mag gebruiken (geen bestanden, shell of web).
VERBODEN = ["Bash", "Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "LS", "WebFetch", "WebSearch",
            "Task", "Agent", "NotebookEdit", "TodoWrite"]
# Lege werkmap: de Claude CLI ziet zo geen repo-bestanden, CLAUDE.md of kennisbronnen/.
LEGE_MAP = tempfile.mkdtemp(prefix="council_")


@dataclass
class Antwoord:
    tekst: str = ""
    toolcalls: list = field(default_factory=list)  # [(tool, argumenten)] in volgorde
    toolresultaten: str = ""  # alle tekst die de tools teruggaven (voor de ID-controle)
    usage: dict = field(default_factory=dict)  # {"invoer", "gecached", "uitvoer"}
    structured: object = None  # JSON-antwoord bij een schema


def naam():
    return os.getenv("BACKEND", "claude").strip().lower()


async def met_retry(fn, pogingen=3):
    for i in range(pogingen):
        try:
            return await fn()
        except Exception as e:  # rate limit of netwerkfout: even wachten en opnieuw
            if i == pogingen - 1:
                raise
            wacht = 10 * (i + 1)
            print(f"  (fout: {type(e).__name__}: {str(e)[:200]}; opnieuw over {wacht}s)")
            await asyncio.sleep(wacht)


def json_uit_tekst(tekst):
    """Modellen zetten soms tekst of ```json rond hun JSON: pak het eerste volledige object."""
    if not tekst:
        return None
    try:
        return json.loads(tekst)
    except json.JSONDecodeError:
        pass
    start = tekst.find("{")
    while start != -1:
        diepte = 0
        for i in range(start, len(tekst)):
            if tekst[i] == "{":
                diepte += 1
            elif tekst[i] == "}":
                diepte -= 1
                if diepte == 0:
                    try:
                        return json.loads(tekst[start:i + 1])
                    except json.JSONDecodeError:
                        break
        start = tekst.find("{", start + 1)
    return None


# ----------------------------------------------------------------------------- Claude (abonnement)

def _ontpak(c):
    """MCP-resultaten komen soms als JSON-tekst ({"result": "...\\n..."}). Zonder ontpakken plakt de letter n
    van een ge-escapete newline aan het volgende ID (\\nENG-097) en mist de ID-controle dat record."""
    if not c:
        return ""
    if isinstance(c, str):
        try:
            d = json.loads(c)
        except json.JSONDecodeError:
            return c.replace("\\n", "\n")
        if isinstance(d, dict):
            return "\n".join(str(v) for v in d.values())
        return str(d)
    return str(c)


def _claude_opties(systeem, model, mcp_url=None, schema=None, effort=None, max_turns=8):
    from claude_agent_sdk import ClaudeAgentOptions
    kw = dict(
        system_prompt=systeem,
        model=model,
        tools=[],  # geen ingebouwde tools
        disallowed_tools=VERBODEN,
        allowed_tools=[TOOL_PREFIX + t for t in TOOLS] if mcp_url else [],
        mcp_servers={"kennisbron": {"type": "http", "url": mcp_url}} if mcp_url else {},
        setting_sources=[],  # geen gebruikers-/projectinstellingen, geen CLAUDE.md
        cwd=LEGE_MAP,
        max_turns=max_turns,
        permission_mode="bypassPermissions",
        extra_args={"strict-mcp-config": None},  # alleen de MCP-server hierboven, geen claude.ai-connectors
        env={"ENABLE_CLAUDEAI_MCP_SERVERS": "false"},
    )
    if schema:
        kw["output_format"] = {"type": "json_schema", "schema": schema}
    if effort:
        kw["effort"] = effort
    return ClaudeAgentOptions(**kw)


async def _claude_run(prompt, opties):
    from claude_agent_sdk import (AssistantMessage, ResultMessage, TextBlock, ToolResultBlock, ToolUseBlock,
                                  UserMessage, query)
    a, teksten, resultaat = Antwoord(), [], None
    async for m in query(prompt=prompt, options=opties):
        if isinstance(m, AssistantMessage):
            for b in m.content:
                if isinstance(b, ToolUseBlock):
                    a.toolcalls.append((b.name.removeprefix(TOOL_PREFIX), b.input or {}))
                elif isinstance(b, TextBlock):
                    teksten.append(b.text)
        elif isinstance(m, UserMessage) and isinstance(m.content, list):
            for b in m.content:
                if isinstance(b, ToolResultBlock):
                    c = b.content
                    if isinstance(c, list):
                        c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
                    a.toolresultaten += _ontpak(c) + "\n"
        elif isinstance(m, ResultMessage):
            if m.is_error:
                raise RuntimeError(f"Claude: {m.subtype}: {m.result}")
            u = m.usage or {}
            gecached = u.get("cache_read_input_tokens") or 0
            a.usage = {"invoer": (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0) + gecached,
                       "gecached": gecached, "uitvoer": u.get("output_tokens") or 0}
            a.structured = m.structured_output
            resultaat = m.result
    a.tekst = (resultaat or (teksten[-1] if teksten else "")).strip()
    return a


# ----------------------------------------------------------------------------- OpenAI-compatibel

def _openai_client():
    from openai import AsyncOpenAI
    return AsyncOpenAI(base_url=os.environ["OPENAI_BASE_URL"], api_key=os.environ["OPENAI_API_KEY"], max_retries=8)


def _tel_openai(a, usage):
    if not usage:
        return
    details = getattr(usage, "prompt_tokens_details", None)
    a.usage["invoer"] = a.usage.get("invoer", 0) + (usage.prompt_tokens or 0)
    a.usage["gecached"] = a.usage.get("gecached", 0) + ((getattr(details, "cached_tokens", 0) or 0) if details else 0)
    a.usage["uitvoer"] = a.usage.get("uitvoer", 0) + (usage.completion_tokens or 0)


async def _openai_raadslid(systeem, prompt, mcp_url, model):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    client, a = _openai_client(), Antwoord()
    async with streamablehttp_client(mcp_url) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = [{"type": "function", "function": {"name": t.name, "description": t.description or "",
                                                       "parameters": t.inputSchema}} for t in (await s.list_tools()).tools]
            msgs = [{"role": "system", "content": systeem}, {"role": "user", "content": prompt}]
            for stap in range(6):
                extra = {"tool_choice": "required"} if stap == 0 else {}  # eerst de Kennisbron lezen
                if stap == 5:
                    extra = {"tool_choice": "none"}
                r_ = await client.chat.completions.create(model=model, messages=msgs, tools=tools, **extra)
                _tel_openai(a, r_.usage)
                msg = r_.choices[0].message
                if not msg.tool_calls:
                    a.tekst = (msg.content or "").strip()
                    break
                msgs.append(msg.model_dump(exclude_none=True))
                for tc in msg.tool_calls:
                    args = json.loads(tc.function.arguments or "{}")
                    a.toolcalls.append((tc.function.name, args))
                    res = await s.call_tool(tc.function.name, args)
                    txt = "\n".join(getattr(c, "text", "") for c in res.content)
                    a.toolresultaten += txt + "\n"
                    msgs.append({"role": "tool", "tool_call_id": tc.id, "content": txt})
    return a


async def _openai_json(systeem, prompt, schema, model):
    client, a = _openai_client(), Antwoord()
    prompt += "\n\nAntwoord met één JSON-object volgens dit schema, zonder tekst eromheen:\n" + json.dumps(schema, ensure_ascii=False)
    r = await client.chat.completions.create(model=model, response_format={"type": "json_object"},
                                             messages=[{"role": "system", "content": systeem}, {"role": "user", "content": prompt}])
    _tel_openai(a, r.usage)
    a.tekst = r.choices[0].message.content or ""
    a.structured = json_uit_tekst(a.tekst)
    return a


# ----------------------------------------------------------------------------- publieke functies

async def raadslid(systeem, prompt, mcp_url):
    """Eén beurt van een Raadslid: leest via MCP alleen de eigen Kennisbron en antwoordt in tekst."""
    model = os.environ["MODEL_KLEIN"]
    if naam() == "claude":
        opties = _claude_opties(systeem, model, mcp_url=mcp_url, effort=os.getenv("EFFORT_KLEIN") or None)
        return await met_retry(lambda: _claude_run(prompt, opties))
    return await met_retry(lambda: _openai_raadslid(systeem, prompt, mcp_url, model))


async def koning(systeem, prompt, schema):
    """Een denkbeurt van de Koning: geen tools, geen Kennisbron, antwoord als JSON volgens `schema`."""
    model = os.environ["MODEL_GROOT"]
    if naam() == "claude":
        opties = _claude_opties(systeem, model, schema=schema, effort=os.getenv("EFFORT_GROOT") or None, max_turns=4)

        async def een():
            a = await _claude_run(prompt, opties)
            if a.structured is None:
                a.structured = json_uit_tekst(a.tekst)
            if a.structured is None:
                raise ValueError("Koning gaf geen JSON terug")
            return a
        return await met_retry(een)

    async def een_openai():
        a = await _openai_json(systeem, prompt, schema, model)
        if a.structured is None:
            raise ValueError("Koning gaf geen JSON terug")
        return a
    return await met_retry(een_openai)


ID = re.compile(r"\b(ARC|ENG|GUA|HIS|ORA|BEA)[-‐‑‒–—―−]([0-9]{3})\b")


def ids_in(tekst):
    """Alle record-ID's in een tekst, met een gewoon koppelteken, in volgorde en zonder dubbels."""
    gezien = []
    for m in ID.finditer(tekst or ""):
        i = f"{m.group(1)}-{m.group(2)}"
        if i not in gezien:
            gezien.append(i)
    return gezien
