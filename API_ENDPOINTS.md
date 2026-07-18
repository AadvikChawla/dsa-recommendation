# ML Service — Endpoint Reference

Purpose of this document: explain **why each endpoint exists**, what it
expects, and what it returns, so backend engineers integrating with this
service don't have to read the controller code to know when to call what.

This is a FastAPI service ([main.py](main.py)). Every route except the ones
listed under **Auth** requires a bearer token (see below). All ML state
(BKT mastery, HLR spaced-repetition state) that the ML service **writes**
goes back to the backend to persist — the ML service itself never writes to
Postgres (`database/postgres/db.py` is read/write for a few tables, but the
architecture treats the backend as the mastery/HLR system of record for
everything routed through `/update`; the two `seed_*` endpoints are the one
exception — see their section below).

---

## Auth

**File:** [middlewares/auth.py](middlewares/auth.py)

Every request (except `/`, `/docs`, `/openapi.json`, `/redoc`) must carry:

```
Authorization: Bearer <token>
```

Two kinds of token are accepted:

1. **A real user session token.** Looked up in Postgres' `session` table;
   resolves to a `user_id`, expires normally. This is what any
   user-initiated request (from the backend, forwarding the end user's own
   session) should send. Route handlers then enforce that the `user_id` in
   the URL/body matches the authenticated session's `user_id` — a user can
   never request or update another user's data through this API.

2. **`ML_SERVICE_TOKEN`** — a shared secret for server-to-server calls that
   have no end-user session in the loop at all (currently only the
   backend's judge0 submission webhook calling `POST /update` right after a
   submission is judged). Set `ML_SERVICE_TOKEN` in the environment to
   enable this path; leave it unset to disable it entirely (every caller
   then needs a real per-user session token). A service-token call is
   trusted to act on whatever `userId` it puts in the request body — there's
   no session to compare against.

**Practical implication for backend:** for anything that's a live user
action (viewing mastery, requesting recommendations), forward the user's
own session token. Only use `ML_SERVICE_TOKEN` for the judge0-style
webhook-to-webhook call where there's genuinely no user session available.

---

## `GET /`

Health check / welcome message. Public, no auth required. Returns
`{"message": "Welcome to Recommendation Service"}`. Use this for uptime
probes, not for anything functional.

---

## Mastery — `routes/mastery.py` / `controllers/mastery_controller.py`

### `GET /mastery/{user_id}`

**Why it exists:** the backend needs to *display* a user's current skill
level per topic (profile page, topic list with mastery badges, etc.)
without re-deriving BKT/HLR math itself.

**What it does:** reads the user's raw BKT mastery (`P(L)` per topic) and
HLR spaced-repetition state from Postgres (read-only — ML never mutates
these tables from this endpoint), then computes **proficiency**: mastery
decayed by the HLR forgetting curve. Raw `mastery` says "how much has this
user ever demonstrated for this topic, historically." `proficiency` says
"how much do they actually still have, right now, accounting for time
since last review." A topic mastered a month ago with zero review since is
stale — `mastery` won't show that, `proficiency` will.

**Response:**
```json
{
  "userId": "user_abc123",
  "mastery": {"array": 0.82, "dynamic_programming": 0.41},
  "mastered_topics": ["array"],
  "proficiency": {"array": 0.71, "dynamic_programming": 0.39}
}
```
`mastered_topics` is every topic at/above the BKT mastery threshold
(`pipeline/recommender/bkt.py::MASTERY_THRESHOLD`).

