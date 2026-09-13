# Report API and schema

Use `python3 .agents/skills/stock-report/scripts/report_client.py`. It reads `~/.config/stock-monitor/report-client.toml` unless `--config` is supplied.

## Read endpoints

`targets` calls `GET /internal/v1/report-targets` and expects:

```json
{
  "targets": [
    {
      "market": "NASDAQ",
      "symbol": "AAPL",
      "name": "Apple",
      "country": "US",
      "security_type": "stock",
      "is_etf": false
    }
  ]
}
```

`context MARKET SYMBOL` calls `GET /internal/v1/report-context/{market}/{symbol}` and expects:

```json
{
  "target": {},
  "quote": null,
  "analytics": {
    "collected_at": "2026-09-13T12:00:00+00:00",
    "recent_candles": [],
    "metrics": {},
    "stock": {},
    "warnings": [],
    "kr_trends": {},
    "exchange_rate": null,
    "errors": []
  },
  "latest_report": null
}
```

Any nullable or absent analytical field is unavailable evidence, never zero.

## Write endpoint

`validate` and `submit` read one JSON object from standard input. `submit` calls `POST /internal/v1/reports` with this exact shape:

```json
{
  "run_id": "6bbf4c89-82c7-4ca7-b6e4-5fd8b0dcb05d",
  "market": "NASDAQ",
  "symbol": "AAPL",
  "analyzed_at": "2026-09-13T21:00:00+09:00",
  "price": "234.56",
  "currency": "USD",
  "title": "공식 실적 발표 이후 중기 관점 유지",
  "summary": "확인된 변화와 현재 판단을 간결하게 설명합니다.",
  "short_term_stance": "balanced",
  "medium_term_stance": "favorable",
  "confidence": "medium",
  "change_label": "facts_updated",
  "sections": [
    {
      "title": "확인된 사실",
      "items": [
        {
          "kind": "fact",
          "text": "분석 기준 현재가는 234.56달러입니다.",
          "source_keys": ["toss-quote"]
        },
        {
          "kind": "fact",
          "text": "회사는 최신 분기 매출을 공시했습니다.",
          "source_keys": ["sec-1"]
        }
      ]
    }
  ],
  "sources": [
    {
      "source_key": "toss-quote",
      "kind": "market_data",
      "title": "현재가",
      "publisher": "토스증권",
      "url": "https://developers.tossinvest.com/",
      "published_at": null,
      "retrieved_at": "2026-09-13T21:00:00+09:00"
    },
    {
      "source_key": "sec-1",
      "kind": "regulator",
      "title": "Form 10-Q",
      "publisher": "SEC",
      "url": "https://www.sec.gov/example",
      "published_at": "2026-09-13T08:00:00-04:00",
      "retrieved_at": "2026-09-13T21:00:00+09:00"
    }
  ],
  "snapshot": {
    "quote_as_of": "2026-09-13T20:59:58+09:00",
    "coverage": {"primary_sources_checked": true}
  }
}
```

`price` and `currency` are required strings sourced from the core Toss quote. If either is unavailable, do not POST. Use a non-negative decimal string for price to preserve precision and a three-letter currency code. `snapshot` contains compact input provenance and coverage, not article text, secrets, or complete agent transcripts.

Allowed item kinds are `fact`, `inference`, `opinion`, and `unknown`. Include 1–20 sources. Source `kind` is a short descriptive string; prefer `market_data`, `regulator`, `exchange`, `issuer`, or `news`. At least one linked `market_data` source must identify the core Toss quote. Every source key referenced by an item must exist in `sources`; facts, inferences, and opinions require at least one source. An `unknown` item may have no source when it describes a missing evidence field. Keep source keys unique and match `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`.

Generate one UUID `run_id` per target analysis. If a POST times out or its result is ambiguous, retry only the identical JSON with the same `run_id`; never generate a replacement UUID for that attempt.

Example commands:

```bash
python3 .agents/skills/stock-report/scripts/report_client.py targets
python3 .agents/skills/stock-report/scripts/report_client.py context NASDAQ AAPL
python3 .agents/skills/stock-report/scripts/report_client.py dart-corp-code 005930
python3 .agents/skills/stock-report/scripts/report_client.py validate < report.json
python3 .agents/skills/stock-report/scripts/report_client.py submit < report.json
```

Do not pass JSON, tokens, or API keys as command-line arguments.

The client configuration contains secrets and must be a regular file owned by the current user with mode `0600`.
