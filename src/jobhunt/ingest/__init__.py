"""Ingest: validate what the AI wrote to ``work/results/`` and commit it to the store.

The model proposes; this package checks every quote against the posting and every claim about
the user against a profile line, and only then writes. Rejections name the field and the fix.
"""
