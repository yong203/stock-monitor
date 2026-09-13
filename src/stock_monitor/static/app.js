const requestJson = async (url, options = {}) => {
  const response = await fetch(url, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  if (!response.ok) {
    let code = "request_failed";
    try {
      code = (await response.json()).detail?.code || code;
    } catch (_) {
      // Keep the stable fallback code for non-JSON failures.
    }
    throw new Error(code);
  }
  return response.status === 204 ? null : response.json();
};

const messages = {
  stock_catalog_not_ready: "종목 목록을 아직 준비하지 못했습니다.",
  watchlist_duplicate: "이미 관심종목에 있습니다.",
  watchlist_full: "관심종목은 20개까지 추가할 수 있습니다.",
  watchlist_order_conflict: "목록이 변경되었습니다. 화면을 새로고침해 주세요.",
  request_failed: "요청을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요.",
};

const displayError = (element, error) => {
  element.textContent = messages[error.message] || messages.request_failed;
  element.classList.add("error");
};

const setupFilters = () => {
  const buttons = [...document.querySelectorAll(".filter")];
  const cards = [...document.querySelectorAll("#watchlist-cards > li")];
  const empty = document.querySelector("#filtered-empty");
  if (!buttons.length) return;

  buttons.forEach((button) => {
    button.addEventListener("click", () => {
      const filter = button.dataset.filter;
      buttons.forEach((item) => {
        const selected = item === button;
        item.classList.toggle("active", selected);
        item.setAttribute("aria-pressed", String(selected));
      });
      let visible = 0;
      cards.forEach((card) => {
        const show =
          filter === "all" ||
          card.dataset.country === filter ||
          (filter === "ETF" && card.dataset.etf === "true");
        card.hidden = !show;
        if (show) visible += 1;
      });
      empty.hidden = visible !== 0;
    });
  });
};

const resultItem = (instrument) => {
  const item = document.createElement("li");
  const name = document.createElement("div");
  name.className = "instrument-name";
  const strong = document.createElement("strong");
  strong.textContent = instrument.name;
  const detail = document.createElement("span");
  detail.textContent = `${instrument.symbol} · ${instrument.market}${instrument.is_etf ? " · ETF" : ""}`;
  name.append(strong, detail);

  const add = document.createElement("button");
  add.type = "button";
  add.className = "button secondary";
  add.textContent = "추가";
  add.setAttribute("aria-label", `${instrument.name} 추가`);
  add.addEventListener("click", async () => {
    add.disabled = true;
    try {
      await requestJson("/api/watchlist", {
        method: "POST",
        body: JSON.stringify({ market: instrument.market, symbol: instrument.symbol }),
      });
      window.location.reload();
    } catch (error) {
      displayError(document.querySelector("#search-message"), error);
      add.disabled = false;
    }
  });
  item.append(name, add);
  return item;
};

const setupSearch = () => {
  const form = document.querySelector("#instrument-search");
  if (!form) return;
  const results = document.querySelector("#search-results");
  const message = document.querySelector("#search-message");
  let activeRequest;

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (activeRequest) activeRequest.abort();
    activeRequest = new AbortController();
    results.replaceChildren();
    message.textContent = "검색 중…";
    message.classList.remove("error");
    try {
      const query = new FormData(form).get("q").trim();
      const data = await requestJson(`/api/instruments/search?q=${encodeURIComponent(query)}`, {
        signal: activeRequest.signal,
      });
      data.items.forEach((item) => results.append(resultItem(item)));
      message.textContent = data.items.length ? `${data.items.length}개 결과` : "검색 결과가 없습니다.";
    } catch (error) {
      if (error.name === "AbortError") return;
      displayError(message, error);
    }
  });
};

const updateMoveButtons = (list) => {
  const items = [...list.children];
  items.forEach((item, index) => {
    item.querySelector(".move-up").disabled = index === 0;
    item.querySelector(".move-down").disabled = index === items.length - 1;
  });
};

