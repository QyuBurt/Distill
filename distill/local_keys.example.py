"""Template for distill/local_keys.py, an optional file of your own API keys (it is git-ignored; copy this one to
local_keys.py and fill it in). Nothing here is needed: without keys Distill uses lower rate limits and plain posters.

    GITHUB_TOKEN   read-only token: GitHub allows 5,000 requests an hour instead of 60 (PC Recomps, tool updates)
    SGDB_API_KEY   steamgriddb.com > Preferences > API: portrait posters for PC Recomps

Values can be plain text or the obfuscated form `python -m distill.github_auth <value>` prints ("enc:...").
Obfuscation only hides a key from a text search; it is NOT secrecy. Anything you build into an .exe you share can be
read back out of it, so never put a key you care about in a build you publish. Type personal keys into Settings >
API keys instead (they are kept in one encrypted file, distill_keys.json) or use the GITHUB_TOKEN / SGDB_API_KEY environment
variables; a key saved in Settings is used first."""
GITHUB_TOKEN = ""
SGDB_API_KEY = ""
