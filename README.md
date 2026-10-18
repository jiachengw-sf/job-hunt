# jobhunt

A local-first job-hunt agent you run with the AI subscription you already have (Claude Code,
Codex CLI, or any tool that supports Agent Skills). It finds fresh roles that fit your resume,
filters out the ones you can't get, researches companies, pre-fills applications for you to review,
and keeps everything in a spreadsheet. **You always click Submit.**

> Status: early development (Milestone 0: skeleton). See [PROGRESS.md](PROGRESS.md).
> Full first-time-user docs arrive in Milestone 6.

## Try it

```bash
uv tool install .            # from a checkout; Python 3.11+
jobhunt doctor --fix --human # creates ~/.jobhunt/ and tells you exactly what else to fix
```

Your data lives only in `~/.jobhunt/` (set `JOBHUNT_HOME` to use another folder). Nothing in the
code is specific to one person; everything personal comes from your onboarding.

For contributors and AI agents: read [AGENTS.md](AGENTS.md).

## License

MIT