const persistOrder = async (list, message, previousItems, movedName) => {
  const itemIds = [...list.children].map((item) => Number(item.dataset.itemId));
  const moveButtons = [...list.querySelectorAll(".move-up, .move-down")];
  moveButtons.forEach((button) => { button.disabled = true; });
  try {
    await requestJson("/api/watchlist/order", {
      method: "PUT",
      body: JSON.stringify({ item_ids: itemIds }),
    });
    message.textContent = `${movedName} 순서를 저장했습니다.`;
    message.classList.remove("error");
  } catch (error) {
    previousItems.forEach((item) => list.append(item));
    displayError(message, error);
  } finally {
    updateMoveButtons(list);
  }
};

const setupWatchlist = () => {
  const list = document.querySelector("#watchlist-items");
  if (!list) return;
  const message = document.querySelector("#watchlist-message");

  list.addEventListener("click", async (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    const item = button.closest("li");
    if (button.classList.contains("remove-item")) {
      if (!window.confirm(`${button.dataset.name}을(를) 삭제할까요?`)) return;
      button.disabled = true;
      try {
        await requestJson(`/api/watchlist/${item.dataset.itemId}`, { method: "DELETE" });
        window.location.reload();
      } catch (error) {
        displayError(message, error);
        button.disabled = false;
      }
      return;
    }
    const previousItems = [...list.children];
    const movedName = item.querySelector(".instrument-name strong").textContent;
    const sibling = button.classList.contains("move-up")
      ? item.previousElementSibling
      : item.nextElementSibling;
    if (!sibling) return;
    if (button.classList.contains("move-up")) list.insertBefore(item, sibling);
    else list.insertBefore(sibling, item);
    await persistOrder(list, message, previousItems, movedName);
  });
};

const quoteKey = (country, symbol) =>
  `${String(country).toUpperCase()}:${String(symbol).toUpperCase()}`;

const numericValue = (value) => {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
};

const fractionDigits = (currency) => (currency === "KRW" ? 0 : 4);

const formatAmount = (value, currency, minimumFractionDigits = 0) =>
  new Intl.NumberFormat("ko-KR", {
    minimumFractionDigits,
    maximumFractionDigits: fractionDigits(currency),
  }).format(value);

const formatPercent = (value) =>
  new Intl.NumberFormat("ko-KR", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value);

const formatKst = (value, includeSeconds = true) => {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-GB", {
      timeZone: "Asia/Seoul",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: includeSeconds ? "2-digit" : undefined,
      hourCycle: "h23",
    }).formatToParts(date).map(({ type, value: part }) => [type, part]),
  );
  return {
    datetime: date.toISOString(),
    label: `${parts.month}/${parts.day} ${parts.hour}:${parts.minute}${includeSeconds ? `:${parts.second}` : ""} KST`,
  };
};

const quoteStatusLabels = {
  idle: "시세 대기",
  live: "실시간",
  connected: "실시간",
  ready: "실시간",
  delayed: "지연",
  stale: "지연",
  disconnected: "연결 끊김",
  unavailable: "시세 없음",
  error: "시세 없음",
  auth_error: "인증 오류",
  ip_forbidden: "허용 IP 확인 필요",
  pending: "시세 대기",
  connecting: "연결 중",
};

const connectionStatusLabels = {
  idle: "시세 서버 대기",
  live: "시세 서버 연결됨",
  connected: "시세 서버 연결됨",
  ready: "시세 서버 연결됨",
  delayed: "시세 서버 응답 지연",
  stale: "시세 서버 응답 지연",
  disconnected: "화면 연결 끊김 · 재연결 중",
  unavailable: "시세 서버 상태 확인 필요",
  error: "시세 서버 오류",
  auth_error: "시세 서버 인증 오류",
  ip_forbidden: "시세 서버 허용 IP 확인 필요",
  pending: "시세 연결 준비 중",
  connecting: "시세 서버 연결 중",
};

const connectionStatusClasses = new Set([
  "idle",
  "live",
  "connected",
  "ready",
  "delayed",
  "stale",
  "disconnected",
  "unavailable",
  "error",
  "auth_error",
  "ip_forbidden",
  "pending",
  "connecting",
]);

