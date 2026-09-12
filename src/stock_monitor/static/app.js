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

const formatKst = (value) => {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return {
    datetime: date.toISOString(),
    label: `${new Intl.DateTimeFormat("ko-KR", {
      timeZone: "Asia/Seoul",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hourCycle: "h23",
    }).format(date)} KST`,
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
  const nextLabel = label || quoteStatusLabels[safeStatus] || "시세 상태 확인 필요";
  if (text.textContent !== nextLabel) text.textContent = nextLabel;
};

const quoteDirection = (quote, change) => {
  if (["up", "down", "flat"].includes(quote.direction)) return quote.direction;
  if (["unchanged", "steady"].includes(quote.direction)) return "flat";
  if (change === null || change === 0) return "flat";
  return change > 0 ? "up" : "down";
};

const renderQuote = (card, quote) => {
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

  card.classList.remove(...connectionStatusClasses);
  card.classList.add(status);
  statusElement.textContent = quoteStatusLabels[status];

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

const renderQuoteSnapshot = (snapshot, cards) => {
  if (!snapshot || typeof snapshot !== "object") return;
  const connection = snapshot.connection;
  if (connection && typeof connection === "object") {
    setConnection(connection.status, connection.label);
  }
  if (!Array.isArray(snapshot.quotes)) return;
  snapshot.quotes.forEach((quote) => {
    if (!quote || typeof quote !== "object") return;
    const card = cards.get(quoteKey(quote.market, quote.symbol));
    if (card) renderQuote(card, quote);
  });
};

const setupQuoteStream = () => {
  const quoteCards = [...document.querySelectorAll("[data-quote-card]")];
  if (!quoteCards.length) return;
  if (!("EventSource" in window)) {
    setConnection("unavailable", "이 브라우저는 실시간 시세를 지원하지 않습니다");
    return;
  }
  const cards = new Map(
    quoteCards.map((card) => {
      const item = card.closest("[data-country][data-symbol]");
      return [quoteKey(item.dataset.country, item.dataset.symbol), card];
    }),
  );
  let pendingSnapshot = null;
  let renderTimer = null;
  const flushRender = () => {
    if (pendingSnapshot) renderQuoteSnapshot(pendingSnapshot, cards);
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
    quoteCards.forEach((card) => {
      card.classList.remove(...connectionStatusClasses);
      card.classList.add("delayed");
      card.querySelector(".quote-status").textContent = "지연";
    });
    setConnection("disconnected", "화면 연결 끊김 · 재연결 중");
  });
  window.addEventListener("pagehide", () => stream.close(), { once: true });
};

setupFilters();
setupSearch();
setupWatchlist();
setupQuoteStream();
