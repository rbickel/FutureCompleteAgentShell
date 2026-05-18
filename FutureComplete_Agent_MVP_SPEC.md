# FutureComplete × Microsoft Teams Agent — MVP Specification

- **Goal:** Bring the MVP to first-customer-demo state at a 1-day co-development hackathon in Lausanne (date TBD).
- **Repo:** `FutureCompleteAssistant`
- **Backend API:** `https://api.forecasting.inait.ai`
- **Web dashboard:** `https://futurecomplete.inait.ai`
- **Dataset format / tutorial docs:** [inait-forecast-docs](https://github.com/inait-external/inait-forecast-docs)

---

## 1. User journey

| # | Step | Behavior |
|---|---|---|
| 1 | **Install & launch** | User adds the FutureComplete app in Teams. No separate signup. |
| 2 | **Authenticate** | No extra sign-in. The Teams user-id is used to mint (or look up) a FutureComplete API key in inait's database on first interaction. The user is never aware of this mechanism. |
| 3 | **Welcome** | *"Welcome — you're on a trial version, but you can already explore the full capabilities for a few test runs."* No quota numbers are shown. Quota enforcement is server-side via inait's quota service: **$30 compute cap over a 14-day window, no limit on the number of requests**. |
| 4 | **Capability prompt** | Two-option menu: **Backtest** or **Forecast**, each with a one-line explanation. Inline links: (a) dataset-format guide from `inait-forecast-docs`, (b) tutorial dataset. |
| 5 | **Dataset upload** | User drops `.csv`, `.xlsx`, or `.parquet` (≤200 MB) in chat. |
| 6 | **Parameter panel (Adaptive Card)** | Common fields: target columns, feature/driver columns, horizon, prediction **intervals** (confidence bands), explainability report toggle.<br>**Backtest-only fields:** backtest window (size *OR* start/end date) AND `prediction_stride` (refresh cadence; must be a multiple of horizon). |
| 7 | **Target / driver column selection** | By default the **first column** is selected as the target. A collapsible **"Fine tune"** panel then lets the user: select all columns, restrict to columns matching `starts_with` / `ends_with` / `contains`, or select all others as drivers. |
| 8 | **Submission routing** | Forecast → `POST /v1/prediction`. Backtest → `POST /v1/benchmark` (always; never reuse predict). |
| 9 | **Job-done notification** | Bot proactively pings the user in the same Teams chat when the job finishes. |
| 10 | **Inspect results** | Button opens `https://futurecomplete.inait.ai/jobs/{session_id}` — curated plots + download CSV. *Replaces the current raw-JSON inline preview, which is not customer-grade.* Each job UUID is per-user-scoped. |
| 11 | **Job history** | User asks "show my jobs" → bot returns a list with status (completed / running / failed). Completed entries have a button that re-opens the dashboard view. |
| 12 | **Clear chat / reset session** | User can clear the current conversation (e.g. `/clear` command or button) to start a fresh interaction. Not possible today. |

---

## 2. Status vs. current repo

| # | Requirement | Status | Where (or what's missing) |
|---|---|---|---|
| 1 | Teams identity → silent API-key provisioning | ❌ Missing | `src/config.py:22` uses a single static key for all users. Needs a per-user mint/lookup keyed off the Teams user-id. |
| 2 | Trial-version welcome copy | 🟡 Partial | Welcome card at `src/cards/builder.py:183-225` — needs "trial" framing added. |
| 3 | Backtest-or-forecast menu + format guide link + tutorial link | 🟡 Partial | Forecast-only path today (`src/tools/forecast_submission.py:215`). No external links to `inait-forecast-docs` or sample dataset. |
| 4 | File drop (CSV / XLSX / Parquet) | ✅ Done | `src/agents/agent.py:135-170`. |
| 5 | Param panel (horizon, targets, features) | ✅ Done | `src/cards/builder.py:256-380`. |
| 6 | Default target = first column + "Fine tune" panel (select-all / pattern match / select-others-as-drivers) | ❌ Missing | Today only literal column names with no default selection and no fine-tune panel (`src/tools/forecast_submission.py:141-164`). |
| 7 | Backtest window (size or dates) + `prediction_stride` | ❌ Missing | `end_date` hardcoded `None` (`src/tools/forecast_submission.py:218`); no backtest UI, no stride field. |
| 8 | Prediction-interval selection + explainability toggle | ❌ Missing | `run_explain` hardcoded `False` (`src/tools/forecast_submission.py:219`). No interval input. *(The existing frequency inference at `src/tools/data_validation.py:109-147` is unrelated and not user-facing.)* |
| 9 | Backtest → `/v1/benchmark` | ❌ Missing | No benchmark client. |
| 10 | Forecast → `/v1/prediction` | ✅ Done | `src/tools/forecast_submission.py:239-249`. |
| 11a | Proactive job-done ping | ✅ Done | `src/services/forecast_poller.py:140-200`. |
| 11b | Dashboard deep-link to inspect results | ❌ Missing | Current inline raw-output preview is not customer-grade. Need `https://futurecomplete.inait.ai/jobs/{session_id}` button. |
| 11c | Job-list query with status | ✅ Done | `src/agents/agent.py:554-558` + `src/services/job_store.py:48`. |
| 12 | Clear chat / reset session | ❌ Missing | No `/clear` command, button, or reset handler in `src/agents/agent.py` or the manifest. |

**Score:** 4 done, 2 partial, 8 missing.

---

## 3. Hackathon work-streams

### Stream A — Identity & silent API-key provisioning *(joint)*
**Goal:** No extra authentication step. The Teams user-id is read from the incoming activity and used to mint (or look up) a FutureComplete API key in inait's database on first interaction; the key is then cached for the session and used for all downstream API calls. The user never sees a key, never signs in twice.

- Read the Teams user-id (and email, as a fallback identifier) from the bot activity context.
- inait backend to handle the new user (TBD with inait team)
- Replace any static shared FutureComplete credential with per-user license/subscription resolution keyed off the Teams user identity.
- Cache the resolved key in the session/job store so it survives within a conversation without re-provisioning on every message.

### Stream B — Backtest path & end-to-end UX *(inait lead)*
- New `/v1/benchmark` client wrapper alongside the existing predict client.
- Backtest variant of the param card with:
  - Window definition: backtest size *or* start/end date (mutually exclusive group).
  - `prediction_stride` field with horizon-multiple validation.
- Branch in the welcome menu so users explicitly pick backtest vs forecast.
- "Trial version" copy in welcome card.
- Backtest/forecast menu copy + links to the `inait-forecast-docs` user guide and tutorial dataset.
- Target/driver column selection UX: first column selected by default, with a "Fine tune" panel offering select-all, pattern matching (`starts_with` / `ends_with` / `contains`), and select-others-as-drivers.
- Prediction-interval input + explainability toggle in the param card.
- **"Inspect results" button → dashboard deep-link** replacing the raw-JSON preview. Final URL pattern TBD on inait side (placeholder: `https://futurecomplete.inait.ai/jobs/{session_id}`); the bot needs the confirmed pattern to wire the button.
- Same dashboard-link button on completed entries in the job-list card.
- **Clear-chat / reset-session command** so the user can start a fresh interaction (currently impossible).
- Tone/copy polish across the welcome → upload → param → done → list flow.