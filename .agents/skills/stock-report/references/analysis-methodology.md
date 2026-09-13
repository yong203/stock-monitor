# Analysis methodology

## Report shape

Treat the saved report as a point-in-time research record, not personalized investment advice.

- `initial`: establish the first evidence-backed view.
- `view_changed`: new evidence changes at least one horizon's stance.
- `view_reinforced`: new evidence materially strengthens the existing stance.
- `facts_updated`: facts changed without changing the stance.
- `unchanged`: the explicit run completed and found no material change.

Lead with what changed since `latest_report`. Keep stable background in the snapshot or a compact section instead of repeating it as news.

## Evidence classes

Every section item has exactly one class:

- `fact`: directly supported by the cited source or supplied context.
- `inference`: a reasoned implication from cited facts; state the assumption.
- `opinion`: the report's bounded assessment; state the horizon and invalidation condition.
- `unknown`: relevant but unconfirmed information or an explicit evidence gap.

Never turn price movement alone into a causal explanation. Preserve currency, unit, reporting period, fiscal basis, and consolidated/separate basis for every number.

## Stance and confidence

Use the API enums and render them in Korean as follows:

- `favorable` — 우호적: verified improvement or catalyst outweighs identified risks.
- `balanced` — 중립: evidence is mixed or no material directional change exists.
- `cautious` — 주의: verified deterioration or a high-impact risk dominates.
- `insufficient` — 판단 보류: evidence is missing, stale, or conflicting.

Assess both `short_term_stance` (1–4 weeks) and `medium_term_stance` (3–12 months). Do not silently map insufficient evidence to balanced.

Confidence is `high` only when current primary evidence and adequate quantitative context directly support the view, `medium` when material assumptions remain, and `low` for limited or secondary-only evidence. Use `insufficient` plus an `unknown` item when non-price external evidence is inadequate. A stance needs up to three decisive reasons and an observable invalidation condition.

## Quantitative frame

Use the supplied Toss quote and analytics without reconstructing unavailable fields.

- Always state the quote timestamp and freshness.
- If the core Toss quote, price, or currency is unavailable, stop without saving a report.
- Use returns, volatility, drawdown, or volume metrics only when present in `analytics.metrics` and their calculation basis is known. Treat `analytics.errors` as explicit coverage gaps and use `analytics.collected_at` to judge freshness.
- Do not infer adjusted-price behavior. If splits or distributions may distort candles and adjustment is unverified, exclude affected comparisons.
- For stocks, prioritize year-over-year revenue, operating income, net income, margins, cash flow, net debt, dilution, and issuer guidance. Distinguish Korean cumulative quarterly figures and consolidated/separate statements.
- Calculate valuation multiples only when the supplied point-in-time inputs are complete and comparable. No DCF, target price, consensus fabrication, or unsupported “cheap/expensive” label.

## Qualitative frame

For stocks, examine business drivers, guidance, capital allocation, competitive or regulatory change, governance, customer/product concentration, and dated catalysts. Separate issuer claims from regulator-confirmed facts.

For ETFs, do not apply a company earnings frame. Use the subtype that fits the official product description:

- Equity: index methodology, holdings concentration, sector/country/currency exposure, rebalance, fees, NAV premium/discount, tracking and liquidity.
- Bond: duration, credit quality, maturity, yield basis, rate/spread/currency risk.
- Commodity/futures: spot versus futures structure, roll effects, collateral and currency exposure.
- Leveraged/inverse: daily reset and path-dependence risk must be prominent.

If ETF subtype data is insufficient, use `insufficient`; do not guess from the product name.

## Required parallel review

Launch exactly the three configured project agents concurrently for the selected scope and wait for all results:

- `quant-analyst`: verify Toss/analytics freshness, calculations, and quantitative limits.
- `fundamental-analyst`: research company/fund fundamentals, filings, IR, and attributable news.
- `risk-reviewer`: independently challenge causality, omissions, source quality, and both horizon stances.

Only the main agent may merge their findings and POST. Resolve disagreements explicitly; never choose a convenient number when sources conflict.
