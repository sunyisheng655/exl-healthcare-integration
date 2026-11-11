"use strict";

const byId = (id) => document.getElementById(id);
const make = (tag, text = "", className = "") => {
  const node = document.createElement(tag);
  if (text !== "") node.textContent = text;
  if (className) node.className = className;
  return node;
};
const state = {
  config: null, jobs: [], selected: null, job: null, timer: null, refreshing: null,
  detailRequest: null, codeCache: new Map(), finalCode: null, approvalBusy: false,
  queryBusy: false, generateBusy: false, reportStamp: null, eventsStamp: null, selectionEpoch: 0,
};
const statusLabels = {
  RUNNING: "Running", PASSED: "Passed", FAILED: "Failed", BLOCKED: "Blocked",
  NEEDS_CLARIFICATION: "Needs clarification", AWAITING_REPAIR_AUTHORIZATION: "Awaiting repair approval",
  NOT_PERFORMED: "Not run", PREVIEW_ONLY: "Preview only", STOPPED: "Stopped",
};
const badStatuses = new Set(["FAILED", "BLOCKED", "STOPPED"]);
const statusLabel = (status) => statusLabels[status] || status || "Unknown";
const isControlled = (job) => job.kind === "controlled_repair" ||
  (job.report?.attempts?.[0]?.plan || "").startsWith("Controlled repair test fixture");
const labelFor = (job) => isControlled(job) ? "Controlled repair demo" : "Model-generated job";
const jobPath = (id) => `/api/jobs/${encodeURIComponent(id)}`;
const pretty = (value) => value === undefined ? "Not provided" : JSON.stringify(value);
const timeLabel = (value) => {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? "" : new Intl.DateTimeFormat("en-IE", {
    day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  }).format(date);
};

function message(id, text = "", error = false) {
  const node = byId(id);
  if (node.textContent !== text) node.textContent = text;
  node.classList.toggle("error", error);
}

async function request(path, body, signal) {
  const controller = new AbortController();
  const cancel = () => controller.abort();
  if (signal?.aborted) cancel();
  signal?.addEventListener("abort", cancel, { once: true });
  const timer = window.setTimeout(cancel, 25000);
  try {
    const options = { signal: controller.signal, cache: "no-store" };
    if (body !== undefined) {
      options.method = "POST";
      options.headers = { "Content-Type": "application/json", "X-Local-Agent": "1" };
      options.body = JSON.stringify(body);
    }
    const response = await fetch(path, options);
    const data = await response.json().catch(() => null);
    if (!response.ok || !data || typeof data !== "object") {
      const info = data?.message ?? (typeof data?.error === "string" ? data.error : data?.error?.message);
      throw new Error(info || `The local server returned an invalid response (HTTP ${response.status}).`);
    }
    return data;
  } catch (error) {
    if (error.name === "AbortError" && !signal?.aborted) throw new Error("The request timed out. Refresh and try again.");
    if (error instanceof TypeError) throw new Error("Could not reach the local server. Make sure it is running, then refresh.");
    throw error;
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", cancel);
  }
}

function setStatus(node, status, approved = false) {
  node.textContent = approved ? "Approved" : statusLabel(status);
  node.className = `status-chip ${approved || status === "PASSED" ? "good" : badStatuses.has(status) ? "bad" : "neutral"}`;
}

function renderHistory() {
  const list = byId("job-list");
  const signature = JSON.stringify([state.selected, state.jobs.map((j) => [j.job_id, j.kind, j.status, j.approved, j.created_at])]);
  if (list.dataset.signature === signature) return;
  const focused = document.activeElement?.dataset?.jobId;
  list.dataset.signature = signature;
  list.replaceChildren();
  for (const job of state.jobs) {
    const row = make("button", "", "job-row");
    row.type = "button";
    row.dataset.jobId = job.job_id;
    if (job.job_id === state.selected) row.setAttribute("aria-current", "true");
    const title = make("span", "", "job-row-title");
    const chip = make("span");
    setStatus(chip, job.status, job.approved);
    title.append(make("span", labelFor(job)), chip);
    row.append(title, make("span", [job.job_id, timeLabel(job.created_at)].filter(Boolean).join(" · "), "job-row-meta"));
    row.addEventListener("click", () => selectJob(job.job_id));
    const li = make("li");
    li.append(row);
    list.append(li);
    if (focused === job.job_id) row.focus({ preventScroll: true });
  }
}

