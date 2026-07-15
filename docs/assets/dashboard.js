(() => {
  "use strict";

  const configNode = document.getElementById("radarConfig");
  const CONFIG = configNode ? JSON.parse(configNode.textContent) : {};
  const TOKEN_STORAGE_KEY = "radar-stage-france-github-token-v1";
  const APPLIED_STORAGE_KEY = "radar-stage-france-applied-v1";
  const API_VERSION = "2026-03-10";
  const STATUS_LABELS = {todo: "À postuler", applied: "Déjà postulé", all: "Tous"};

  let activeSource = "*";
  let activeDomain = "*";
  let activeRegion = CONFIG.defaultRegion || "france";
  let activeStatus = "todo";
  let highlightOnly = false;
  let searchTerm = "";
  let activeRunId = null;
  let scanBusy = false;
  let toastTimer = null;

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const delay = milliseconds => new Promise(resolve => window.setTimeout(resolve, milliseconds));

  const elements = {
    refreshButton: $("#refreshBtn"),
    scanButton: $("#scanTriggerBtn"),
    searchInput: $("#searchInput"),
    visibleCount: $("#visibleCount"),
    todoCount: $("#todoCount"),
    appliedCount: $("#appliedCount"),
    activeFilters: $("#activeFilters"),
    resetButton: $("#resetBtn"),
    highlightFilter: $("#highlightFilter"),
    emptyState: $("#emptyState"),
    dialog: $("#scanDialog"),
    dialogClose: $("#scanDialogClose"),
    tokenInput: $("#githubToken"),
    toggleToken: $("#toggleTokenVisibility"),
    tokenCreationLink: $("#tokenCreationLink"),
    tokenRepositoryName: $("#tokenRepositoryName"),
    runNowButton: $("#runNowBtn"),
    forgetTokenButton: $("#forgetTokenBtn"),
    runState: $("#runState"),
    runStateTitle: $("#runStateTitle"),
    runStateCopy: $("#runStateCopy"),
    runDetailsLink: $("#runDetailsLink"),
    toast: $("#toast")
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
      activeDomain === "*" && activeRegion === CONFIG.defaultRegion &&
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
    activeRegion = CONFIG.defaultRegion || "france";
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

  function repositoryCoordinates() {
    const host = window.location.hostname;
    const path = window.location.pathname.split("/").filter(Boolean);
    if (host.endsWith(".github.io") && path.length) {
      return {owner: host.slice(0, -".github.io".length), repo: path[0]};
    }
    if (CONFIG.repository && CONFIG.repository.includes("/")) {
      const [owner, repo] = CONFIG.repository.split("/", 2);
      return {owner, repo};
    }
    return null;
  }

  const repository = repositoryCoordinates();

  function readToken() {
    try {
      return sessionStorage.getItem(TOKEN_STORAGE_KEY) || "";
    } catch (_error) {
      return "";
    }
  }

  function storeToken(token) {
    sessionStorage.setItem(TOKEN_STORAGE_KEY, token);
  }

  function forgetToken() {
    sessionStorage.removeItem(TOKEN_STORAGE_KEY);
    elements.tokenInput.value = "";
    elements.forgetTokenButton.hidden = true;
    setRunState("idle", "Accès GitHub oublié", "Le token a été supprimé de cet onglet.");
    showToast("Accès GitHub supprimé de cette session.");
  }

  function apiUrl(path) {
    if (!repository) throw new Error("Dépôt GitHub introuvable depuis cette page.");
    return `https://api.github.com/repos/${encodeURIComponent(repository.owner)}/${encodeURIComponent(repository.repo)}${path}`;
  }

  async function githubRequest(path, token, options = {}) {
    const response = await fetch(apiUrl(path), {
      ...options,
      headers: {
        Accept: "application/vnd.github+json",
        Authorization: `Bearer ${token}`,
        "X-GitHub-Api-Version": API_VERSION,
        ...(options.headers || {})
      }
    });

    let payload = null;
    if (response.status !== 204) {
      const contentType = response.headers.get("content-type") || "";
      payload = contentType.includes("application/json") ? await response.json() : await response.text();
    }
    if (!response.ok) {
      const error = new Error(payload && payload.message ? payload.message : `Erreur GitHub ${response.status}`);
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  function errorMessage(error) {
    if (error.status === 401) return "Token refusé ou expiré. Créez un nouveau token puis réessayez.";
    if (error.status === 403) return "Permission insuffisante. Le token doit avoir Actions : Read and write sur ce dépôt.";
    if (error.status === 404) return "Workflow introuvable ou dépôt non autorisé pour ce token.";
    if (error.status === 422) return "Le workflow ne peut pas être lancé actuellement sur la branche main.";
    return `Impossible de contacter GitHub : ${error.message || "erreur réseau"}.`;
  }

  function setRunState(kind, title, copy, runUrl = "") {
    elements.runState.hidden = false;
    elements.runState.dataset.kind = kind;
    elements.runStateTitle.textContent = title;
    elements.runStateCopy.textContent = copy;
    elements.runDetailsLink.hidden = !runUrl;
    if (runUrl) elements.runDetailsLink.href = runUrl;
  }

  function setScanButtonState(kind, label) {
    const icon = $(".button-icon", elements.scanButton);
    const text = $(".button-label", elements.scanButton);
    elements.scanButton.classList.toggle("is-loading", kind === "running");
    elements.scanButton.disabled = kind === "running";
    icon.textContent = kind === "success" ? "✓" : (kind === "error" ? "!" : "↻");
    text.textContent = label;
  }

  function showToast(message) {
    elements.toast.textContent = message;
    elements.toast.classList.add("show");
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => elements.toast.classList.remove("show"), 4200);
  }

  function openDialog() {
    const token = readToken();
    elements.tokenInput.value = token;
    elements.forgetTokenButton.hidden = !token;
    if (!elements.dialog.open) elements.dialog.showModal();
    if (!token) window.setTimeout(() => elements.tokenInput.focus(), 60);
  }

  async function latestActiveRun(token) {
    const workflow = encodeURIComponent(CONFIG.workflowFile || "scan.yml");
    const payload = await githubRequest(
      `/actions/workflows/${workflow}/runs?branch=${encodeURIComponent(CONFIG.workflowRef || "main")}&per_page=10`,
      token
    );
    return (payload.workflow_runs || []).find(run => run.status === "queued" || run.status === "in_progress") || null;
  }

  async function latestDispatchedRun(token, startedAt) {
    const workflow = encodeURIComponent(CONFIG.workflowFile || "scan.yml");
    const threshold = startedAt - 60000;
    for (let attempt = 0; attempt < 6; attempt++) {
      if (attempt > 0) await delay(2000);
      const payload = await githubRequest(
        `/actions/workflows/${workflow}/runs?event=workflow_dispatch&branch=${encodeURIComponent(CONFIG.workflowRef || "main")}&per_page=5`,
        token
      );
      const run = (payload.workflow_runs || []).find(
        candidate => Date.parse(candidate.created_at) >= threshold
      );
      if (run) return run;
    }
    return null;
  }

  async function dispatchWorkflow(token) {
    const workflow = encodeURIComponent(CONFIG.workflowFile || "scan.yml");
    return githubRequest(`/actions/workflows/${workflow}/dispatches`, token, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ref: CONFIG.workflowRef || "main"})
    });
  }

  async function waitForDashboardPublication() {
    setRunState(
      "running",
      "Publication du dashboard…",
      "Le scan est terminé. GitHub Pages prépare les nouvelles offres.",
      elements.runDetailsLink.href
    );
    setScanButtonState("running", "Publication…");

    for (let attempt = 0; attempt < 24; attempt++) {
      await delay(10000);
      try {
        const url = new URL(window.location.href);
        url.searchParams.set("radar_refresh", Date.now().toString());
        const response = await fetch(url, {cache: "no-store"});
        if (!response.ok) continue;
        const page = new DOMParser().parseFromString(await response.text(), "text/html");
        const generatedAt = $("meta[name='radar-generated-at']", page)?.content;
        if (generatedAt && generatedAt !== CONFIG.pageGeneratedAt) {
          setRunState("success", "Dashboard actualisé", "Les nouvelles offres sont disponibles.");
          setScanButtonState("success", "Dashboard actualisé");
          window.setTimeout(() => window.location.reload(), 900);
          return;
        }
      } catch (_error) {
        // Le déploiement Pages peut être momentanément indisponible : on réessaie.
      }
    }

    scanBusy = false;
    setRunState("success", "Scan terminé", "La publication prend plus de temps que prévu. Utilisez Actualiser dans quelques instants.");
    setScanButtonState("success", "Scan terminé");
  }

  async function monitorRun(runId, token) {
    activeRunId = runId;
    while (activeRunId === runId) {
      try {
        const run = await githubRequest(`/actions/runs/${runId}`, token);
        const runUrl = run.html_url || "";
        if (run.status === "queued") {
          setRunState("running", "Scan en file d’attente", "GitHub prépare le runner.", runUrl);
          setScanButtonState("running", "En attente…");
        } else if (run.status === "in_progress") {
          setRunState("running", "Scan en cours", "Les offres sont collectées sur les sources et régions actives.", runUrl);
          setScanButtonState("running", "Scan en cours…");
        } else if (run.status === "completed") {
          activeRunId = null;
          if (run.conclusion === "success") {
            await waitForDashboardPublication();
          } else {
            scanBusy = false;
            setRunState("error", "Le scan a échoué", `Conclusion GitHub : ${run.conclusion || "inconnue"}.`, runUrl);
            setScanButtonState("error", "Relancer le scan");
          }
          return;
        }
      } catch (error) {
        activeRunId = null;
        scanBusy = false;
        setRunState("error", "Suivi interrompu", errorMessage(error));
        setScanButtonState("error", "Réessayer");
        return;
      }
      await delay(5000);
    }
  }

  async function triggerScan() {
    if (scanBusy) return;
    const token = elements.tokenInput.value.trim() || readToken();
    if (!token) {
      setRunState("error", "Token requis", "Ajoutez un token finement limité pour autoriser le lancement.");
      elements.tokenInput.focus();
      return;
    }

    storeToken(token);
    elements.tokenInput.value = token;
    elements.forgetTokenButton.hidden = false;
    scanBusy = true;
    elements.runNowButton.disabled = true;
    setRunState("running", "Connexion à GitHub…", "Vérification du workflow et des exécutions en cours.");
    setScanButtonState("running", "Connexion…");

    try {
      let run = await latestActiveRun(token);
      if (run) {
        showToast("Un scan est déjà en cours : suivi de cette exécution.");
      } else {
        const startedAt = Date.now();
        const response = await dispatchWorkflow(token);
        if (response && response.workflow_run_id) {
          run = {
            id: response.workflow_run_id,
            html_url: response.html_url || ""
          };
        } else {
          run = await latestDispatchedRun(token, startedAt);
        }
      }

      if (!run || !run.id) throw new Error("Le run a été accepté mais son identifiant n’est pas encore disponible.");
      setRunState("running", "Scan lancé", "Le suivi est automatique jusqu’à la publication du dashboard.", run.html_url || "");
      showToast("Scan lancé avec succès depuis le dashboard.");
      await monitorRun(run.id, token);
    } catch (error) {
      scanBusy = false;
      if (error.status === 401) {
        sessionStorage.removeItem(TOKEN_STORAGE_KEY);
        elements.forgetTokenButton.hidden = true;
      }
      setRunState("error", "Lancement impossible", errorMessage(error));
      setScanButtonState("error", "Réessayer");
    } finally {
      elements.runNowButton.disabled = scanBusy;
    }
  }

  function configureGitHubControls() {
    if (!repository) {
      elements.scanButton.disabled = true;
      elements.scanButton.title = "Le dépôt GitHub ne peut pas être déterminé sur cette URL.";
      return;
    }
    const fullName = `${repository.owner}/${repository.repo}`;
    elements.tokenRepositoryName.textContent = fullName;
    const tokenUrl = new URL("https://github.com/settings/personal-access-tokens/new");
    tokenUrl.searchParams.set("name", "Radar Stage France");
    tokenUrl.searchParams.set("description", `Déclenche uniquement le workflow de ${fullName}`);
    tokenUrl.searchParams.set("target_name", repository.owner);
    tokenUrl.searchParams.set("expires_in", "90");
    tokenUrl.searchParams.set("actions", "write");
    elements.tokenCreationLink.href = tokenUrl.toString();
    elements.runDetailsLink.href = `https://github.com/${fullName}/actions/workflows/${CONFIG.workflowFile || "scan.yml"}`;
  }

  elements.refreshButton.addEventListener("click", () => window.location.reload());
  elements.scanButton.addEventListener("click", async () => {
    openDialog();
    if (readToken() && !scanBusy) await triggerScan();
  });
  elements.dialogClose.addEventListener("click", () => elements.dialog.close());
  elements.dialog.addEventListener("click", event => {
    if (event.target === elements.dialog) elements.dialog.close();
  });
  elements.toggleToken.addEventListener("click", () => {
    const visible = elements.tokenInput.type === "text";
    elements.tokenInput.type = visible ? "password" : "text";
    elements.toggleToken.textContent = visible ? "Afficher" : "Masquer";
  });
  elements.runNowButton.addEventListener("click", triggerScan);
  elements.forgetTokenButton.addEventListener("click", forgetToken);
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

  configureGitHubControls();
  refreshOffers();
})();
