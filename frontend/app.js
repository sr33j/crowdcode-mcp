const DEFAULT_PROD_API_BASE = "https://crowdcode-backend.onrender.com";
const DEFAULT_LOCAL_API_BASE = "http://127.0.0.1:8000";

const apiBase =
  window.CROWDCODE_API_BASE_URL ||
  (window.location.hostname === "localhost" ||
  window.location.hostname === "127.0.0.1"
    ? DEFAULT_LOCAL_API_BASE
    : DEFAULT_PROD_API_BASE);

const PAGE_SIZE = 15;

let services = [];
let provFilter = "all";
let sortKey = "rank_score";
let page = 1;
let expandedId = null;
const detailCache = new Map();

const tbody = document.querySelector("#tbody");
const pager = document.querySelector("#pager");
const statline = document.querySelector("#statline");
const searchInput = document.querySelector("#q");
const carousel = document.querySelector("#car");

function esc(value) {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (ch) =>
      ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      })[ch]
  );
}

function railLabel(provider) {
  if (provider === "mppx") return "MPP";
  if (provider === "x402") return "x402";
  return provider || "—";
}

function railClass(provider) {
  return provider === "x402" || provider === "mppx" ? provider : "other";
}

function filteredServices() {
  const q = searchInput.value.trim().toLowerCase();
  const rows = services.filter((s) => {
    if (provFilter !== "all" && s.payment_provider !== provFilter) return false;
    if (!q) return true;
    return (
      (s.name || "").toLowerCase().includes(q) ||
      (s.canonical_endpoint || "").toLowerCase().includes(q) ||
      (s.directory_slug || "").toLowerCase().includes(q)
    );
  });
  return rows.sort(
    (a, b) => (b[sortKey] || 0) - (a[sortKey] || 0) || b.num_reviews - a.num_reviews
  );
}

function renderTable() {
  const rows = filteredServices();
  const pages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  page = Math.min(page, pages);
  const start = (page - 1) * PAGE_SIZE;
  const slice = rows.slice(start, start + PAGE_SIZE);

  if (!slice.length) {
    tbody.innerHTML =
      '<tr><td colspan="5" class="state">No services match.</td></tr>';
  } else {
    expandedId = null;
    tbody.innerHTML = slice
      .map((s, i) => {
        const score = Number(s.score ?? s.rank_score) || 0;
        const width = Math.max(4, ((score - 1) / 4) * 100);
        const endpoint = s.canonical_endpoint || s.directory_slug || s.service_id;
        const scoreCell = s.unproven
          ? '<span class="chip unproven" title="Not enough trusted reviews yet">unproven</span>'
          : `<div class="score-cell"><span class="score-num">${score.toFixed(2)}</span><span class="score-bar"><i style="width:${width}%"></i></span></div>`;
        return `
        <tr data-service-id="${esc(s.service_id)}">
          <td class="rank">${start + i + 1}</td>
          <td class="name"><div class="nm">${esc(s.name || s.service_id)}</div><div class="ep">${esc(endpoint)}</div></td>
          <td><span class="chip ${railClass(s.payment_provider)}">${esc(railLabel(s.payment_provider))}</span></td>
          <td class="score">${scoreCell}</td>
          <td class="reviews">${
            s.unproven
              ? '<span class="lo">' + s.num_reviews + "</span>"
              : s.num_reviews
          }</td>
        </tr>`;
      })
      .join("");
  }

  let html = `<span class="info">${rows.length ? start + 1 : 0}–${
    start + slice.length
  } of ${rows.length}</span>`;
  html += `<button data-goto="${page - 1}" ${
    page === 1 ? "disabled" : ""
  } aria-label="Previous page">‹</button>`;
  for (let p = 1; p <= pages; p++) {
    html += `<button data-goto="${p}" aria-current="${p === page}">${p}</button>`;
  }
  html += `<button data-goto="${page + 1}" ${
    page === pages ? "disabled" : ""
  } aria-label="Next page">›</button>`;
  pager.innerHTML = html;

  document.querySelectorAll(".arr").forEach((el) => (el.textContent = ""));
  const arrow = document.querySelector(`#arr-${sortKey}`);
  if (arrow) arrow.textContent = "▼";
}