function schedulePoll() {
  clearTimeout(state.timer);
  if (state.job?.status === "RUNNING" || state.jobs.some((job) => job.status === "RUNNING")) {
    state.timer = window.setTimeout(() => refreshAll(), 1700);
  }
}

async function loadConfig() {
  try {
    state.config = await request("/api/config");
    byId("model-label").textContent = `OpenRouter · ${state.config.model || "Unknown model"}`;
    byId("example-button").disabled = !state.config.default_goal;
    byId("generate-button").disabled = state.generateBusy;
  } catch (error) {
    byId("model-label").textContent = "Model configuration unavailable";
    message("generate-message", error.message, true);
  }
}

function refreshAll({ chooseDefault = true } = {}) {
  if (state.refreshing) return state.refreshing;
  clearTimeout(state.timer);
  byId("refresh-button").disabled = true;
  state.refreshing = (async () => {
    try {
      const data = await request("/api/jobs");
      state.jobs = Array.isArray(data.jobs) ? data.jobs : [];
      message("history-message", state.jobs.length ? "" : "No local jobs yet.");
      if (chooseDefault && !state.selected && state.jobs.length) {
        const first = state.jobs.find((job) => job.approved) ?? state.jobs[0];
        await selectJob(first.job_id);
      } else {
        renderHistory();
        if (state.selected) await loadDetail(state.selected);
      }
    } catch (error) {
      message("history-message", error.message, true);
    } finally {
      byId("refresh-button").disabled = false;
      state.refreshing = null;
      schedulePoll();
    }
  })();
  return state.refreshing;
}

function selectJob(id) {
  if (id !== state.selected) {
    state.detailRequest?.controller.abort();
    state.selectionEpoch += 1;
    state.selected = id;
    state.job = null;
    state.finalCode = null;
    state.reportStamp = null;
    state.eventsStamp = null;
    state.approvalBusy = false;
    state.queryBusy = false;
    byId("job-detail").hidden = true;
    byId("empty-detail").hidden = false;
    byId("empty-detail").textContent = "Loading job…";
    byId("job-status").textContent = "Loading";
    byId("job-status").className = "status-chip neutral";
    byId("query-result").hidden = true;
    byId("query-result").replaceChildren();
    message("approval-message");
    message("query-message");
    renderHistory();
  }
  return loadDetail(id);
}

function loadDetail(id) {
  if (state.detailRequest?.id === id) return state.detailRequest.promise;
  const controller = new AbortController();
  const pending = { id, controller };
  state.detailRequest = pending;
  pending.promise = (async () => {
    try {
      const job = await request(jobPath(id), undefined, controller.signal);
      if (state.detailRequest !== pending || id !== state.selected) return;
      state.job = job;
      const index = state.jobs.findIndex((item) => item.job_id === id);
      if (index >= 0) state.jobs[index] = { ...state.jobs[index], status: job.status, approved: job.approved, kind: job.kind };
      renderHistory();
      renderJob(job);
    } catch (error) {
      if (controller.signal.aborted || state.selected !== id) return;
      if (state.job) message("job-message", error.message, true);
      else byId("empty-detail").textContent = error.message;
    } finally {
      if (state.detailRequest === pending) state.detailRequest = null;
      schedulePoll();
    }
  })();
  return pending.promise;
}

