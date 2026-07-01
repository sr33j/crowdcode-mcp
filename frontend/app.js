const DEFAULT_PROD_API_BASE = "https://crowdcode-backend.onrender.com";
const DEFAULT_LOCAL_API_BASE = "http://127.0.0.1:8000";

const apiBase =
  window.CROWDCODE_API_BASE_URL ||
  (window.location.hostname === "localhost" ||
  window.location.hostname === "127.0.0.1"
    ? DEFAULT_LOCAL_API_BASE
    : DEFAULT_PROD_API_BASE);

const ideasRoot = document.querySelector("#ideas");
const ideasStatus = document.querySelector("#ideas-status");
const servicesRoot = document.querySelector("#services");
const servicesStatus = document.querySelector("#services-status");
const refreshButton = document.querySelector("#refresh");

function formatNumber(value, digits = 1) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "0";
  return number.toFixed(digits);
}

function setStatus(element, text, isError = false) {
  element.textContent = text;
  element.className = isError ? "status error" : "status";
}

function emptyBlock(text) {
  const block = document.createElement("div");
  block.className = "empty";
  block.textContent = text;
  return block;
}

function renderIdeas(payload) {
  ideasRoot.replaceChildren();
  const ideas = Array.isArray(payload.ideas) ? payload.ideas : [];

  if (!ideas.length) {
    ideasRoot.append(emptyBlock("No recent requests found."));
    setStatus(
      ideasStatus,
      `${payload.source_request_count || 0} recent requests analyzed.`
    );
    return;
  }

  setStatus(
    ideasStatus,
    `${payload.source_request_count || 0} recent requests analyzed via ${payload.source}.`
  );

  for (const idea of ideas) {
    const article = document.createElement("article");
    article.className = "idea";

    const header = document.createElement("div");
    header.className = "idea-header";

    const text = document.createElement("div");
    const title = document.createElement("h3");
    title.textContent = idea.title || "Untitled idea";
    const summary = document.createElement("p");
    summary.textContent = idea.summary || "";
    text.append(title, summary);

    const count = document.createElement("span");
    count.className = "count";
    count.textContent = `${idea.request_count || 1} requests`;
    header.append(text, count);
    article.append(header);

    if (Array.isArray(idea.tags) && idea.tags.length) {
      const tags = document.createElement("div");
      tags.className = "tags";
      for (const value of idea.tags) {
        const tag = document.createElement("span");
        tag.className = "tag";
        tag.textContent = value;
        tags.append(tag);
      }
      article.append(tags);
    }

    if (Array.isArray(idea.example_requests) && idea.example_requests.length) {
      const examples = document.createElement("div");
      examples.className = "examples";
      for (const value of idea.example_requests.slice(0, 3)) {
        const example = document.createElement("div");
        example.textContent = value;
        examples.append(example);
      }
      article.append(examples);
    }

    ideasRoot.append(article);
  }
}

function renderServices(payload) {
  servicesRoot.replaceChildren();
  const services = Array.isArray(payload.services) ? payload.services : [];

  if (!services.length) {
    servicesRoot.append(emptyBlock("No reviewed services yet."));
    setStatus(servicesStatus, "0 ranked services.");
    return;
  }

  setStatus(servicesStatus, `${services.length} ranked services.`);

  services.forEach((service, index) => {
    const item = document.createElement("li");
    item.className = "service";

    const main = document.createElement("div");
    main.className = "service-main";

    const rank = document.createElement("div");
    rank.className = "rank";
    rank.textContent = String(index + 1);

    const text = document.createElement("div");
    const title = document.createElement("h3");
    title.textContent = service.name || service.service_id || "Unknown service";
    const meta = document.createElement("div");
    meta.className = "service-meta";
    meta.textContent =
      service.directory_slug ||
      service.canonical_endpoint ||
      service.payment_provider ||
      service.service_id ||
      "";
    text.append(title, meta);
    main.append(rank, text);

    const scoreRow = document.createElement("div");
    scoreRow.className = "score-row";
    scoreRow.append(
      metric(formatNumber(service.avg_rating), "Rating"),
      metric(String(service.num_reviews || 0), "Reviews"),
      metric(formatNumber(service.rank_score), "Score")
    );

    item.append(main, scoreRow);
    servicesRoot.append(item);
  });
}

function metric(value, label) {
  const box = document.createElement("div");
  box.className = "metric";
  const strong = document.createElement("strong");
  strong.textContent = value;
  const span = document.createElement("span");
  span.textContent = label;
  box.append(strong, span);
  return box;
}

async function fetchJson(path) {
  const response = await fetch(`${apiBase}${path}`, {
    headers: { Accept: "application/json" },
  });
  const payload = await response.json();
  if (!response.ok || payload.ok === false) {
    throw new Error(payload.error || `Request failed: ${response.status}`);
  }
  return payload;
}

async function loadIdeas(refresh = false) {
  setStatus(ideasStatus, "Loading recent requests...");
  ideasRoot.replaceChildren();
  try {
    const payload = await fetchJson(
      `/api/project-ideas${refresh ? "?refresh=true" : ""}`
    );
    renderIdeas(payload);
  } catch (error) {
    setStatus(ideasStatus, error.message, true);
    ideasRoot.append(emptyBlock("Project ideas are unavailable."));
  }
}

async function loadServices() {
  setStatus(servicesStatus, "Loading service rankings...");
  servicesRoot.replaceChildren();
  try {
    const payload = await fetchJson("/api/services/top");
    renderServices(payload);
  } catch (error) {
    setStatus(servicesStatus, error.message, true);
    servicesRoot.append(emptyBlock("Service rankings are unavailable."));
  }
}

refreshButton.addEventListener("click", () => loadIdeas(true));

loadIdeas();
loadServices();
