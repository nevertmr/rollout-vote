# rollout-vote

A tiny pairwise-comparison site for **ranking successful robot rollouts against each other**.

Two clips of the same task are shown side by side and the visitor picks the one that was performed better.
No login, no accounts: open the page and vote. The collected comparisons are meant to be fit with a
Bradley-Terry model to get a latent quality score per episode, which then feeds preference learning (DPO).

Why pairwise: automatic motion metrics (jerk, spectral arc length) turned out not to separate good successes
from mediocre ones in our data, and in one task the worst failures were actually the *smoothest* trajectories.
Human judgement is the ground truth here, and people are far more consistent comparing two clips than scoring
one on an absolute scale.

## Design decisions

- **Video only.** No labels, metrics, run names or episode ids on the voting screen. The whole point is that
  the decision comes from watching, so nothing else is shown.
- **Least-voted pair first.** Every request serves the pair with the fewest votes so far, so coverage spreads
  evenly instead of piling onto a few pairs. Left/right is shuffled per serve to cancel position bias.
- **Click only.** Keyboard shortcuts were removed after mis-votes from key repeats.
- **Colour, not text.** Left is teal, right is amber, matching the video borders and the buttons. The tie
  button uses the colour between them.
- **Edit your last answer.** Votes are never overwritten. A revision inserts a new row and marks the old one
  `active=0` with `superseded_by`, so the full history stays auditable.
- **Synchronised looping.** The two clips have different lengths (episodes were ended by hand), so instead of
  looping independently and drifting apart, both restart together once both have ended.
- **Ties are allowed but discouraged.** Forcing a choice on genuinely equal pairs biases preference strength,
  but a high tie rate means the step has no discriminative power, so the UI nudges towards picking a side.

## Input

1. `vote_pool.json` — the candidates:

```json
{"items": [
  {"eid": "run01/ep0000", "step": 1,
   "instruction": "pick up the cup on the blue circle",
   "clip": "run01_0000.mp4", "n_frames": 239,
   "run": "run01", "ep": 0, "outcome": "success"}
]}
```

2. A clips directory holding those `clip` filenames. Ours are the top and wrist views stacked horizontally
   into one 1280×480 mp4 per episode, so the two cameras can never drift out of sync.

On boot the app seeds the database and builds every pair **within the same `step`**: with 15 candidates per
step over 10 steps that is `C(15,2) × 10 = 1050` pairs.

## Run

```bash
VOTE_PORT=8080 \
VOTE_CLIPS=/path/to/clips \
VOTE_POOL=/path/to/vote_pool.json \
VOTE_DB=./data/vote.db \
python3 app.py
```

Standard library only, no dependencies.

## Deploy

```bash
docker compose up -d --build
```

Bind-mount the clips read-only and the data directory read-write; the video files are never baked into the
image. The container runs as a non-root uid, so the data directory must be writable by it.

## Endpoints

| endpoint | purpose |
|---|---|
| `GET /` | voting page |
| `GET /api/next` | next pair for this voter (least-voted first, sides shuffled) |
| `POST /api/vote` | record a vote; a repeat on the same pair supersedes the earlier one |
| `GET /api/history` | this voter's recent votes, used by the edit-previous flow |
| `GET /clip/<file>` | mp4 with range support |
| `GET /stats` | coverage, votes per pair, per-voter counts, tie and revision rates |
| `GET /api/export` | all comparisons as JSON, ready for Bradley-Terry fitting |

## Notes

- No authentication and no rate limiting. Run it on an internal network.
- Voters are identified by a cookie, so the same person on a different browser or a different hostname counts
  as a new voter and may be shown pairs they have already seen.
- Clip filenames are visible in the network tab. If that matters for your study, randomise them.