function renderJob(job) {
  byId("empty-detail").hidden = true;
  byId("job-detail").hidden = false;
  setStatus(byId("job-status"), job.status, job.approved);
  byId("run-kind").textContent = labelFor(job);
  byId("run-goal").textContent = job.goal || "No integration goal recorded.";
  const model = (job.report?.model_calls || []).find((call) => call.model && call.model !== "controlled-fixture")?.model;
  byId("run-meta").textContent = [job.job_id, model, timeLabel(job.created_at)].filter(Boolean).join(" · ");
  byId("controlled-note").hidden = !isControlled(job);
  const events = Array.isArray(job.events) ? job.events.filter((item) => typeof item === "string") : [];
  const reply = job.approved ? "This version is approved and ready for patient lookup." : job.status === "RUNNING" ?
    (events.at(-1) || "Preparing the integration job.") : job.status === "PASSED" ?
      "Development and holdout tests passed. Review the code before approving it." : job.report?.reason || statusLabel(job.status);
  byId("run-reply").textContent = reply;
  message("job-message", job.error || "", Boolean(job.error));
  const eventsStamp = JSON.stringify(events);
  if (eventsStamp !== state.eventsStamp) {
    state.eventsStamp = eventsStamp;
    byId("event-panel").hidden = events.length === 0;
    byId("event-list").replaceChildren(...events.map((event) => make("li", event)));
  }
  const reportStamp = job.report ? JSON.stringify(job.report) : null;
  if (reportStamp !== state.reportStamp) {
    state.reportStamp = reportStamp;
    state.finalCode = null;
    renderReport(job);
  } else if (!job.report) {
    byId("report-panel").hidden = true;
  }
  updateActions();
}

function metric(label, value, note) {
  const cell = make("div", "", "metric");
  cell.append(make("span", label, "metric-label"), make("div", value, "metric-value"), make("span", note, "metric-note"));
  return cell;
}

function renderReport(job) {
  const report = job.report;
  byId("report-panel").hidden = !report;
  if (!report) return;
  const attempts = Array.isArray(report.attempts) ? report.attempts : [];
  const final = attempts.at(-1);
  const dev = final?.development;
  const held = report.heldout;
  const tests = byId("test-summary");
  tests.replaceChildren(
    metric("Development tests", dev ? `${dev.passed} / ${dev.total}` : "—", dev ? statusLabel(dev.status) : "Not run"),
    metric("Holdout tests", held ? `${held.passed} / ${held.total}` : "—", held ? "Summary only" : statusLabel(report.heldout_status)),
    metric("Repair attempts", String(Math.max(0, attempts.length - 1)),
      Number.isFinite(report.elapsed_seconds) ? `${report.elapsed_seconds.toFixed(1)} s total` : "No time recorded"),
  );
  const list = byId("attempt-list");
  list.replaceChildren();
  if (!attempts.length) list.append(make("p", "No candidate code to review.", "muted"));
  for (const attempt of attempts) {
    list.append(renderAttempt(job.job_id, attempt, attempt === final || attempt.development?.status === "FAILED"));
  }
  if (passingFinal(job)) verifyFinal(job);
}

function passingFinal(job) {
  const final = job?.report?.attempts?.at(-1);
  return job?.status === "PASSED" && job.report.status === "PASSED" &&
    job.report.heldout_status === "PASSED" && final?.development?.status === "PASSED" &&
    /^candidate-0[1-4]\.py$/.test(final.candidate) && /^[a-f0-9]{64}$/.test(final.code_sha256) ? final : null;
}

async function codeFor(id, file, expectedHash) {
  const key = `${id}:${file}:${expectedHash}`;
  if (!state.codeCache.has(key)) {
    const promise = request(`${jobPath(id)}/code/${encodeURIComponent(file)}`).then((data) => {
      if (typeof data.code !== "string" || data.sha256 !== expectedHash || !/^[a-f0-9]{64}$/.test(data.sha256)) {
        throw new Error("The code hash does not match the report. Refresh and revalidate this version.");
      }
      return data;
    });
    state.codeCache.set(key, promise);
    promise.catch(() => state.codeCache.delete(key));
  }
  return state.codeCache.get(key);
}

