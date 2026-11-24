"""Voorbeeld: dezelfde Raadsleden als MAF-group chat in de DevUI van Microsoft Agent Framework.

Start met:  uv run --extra devui python voorbeelden/devui_debat.py
Daarna opent http://127.0.0.1:8080: kies "Council (group chat)" en typ een vraag,
of praat apart met één Raadslid. Dit voorbeeld plakt de Kennisbron-bestanden uit
kennisbronnen/<rol>/ in de prompt (geen MCP) en schrijft GEEN trace.json of verdict.json:
het is een speeltuin om de orchestratie te verkennen, niet om in te leveren.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import os

from agent_framework import Agent
from agent_framework.devui import serve
from agent_framework.openai import OpenAIChatCompletionClient
from agent_framework.orchestrations import GroupChatBuilder
from dotenv import load_dotenv
from openai import AsyncOpenAI

import raadsleden

ROOT = Path(__file__).parent.parent
KENNISBRONNEN = ROOT / "kennisbronnen"


def client(model_env):  # alleen OpenAI-compatibele endpoints (blok 1-3 van .env.example)
    return OpenAIChatCompletionClient(model=os.environ[model_env], async_client=AsyncOpenAI(
        base_url=os.environ["OPENAI_BASE_URL"], api_key=os.environ["OPENAI_API_KEY"], max_retries=8))


def kennisbron_bestanden(rol):
    map_ = KENNISBRONNEN / rol
    return sorted(p for p in map_.glob("*") if p.suffix in (".md", ".csv")) if map_.is_dir() else []


def instructies(lid, bestanden, _mcp=None):
    return (f"Je bent de {lid.NAAM}, een Raadslid in de Council of Achrona. {lid.PERSPECTIEF}\n"
            "Baseer je alleen op je eigen Kennisbron en noem bij elke bewering de record-ID's. Verzin nooit een ID.\n\n"
            "Je Kennisbron:\n\n" + "\n\n".join(f"## {p.name}\n\n{p.read_text(encoding='utf-8')}" for p in bestanden))


def main():
    load_dotenv(ROOT / ".env")
    klein = client("MODEL_KLEIN")
    leden = [Agent(klein, instructies(l, kennisbron_bestanden(l.ROL), None), name=l.ROL, description=l.PERSPECTIEF)
             for l in raadsleden.alle()]

    def om_de_beurt(state):  # vaste volgorde, zoals council.py
        namen = list(state.participants)
        return namen[state.current_round % len(namen)]

    council = GroupChatBuilder(name="Council (group chat)", participants=leden, selection_func=om_de_beurt,
                               max_rounds=2 * len(leden)).build()
    serve(entities=[council, *leden], port=8080, auto_open=True, auth_enabled=False)


if __name__ == "__main__":
    main()
