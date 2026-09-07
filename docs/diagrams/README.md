# docs/diagrams

These are archify IR, one JSON file per diagram (`architecture`, `investigation`,
`ground-truth`, `eval-run`), each validated at `--quality showcase` and rendered to
`.html`, `.svg`, and `.png` by `make diagrams`. Edit the JSON, run `make diagrams`,
and look at the PNG before committing.

`tests/test_diagrams.py` checks each diagram against the code it names, not just
against its own schema: an MCP tool in a label has to be one `agent/mcp_client.py`
actually allows, a module path has to exist on disk, and `ground-truth.json`'s
attribute names have to be real ones from `gen/topology.py` and `gen/README.md`.
It also renders-checks each diagram against its own SVG, so an IR edited without
`make diagrams` fails the suite the same way a stale number fails
`scripts/check_readme_numbers.py`.