async function verifyFinal(job) {
  const final = passingFinal(job);
  if (!final) return;
  const stamp = state.reportStamp;
  try {
    const code = await codeFor(job.job_id, final.candidate, final.code_sha256);
    if (state.selected !== job.job_id || stamp !== state.reportStamp) return;
    state.finalCode = code;
    updateActions();
  } catch (error) {
    if (state.selected !== job.job_id || stamp !== state.reportStamp) return;
    state.finalCode = null;
    updateActions();
    message("approval-message", error.message, true);
    if (job.approved) message("query-message", error.message, true);
  }
}

function renderAttempt(id, attempt, open) {
  const block = make("details", "", "attempt");
  block.open = open;
  const summary = make("summary");
  const title = make("span", "", "attempt-title");
  const suite = attempt.development || {};
  const chip = make("span");
  setStatus(chip, suite.status);
  title.append(make("span", `Attempt ${attempt.attempt} · ${attempt.candidate}`), chip);
  if (Number.isInteger(suite.passed) && Number.isInteger(suite.total)) {
    const count = make("span", `${suite.passed} / ${suite.total}`, "attempt-count");
    count.setAttribute("aria-label", `${suite.passed} of ${suite.total} tests passed`);
    title.append(count);
  }
  summary.append(title);
  const content = make("div", "", "attempt-body");
  const artifact = make("div", "", "artifact-line");
  artifact.append(make("span", "Code SHA-256"), make("code", attempt.code_sha256 || "Not recorded"));
  content.append(artifact);
  const cases = Array.isArray(suite.cases) ? suite.cases : [];
  if (cases.length) {
    const list = make("ul", "", "case-list");
    for (const test of cases) list.append(renderCase(test));
    content.append(make("p", `Ran ${suite.executed ?? 0} of ${suite.total ?? 0} · ${suite.passed ?? 0} passed · ${suite.failed ?? 0} failed`, "hint"), list);
  }
  if (attempt.plan) {
    const plan = make("details", "", "sub-detail");
    plan.append(make("summary", "View integration plan"), make("pre", attempt.plan, "plan-view"));
    content.append(plan);
  }
  const codeDetail = make("details", "", "sub-detail");
  const code = make("pre", "", "code-view");
  code.tabIndex = 0;
  codeDetail.append(make("summary", "View source code"), code);
  let loaded = false;
  codeDetail.addEventListener("toggle", async () => {
    if (!codeDetail.open || loaded) return;
    code.textContent = "Loading code…";
    try {
      const data = await codeFor(id, attempt.candidate, attempt.code_sha256);
      code.textContent = data.code;
      loaded = true;
    } catch (error) {
      code.textContent = error.message;
    }
  });
  content.append(codeDetail);
  block.append(summary, content);
  return block;
}

function renderCase(test) {
  const row = make("li");
  const title = make("span", "", "case-title");
  const chip = make("span");
  setStatus(chip, test.status);
  title.append(make("span", test.case_id || "Unnamed test"), chip);
  const diffs = Array.isArray(test.differences) ? test.differences : [];
  if (!diffs.length && !test.execution_error) {
    row.append(title);
    return row;
  }
  const detail = make("details");
  const summary = make("summary");
  summary.append(title);
  detail.open = test.case_id === "patient-P001";
  detail.append(summary);
  const list = make("ul", "", "diff-list");
  for (const diff of diffs) {
    const item = make("li", "", "diff");
    item.append(make("p", `${diff.path || "$"} · ${diff.code || "Difference"}`),
      make("p", `Expected: ${pretty(diff.expected)}`), make("p", `Actual: ${pretty(diff.actual)}`));
    list.append(item);
  }
  if (test.execution_error) list.append(make("li", `Execution error: ${typeof test.execution_error === "string" ? test.execution_error : pretty(test.execution_error)}`, "diff"));
  detail.append(list);
  row.append(detail);
  return row;
}