**When to call:** any UI surface that shows "how good is this user at X" —
profile pages, topic breakdowns, progress dashboards. Prefer `proficiency`
over raw `mastery` for anything time-sensitive (e.g. "you're getting rusty
at X").

### `GET /urgency/{user_id}`

**Why it exists:** spaced-repetition-style "you should review this soon"
signals, independent of the main recommendation feed — useful for a
dedicated "due for review" widget/notification rather than mixing it into
the general problem feed.

**What it does:** reads HLR state per topic, runs
`pipeline/recommender/hlr.py::calculate_urgency` (a function of how long
it's been since last review vs. the topic's half-life) and returns a
per-topic urgency score.

**Response:**
```json
{"userId": "user_abc123", "urgency_scores": {"array": 0.12, "graph": 0.87}}
```
Higher = more overdue for review. Use this to power "review reminders" or
to sort a "topics getting rusty" list; it does NOT return problems, just
topic-level urgency — pair it with `/topic/recommend/problems` if you want
actual problems for the most-urgent topic.

---

## Recommendations — `routes/recommendation.py` / `controllers/recommendation_controller.py`

### `GET /recommend/{user_id}?limit=10`

**Why it exists:** the core "what should this user solve next" feed — the
main recommendation surface of the product.

**What it does:** the full ML pipeline. Reads mastery/HLR (Postgres,
read-only), builds a `UserGraph` (bootstrapped from Neo4j if available,
else from the user's own Postgres submission/recommendation history, else
a cold-start graph for a brand-new user), runs a 7-pool candidate-mixing +
ranking pipeline against the Qdrant problem vector pool, and returns a
ranked list. `limit` is clamped to `[1, 50]`, defaults to 10. As a
**side-effect**, every returned recommendation is logged to
`recommendation_log` in Postgres — this is the write-half of a
recommend → attempt feedback loop (`POST /update` closes the loop by
marking a `recommendation_log` row as attempted when the user actually
submits that problem). This log write is best-effort: if it fails, the
recommendations are still returned (the log failing shouldn't turn a
successful recommendation call into a 500).

**When to call:** the main "recommended for you" feed/homepage. This is
the expensive, full-pipeline endpoint — don't call it more often than the
UI actually needs a fresh feed (e.g. not on every keystroke of an
unrelated search box).

### `GET /topic/recommend/{user_id}`

**Why it exists:** "what ONE topic should this user focus on next" — a
lighter-weight signal than a full problem list, for UI like "Suggested
focus: Dynamic Programming" banners, without paying for a Qdrant
candidate-ranking pass.

**What it does:** builds the same `UserGraph` `/recommend` does, but picks
a single best-next topic (`recommend_topic`) instead of running the full
pool pipeline. No Qdrant call at all.

**Response:**
```json
{"userId": "user_abc123", "topicId": "graph", "reason": "..."}
```
`reason` is a short machine-generated explanation (e.g. prerequisite
gating, weak-topic signal) — safe to show directly in UI copy like "We
suggest Graphs because...".

### `POST /topic/recommend/problems`

**Why it exists:** the backend/UI already knows which topic the user wants
to practice (e.g. they clicked "Practice Arrays" in a topic picker) — this
returns problems for *that specific topic*, ranked by relevance to the
user's own current level, rather than the general mixed-pool feed.

**Body:**
```json
{"userId": "user_abc123", "topicId": "array", "limit": 10}
```
(`userId` must match the authenticated session; `limit` is 1–50, default 10.)

**What it does:** builds the user's graph, filters/ranks candidates from
Qdrant for the requested topic by relevance to the user's BKT mastery on
that topic vs. each candidate's real difficulty score — not a fixed
difficulty band, so it adapts as the user improves.

**Response:** a list of problems with `problem_id`, `title`, `title_slug`,
`difficulty_score`, `topic_tags`, `predicted_success` (the model's estimate
of how likely this user is to solve it).

**When to call:** any "browse problems for topic X" UI, especially
topic-picker flows — use this instead of client-side filtering the
`/recommend` feed by topic, since this endpoint actually re-ranks for that
topic rather than just filtering a mixed-pool result.

---

## Seeding — `routes/seeding.py` / `controllers/seeding_controller.py`

**Why these exist:** cold-start. A brand-new user (or one who links a
Codeforces/LeetCode account after already having solved problems there)
has no BKT/HLR history in this system yet. These two endpoints backfill
that history from the external platform's public API so recommendations
aren't starting from zero for someone who's actually already solved 200
problems elsewhere. **These are the one place this ML service writes
mastery/HLR state directly** (`save_user_mastery` / `save_user_hlr`) rather
than returning it for the backend to persist — because it's bulk-importing
external history, not processing a single live submission.

### `POST /seed_hlr/{user_id}`

Requires the user to have a linked Codeforces handle (`user.linked_codeforces`
in Postgres). Fetches their CF submission history, derives per-topic HLR
half-lives from it (`pipeline/recommender/hlr.py::seed_half_life_from_cf`),
and writes **only the topics that don't already have HLR data** — so
calling this again after the user already has review schedules won't wipe
them out. Returns `{"message": "..."}` variants for "user not found", "no
CF handle linked", or "no submissions found", and
`{"error": "provider_unavailable", ...}` if Codeforces itself is
unreachable/erroring (distinct from "genuinely has no submissions" — safe
to retry on `provider_unavailable`, not safe to retry-and-expect-different
on a genuine empty history).

### `POST /seed_bkt/{user_id}`

Same shape, but for LeetCode + BKT mastery (not HLR). Requires a linked
LeetCode handle. Fetches recent submissions, fetches each **unique**
problem's real topic tags from LeetCode's own GraphQL API (translated to
this service's canonical 70-tag taxonomy via
`database/postgres/topic_taxonomy.py`), replays them through BKT **oldest
to newest** (BKT is sequential — replaying newest-first would produce a
materially wrong final mastery state), and writes the resulting mastery
map. Unlike `/seed_hlr`, this **overwrites** current mastery per topic
(it's meant to be run once, early, before the user has real in-app
history) rather than skip-if-exists.

**When to call either:** right after a user links a Codeforces/LeetCode
account, as a one-time "import my history" action — not on a schedule, and
not silently in the background without the user knowing their handle needs
to be linked first (both return a clear "no handle linked" message if it
isn't).

---

## Submission — `routes/submission.py` / `controllers/submission_controller.py`

### `POST /update`

**Why it exists:** this is the **only** place per-submission mastery/HLR
updates happen. The architecture is deliberately stateless on the ML
side: the backend sends the user's *current* mastery/HLR state for every
topic the just-submitted problem touches (`problemTopics`), ML computes
the Bayesian BKT update and the HLR review-schedule update, and returns
the *new* state. ML does not fetch current state itself here — the
backend already has it and sending it avoids an extra round-trip/read.

**Auth note:** this is the one endpoint reachable via `ML_SERVICE_TOKEN` —
the backend's judge0 submission webhook calls this directly right after a
submission is judged, with no end-user session in that request path. A
real per-user session token still can't update someone else's `userId`
(that check is skipped **only** for verified service calls).

**Body** (see [models/schemas/submission.py](models/schemas/submission.py)
for full field docs/bounds):
```json
{
  "userId": "user_abc123",
  "problemId": "two-sum",
  "verdict": "OK",
  "hintsUsed": 0,
  "testCasesPassed": 10,
  "totalTestCases": 10,
  "submissionCount": 1,
  "normalisedScore": 0.95,
  "problemDifficulty": 0.35,
  "problemTopics": [
    {"topicId": "array", "currentMastery": 0.45, "currentHlr": {...}, "weight": 0.7}
  ]
}
```
`verdict` must be exactly `"OK"` for a full-credit solve — anything else is
treated as a failed attempt. `weight` (0–1, optional) lets the backend
indicate how central a topic is to the problem, so a problem that's
"70% graph, 30% dfs" doesn't apply the same full-strength update to both
tags. Numeric fields are bounds-checked at the API boundary (422 on a
malformed value like a negative score) rather than being silently clamped
deep in the BKT math and absorbed as a real (wrong) mastery penalty.

**What it does, and persists (ML writes these — this is the exception to
"ML never writes mastery"):**
1. Computes updated BKT mastery + HLR state.
2. Persists both via `save_user_mastery_live` / `save_user_hlr`
   (`database/postgres/db.py`) — **persistence failures propagate as a
   real error**, not swallowed, so a webhook caller sees a failure and can
   retry/queue rather than getting a `200` that silently didn't save.
3. Marks the most recent matching not-yet-attempted `recommendation_log`
   row as attempted — the read-half of the feedback loop `/recommend`
   writes the other half of. Best-effort: most submissions won't
   correspond to a prior recommendation, and a lookup miss is a no-op.

**Response:**
```json
{
  "userId": "user_abc123",
  "problemId": "two-sum",
  "updatedTopics": [
    {"topicId": "array", "updatedMastery": 0.51, "updatedHlr": {...}}
  ],
  "masteredTopics": ["array"],
  "results": {"bkt": {...}, "hlr": {...}}
}
```
`updatedTopics` is what the backend should persist as the new
current-state for next time it calls `/update` for this user (`bkt`/`hlr`
under `results` are diagnostic detail, not required for the backend to
store).

**When to call:** every time a submission is judged (accepted or not) for
a problem the recommendation engine tracks topics for — this is what keeps
mastery/HLR state current. This is the one endpoint expected to be called
very frequently (once per judged submission), unlike the others.

---

## Quick reference

| Endpoint | Method | Auth | Purpose |
|---|---|---|---|
| `/` | GET | public | health check |
| `/mastery/{user_id}` | GET | session | display current mastery + decayed proficiency per topic |
| `/urgency/{user_id}` | GET | session | spaced-repetition "review this soon" scores per topic |
| `/recommend/{user_id}` | GET | session | main recommended-problems feed |
| `/topic/recommend/{user_id}` | GET | session | single best-next-topic suggestion |
| `/topic/recommend/problems` | POST | session | problems for a caller-specified topic, ranked to user's level |
| `/seed_hlr/{user_id}` | POST | session | one-time import: Codeforces history → HLR state |
| `/seed_bkt/{user_id}` | POST | session | one-time import: LeetCode history → BKT mastery |
| `/update` | POST | session **or** `ML_SERVICE_TOKEN` | per-submission BKT/HLR update (ML writes state here) |
