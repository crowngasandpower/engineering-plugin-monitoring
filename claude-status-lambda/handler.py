"""
Claude status -> spinner-verb proxy (AWS Lambda, API Gateway).

Fetches https://status.claude.com/api/v2/status.json and returns a small JSON
array the Grafana Infinity panel on the Infrastructure dashboard consumes:

    [{"<label>": <code>}]

The label is the JSON *key*; a numeric severity *code* is the value. This
shape is deliberate: a Grafana stat panel won't render a free-text string
*value*, but it always renders the field *name* (textMode="name"), and a
numeric value drives the background colour via thresholds. So the panel shows
the label big and coloured:

    code 0 -> green   (operational; label = a random Claude Code spinner verb)
    code 1 -> yellow  (minor outage, or status unknown)
    code 2 -> red     (major outage)
    code 3 -> red     (critical outage)

The operational verb is one of the 185 built-in defaults from
https://github.com/wynandw87/claude-code-spinner-verbs, re-picked every call
(i.e. every panel refresh).
"""

import json
import random
import urllib.request

STATUS_URL = "https://status.claude.com/api/v2/status.json"

# Claude Code's 185 built-in default spinner verbs.
VERBS = [
    "Accomplishing",
    "Actioning",
    "Actualizing",
    "Architecting",
    "Baking",
    "Beaming",
    "Beboppin'",
    "Befuddling",
    "Billowing",
    "Blanching",
    "Bloviating",
    "Boogieing",
    "Boondoggling",
    "Booping",
    "Bootstrapping",
    "Brewing",
    "Burrowing",
    "Calculating",
    "Canoodling",
    "Caramelizing",
    "Cascading",
    "Catapulting",
    "Cerebrating",
    "Channeling",
    "Channelling",
    "Choreographing",
    "Churning",
    "Clauding",
    "Coalescing",
    "Cogitating",
    "Combobulating",
    "Composing",
    "Computing",
    "Concocting",
    "Considering",
    "Contemplating",
    "Cooking",
    "Crafting",
    "Creating",
    "Crunching",
    "Crystallizing",
    "Cultivating",
    "Deciphering",
    "Deliberating",
    "Determining",
    "Dilly-dallying",
    "Discombobulating",
    "Doing",
    "Doodling",
    "Drizzling",
    "Ebbing",
    "Effecting",
    "Elucidating",
    "Embellishing",
    "Enchanting",
    "Envisioning",
    "Evaporating",
    "Fermenting",
    "Fiddle-faddling",
    "Finagling",
    "Flambeing",
    "Flibbertigibbeting",
    "Flowing",
    "Flummoxing",
    "Fluttering",
    "Forging",
    "Forming",
    "Frolicking",
    "Frosting",
    "Gallivanting",
    "Galloping",
    "Garnishing",
    "Generating",
    "Germinating",
    "Gitifying",
    "Grooving",
    "Gusting",
    "Harmonizing",
    "Hashing",
    "Hatching",
    "Herding",
    "Honking",
    "Hullaballooing",
    "Hyperspacing",
    "Ideating",
    "Imagining",
    "Improvising",
    "Incubating",
    "Inferring",
    "Infusing",
    "Ionizing",
    "Jitterbugging",
    "Julienning",
    "Kneading",
    "Leavening",
    "Levitating",
    "Lollygagging",
    "Manifesting",
    "Marinating",
    "Meandering",
    "Metamorphosing",
    "Misting",
    "Moonwalking",
    "Moseying",
    "Mulling",
    "Mustering",
    "Musing",
    "Nebulizing",
    "Nesting",
    "Newspapering",
    "Noodling",
    "Nucleating",
    "Orbiting",
    "Orchestrating",
    "Osmosing",
    "Perambulating",
    "Percolating",
    "Perusing",
    "Philosophising",
    "Photosynthesizing",
    "Pollinating",
    "Pondering",
    "Pontificating",
    "Pouncing",
    "Precipitating",
    "Prestidigitating",
    "Processing",
    "Proofing",
    "Propagating",
    "Puttering",
    "Puzzling",
    "Quantumizing",
    "Razzle-dazzling",
    "Razzmatazzing",
    "Recombobulating",
    "Reticulating",
    "Roosting",
    "Ruminating",
    "Sauteing",
    "Scampering",
    "Schlepping",
    "Scurrying",
    "Seasoning",
    "Shenaniganing",
    "Shimmying",
    "Simmering",
    "Skedaddling",
    "Sketching",
    "Slithering",
    "Smooshing",
    "Sock-hopping",
    "Spelunking",
    "Spinning",
    "Sprouting",
    "Stewing",
    "Sublimating",
    "Swirling",
    "Swooping",
    "Symbioting",
    "Synthesizing",
    "Tempering",
    "Thinking",
    "Thundering",
    "Tinkering",
    "Tomfoolering",
    "Topsy-turvying",
    "Transfiguring",
    "Transmuting",
    "Twisting",
    "Undulating",
    "Unfurling",
    "Unravelling",
    "Vibing",
    "Waddling",
    "Wandering",
    "Warping",
    "Whatchamacalliting",
    "Whirlpooling",
    "Whirring",
    "Whisking",
    "Wibbling",
    "Working",
    "Wrangling",
    "Zesting",
    "Zigzagging"
]

# Fixed labels for the non-operational states (kept stable so the dashboard
# value-mappings can colour them yellow/red).
# label + numeric severity code per indicator. Code drives the panel colour
# via thresholds (0 green, 1 yellow, 2/3 red).
OUTAGE = {
    "minor": ("Minor Outage", 1),
    "major": ("Major Outage", 2),
    "critical": ("Critical Outage", 3),
}


def _fetch_indicator():
    req = urllib.request.Request(STATUS_URL, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return (data.get("status") or {}).get("indicator", "unknown")


def handler(event, context):
    try:
        indicator = _fetch_indicator()
    except Exception:
        indicator = "unknown"

    if indicator == "none":
        label, code = random.choice(VERBS) + "...", 0
    elif indicator in OUTAGE:
        label, code = OUTAGE[indicator]
    else:
        label, code = "Status Unknown", 1

    # Label is the field NAME (shown via textMode=name); code is the numeric
    # value (drives threshold colour). One object, one row.
    body = [{label: code}]
    return {
        "statusCode": 200,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
        },
        "body": json.dumps(body),
    }