function updateActions() {
  const job = state.job;
  const final = passingFinal(job);
  const verified = Boolean(final && state.finalCode?.sha256 === final.code_sha256);
  byId("approval-panel").hidden = !final;
  byId("query-panel").hidden = !job || job.status === "RUNNING";
  byId("approve-button").disabled = !verified || job.approved || state.approvalBusy;
  byId("approve-button").textContent = job?.approved ? "Version approved" : state.approvalBusy ? "Approving…" : "Approve this version";
  if (final) {
    byId("approval-filename").textContent = final.candidate;
    byId("approval-hash").textContent = final.code_sha256;
    byId("approval-description").textContent = job.approved ? "Approval applies to this exact code version and its test report." :
      verified ? "Both test suites passed. Review the code and report before approving this version." : "Checking the final candidate’s code hash against the test report.";
  }
  const canQuery = verified && job.approved && !state.queryBusy;
  byId("query-jump").hidden = !job?.approved;
  byId("patient-id").disabled = !canQuery;
  byId("query-button").disabled = !canQuery;
  byId("query-button").textContent = state.queryBusy ? "Looking up…" : "Look up patient";
  byId("query-description").textContent = job?.approved ? "The approved code reads all three mock sources directly." : "Approve a passing version to enable patient lookup.";
}

function renderTable(title, source, rows, columns) {
  const section = make("section", "", "result-block");
  const heading = make("h3", `${title} (${rows.length})`);
  if (source) heading.append(make("span", source, "source-caption"));
  section.append(heading);
  if (rows.length === 0) {
    section.append(make("p", "No records.", "hint"));
    return section;
  }
  const wrap = make("div", "", "table-wrap");
  wrap.tabIndex = 0;
  wrap.setAttribute("role", "region");
  wrap.setAttribute("aria-label", title);
  const table = make("table");
  const head = make("thead");
  const headingRow = make("tr");
  for (const [, label] of columns) {
    const th = make("th", label);
    th.scope = "col";
    headingRow.append(th);
  }
  head.append(headingRow);
  const body = make("tbody");
  for (const row of rows) {
    const tr = make("tr");
    for (const [key] of columns) tr.append(make("td", row[key] == null ? "—" : String(row[key])));
    body.append(tr);
  }
  table.append(head, body);
  wrap.append(table);
  section.append(wrap);
  return section;
}

function renderQuery(result) {
  const panel = byId("query-result");
  panel.replaceChildren();
  panel.hidden = false;
  const patient = result.patient;
  const provenance = result.provenance || {};
  const title = make("h3", result.found ? `${result.patient_id} · ${patient?.display_name || "Synthetic patient"}` : `${result.patient_id} · Patient not found`, "patient-title");
  panel.append(title);
  if (!result.found || !patient) {
    panel.append(make("p", "The registration system has no record for this synthetic patient ID.", "hint"));
    return;
  }
  if (provenance.patient) panel.append(make("p", `Registration source · REST · ${provenance.patient}`, "hint"));
  const metadata = make("dl", "", "patient-details");
  for (const [name, key] of [["Date of birth", "birth_date"], ["Gender", "gender"], ["Middle name", "middle_name"], ["Status", "active"], ["Last updated", "updated_at"]]) {
    const field = make("div");
    const value = key === "active" && typeof patient[key] === "boolean" ? (patient[key] ? "Active" : "Inactive") : patient[key];
    field.append(make("dt", name), make("dd", value == null ? "—" : String(value)));
    metadata.append(field);
  }
  panel.append(metadata);
  panel.append(renderTable("Appointments", provenance.appointments && `SQLite · ${provenance.appointments}`, result.appointments || [], [
    ["appointment_id", "ID"], ["scheduled_at", "Scheduled for"], ["department_code", "Department"], ["visit_type", "Visit type"], ["status", "Status"],
  ]));
  panel.append(renderTable("Observations", provenance.observations && `CSV · ${provenance.observations}`, result.observations || [], [
    ["observation_id", "ID"], ["test_code", "Test code"], ["value", "Value"], ["unit", "Unit"], ["observed_at", "Observed at"], ["status", "Status"],
  ]));
}

