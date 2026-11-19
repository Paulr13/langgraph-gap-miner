# gap-miner-langraph

The gap-miner agent loop, rebuilt as a LangGraph state graph and shipped to
Kubernetes. Same research loop as the cron-driven original (one product per
tick, evidence-first, append-only findings), now expressed in the standard
orchestration vocabulary: typed state, nodes, conditional edges, retry loop.

## Architecture

```
                ┌────────────────────────── k3s (inf01) ──────────────────────┐
                │  CronJob gap-miner (every 4h, Forbid, backoffLimit 0)       │
                │  ┌───────────────────────────────┐    ┌──────────────────┐  │
                │  │  pod: gap-miner-langraph      │ PVC│ local-path 1Gi   │  │
                │  │  pick -> fetch -> extract ─┐  │◄──►│ state.json       │  │
                │  │        ▲        │retry     │  │    │ findings.md      │  │
                │  │        └────────┘(max 2)   │  │    └──────────────────┘  │
                │  │        │ok                 │  │                          │
                │  │        ▼                    │  │                          │
                │  │   append -> update -> END   │  │                          │
                │  └───────────────┬─────────────┘  └──────────────────────────┘
                │                  │ BEARER_TOKEN (k8s Secret, never in git)
                └──────────────────┼───────────────────────────────────────────
                                   ▼
                     rile.cc /v1/prod1 (Caddy, bearer-gated)
                     → Qwen pool: 4 llama.cpp instances, 4×3090 (prod1+prod2)
                     thinking disabled: chat_template_kwargs.enable_thinking=false

  Sources (evidence-only, never fabricated):
    iTunes RSS customer reviews (resolves app id via iTunes Search API)
    DuckDuckGo lite snippets (the only search path that works from this network)
```

## What each run does

1. *pick* — first `pending` item in `backlog.json`-seeded state (PVC-persisted).
2. *fetch* — iTunes reviews + DDG snippets; blocked sources are counted, not retried forever.
3. *extract* — one structured call to the Qwen pool; strict single-line JSON,
   robust parse; on failure the graph loops back through `retry_extract`
   (max 2), then records the item as `partial` instead of faking output.
4. *append* — section appended to `findings.md` (never reordered, never edited).
5. *update* — state: status, themes found, heartbeat, history; one-line tick log.

## Honest notes

- The LLM is a self-hosted pool reached through an authed public gateway, not
  a managed cloud AI platform. The port proves the orchestration layer, not cloud
  tooling.
- Review coverage is thin by design: this loop is a wedge probe for
  [deepscaleanalytics.com](https://deepscaleanalytics.com), not a market
  research product. Blocked sources are recorded per tick.
- Findings quotes come only from fetched text; no fabricated reviews. A thin
  tick (`sources-blocked`) is recoverable, a fake one is not.

## Run locally

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export LLM_CHAT_URL=https://rile.cc/v1/prod1/v1/chat/completions
export BEARER_TOKEN=<token>          # never commit
python graph.py --dry-run            # pick + fetch only
python graph.py                      # one full product per run
```

## Run on k3s

```sh
docker build -t gap-miner-langraph:0.1.0 .
docker save gap-miner-langraph:0.1.0 | sudo k3s ctr images import -
sudo k3s kubectl apply -f k8s/gap-miner.yaml
printf '%s' "$BEARER_TOKEN" | sudo k3s kubectl -n gap-miner create secret \
  generic gateway-bearer --from-file=BEARER_TOKEN=/dev/stdin
# manual test tick:
sudo k3s kubectl -n gap-miner create job --from=cronjob/gap-miner test-run
sudo k3s kubectl -n gap-miner logs -f job/test-run
```
