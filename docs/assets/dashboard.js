(() => {
  "use strict";

  const configNode = document.getElementById("radarConfig");
  const CONFIG = configNode ? JSON.parse(configNode.textContent) : {};
  const APPLIED_STORAGE_KEY = "stage-radar-applied-v1";
  const STATUS_LABELS = {todo: "À postuler", applied: "Déjà postulé", all: "Tous"};

  let activeSource = "*";
  let activeDomain = "*";
  let activeRegion = CONFIG.defaultRegion || "*";
  let activeStatus = "todo";
  let highlightOnly = false;
  let searchTerm = "";

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
    emptyState: $("#emptyState")
  };

  function normalizeText(value) {
    return String(value || "")
      .normalize("NFD")
      .replace(/[\u0300-\u036f]/g, "")
      .toLowerCase()
      .trim();
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
    elements.activeFilters.textContent = parts.join(" · ");

    const defaultState = activeStatus === "todo" && activeSource === "*" &&
      activeDomain === "*" && activeRegion === (CONFIG.defaultRegion || "*") &&
      !highlightOnly && !searchTerm;
    elements.resetButton.disabled = defaultState;
  }

  function refreshOffers() {
    let visible = 0;
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
      const sourceMatches = activeSource === "*" || job.dataset.src === activeSource;
      const domainMatches = activeDomain === "*" || domains.includes(activeDomain);
      const regionMatches = activeRegion === "*" || regions.includes(activeRegion);
      const statusMatches = activeStatus === "all" ||
        (activeStatus === "applied" ? isApplied : !isApplied);
      const highlightMatches = !highlightOnly || job.dataset.highlight === "1";
      const searchMatches = !searchTerm || normalizeText(job.dataset.search).includes(searchTerm);
      const show = sourceMatches && domainMatches && regionMatches && statusMatches &&
        highlightMatches && searchMatches;

      job.hidden = !show;
      if (show) visible++;
    });

    elements.todoCount.textContent = `(${todo})`;
    elements.appliedCount.textContent = `(${done})`;
    elements.visibleCount.textContent = `${visible} offre${visible > 1 ? "s" : ""} affichée${visible > 1 ? "s" : ""}`;

    if (visible === 0) {
      let message = searchTerm
        ? "Aucune offre ne correspond à cette recherche et à ces filtres."
        : "Aucune offre ne correspond à ces filtres.";
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
    activeRegion = CONFIG.defaultRegion || "*";
    activeStatus = "todo";
    highlightOnly = false;
    searchTerm = "";
    elements.searchInput.value = "";
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
  $$(".source-chip").forEach(button => button.addEventListener("click", () => setSource(button, button.dataset.src)));
  $$(".status-chip").forEach(button => button.addEventListener("click", () => setStatus(button, button.dataset.status)));
  $$(".domain-chip").forEach(button => button.addEventListener("click", () => setDomain(button, button.dataset.domain)));
  $$(".region-chip").forEach(button => button.addEventListener("click", () => setRegion(button, button.dataset.region)));
  elements.highlightFilter.addEventListener("click", event => toggleHighlight(event.currentTarget));
  elements.resetButton.addEventListener("click", resetFilters);
  $$(".apply-btn").forEach(button => button.addEventListener("click", () => toggleApplied(button.dataset.offerId)));

  refreshOffers();
})();