byId("example-button").addEventListener("click", () => {
  if (!state.config?.default_goal) return;
  byId("goal-input").value = state.config.default_goal;
  byId("goal-input").focus();
});

byId("refresh-button").addEventListener("click", async () => {
  if (!state.config) await loadConfig();
  if (state.selected && !state.finalCode && passingFinal(state.job)) verifyFinal(state.job);
  await refreshAll();
});

byId("generate-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.config || state.generateBusy) return;
  const goal = byId("goal-input").value.trim();
  if (!goal) {
    message("generate-message", "Enter an integration goal.", true);
    byId("goal-input").focus();
    return;
  }
  state.generateBusy = true;
  const button = byId("generate-button");
  button.disabled = true;
  button.textContent = "Submitting…";
  message("generate-message", "Creating the job…");
  try {
    const data = await request("/api/jobs", { goal, allow_auto_repair: byId("auto-repair").checked });
    if (typeof data.job_id !== "string") throw new Error("The server did not return a job ID. Refresh the job history.");
    byId("auto-repair").checked = false;
    message("generate-message", "Job submitted. Follow its progress in Validation & Queries.");
    state.jobs = [{ job_id: data.job_id, status: "RUNNING", kind: "live_generation", approved: false }, ...state.jobs.filter((job) => job.job_id !== data.job_id)];
    await selectJob(data.job_id);
    await refreshAll({ chooseDefault: false });
    byId("evidence-title").focus({ preventScroll: true });
  } catch (error) {
    message("generate-message", error.message, true);
    await refreshAll({ chooseDefault: false });
  } finally {
    state.generateBusy = false;
    button.disabled = false;
    button.textContent = "Generate integration code";
  }
});

byId("approve-button").addEventListener("click", async () => {
  const job = state.job;
  const final = passingFinal(job);
  if (!final || job.approved || state.approvalBusy || state.finalCode?.sha256 !== final.code_sha256) return;
  const id = job.job_id;
  const epoch = state.selectionEpoch;
  state.approvalBusy = true;
  message("approval-message", "Approving the displayed version…");
  updateActions();
  try {
    const data = await request(`${jobPath(id)}/approve`, { code_sha256: final.code_sha256 });
    if (data.approved !== true) throw new Error("The server did not confirm approval.");
    if (state.selectionEpoch !== epoch) return;
    await loadDetail(id);
    if (state.selectionEpoch === epoch) message("approval-message", state.job?.approved ? "This code version is approved." : "Approval submitted. Refresh to confirm the version status.");
  } catch (error) {
    if (state.selectionEpoch === epoch) message("approval-message", error.message, true);
  } finally {
    if (state.selectionEpoch === epoch) {
      state.approvalBusy = false;
      updateActions();
    }
  }
});

byId("query-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const job = state.job;
  const final = passingFinal(job);
  if (!job?.approved || !final || state.queryBusy || state.finalCode?.sha256 !== final.code_sha256) return;
  const id = job.job_id;
  const epoch = state.selectionEpoch;
  const patientId = byId("patient-id").value.trim();
  if (!patientId) {
    message("query-message", "Enter a synthetic patient ID.", true);
    return;
  }
  state.queryBusy = true;
  byId("query-result").hidden = true;
  message("query-message", "Running the approved code…");
  updateActions();
  try {
    const data = await request(`${jobPath(id)}/query`, { patient_id: patientId });
    if (state.selectionEpoch !== epoch) return;
    if (data.job_id !== id || data.code_sha256 !== final.code_sha256 || !data.result || data.result.patient_id !== patientId) {
      throw new Error("The result does not match the selected version or patient ID.");
    }
    renderQuery(data.result);
    message("query-message", data.result.found ? "Lookup complete." : "Lookup complete. Patient not found.");
  } catch (error) {
    if (state.selectionEpoch === epoch) message("query-message", error.message, true);
  } finally {
    if (state.selectionEpoch === epoch) {
      state.queryBusy = false;
      updateActions();
    }
  }
});

loadConfig();
refreshAll();