function renderDetail(d) {
  const histogram = d.histogram || {};
  const total =
    Object.values(histogram).reduce((sum, n) => sum + (n || 0), 0) || 1;
  const bars = [5, 4, 3, 2, 1]
    .map((star) => {
      const n = histogram[String(star)] || 0;
      const width = Math.round((n / total) * 100);
      return `<div class="hist-row"><span class="hist-star">★${star}</span><span class="hist-bar"><i style="width:${width}%"></i></span><span class="hist-n">${n}</span></div>`;
    })
    .join("");

  const summary = d.summary;
  const sections = [
    ["Strengths", summary?.strengths],
    ["Failure modes", summary?.failure_modes],
    ["Caveats", summary?.caveats],
  ]
    .filter(([, items]) => Array.isArray(items) && items.length)
    .map(
      ([label, items]) => `
      <div class="sum-section">
        <div class="sum-label">${label}</div>
        <ul>${items.map((item) => `<li>${esc(item)}</li>`).join("")}</ul>
      </div>`
    )
    .join("");
  const summaryBlock = sections
    ? `<div class="sum">
         <div class="sum-note">AI-generated from ${summary.n_reviews} review${
           summary.n_reviews === 1 ? "" : "s"
         }${summary.through_date ? ` through ${esc(summary.through_date)}` : ""}</div>
         ${sections}
       </div>`
    : '<div class="sum state">No review summary yet — summaries are generated nightly once a service has trusted reviews.</div>';

  const scoreLine = d.unproven
    ? '<span class="chip unproven">unproven</span>'
    : `<span class="score-num">${(Number(d.score) || 0).toFixed(2)}</span>`;

  const reviews = Array.isArray(d.recent_reviews) ? d.recent_reviews : [];
  const reviewItems = reviews
    .filter((review) => review.reason)
    .map(
      (review) => `
      <div class="rev">
        <span class="rev-stars">${"★".repeat(review.rating)}${"☆".repeat(5 - review.rating)}</span>
        ${review.payment_verified ? '<span class="chip verified">verified</span>' : ""}
        <span class="rev-text">${esc(review.reason)}</span>
      </div>`
    )
    .join("");

  return `
    <div class="detail">
      <div class="detail-head">
        ${scoreLine}
        <span class="detail-meta">n_eff ${(Number(d.n_eff) || 0).toFixed(1)}
          · ${d.num_reviews} review${d.num_reviews === 1 ? "" : "s"}
          · ${d.num_verified_reviews} verified</span>
      </div>
      <div class="detail-grid">
        ${summaryBlock}
        <div class="hist">${bars}</div>
      </div>
      ${reviewItems ? `<div class="revs">${reviewItems}</div>` : ""}
    </div>`;
}

async function toggleDetail(row) {
  const id = row.dataset.serviceId;
  const open = tbody.querySelector("tr.detail-row");
  const wasOpen = expandedId === id;
  if (open) open.remove();
  expandedId = null;
  if (wasOpen) return;

  expandedId = id;
  const detail = document.createElement("tr");
  detail.className = "detail-row";
  detail.innerHTML = '<td colspan="5"><div class="detail state">Loading…</div></td>';
  row.after(detail);

  try {
    let payload = detailCache.get(id);
    if (!payload) {
      payload = await fetchJson(`/api/services/${encodeURIComponent(id)}`);
      detailCache.set(id, payload);
    }
    if (expandedId === id && detail.isConnected) {
      detail.innerHTML = `<td colspan="5">${renderDetail(payload)}</td>`;
    }
  } catch (error) {
    if (expandedId === id && detail.isConnected) {
      detail.innerHTML = `<td colspan="5"><div class="detail state error">${esc(
        error.message
      )}</div></td>`;
    }
  }
}

