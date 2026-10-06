"""Skills: reusable instruction packs in skills/<name>/SKILL.md (same layout as Claude Code / Agent Skills).

Frontmatter gives `name`, `description` and `agents` (which agents get it). Agents see every relevant skill's
name and description in their system prompt and pull the full body with the `load_skill` tool when needed,
so prompts stay short (progressive disclosure). Add a folder to add a skill; no code change needed.
"""

import re

from . import config


def _parse(path):
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    meta, body = ({}, text) if not m else ({}, m.group(2))
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
    meta["agents"] = [a.strip() for a in meta.get("agents", "").split(",") if a.strip()]
    meta["body"] = body.strip()
    meta.setdefault("name", path.parent.name)
    return meta


def all_skills():
    return {s["name"]: s for s in (_parse(p) for p in sorted(config.SKILLS_DIR.glob("*/SKILL.md")))}


def names():
    return list(all_skills())


def get(name):
    return all_skills().get(name)


def for_agent(agent):
    return [s for s in all_skills().values() if agent in s["agents"] or "*" in s["agents"]]


def prompt_block(agent):
    skills = for_agent(agent)
    if not skills:
        return ""
    lines = "\n".join(f"- {s['name']}: {s['description']}" for s in skills)
    return f"\n\nSkills you can load with load_skill(name) before acting:\n{lines}"
