"""Resume-fit engine: gates (Stage 0), recall (1), requirement evidence (2), scores and band (3),
and the ranking (rank.py).

Qualification ("can I get it?") and preference ("do I want it?") are separate outputs. Ranking
is a fixed order of keys: match tier, Nice preferences met, then date. Every claim about the
user cites a profile line. No AI runs here: anything that needs judgment becomes a task file
for the user's AI tool.
"""