const setConnection = (status, label) => {
  const connection = document.querySelector("#quote-connection");
  const text = document.querySelector("#quote-connection-label");
  if (!connection || !text) return;
  connectionStatusClasses.forEach((item) => connection.classList.remove(item));
  const safeStatus = connectionStatusClasses.has(status) ? status : "unavailable";
  connection.classList.add(safeStatus);
  const nextLabel = label || connectionStatusLabels[safeStatus] || "시세 서버 상태 확인 필요";
  if (text.textContent !== nextLabel) text.textContent = nextLabel;
};

const marketStatusClasses = new Set(["loading", "ready", "stale", "error"]);
const marketPhaseClasses = new Set([
  "phase-preopen",
  "phase-open",
  "phase-between",
  "phase-closed",
  "phase-holiday",
  "phase-unknown",
]);
const cardVisualClasses = new Set([
  ...connectionStatusClasses,
  "market-open",
  "market-preopen",
  "market-between",
  "market-closed",
  "market-holiday",
  "market-unknown",
]);
const marketCountries = ["KR", "US"];
const marketSessionLabels = {
  KR: { pre: "NXT 장전", regular: "정규장", after: "NXT 장후" },
  US: { day: "데이마켓", pre: "프리마켓", regular: "정규장", after: "애프터마켓" },
};

const normalizedMarket = (value) => {
  const market = value && typeof value === "object" ? value : {};
  return {
    status: marketStatusClasses.has(market.status) ? market.status : "error",
    phase: ["preopen", "open", "between", "closed", "holiday"].includes(market.phase)
      ? market.phase
      : null,
    session: typeof market.session === "string" ? market.session : null,
    error: typeof market.error === "string" ? market.error : null,
    nextEvent: market.next_event && typeof market.next_event === "object"
      ? market.next_event
      : null,
  };
};

const sessionLabel = (country, session) =>
  marketSessionLabels[country]?.[session] || "거래 세션";

const nextEventDescription = (country, nextEvent) => {
  if (!nextEvent) return null;
  const time = formatKst(nextEvent.at, false);
  if (!time) return null;
  const kind = nextEvent.kind;
  const isEnd = kind === "end" || kind === "close";
  return {
    datetime: time.datetime,
    time: time.label,
    suffix: isEnd ? "마감" : `${sessionLabel(country, nextEvent.session)} 시작`,
  };
};

const marketDescription = (country, value) => {
  const market = normalizedMarket(value);
  const next = nextEventDescription(country, market.nextEvent);
  if (market.status === "loading") {
    return { ...market, label: "시장 시간 확인 중", detail: "Toss 일정을 불러오는 중", next: null };
  }
  if (market.error === "ip_forbidden") {
    return { ...market, label: "허용 IP 확인 필요", detail: "Toss 앱의 등록 공인 IP를 확인하세요", next };
  }
  if (market.error === "auth_error") {
    return { ...market, label: "시장 일정 인증 오류", detail: "Toss API 인증 정보를 확인하세요", next };
  }
  if (market.error === "rate_limited") {
    return { ...market, label: "시장 시간 요청 지연", detail: "Toss 재시도 대기 중", next };
  }
  if (market.status === "error" || !market.phase) {
    return { ...market, label: "시장 시간 확인 불가", detail: "잠시 후 다시 확인", next: null };
  }
  if (market.status === "stale") {
    return { ...market, label: "시장 시간 지연", detail: "최신 일정을 확인하는 중", next };
  }
  const labels = {
    preopen: "개장 전",
    open: sessionLabel(country, market.session),
    between: "세션 전환 대기",
    closed: "장 마감",
    holiday: "휴장",
  };
  return {
    ...market,
    label: labels[market.phase],
    detail: next ? "" : market.phase === "open" ? "종료 시간 확인 중" : "다음 일정 확인 중",
    next,
  };
};

