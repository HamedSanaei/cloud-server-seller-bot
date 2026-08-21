# Codex + LiteLLM sub-agent mapping

This repository deliberately does **not** overwrite your existing Codex/LiteLLM model aliases. Your two Hetzner-backed sub-agents are already configured outside this starter according to the project requirement.

Root `AGENTS.md` tells Codex to prefer those two sub-agents for bounded development tasks.

If you later choose project-local custom agent definitions, keep model routing in `.codex/agents/*.toml` and preserve these roles:

- `hetzner-builder`: implementation-heavy, bounded file scope
- `hetzner-reviewer`: tests, review, provider edge cases

Do not put API keys in this repository. Point Codex/LiteLLM at existing model aliases/secrets instead.

Before relying on model routing, verify in your Codex runtime that each spawned child session is actually using the expected LiteLLM/Hetzner model alias. `AGENTS.md` can guide delegation but should not be treated as proof of runtime model assignment.
