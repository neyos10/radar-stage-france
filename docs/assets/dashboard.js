(() => {
  "use strict";

  const configNode = document.getElementById("radarConfig");
  const CONFIG = configNode ? JSON.parse(configNode.textContent) : {};
  const APPLIED_STORAGE_KEY = "stage-radar-applied-v1";
  const STATUS_LABELS = {todo: "À postuler", applied: "Déjà postulé", all: "Tous"};

  let activeSource = "*";
  let activeDomain = "*";
  let activeRegion = CONFIG.defaultRegion || "france";
  let activeStatus = "todo";
  let highlightOnly = false;
  let searchTerm = "";
  let sortMode = "date";

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

  const elements = {
    refreshButton: $("#refreshBtn"),
    searchInput: $("#searchInput"),
    visibleCount: $("#visibleCount"),
    todoCount: $("#todoCount"),
    appliedCount: $("#appliedCount"),
    activeFilters: $("#activeFilters"),
    resetButton: $("#resetBtn"),
    highlightFilter: $("#highlightFilter"),
    emptyState: $("#emptyState"),
    sortSelect: $("#sortSelect"),
    expiredVisibleCount: $("#expiredVisibleCount"),
    expiredEmptyState: $("#expiredEmptyState")
  };

  if (elements.sortSelect && elements.sortSelect.value === "relevance") {
    sortMode = "relevance";
  }

  const initialOrder = new WeakMap();
  $$(".job").forEach((job, index) => initialOrder.set(job, index));

  function normalizeText(value) {
    return String(value || "")
      .normalize("NFD")
      .replace(/[\u0300-\u036f]/g, "")
      .toLowerCase()
      .trim();
  }

  function csvValues(value) {
    return String(value || "")
      .split(",")
      .map(item => item.trim())
      .filter(Boolean);
  }

  function sourcesFor(job) {
    const mergedSources = csvValues(job.dataset.sources);
    return mergedSources.length ? mergedSources : csvValues(job.dataset.src);
  }

  function domainScoresFor(job) {
    if (!job.dataset.domainScores) return {};
    try {
      const scores = JSON.parse(job.dataset.domainScores);
      return scores && typeof scores === "object" && !Array.isArray(scores) ? scores : {};
    } catch (_error) {
      return {};
    }
  }

  function numericValue(value, fallback = Number.NEGATIVE_INFINITY) {
    if (value === "" || value === null || value === undefined) return fallback;
    const number = Number(value);
    return Number.isFinite(number) ? number : fallback;
  }

  function relevanceFor(job) {
    const scores = domainScoresFor(job);
    if (activeDomain !== "*") {
      return numericValue(scores[activeDomain]);
    }
    const explicitRelevance = numericValue(job.dataset.relevance);
    if (Number.isFinite(explicitRelevance)) return explicitRelevance;
    const values = Object.values(scores)
      .map(value => numericValue(value))
      .filter(Number.isFinite);
    return values.length ? Math.max(...values) : Number.NEGATIVE_INFINITY;
  }

  function dateFor(job) {
    const rawDate = job.dataset.sortDate || job.dataset.publishedAt || job.dataset.date || "";
    const timestamp = Date.parse(rawDate);
    return Number.isFinite(timestamp) ? timestamp : Number.NEGATIVE_INFINITY;
  }

  function compareJobs(left, right) {
    const expiryDifference = Number(isLikelyExpired(left)) - Number(isLikelyExpired(right));
    if (expiryDifference !== 0) return expiryDifference;
    if (sortMode === "relevance") {
      const relevanceDifference = relevanceFor(right) - relevanceFor(left);
      if (Number.isFinite(relevanceDifference) && relevanceDifference !== 0) {
        return relevanceDifference;
      }
    }
    const dateDifference = dateFor(right) - dateFor(left);
    if (Number.isFinite(dateDifference) && dateDifference !== 0) return dateDifference;
    return (initialOrder.get(left) || 0) - (initialOrder.get(right) || 0);
  }

  function sortOffers() {
    const byContainer = new Map();
    $$(".job").forEach(job => {
      const container = job.parentElement;
      if (!container) return;
      if (!byContainer.has(container)) byContainer.set(container, []);
      byContainer.get(container).push(job);
    });
    byContainer.forEach((jobs, container) => {
      jobs.sort(compareJobs).forEach(job => container.appendChild(job));
    });
  }

  function isLikelyExpired(job) {
    return job.classList.contains("likely-expired") ||
      ["1", "true"].includes(String(job.dataset.likelyExpired || "").toLowerCase());
  }

  function readApplied() {
    try {
      const value = JSON.parse(localStorage.getItem(APPLIED_STORAGE_KEY) || "[]");
      return new Set(Array.isArray(value) ? value : []);
    } catch (_error) {
      return new Set();
    }
  }

  let applied = readApplied();

  function saveApplied() {
    localStorage.setItem(APPLIED_STORAGE_KEY, JSON.stringify([...applied]));
  }

  function activateChip(selector, selectedButton) {
    $$(selector).forEach(button => {
      const active = button === selectedButton;
      button.classList.toggle("on", active);
      button.setAttribute("aria-pressed", active ? "true" : "false");
    });
  }

  function setSource(button, source) {
    activeSource = source;
    activateChip(".source-chip", button);
    refreshOffers();
  }

  function setStatus(button, status) {
    activeStatus = status;
    activateChip(".status-chip", button);
    refreshOffers();
  }

  function setDomain(button, domain) {
    activeDomain = domain;
    activateChip(".domain-chip", button);
    refreshOffers();
  }

  function setRegion(button, region) {
    activeRegion = region;
    activateChip(".region-chip", button);
    refreshOffers();
  }

  function toggleHighlight(button) {
    highlightOnly = !highlightOnly;
    button.classList.toggle("on", highlightOnly);
    button.setAttribute("aria-pressed", highlightOnly ? "true" : "false");
    refreshOffers();
  }

  function toggleApplied(id) {
    applied.has(id) ? applied.delete(id) : applied.add(id);
    saveApplied();
    refreshOffers();
  }

  function labelFor(mapping, value, allLabel) {
    return value === "*" ? allLabel : (mapping[value] || value);
  }

  function updateActiveFilters() {
    const parts = [
      `Statut : ${STATUS_LABELS[activeStatus]}`,
      `Source : ${activeSource === "*" ? "Toutes" : activeSource}`,
      `Domaine : ${labelFor(CONFIG.domainLabels || {}, activeDomain, "Tous")}`,
      `Région : ${labelFor(CONFIG.regionLabels || {}, activeRegion, "Toutes")}`
    ];
    if (highlightOnly) parts.push(`${CONFIG.highlightLabel || "Sélection"} uniquement`);
    if (searchTerm) parts.push(`Recherche : “${elements.searchInput.value.trim()}”`);
    if (sortMode === "relevance") parts.push("Tri : Pertinence");
    elements.activeFilters.textContent = parts.join(" · ");

    const defaultState = activeStatus === "todo" && activeSource === "*" &&
      activeDomain === "*" && activeRegion === CONFIG.defaultRegion &&
      !highlightOnly && !searchTerm && sortMode === "date";
    elements.resetButton.disabled = defaultState;
  }

  function refreshOffers() {
    sortOffers();

    let visible = 0;
    let visibleExpired = 0;
    let todo = 0;
    let done = 0;

    $$(".job").forEach(job => {
      const isApplied = applied.has(job.dataset.id);
      isApplied ? done++ : todo++;
      job.classList.toggle("applied", isApplied);

      const action = $(".apply-btn", job);
      action.textContent = isApplied ? "↩ Remettre à postuler" : "✓ Marquer comme postulé";
      action.classList.toggle("undo", isApplied);

      const domains = (job.dataset.domains || "").split(",").filter(Boolean);
      const regions = (job.dataset.regions || "").split(",").filter(Boolean);
      const sourceMatches = activeSource === "*" || sourcesFor(job).includes(activeSource);
      const domainMatches = activeDomain === "*" || domains.includes(activeDomain);
      const regionMatches = activeRegion === "*" || regions.includes(activeRegion);
      const statusMatches = activeStatus === "all" ||
        (activeStatus === "applied" ? isApplied : !isApplied);
      const highlightMatches = !highlightOnly || job.dataset.highlight === "1";
      const searchMatches = !searchTerm ||
        normalizeText(job.dataset.search).includes(searchTerm);
      const show = sourceMatches && domainMatches && regionMatches && statusMatches &&
        highlightMatches && searchMatches;

      job.hidden = !show;
      if (show) {
        isLikelyExpired(job) ? visibleExpired++ : visible++;
      }
    });

    elements.todoCount.textContent = `(${todo})`;
    elements.appliedCount.textContent = `(${done})`;
    elements.visibleCount.textContent = `${visible} offre${visible > 1 ? "s" : ""} affichée${visible > 1 ? "s" : ""}`;
    if (elements.expiredVisibleCount) {
      elements.expiredVisibleCount.textContent =
        `${visibleExpired} offre${visibleExpired > 1 ? "s" : ""}`;
    }

    $$(".expired-section").forEach(section => {
      const sectionJobs = $$(".job", section);
      const sectionHasVisibleOffers = sectionJobs.some(job => !job.hidden);
      section.hidden = sectionJobs.length === 0;
      section.classList.toggle("no-matches", !sectionHasVisibleOffers);
    });
    if (elements.expiredEmptyState) {
      elements.expiredEmptyState.hidden = visibleExpired !== 0;
    }

    if (visible === 0) {
      let message = visibleExpired > 0
        ? `Aucune offre active ne correspond, mais ${visibleExpired} offre${visibleExpired > 1 ? "s probablement pourvues correspondent" : " probablement pourvue correspond"} dans la section ci-dessous.`
        : (searchTerm
          ? "Aucune offre ne correspond à cette recherche et à ces filtres."
          : "Aucune offre ne correspond à ces filtres.");
      if (activeSource !== "*") {
        const chip = $(`.source-chip[data-src="${CSS.escape(activeSource)}"]`);
        if (chip && chip.dataset.note) {
          message = `${activeSource} : ${chip.dataset.note} — aucune offre à afficher.`;
        }
      }
      elements.emptyState.textContent = message;
    }
    elements.emptyState.hidden = visible !== 0;
    updateActiveFilters();
  }

  function resetFilters() {
    activeSource = "*";
    activeDomain = "*";
    activeRegion = CONFIG.defaultRegion || "france";
    activeStatus = "todo";
    highlightOnly = false;
    searchTerm = "";
    sortMode = "date";
    elements.searchInput.value = "";
    if (elements.sortSelect) elements.sortSelect.value = "date";
    activateChip(".source-chip", $(".source-chip[data-src='*']"));
    activateChip(".domain-chip", $(".domain-chip[data-domain='*']"));
    activateChip(".region-chip", $(`.region-chip[data-region="${CSS.escape(activeRegion)}"]`));
    activateChip(".status-chip", $(".status-chip[data-status='todo']"));
    elements.highlightFilter.classList.remove("on");
    elements.highlightFilter.setAttribute("aria-pressed", "false");
    refreshOffers();
  }

  elements.refreshButton.addEventListener("click", () => window.location.reload());
  elements.searchInput.addEventListener("input", event => {
    searchTerm = normalizeText(event.target.value);
    refreshOffers();
  });
  if (elements.sortSelect) {
    elements.sortSelect.addEventListener("change", event => {
      sortMode = event.target.value === "relevance" ? "relevance" : "date";
      refreshOffers();
    });
  }
  $$(".source-chip").forEach(button => button.addEventListener("click", () => setSource(button, button.dataset.src)));
  $$(".status-chip").forEach(button => button.addEventListener("click", () => setStatus(button, button.dataset.status)));
  $$(".domain-chip").forEach(button => button.addEventListener("click", () => setDomain(button, button.dataset.domain)));
  $$(".region-chip").forEach(button => button.addEventListener("click", () => setRegion(button, button.dataset.region)));
  elements.highlightFilter.addEventListener("click", event => toggleHighlight(event.currentTarget));
  elements.resetButton.addEventListener("click", resetFilters);
  $$(".apply-btn").forEach(button => button.addEventListener("click", () => toggleApplied(button.dataset.offerId)));

  refreshOffers();
})();