const setMarketDetail = (element, description) => {
  element.replaceChildren();
  if (!description.next) {
    element.textContent = description.detail;
    return;
  }
  const time = document.createElement("time");
  time.dateTime = description.next.datetime;
  time.textContent = description.next.time;
  element.append(time, document.createTextNode(` · ${description.next.suffix}`));
};

let lastMarketAnnouncement = "";

const renderMarkets = (markets) => {
  const rendered = new Map();
  marketCountries.forEach((country) => {
    const description = marketDescription(country, markets?.[country]);
    rendered.set(country, description);
    const tile = document.querySelector(`[data-market-status][data-country="${country}"]`);
    if (!tile) return;
    marketStatusClasses.forEach((item) => tile.classList.remove(item));
    marketPhaseClasses.forEach((item) => tile.classList.remove(item));
    tile.classList.add(description.status, `phase-${description.phase || "unknown"}`);
    tile.querySelector(".market-phase").textContent = description.label;
    setMarketDetail(tile.querySelector(".market-next"), description);
  });

  const announcement = document.querySelector("#market-state-announcement");
  const nextAnnouncement = marketCountries
    .map((country) => `${country === "KR" ? "한국" : "미국"} ${rendered.get(country).label}`)
    .join(", ");
  if (announcement && nextAnnouncement !== lastMarketAnnouncement) {
    announcement.textContent = nextAnnouncement;
    lastMarketAnnouncement = nextAnnouncement;
  }
  return rendered;
};

const markMarketsDisconnected = (latestMarkets) => {
  const fallback = {};
  marketCountries.forEach((country) => {
    const current = latestMarkets.get(country);
    fallback[country] = current
      ? {
          status: "stale",
          phase: current.phase || "unknown",
          session: current.session,
          next_event: current.nextEvent,
        }
      : { status: "error", phase: "unknown", session: null, next_event: null };
  });
  const rendered = renderMarkets(fallback);
  rendered.forEach((market, country) => latestMarkets.set(country, market));
};

const marketAllowsLive = (market) =>
  market?.status === "ready" && market.phase === "open" && Boolean(market.session);

const cardMarketLabel = (market) => {
  if (!market) return "시장 확인 중";
  return market.label;
};

const cardQuoteLabel = (market, status, hasPrice) => {
  if (["auth_error", "ip_forbidden", "disconnected", "error", "unavailable"].includes(status)) {
    return quoteStatusLabels[status];
  }
  if (["delayed", "stale"].includes(status)) return "시세 지연";
  if (marketAllowsLive(market) && ["live", "connected", "ready"].includes(status)) {
    return "실시간";
  }
  if (hasPrice) return market?.status === "ready" ? "마지막 가격" : "최신 가격";
  return quoteStatusLabels[status] || "시세 대기";
};

const quoteDirection = (quote, change) => {
  if (["up", "down", "flat"].includes(quote.direction)) return quote.direction;
  if (["unchanged", "steady"].includes(quote.direction)) return "flat";
  if (change === null || change === 0) return "flat";
  return change > 0 ? "up" : "down";
};

