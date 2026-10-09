"""LLM-backed reasoning: the only layer allowed to call an LLM. Stage 2: business-rule
proposals from schema + profile statistics. Never runs at check time; every output is a
proposal with confidence + evidence for a person to review. See docs/design/ai_rules.md."""
