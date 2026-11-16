# github-core-clone-01

[![make.fun · github-core-clone-01](https://make.fun/badge/github-core-clone-01.svg)](https://make.fun/bounty/github-core-clone-01?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)
[![rank](https://make.fun/badge/github-core-clone-01/rismzz/github-core-clone-01/rank.svg)](https://make.fun/bounty/github-core-clone-01/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)
[![reward](https://make.fun/badge/github-core-clone-01/rismzz/github-core-clone-01/reward.svg)](https://make.fun/bounty/github-core-clone-01?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)

<!-- TODO before submitting: replace OWNER and REPO in the two badge URLs
     above with the real GitHub owner and repository name. The repo_readme
     check refuses badge URLs that still hold a template placeholder. -->

A GitHub-compatible git forge in a single Python file — standard library
only, no git binary, no dependencies. One process serves four surfaces over
one base URL, all backed by the same in-memory store:

- **Git smart HTTP** at `/{owner}/{repo}.git` — protocol v2 (`ls-refs`,
  `fetch` with shallow support and wait-for-done negotiation) and v0
  upload-pack / receive-pack, with hand-written packfile parsing and
  generation (including OFS/REF delta resolution), per-ref fast-forward
  enforcement, and report-status replies. Works with the real `git` CLI:
  clone, push, pull, shallow clone, branch deletion.
- **REST API v3** under `/api/v3` — repository CRUD, contents API backed by
  real commits, branches, labels, issues and comments, pull requests with
  conflict detection and squash merges, search, personal access tokens, and
  webhooks delivered with `X-Hub-Signature-256` HMAC-SHA256 signatures.
- **GraphQL** at `/api/graphql` — a hand-written parser and executor with
  `__type` / `__schema` introspection, repository/issue/pull-request
  queries, issue ordering, and a `createPullRequest` mutation.
- **MCP** at `/mcp` — JSON-RPC 2.0 Streamable HTTP (`server/discover` and
  the classic `initialize` handshake) exposing repo/issue/pull-request
  tools that act on the same store as REST and GraphQL.

Behaviour is deterministic — object ids, timestamps (a logical clock) and
record ids derive only from the operation sequence — so a trial replays
byte-identically.

Built for the [make.fun](https://make.fun) bounty
[github-core-clone-01](https://make.fun/bounty/github-core-clone-01).

## Run it

The grader boots the program with no arguments:

```
python3 main.py
```

It binds an ephemeral port, registers it with `$MAKEFUN_ENV_URL/register`,
serves until `$MAKEFUN_ENV_URL/done` reports completion, then exits 0.
Requests authenticate with `Authorization: Bearer $MAKEFUN_SHOP_TOKEN`.

For local experimentation:

```
FORGE_PORT=8791 FORGE_TOKEN=dev-token FORGE_LOGIN=octocat python3 main.py
git clone http://127.0.0.1:8791/octocat/<repo>.git   # after creating it via REST
```

## License

MIT — see [LICENSE](LICENSE).