const renderQuote = (card, quote, market) => {
  const price = numericValue(quote.price);
  const change = numericValue(quote.change);
  const percent = numericValue(quote.change_percent);
  const currency = typeof quote.currency === "string" ? quote.currency.toUpperCase() : "";
  const status = quoteStatusLabels[quote.status] ? quote.status : "unavailable";
  const statusElement = card.querySelector(".quote-status");
  const priceElement = card.querySelector(".price");
  const priceValue = card.querySelector(".price-value");
  const priceCurrency = card.querySelector(".price-currency");
  const changeElement = card.querySelector(".change");
  const changeSymbol = card.querySelector(".change-symbol");
  const changeText = card.querySelector(".change-text");
  const time = card.querySelector(".quote-time");

  card.classList.remove(...cardVisualClasses);
  const live = marketAllowsLive(market) && ["live", "connected", "ready"].includes(status);
  if (
    live ||
    ["delayed", "stale", "disconnected", "unavailable", "error", "auth_error", "ip_forbidden"].includes(status)
  ) {
    card.classList.add(live ? "live" : status);
  } else {
    card.classList.add(`market-${market?.phase || "unknown"}`);
  }
  statusElement.textContent = `${cardMarketLabel(market)} · ${cardQuoteLabel(market, status, price !== null)}`;

  if (price !== null && currency) {
    priceValue.textContent = formatAmount(price, currency, currency === "USD" ? 2 : 0);
    priceCurrency.textContent = currency;
    priceElement.classList.remove("unavailable");
    card.dataset.hasQuote = "true";
  } else if (card.dataset.hasQuote !== "true") {
    priceValue.textContent = "—";
    priceCurrency.textContent = status === "pending" ? "시세 준비 중" : "시세 없음";
    priceElement.classList.add("unavailable");
  }

  if (change !== null && percent !== null && currency) {
    const direction = quoteDirection(quote, change);
    const sign = direction === "up" ? "+" : direction === "down" ? "−" : "";
    const symbol = direction === "up" ? "▲" : direction === "down" ? "▼" : "—";
    const directionLabel = direction === "up" ? "상승" : direction === "down" ? "하락" : "보합";
    const amount = formatAmount(Math.abs(change), currency, currency === "USD" ? 2 : 0);
    const rate = formatPercent(Math.abs(percent));
    changeElement.classList.remove("up", "down", "neutral");
    changeElement.classList.add(direction === "flat" ? "neutral" : direction);
    changeSymbol.textContent = symbol;
    changeText.textContent = `${sign}${amount} ${currency} (${sign}${rate}%)`;
    changeElement.setAttribute(
      "aria-label",
      `${directionLabel}, 전일 대비 ${sign}${amount} ${currency}, ${sign}${rate}퍼센트`,
    );
    card.dataset.hasChange = "true";
  } else if (card.dataset.hasChange !== "true") {
    changeElement.classList.remove("up", "down");
    changeElement.classList.add("neutral");
    changeSymbol.textContent = "—";
    changeText.textContent = "전일 대비 정보 없음";
    changeElement.removeAttribute("aria-label");
  }

  const updated = formatKst(quote.provider_at || quote.received_at);
  if (updated) {
    time.dateTime = updated.datetime;
    time.textContent = updated.label;
  }
};

const renderQuoteSnapshot = (snapshot, cards, latestMarkets, latestQuotes) => {
  if (!snapshot || typeof snapshot !== "object") return;
  const connection = snapshot.connection;
  if (connection && typeof connection === "object") {
    setConnection(connection.status);
  }
  const renderedMarkets = renderMarkets(snapshot.markets);
  renderedMarkets.forEach((market, country) => latestMarkets.set(country, market));
  if (Array.isArray(snapshot.quotes)) {
    snapshot.quotes.forEach((quote) => {
      if (!quote || typeof quote !== "object") return;
      latestQuotes.set(quoteKey(quote.market, quote.symbol), quote);
    });
  }
  cards.forEach(({ card, country }, key) => {
    const quote = latestQuotes.get(key) || { status: "pending" };
    renderQuote(card, quote, latestMarkets.get(country));
  });
};

