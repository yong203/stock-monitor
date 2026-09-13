---
name: stock-report
description: Create and save an evidence-linked investment report for one watched instrument or the full watchlist. Use only when the user explicitly invokes $stock-report; do not use for ordinary market questions.
---

# Stock Report

An explicit `$stock-report` invocation authorizes saving reports for the named instrument or `관심종목 전체`. Do not expand that target or perform any other mutation. If the target is ambiguous, ask before continuing.

## Workflow

1. Read [analysis-methodology.md](references/analysis-methodology.md), [report-schema.md](references/report-schema.md), and [source-policy.md](references/source-policy.md).
2. Use `scripts/report_client.py` to resolve the selected targets and fetch each target's context. Never expose configuration values in prompts, logs, or output. Skip any target whose core Toss quote, price, or currency is unavailable and report that failure explicitly.
3. Delegate the selected targets and contexts concurrently to exactly these three project agents: `quant-analyst`, `fundamental-analyst`, and `risk-reviewer`. They are read-only: they must not edit files or call the report POST endpoint.
4. Wait for all three agents to finish. If any agent fails, do not submit a partial report.
5. The main agent alone reconciles conflicts, separates facts from inference and opinion, validates source links, and builds the exact report JSON.
6. Validate and submit one JSON document per successfully reviewed target through standard input. Retry an ambiguous submission only with the identical body and `run_id`.

Never state buy/sell instructions, invent a target price, or generate a DCF. A successful analysis is persisted even when its `change_label` is `unchanged`; transport or evidence failures are not reported as “no change.”
