# CRM Gap Detector

Detects operational facts mentioned in Aircall call transcripts that are missing from the corresponding SmartMoving opportunity. Outputs a structured JSON list of findings.

## Contents

* [Setup](#setup)
* [How to Run](#how-to-run)
* [Repository Layout](#repository-layout)
* [CLI Options](#cli-options)
* [Prompt Design](#prompt-design)
* [Inbound vs Outbound Handling](#inbound-vs-outbound-handling)
* [Cost Estimate](#cost-estimate)
* [What I Would Do Differently for Production](#what-i-would-do-differently-for-production)
* [Sample Outputs](#sample-outputs)


## Setup

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=YOUR_API_KEY_HERE
```


## How to Run

```bash
# Inbound call
python analyze_call.py data/aircall_sample_call.json data/smartmoving_sample_opportunity.json

# Outbound call
python analyze_call.py data/aircall_sample_call_outbound.json data/smartmoving_sample_opportunity_outbound.json

# Debug: print the constructed prompt without calling the API
python analyze_call.py data/aircall_sample_call.json data/smartmoving_sample_opportunity.json --dry-run

# Verbose: also print parsed output to stderr
python analyze_call.py data/aircall_sample_call.json data/smartmoving_sample_opportunity.json --verbose
```

Output is always valid JSON to stdout. Informational messages go to stderr.

## Repository Layout

| Path | What it is |
|---|---|
| analyze_call.py | Main script. Reads the two JSON inputs, builds the prompt, calls the model, prints JSON output to stdout. |
| requirements.txt | Python dependencies. |
| data/aircall_sample_call.json | Sample inbound Aircall call payload. |
| data/aircall_sample_call_outbound.json | Sample outbound Aircall call payload. |
| data/smartmoving_sample_opportunity.json | Sample SmartMoving opportunity for the inbound example. |
| data/smartmoving_sample_opportunity_outbound.json | Sample SmartMoving opportunity for the outbound example. |

## CLI Options

| Option | Purpose |
|---|---|
| `--dry-run` | Prints the constructed prompt and exits without calling the API. |
| `--verbose` | Prints parsed output and extra details to stderr. JSON output still goes to stdout. |


## Prompt Design

### Input Compression

The biggest cost lever on a high volume pipeline is token count, not model choice.

**Transcript:** Raw Aircall utterance objects contain about 6 fields each (timestamps, confidence scores, speaker IDs, etc.). We strip everything except `speaker` and `text`, reducing transcript token count by about 60%.

**CRM:** We flatten the SmartMoving JSON into a plain text operational summary: stops, inventory, notes, job type, special instructions. Dumping the raw JSON would waste 2 to 3x the tokens on billing fields, internal IDs, and status codes the model has no use for.

### Precision Over Recall

False positives create alert fatigue. Once salespeople start ignoring alerts, the tool has no value. The prompt is tuned for precision through eight explicit rules. The most important are:

* Every finding requires a verbatim quote from a single continuous utterance. No inference and no concatenating separate turns.
* The finding summary must only describe what is literally stated in the quote, not what can be inferred from it.
* Concerns explicitly resolved by the agent during the call are not flagged. No gap exists if it was closed in conversation.
* Each operational fact is flagged exactly once under the most specific applicable category.
* A quote already used to support one finding cannot be reused to justify a second.

Category definitions are deliberately operational rather than literal. `PETS_CHILDREN`, for example, is defined as *"present and unattended during active crew operations, requiring crew awareness or affecting safe movement"* and not simply any mention of children. This excludes timing context (a child being dropped at school before the crew arrives) while still capturing genuine move day constraints.

### Structured Output via `instructor`

We use `instructor` to enforce the `AnalysisResult` Pydantic schema directly on the API response, with `max_retries=2`. This eliminates all `json.loads` error handling, guarantees the `Category` enum and `confidence` Literal are always valid, and surfaces schema violations as clean Python exceptions rather than silent bad data. Given how constrained the output is, retries almost never fire, so the cost overhead is negligible.

### Why Not LangChain, CrewAI, or Similar Frameworks?

Agent frameworks like CrewAI, AutoGen, and LangChain/LangGraph are the natural first instinct for AI tooling. They're worth evaluating honestly.

However, frameworks like these are designed for *agents*. These are systems that reason in a loop, call tools, and decide what to do next. This script is a *pipeline step*: one prompt in, structured JSON out. There is no loop, no tool use, no multi turn reasoning. Introducing that machinery here adds abstraction without adding value.

Taking LangChain/LangGraph specifically as the most mature example, `create_agent` does handle structured output (via `response_format=`) with smart retries, and ships middleware for PII detection, model fallback, and model retry. These are genuinely useful. But they solve problems that are either already handled by `instructor` in this script, or not yet problems at this scale.

Some middleware examples LangChain provides that this script does not implement:

* PII detection
* Model fallback
* Model retry for API errors


`instructor` is purpose built for exactly this pattern: single prompt to validated schema. It adds zero framework overhead and does the one thing we need, directly.

**When to migrate:** if this system evolves into true agent behavior (autonomously fetching CRM records, deciding which calls to analyze, routing findings across Telegram/email/CRM, spawning subagents per opportunity), then LangGraph becomes the right foundation, and its middleware becomes genuinely valuable rather than dependencies added to solve problems already handled by simpler tools. The Pydantic schemas defined here transfer directly to `response_format=AnalysisResult` with no changes.

### What Was Considered But Skipped

* Few shot examples in prompt: adds about 500 tokens per call, around $0.04 per 1,000 calls extra. Deferred until baseline is measured.
* Chain of thought (`think step by step`): balloons output tokens. Not worth it for a structured extraction task.
* One API call per category: 12 API calls per transcript. Cost and latency not justified.


## Inbound vs Outbound Handling

Outbound calls under **30 seconds** are almost always voicemails or no answers. There is no customer voice in the transcript to mine for operational facts. The script detects this via `call.direction == "outbound" && call.duration < 30` and returns `{"findings": []}` immediately, logging a skip reason to stderr. No API call is made.

For longer outbound calls (follow ups, confirmation calls, quote discussions), the same analysis pipeline runs. The system prompt does not differentiate. Operational facts surface regardless of who initiated the call, and the model is instructed to identify gaps in the CRM, not to attribute them to one party.

The 30 second threshold is defined as `MIN_OUTBOUND_DURATION_SECONDS` at the top of the script. The heavy item threshold (`HEAVY_ITEM_THRESHOLD_LBS`) and the set of standard SmartMoving charge names that get filtered out of the CRM summary (`STANDARD_CHARGES`) sit next to it. These are three tunables that ops can adjust without touching the prompt.

## Cost Estimate

### Token Breakdown

The numbers below come from running the script against the provided sample files.

* System prompt: about 450 tokens
* Compressed transcript (sample call, 10 min): about 900 to 1,000 tokens (raw JSON is about 2,000)
* Flattened CRM summary: about 300 tokens
* `instructor` tool schema (Pydantic to JSON Schema): about 800 tokens
* Measured input tokens: 2,511 (inbound) and 2,763 (outbound)
* Measured output tokens: 727 (inbound) and 561 (outbound)

Measured token usage summary:

| Example | Input tokens | Output tokens |
|---|---:|---:|
| Inbound | 2,511 | 727 |
| Outbound | 2,763 | 561 |

A significant component is the `instructor` tool schema overhead (about 800 tokens). `instructor` serialises the `AnalysisResult` Pydantic model as a JSON Schema tool definition on every call. This is how it enforces structured output, and it is static across all calls.

### At 1,000 Calls/Month

Claude Haiku 4.5 pricing: **$1 / MTok input**, **$5 / MTok output**

```
Input:  1,000 x 2,650 tokens = 2.65M tokens = $2.65
Output: 1,000 x   650 tokens = 0.65M tokens = $3.25
---------------------------------------------
Total:  about $5.90 per 1,000 calls, about $0.0059 per call
```

In production, using the **Anthropic Batch API** (50% discount, 24h turnaround) would cut this to **about $2.95 per 1,000 calls** for non urgent moves. Synchronous calls are reserved only for moves within 48 hours.


## What I Would Do Differently for Production

### 1. Event Driven Pipeline

The PoC reads files from disk. In production, Aircall fires a webhook when a call ends and transcription is ready. A lightweight queue (Redis/BullMQ or SQS) sits between the webhook receiver and the analysis worker. Aircall webhooks have short timeout windows and you don't want API latency blocking the acknowledgment. This gives you natural retry and dead letter handling for failed analyses.

### 2. Live CRM Fetch

Right now, CRM data comes from a static file. In production, the pipeline fetches the SmartMoving opportunity via API at analysis time, not from a cache, because the record may have been updated between the call and the analysis. This also surfaces a new case: no CRM record exists yet (new inbound lead), which is itself worth flagging.

### 3. Batch API for Non Urgent Calls

Most moves are days or weeks away. The Anthropic Batch API gives 50% cost reduction with 24 hour turnaround. Production would batch non urgent calls nightly and reserve synchronous calls for moves within 48 hours, roughly halving the monthly API spend.

### 4. Feedback Loop and Confidence Calibration

Confidence levels are currently uncalibrated estimates from the model. In production, salespeople see each finding and can mark it useful or a false alarm. After a few hundred labeled examples, you can measure precision and recall per category, tune the prompt accordingly, and drop categories that consistently generate noise. This is the step that turns the tool from an AI thing into a system that improves.

### 5. Alert Routing and Deduplication

The script outputs JSON to stdout. Production needs to deliver findings to the right person via the right channel (Telegram bot, email digest, or CRM notification), grouped by opportunity rather than by call. A customer may call three times; without deduplication across calls, the salesperson gets three alerts about the same piano. A lightweight store of already surfaced findings per opportunity handles this.

### 6. Observability

Logging prompt and response pairs is a start. Production needs a proper observability layer. Several tools fit here depending on existing infrastructure: **Langfuse** (integrates directly with the Anthropic SDK and LangChain), **LangSmith** (natural fit if the system migrates to LangChain/LangGraph), or **Datadog LLM Observability** (better choice if the team already runs Datadog for the rest of the stack). The tool matters less than what you instrument.

What to track per call:

* **Traces:** full span from webhook receipt to CRM fetch to prompt construction to API call to finding delivery. End to end visibility into where time is spent and where failures occur.
* **p99 latency:** the 99th percentile matters more than average. Slow outliers are exactly the calls where something went wrong (API retry, schema validation failure, large transcript). A p99 spike is the earliest signal of a prompt regression or upstream issue.
* **Error rate by type:** schema validation failures (instructor retries), Anthropic API errors, CRM fetch failures, and voicemail skips each have different root causes and different fixes; they should be tracked separately, not aggregated.
* **Token usage distribution:** percentiles, not just average. A transcript at the 95th percentile token count might be pushing against context limits or inflating costs unexpectedly.
* **Findings rate per category:** a sudden drop in `HEAVY_ITEMS` findings or spike in `ACCESS` findings signals the prompt or upstream data changed. Over time this becomes a regression detector.
* **Empty findings rate:** if this climbs above baseline, either call quality dropped, the CRM improved, or the prompt got too conservative.

Prompt versions are tagged on every trace so any metric can be sliced by version, making prompt iteration safe rather than a leap of faith.

### 7. Evals

There is no ground truth data today, so a full eval suite isn't built into this PoC. But the script was deliberately designed to be evaluable when that data exists.

**Why the output structure makes evals tractable:**

* Structured JSON output means findings are machine comparable, not free text
* The `quote` field is an eval anchor: if the quote doesn't appear verbatim in the transcript, the finding is automatically a hallucination. This is a free deterministic check that requires zero labeled data
* The `category` enum enables per category precision and recall, not just aggregate accuracy
* The `confidence` field lets you measure precision separately at `high`, `medium`, and `low` tiers to verify those labels actually mean something

**Three layers, in order of implementation cost:**

**Layer 1: Deterministic checks (buildable now, no labeled data needed)**
* Output always matches the schema
* Every `quote` appears verbatim in the transcript
* No finding is flagged for a fact already present in the CRM
* Voicemail fixtures always return empty findings

**Layer 2: Human labeled regression suite (20 to 50 examples, build after launch)**
* A CSV of `(aircall_path, smartmoving_path, expected_findings)`
* Compute precision and recall per category on every prompt change
* This is what makes iterating on the prompt safe; without it, every change is a guess

**Layer 3: LLM as judge (once Layer 2 baseline exists)**
* For findings that don't exactly match the ground truth label, a stronger model judges whether the finding is still valid. This handles paraphrasing and equivalent findings with different wording
* Prevents the regression suite from becoming a brittle exact match test as the prompt evolves

### 8. Framework Migration if the System Becomes an Agent

The PoC is a pipeline step: one prompt in, structured JSON out. This makes `instructor` the right tool. If the system evolves toward true agent behavior (autonomously fetching CRM records, deciding which calls to analyze, routing findings across Telegram/email/CRM, spawning subagents per opportunity), migrating to LangChain's `create_agent` becomes justified. At that point, `PIIMiddleware` (call transcripts contain addresses and phone numbers), `ModelFallbackMiddleware` (resilience against Anthropic outages), and `ModelRetryMiddleware` (transient API errors) are no longer over engineering, they are the right defaults for a production agent running unsupervised on thousands of calls. The Pydantic schemas defined here transfer directly to LangChain's `response_format=AnalysisResult`.

### 9. Graceful Degradation

API down? Rate limited? Transcription garbage? Each case needs a defined behavior: retry with backoff, queue for later, flag for human review. The pipeline should never silently drop a call. Every call gets either a result or an explicit "needs manual review" status.

### 10. Transcription Quality Gating

Aircall returns per-utterance confidence scores. Low-confidence transcripts (heavy accents, poor audio) generate noisy input that produces false findings. Production should gate on average transcription confidence and route low-quality calls to human review rather than feeding them to the model.

### 11. Security and Data Retention

Call transcripts contain PII (names, addresses, phone numbers). The pipeline needs a defined retention policy, structured logging that masks PII, and compliance with state level call recording consent laws (California two party consent, etc.).

### 12. Category Config as Data

The 12 categories and their definitions are currently hardcoded in the system prompt. In production, these would live in a config file (JSON or YAML) loaded at runtime. This lets operations staff tune category definitions and add new ones without touching code or redeploying.

## Sample Outputs

### Inbound Call

**Command:**
```bash
python analyze_call.py data/aircall_sample_call.json data/smartmoving_sample_opportunity.json
```

**Output:**
[tokens] input=2511, output=727
```json
{
  "findings": [
    {
      "category": "HEAVY_ITEMS",
      "summary": "Peloton Bike Plus (140 pounds) in upstairs bedroom not recorded in inventory.",
      "quote": "So first, my wife and I just realized we have this Peloton in the upstairs bedroom that we forgot to tell you about. It's the bigger one, the Bike Plus, I think it weighs around 140 pounds.",
      "confidence": "high"
    },
    {
      "category": "ACCESS",
      "summary": "Service entrance off the alley at destination is the required entrance, not the front entrance shown on Google Maps.",
      "quote": "The destination address, the one in Santa Monica, the building entrance is actually around the back, not the front like Google Maps shows. There's a service entrance off the alley. The building manager said the movers have to use that one.",
      "confidence": "high"
    },
    {
      "category": "BUILDING_MGMT",
      "summary": "Certificate of Insurance required 48 hours before move-in, to be sent to manager@elmtowers.example.com.",
      "quote": "First, they need a Certificate of Insurance, a COI, before move-in day. They were really firm about that. They want it sent to manager@elmtowers.example.com at least 48 hours before.",
      "confidence": "high"
    },
    {
      "category": "TIMING",
      "summary": "Freight elevator at destination must be reserved and used between 10 am and 2 pm; regular elevator unavailable.",
      "quote": "Second, the freight elevator has to be reserved between 10 am and 2 pm. They won't let movers use the regular elevator at all.",
      "confidence": "high"
    },
    {
      "category": "COMMUNICATION_PREFS",
      "summary": "Mother-in-law who speaks Russian will be at destination to let crew in; crew lead should be aware.",
      "quote": "My mother-in-law is going to be at the destination to let the crew in. She doesn't speak much English. She speaks Russian mainly.",
      "confidence": "high"
    },
    {
      "category": "SPECIAL_HANDLING",
      "summary": "Empty 75-gallon saltwater fish tank to be moved; customer will drain it beforehand.",
      "quote": "We have a saltwater fish tank, it's a 75 gallon tank. We're going to drain it ourselves but the tank itself, the empty glass tank, do you guys move that?",
      "confidence": "high"
    },
    {
      "category": "PAYMENT",
      "summary": "Credit card payment will incur a 3% processing fee on move day.",
      "quote": "Credit card payment is fine, there's a small processing fee of three percent that gets added on the day of.",
      "confidence": "high"
    }
  ]
}
```

### Outbound Call

**Command:**
```bash
python analyze_call.py data/aircall_sample_call_outbound.json data/smartmoving_sample_opportunity_outbound.json
```

**Output:**
[tokens] input=2763, output=561
```json
{
  "findings": [
    {
      "category": "ADDRESS_CHANGE",
      "summary": "Destination address changed from Pasadena apartment to Glendale condo unit.",
      "quote": "We're moving to Glendale instead. The new address is 847 North Brand Boulevard, unit 12B. It's a condo.",
      "confidence": "high"
    },
    {
      "category": "HEAVY_ITEMS",
      "summary": "Customer has a baby grand piano (approximately 600 pounds or more) in the living room at origin that needs to be moved.",
      "quote": "I forgot to mention before, but we have a baby grand piano. It's been in the family for years. It's at the origin in our living room.",
      "confidence": "high"
    },
    {
      "category": "TIMING",
      "summary": "Arrival window changed from 7:00 AM - 8:00 AM to 10:00 AM - 11:00 AM due to customer's daughter having a soccer game at 7:30 AM.",
      "quote": "Can we make it a little later? Like 9 or 10 am? My daughter has a soccer game in the morning we want to drop her off at first. She has it at 7:30.",
      "confidence": "high"
    },
    {
      "category": "BUILDING_MGMT",
      "summary": "Destination HOA requires a $400 elevator deposit and freight elevator scheduling restricted to weekdays between 9 AM and 4 PM.",
      "quote": "The HOA requires us to pay a $400 elevator deposit and we have to schedule the freight elevator. They only allow moves Monday through Friday between 9 am and 4 pm.",
      "confidence": "high"
    },
    {
      "category": "PAYMENT",
      "summary": "Customer prefers to pay via Zelle instead of credit card to avoid the 3% credit card processing fee.",
      "quote": "you guys take Zelle right? Because the credit card fee you mentioned, three percent, on a five thousand dollar move, that's like a hundred fifty dollars. I'd rather just do Zelle.",
      "confidence": "high"
    }
  ]
}
```