const setupQuoteStream = () => {
  const quoteCards = [...document.querySelectorAll("[data-quote-card]")];
  const marketTiles = [...document.querySelectorAll("[data-market-status]")];
  if (!quoteCards.length && !marketTiles.length) return;
  if (!("EventSource" in window)) {
    setConnection("unavailable", "이 브라우저는 실시간 시세를 지원하지 않습니다");
    renderMarkets({});
    return;
  }
  const cards = new Map(
    quoteCards.map((card) => {
      const item = card.closest("[data-country][data-symbol]");
      return [
        quoteKey(item.dataset.country, item.dataset.symbol),
        { card, country: item.dataset.country.toUpperCase() },
      ];
    }),
  );
  const latestMarkets = new Map();
  const latestQuotes = new Map();
  let pendingSnapshot = null;
  let renderTimer = null;
  const flushRender = () => {
    if (pendingSnapshot) {
      renderQuoteSnapshot(pendingSnapshot, cards, latestMarkets, latestQuotes);
    }
    pendingSnapshot = null;
    renderTimer = null;
  };
  const scheduleRender = (snapshot) => {
    pendingSnapshot = snapshot;
    if (renderTimer !== null) return;
    renderTimer = window.setTimeout(flushRender, 250);
  };
  const flushBeforeConnectionState = () => {
    if (renderTimer !== null) window.clearTimeout(renderTimer);
    flushRender();
  };

  setConnection("connecting", "시세 연결 중");
  const stream = new EventSource("/api/quotes/stream");
  stream.addEventListener("open", () => setConnection("connecting", "시세 상태 확인 중"));
  stream.addEventListener("message", (event) => {
    try {
      scheduleRender(JSON.parse(event.data));
    } catch (_) {
      flushBeforeConnectionState();
      setConnection("unavailable", "시세 응답을 확인할 수 없습니다");
    }
  });
  stream.addEventListener("error", () => {
    flushBeforeConnectionState();
    markMarketsDisconnected(latestMarkets);
    cards.forEach(({ card, country }, key) => {
      const quote = { ...(latestQuotes.get(key) || {}), status: "disconnected" };
      renderQuote(card, quote, latestMarkets.get(country));
    });
    setConnection("disconnected");
  });
  window.addEventListener("pagehide", () => stream.close(), { once: true });
};

const setupKstTimes = () => {
  document.querySelectorAll("[data-kst-time]").forEach((element) => {
    const formatted = formatKst(element.dateTime || element.textContent, false);
    if (formatted) element.textContent = formatted.label;
  });
};

