# Source policy

## Priority

Prefer the source closest to the asserted fact:

1. Regulator or exchange filing.
2. Issuer or ETF manager IR, product page, prospectus, and shareholder report.
3. Reputable original reporting for context or events not yet officially confirmed.
4. Search results, aggregators, social posts, and commentary are discovery aids only.

For Korea, start with [OpenDART disclosure search](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS001&apiId=2019001), KRX KIND, and issuer IR. For the US, start with the [SEC submissions and XBRL APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces) and issuer IR. For ETFs, prefer exchange disclosures and manager materials covering holdings, NAV, methodology, fees, and distributions.

For a Korean target, resolve its eight-digit DART identifier from the official company-code archive before querying disclosures:

```bash
python3 .agents/skills/stock-report/scripts/report_client.py dart-corp-code 005930
python3 .agents/skills/stock-report/scripts/report_client.py dart-corp-code 삼성전자
python3 .agents/skills/stock-report/scripts/report_client.py dart-disclosures CORP_CODE --begin YYYYMMDD --end YYYYMMDD
```

The first command prefers an exact listed stock-code match; a company-name query returns at most 20 listed matches. Both commands use the configured DART key without exposing it. Follow SEC automated-access rules, including an identified User-Agent and the current published request-rate limit.

## Claim linkage

- Attach source keys to the specific item they support; a bibliography alone is insufficient.
- A primary source can support a fact without a second source. Material news without primary confirmation remains `unknown` and cannot alone raise confidence above `low` or change a stance.
- IR is primary evidence of what the issuer said, not independent confirmation that a forecast will occur.
- Preserve publication and retrieval timestamps. Do not mix live prices with old fundamentals without calling out their different as-of times.
- If sources disagree, show the conflict and lower confidence. Do not average or select values without a documented basis.

## Deduplication and corrections

Use DART `rcept_no`, SEC accession number, or the official document identifier when available. Otherwise normalize the URL and cluster by instrument, event type, event date, actors, and key figures.

Syndicated versions of one event are one source cluster. Prefer the official original link. A correction, amendment, or withdrawal is new evidence: keep the old claim traceable and mark it superseded rather than silently overwriting it.

## Safety and storage

Store summaries and source metadata, not full articles. Do not reproduce paywalled text or long quotations. Exclude rumors, anonymous social claims, generic market commentary, unsupported analyst targets, and price-move stories with no new attributable fact.

End user-facing reports with a compact limitation: automated research may contain errors or omissions, and original sources should be checked before making an investment decision.
