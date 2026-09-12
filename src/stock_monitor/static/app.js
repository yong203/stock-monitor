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

setupFilters();
setupSearch();
setupWatchlist();
