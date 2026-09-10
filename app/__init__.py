"""AI Coach Service -- a defensive LLM integration with a hard trust boundary.

The design rule everything else follows: model output is *data*, never
instructions, never identity, and never trusted application state.
"""