function renderStatline(stats) {
  const totalReviews =
    stats?.total_reviews ??
    services.reduce((sum, s) => sum + (s.num_reviews || 0), 0);
  const numServices = stats?.num_services ?? services.length;
  statline.innerHTML =
    `<b>${totalReviews}</b> payment-verified reviews ` +
    '<span class="sep">/</span> ' +
    `<b>${numServices}</b> services ranked ` +
    '<span class="sep">/</span> <span class="live">growing daily</span>';
}

function renderIdeas(payload) {
  const ideas = Array.isArray(payload.ideas) ? payload.ideas : [];
  if (!ideas.length) {
    carousel.innerHTML = '<div class="idea state">No open requests right now.</div>';
    return;
  }
  carousel.innerHTML = ideas
    .map((idea) => {
      const count = idea.request_count || 1;
      return `
      <div class="idea">
        <div class="t">${esc(idea.title)}</div>
        <p>${esc(idea.summary)}</p>
        <div class="n">${count} request${count > 1 ? "s" : ""}</div>
      </div>`;
    })
    .join("");
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

async function loadServices() {
  try {
    let payload;
    try {
      payload = await fetchJson("/api/services");
    } catch {
      payload = await fetchJson("/api/services/top");
    }
    services = Array.isArray(payload.services) ? payload.services : [];
    renderStatline(payload.stats);
    renderTable();
  } catch (error) {
    tbody.innerHTML = `<tr><td colspan="5" class="state error">${esc(
      error.message
    )}</td></tr>`;
  }
}

async function loadIdeas() {
  try {
    renderIdeas(await fetchJson("/api/project-ideas"));
  } catch {
    carousel.innerHTML =
      '<div class="idea state">Requests are unavailable right now.</div>';
  }
}

/* ---------- wiring ---------- */

searchInput.addEventListener("input", () => {
  page = 1;
  renderTable();
});

tbody.addEventListener("click", (event) => {
  const row = event.target.closest("tr[data-service-id]");
  if (!row) return;
  toggleDetail(row);
});

document.querySelectorAll(".seg button").forEach((button) => {
  button.addEventListener("click", () => {
    document
      .querySelectorAll(".seg button")
      .forEach((b) => b.setAttribute("aria-pressed", "false"));
    button.setAttribute("aria-pressed", "true");
    provFilter = button.dataset.prov;
    page = 1;
    renderTable();
  });
});

document.querySelectorAll("th.sortable").forEach((th) => {
  th.addEventListener("click", () => {
    sortKey = th.dataset.sort;
    page = 1;
    renderTable();
  });
});

pager.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-goto]");
  if (!button || button.disabled) return;
  page = Number(button.dataset.goto);
  renderTable();
});

document.querySelectorAll(".install-tabs button").forEach((button) => {
  button.addEventListener("click", () => {
    button.parentElement
      .querySelectorAll("button")
      .forEach((b) => b.setAttribute("aria-selected", "false"));
    button.setAttribute("aria-selected", "true");
    for (const key of ["claude", "codex", "json"]) {
      document
        .querySelector(`#cmd-${key}`)
        .classList.toggle("hidden", key !== button.dataset.tab);
    }
  });
});

document.querySelectorAll(".copy-btn").forEach((button) => {
  button.addEventListener("click", () => {
    const pre = button.parentElement.querySelector("pre:not(.hidden)");
    const text = pre.textContent.replace(/^\$\s*/, "");
    navigator.clipboard.writeText(text).then(() => {
      button.textContent = "copied ✓";
      setTimeout(() => (button.textContent = "copy"), 1500);
    });
  });
});

document.querySelectorAll("[data-page]").forEach((button) => {
  button.addEventListener("click", () => {
    const which = button.dataset.page;
    document
      .querySelector("#page-rank")
      .classList.toggle("hidden", which !== "rank");
    document
      .querySelector("#page-docs")
      .classList.toggle("hidden", which !== "docs");
    document
      .querySelector("#nav-docs")
      .setAttribute("aria-current", which === "docs");
    window.scrollTo(0, 0);
  });
});

loadServices();
loadIdeas();