const setupReportTimeline = () => {
  const timeline = document.querySelector(".report-timeline");
  if (!timeline) return;

  let activeTrigger = null;
  let ownsHistoryEntry = false;
  let focusAfterHistory = null;

  const bodyFor = (trigger) => document.getElementById(trigger.getAttribute("aria-controls"));
  const cardFor = (trigger) => trigger.closest(".report-card");
  const setExpanded = (trigger, expanded) => {
    const body = bodyFor(trigger);
    if (!body) return;
    trigger.setAttribute("aria-expanded", String(expanded));
    trigger.querySelector("[data-expand-label]").textContent = expanded
      ? "상세 분석 접기"
      : "상세 분석 펼치기";
    body.hidden = !expanded;
    cardFor(trigger).classList.toggle("expanded", expanded);
  };
  const closeActive = ({ restoreFocus = false } = {}) => {
    if (!activeTrigger) return;
    const closing = activeTrigger;
    setExpanded(closing, false);
    activeTrigger = null;
    if (restoreFocus) closing.focus({ preventScroll: true });
  };
  const open = (trigger) => {
    if (activeTrigger && activeTrigger !== trigger) setExpanded(activeTrigger, false);
    setExpanded(trigger, true);
    activeTrigger = trigger;
  };
  const triggerForHash = () => {
    if (!window.location.hash) return null;
    const target = document.getElementById(decodeURIComponent(window.location.hash.slice(1)));
    return target?.closest(".report-card")?.querySelector("[data-report-toggle]") || null;
  };
  const baseUrl = `${window.location.pathname}${window.location.search}`;

  timeline.addEventListener("click", (event) => {
    const link = event.target.closest(".source-references a");
    if (link) {
      const target = document.getElementById(decodeURIComponent(link.hash.slice(1)));
      if (!target) return;
      event.preventDefault();
      if (target.hidden) {
        const sources = target.closest(".report-sources");
        sources.querySelectorAll("[data-source-extra]").forEach((source) => {
          source.hidden = false;
        });
        const more = sources.querySelector("[data-source-more]");
        if (more) {
          more.setAttribute("aria-expanded", "true");
          more.textContent = "출처 접기";
        }
      }
      window.history.replaceState(window.history.state, "", link.hash);
      target.scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }

    const more = event.target.closest("[data-source-more]");
    if (more) {
      const expanded = more.getAttribute("aria-expanded") === "true";
      const sources = more.closest(".report-sources");
      sources.querySelectorAll("[data-source-extra]").forEach((source) => {
        source.hidden = expanded;
      });
      more.setAttribute("aria-expanded", String(!expanded));
      more.textContent = expanded
        ? `출처 ${sources.querySelectorAll("[data-source-extra]").length}개 더 보기`
        : "출처 접기";
      return;
    }

    const close = event.target.closest("[data-report-close]");
    if (close) {
      const trigger = close.closest(".report-card")?.querySelector("[data-report-toggle]");
      if (!trigger) return;
      if (ownsHistoryEntry && window.history.state?.inlineReport) {
        focusAfterHistory = trigger;
        window.history.back();
      } else {
        closeActive({ restoreFocus: true });
        window.history.replaceState(window.history.state, "", baseUrl);
      }
      return;
    }

    const trigger = event.target.closest("[data-report-toggle]");
    if (!trigger) return;
    if (activeTrigger === trigger) {
      if (ownsHistoryEntry && window.history.state?.inlineReport) {
        focusAfterHistory = trigger;
        window.history.back();
      } else {
        closeActive({ restoreFocus: true });
        window.history.replaceState(window.history.state, "", baseUrl);
      }
      return;
    }

    const replacing = Boolean(activeTrigger);
    open(trigger);
    const nextUrl = `#${cardFor(trigger).id}`;
    const nextState = { ...(window.history.state || {}), inlineReport: true };
    if (replacing || ownsHistoryEntry) {
      window.history.replaceState(nextState, "", nextUrl);
    } else {
      window.history.pushState(nextState, "", nextUrl);
      ownsHistoryEntry = true;
    }
  });

  const loadMore = document.querySelector("[data-report-more]");
  if (loadMore) {
    loadMore.addEventListener("click", async () => {
      const status = loadMore.parentElement.querySelector(".report-more-status");
      loadMore.disabled = true;
      status.textContent = "과거 보고서를 불러오는 중…";
      try {
        const market = encodeURIComponent(loadMore.dataset.market);
        const symbol = encodeURIComponent(loadMore.dataset.symbol);
        const cursor = encodeURIComponent(loadMore.dataset.nextCursor);
        const response = await fetch(
          `/api/instruments/${market}/${symbol}/reports?cursor=${cursor}`,
        );
        if (!response.ok) throw new Error("request_failed");
        const template = document.createElement("template");
        template.innerHTML = (await response.text()).trim();
        const count = template.content.querySelectorAll(".report-card").length;
        timeline.append(template.content);
        setupKstTimes();
        const nextCursor = response.headers.get("X-Next-Cursor");
        if (nextCursor) {
          loadMore.dataset.nextCursor = nextCursor;
          loadMore.disabled = false;
          status.textContent = `${count}개를 불러왔습니다.`;
        } else {
          loadMore.remove();
          status.textContent = "모든 보고서를 불러왔습니다.";
        }
      } catch (_) {
        loadMore.disabled = false;
        status.textContent = "과거 보고서를 불러오지 못했습니다. 다시 시도해 주세요.";
      }
    });
  }

  const syncFromHistory = () => {
    const trigger = triggerForHash();
    if (trigger) open(trigger);
    else closeActive();
    ownsHistoryEntry = Boolean(window.history.state?.inlineReport);
    if (focusAfterHistory) {
      focusAfterHistory.focus({ preventScroll: true });
      focusAfterHistory = null;
    }
  };
  window.addEventListener("popstate", syncFromHistory);

  const initialTrigger = triggerForHash();
  if (initialTrigger) {
    open(initialTrigger);
    window.requestAnimationFrame(() => {
      const target = document.getElementById(decodeURIComponent(window.location.hash.slice(1)));
      target?.scrollIntoView({ block: "start" });
    });
  }
};

setupFilters();
setupSearch();
setupWatchlist();
setupQuoteStream();
setupKstTimes();
setupReportTimeline();
