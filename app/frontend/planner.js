const $ = (id) => document.getElementById(id);
const state = { courses: [], selected: new Map(), session: null, contextId: null, context: null,
  evidence: null, generations: [], revisions: {}, evidenceByGeneration: {}, active: null,
  draft: null, dirty: true, editDirty: false, busy: false };
const value = (id) => $(id).value.trim();
const lines = (id) => value(id).split("\n").map((s) => s.trim()).filter(Boolean);
const names = ["classroom_community_profile", "discipline_target_standards", "community_assets_cultural_wealth",
  "research_grounding", "topic_subject", "teacher_raw_intentions", "seed_lesson", "classroom_resource_constraints",
  "time_budget", "output_deliverable_scope", "assessment_preference"];
const draftKeys = ["title", "objectives", "alignment", "materials", "sequence", "access_options", "assessment",
  "community_use", "assumptions", "citations"];

function node(tag, text, className) {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = text;
  if (className) el.className = className;
  return el;
}
function status(text, error = false) { $("planning-status").textContent = text; $("planning-status").dataset.error = error; }
async function api(path, options = {}) {
  const response = await fetch(`/api/planning${path}`, { ...options, headers: {"Content-Type": "application/json"} });
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Some fields are invalid. Check the form and try again.");
  return data;
}
async function work(fn) {
  if (state.busy) return;
  state.busy = true;
  const controls = document.querySelectorAll("button, input, textarea, select");
  const previouslyDisabled = new Set([...controls].filter((el) => el.disabled));
  controls.forEach((el) => { el.disabled = true; });
  try { await fn(); } catch (error) { status(error.message, true); }
  finally { controls.forEach((el) => { if (el.isConnected) el.disabled = previouslyDisabled.has(el); }); state.busy = false; }
}
function markDirty() { state.dirty = true; status("Planning context changed. Save it or generate a new draft."); }
function course() { return state.courses.find((c) => c.course_key === value("course")); }
function fillCourses(selected = null) {
  $("course").replaceChildren(new Option("Select a course", ""));
  for (const c of state.courses.filter((c) => c.subject === value("subject"))) {
    const option = new Option(`${c.course} · ${c.standards_version}${c.served ? "" : " · unavailable"}`, c.course_key);
    option.disabled = !c.served;
    $("course").append(option);
  }
  if (selected) $("course").value = selected;
}
function selectedView() {
  $("selected-standards").replaceChildren();
  for (const [key, record] of state.selected) {
    const remove = node("button", `${record.printed_code} ×`);
    remove.type = "button";
    remove.setAttribute("aria-label", `Remove standard ${record.printed_code}`);
    remove.onclick = () => { state.selected.delete(key); selectedView(); markDirty(); };
    $("selected-standards").append(remove);
  }
  document.querySelectorAll("input[data-standard]").forEach((el) => { el.checked = state.selected.has(el.dataset.standard); });
}
function sourceLink(url, label) {
  try {
    const parsed = new URL(url);
    if (!["http:", "https:"].includes(parsed.protocol)) return node("span", label);
    const link = node("a", label); link.href = parsed.href; link.target = "_blank"; link.rel = "noopener noreferrer"; return link;
  } catch { return node("span", label); }
}
async function search() {
  if (!value("course")) { $("standards").replaceChildren(node("p", "Select a course first.", "small")); return; }
  const records = await api(`/standards?course_key=${encodeURIComponent(value("course"))}&q=${encodeURIComponent(value("standard-search"))}&limit=100`);
  $("standards").replaceChildren();
  for (const record of records) {
    const item = node("div", undefined, "standard-option");
    const label = node("label"); const check = node("input"); check.type = "checkbox"; check.dataset.standard = record.standard_key;
    check.checked = state.selected.has(record.standard_key);
    check.onchange = () => {
      if (check.checked && state.selected.size >= 3) { check.checked = false; status("Select at most three standards.", true); return; }
      if (check.checked) state.selected.set(record.standard_key, record); else state.selected.delete(record.standard_key);
      selectedView(); markDirty();
    };
    label.append(check, node("span", `${record.printed_code} · ${record.domain || record.grade_or_course}`));
    item.append(label, node("p", record.text), sourceLink(record.source_url, `Official source · page ${record.page_number}`));
    $("standards").append(item);
  }
  if (!records.length) $("standards").append(node("p", "No matching standards in this course.", "small"));
}

function collectContext() {
  const origin = value("case-origin") === "synthetic" ? "synthetic_scenario" : "developer_authored";
  const wrap = (data, known = true, fieldOrigin = origin) => ({ value: known ? data : null, status: known ? "known" : "unknown",
    origin: known ? fieldOrigin : null, teacher_confirmed: false, source_refs: [] });
  const context = structuredClone(state.context || {});
  for (const name of names) context[name] ??= wrap(null, false);
  // Keep unexposed subfields from a loaded context while editing visible fields.
  const set = (name, data, known = true, fieldOrigin = origin) => {
    const previous = context[name];
    const next = { ...(previous.value || {}), ...data };
    const unchanged = state.context?.case_origin === value("case-origin") &&
      ((known && previous.status === "known" && JSON.stringify(next) === JSON.stringify(previous.value)) ||
       (!known && previous.status === "withheld"));
    context[name] = unchanged ? previous : wrap(next, known, fieldOrigin);
  };
  context.case_origin = value("case-origin");
  set("classroom_community_profile", { grades: value("grade") ? [Number(value("grade"))] : [], course: course()?.course || null,
    prior_knowledge: value("prior-knowledge") || null, access_needs: lines("access-needs"),
    languages: value("languages") ? value("languages").split(",").map((s) => s.trim()).filter(Boolean) : null });
  set("discipline_target_standards", { subject: value("subject"), requested_standard_keys: [...state.selected.keys()],
    confirmed_standard_keys: [...state.selected.keys()] });
  set("community_assets_cultural_wealth", { contributor_description: value("asset"), asset_refs: [] }, Boolean(value("asset")), value("asset-origin"));
  context.community_assets_cultural_wealth.source_refs = value("asset-source") ? [value("asset-source")] : [];
  set("research_grounding", { teacher_preferences: lines("research-preferences") });
  set("topic_subject", { topic: value("topic"), subject: value("subject") });
  set("teacher_raw_intentions", { raw_text: value("intention") });
  set("seed_lesson", { source_id: value("seed-source") || "user-seed", permitted_excerpts: lines("seed"), rights: value("seed-source") || null,
    training_use: "unknown", training_permission_basis: null }, Boolean(value("seed")));
  set("classroom_resource_constraints", { available: lines("available"), unavailable: lines("unavailable"), hard_constraints: lines("constraints") },
    Boolean(value("available") || value("unavailable") || value("constraints")));
  set("time_budget", { sessions: value("session-count") ? Number(value("session-count")) : null,
    minutes_per_session: value("minutes") ? Number(value("minutes")) : null }, Boolean(value("session-count") || value("minutes")));
  set("output_deliverable_scope", { type: value("scope"), include: ["assessment"] });
  set("assessment_preference", { purpose: value("assessment-purpose"), modes: lines("assessment-modes"), evidence_sought: value("assessment-evidence") || null });
  return context;
}
function restoreContext(context) {
  state.context = context;
  const get = (key) => context[key]?.status === "known" ? context[key].value || {} : {};
  const put = (id, text) => { $(id).value = Array.isArray(text) ? text.join("\n") : text ?? ""; };
  put("case-origin", context.case_origin === "synthetic" ? "synthetic" : "developer_authored");
  put("subject", get("discipline_target_standards").subject || "Mathematics");
  const key = get("discipline_target_standards").requested_standard_keys?.[0];
  fillCourses(key ? key.slice(0, key.lastIndexOf("|")) : null);
  const profile = get("classroom_community_profile");
  put("grade", profile.grades?.[0]); put("prior-knowledge", profile.prior_knowledge); put("access-needs", profile.access_needs);
  put("languages", profile.languages?.join(", "));
  put("topic", get("topic_subject").topic); put("intention", get("teacher_raw_intentions").raw_text);
  put("minutes", get("time_budget").minutes_per_session); put("session-count", get("time_budget").sessions);
  put("scope", get("output_deliverable_scope").type || "single_lesson");
  put("asset", get("community_assets_cultural_wealth").contributor_description);
  put("asset-origin", context.community_assets_cultural_wealth?.origin || "synthetic_scenario");
  put("asset-source", context.community_assets_cultural_wealth?.source_refs?.join("; "));
  put("research-preferences", get("research_grounding").teacher_preferences);
  put("seed", get("seed_lesson").permitted_excerpts); put("seed-source", get("seed_lesson").rights);
  const resources = get("classroom_resource_constraints");
  put("available", resources.available); put("unavailable", resources.unavailable); put("constraints", resources.hard_constraints);
  const assessment = get("assessment_preference");
  put("assessment-purpose", assessment.purpose || "formative"); put("assessment-modes", assessment.modes); put("assessment-evidence", assessment.evidence_sought);
  state.selected = new Map((state.evidence?.bundle.standards || []).map((r) => [r.standard_key, r]));
  selectedView(); state.dirty = false;
}
async function loadSessions() {
  const sessions = await api("/sessions");
  $("sessions").replaceChildren(new Option("Start a new lesson", ""));
  for (const row of sessions) $("sessions").append(new Option(row.title || "Lesson", row.session_id));
  $("sessions").value = state.session || "";
}
async function saveContext() {
  if (!$("planning-form").reportValidity()) throw new Error("Complete the required fields.");
  if (!value("topic") || !value("intention")) throw new Error("Enter the topic and planning intention.");
  if (!state.selected.size) throw new Error("Select at least one standard.");
  if (!state.session) {
    const result = await api("/sessions", {method: "POST", body: JSON.stringify({title: value("topic").slice(0, 120) || "Lesson"})});
    state.session = result.session_id; localStorage.setItem("praxis.session", state.session); await loadSessions();
  }
  const saved = await api(`/sessions/${state.session}/contexts`, {method: "POST", body: JSON.stringify({context: collectContext()})});
  state.contextId = saved.context_snapshot.event_id; state.context = saved.context_snapshot.context; state.evidence = saved.evidence_snapshot;
  state.dirty = false; status("Planning context and source evidence saved."); showEvidence(state.evidence);
}
function showEvidence(snapshot) {
  $("evidence").replaceChildren(); $("evidence-panel").hidden = !snapshot;
  if (!snapshot) return;
  for (const r of snapshot.bundle.standards) {
    $("evidence").append(node("h4", `${r.printed_code} · ${r.grade_or_course} · ${r.standards_version}`), node("p", r.text), sourceLink(r.source_url, `Official source · page ${r.page_number}`));
  }
  for (const r of snapshot.bundle.research) $("evidence").append(node("p", `${r.framework}: ${r.claim}`), sourceLink(r.citation, "Framework source"));
  for (const r of snapshot.bundle.community) $("evidence").append(node("p", `Asset origin: ${r.asset_origin}. Community validation: ${r.community_validation_status}.`), node("p", r.contributor_description));
}
function showValidation(report) {
  $("validation").replaceChildren(); $("validation").dataset.failed = report?.findings.some((f) => f.severity === "error") || false;
  if (!report) return;
  $("validation").append(node("p", `Source verification: ${report.source_verification_status}. Pedagogical alignment: ${report.alignment_status}.`));
  if (!report.findings.length) $("validation").append(node("p", "Automated checks passed. Teaching quality still needs review."));
  for (const finding of report.findings) $("validation").append(node("p", `${finding.severity}: ${finding.message}`));
}
const titleCase = (s) => s.replaceAll("_", " ").replace(/^./, (c) => c.toUpperCase());
function renderDraft(plan) {
  $("lesson").replaceChildren(); state.draft = Object.fromEntries(draftKeys.filter((k) => k in plan).map((k) => [k, structuredClone(plan[k])]));
  let counter = 0;
  function field(parent, object, key, label) {
    const val = object[key];
    if (val === null || val === undefined) return;
    if (typeof val === "object") {
      if (Array.isArray(val) && !val.length) return;
      const group = node("div", undefined, "lesson-item"); parent.append(group);
      if (Array.isArray(val)) val.forEach((_, index) => field(group, val, index, `${label} ${index + 1}`));
      else Object.keys(val).forEach((child) => field(group, val, child, titleCase(child)));
      return;
    }
    if (["standard_key", "evidence_ref"].includes(key) || label.startsWith("Citations")) { parent.append(node("p", `${label}: ${val}`, "readonly")); return; }
    const id = `lesson-field-${counter++}`; const lab = node("label", label); lab.htmlFor = id;
    let input;
    if (key === "coverage" || key === "purpose") {
      input = node("select"); const values = key === "coverage" ? ["full", "partial", "not_addressed"] : ["formative", "summative", "both"];
      values.forEach((v) => input.append(new Option(v, v))); input.value = val;
    } else if (typeof val === "number") { input = node("input"); input.type = "number"; input.min = "1"; input.value = val; }
    else { input = node("textarea"); input.rows = Math.max(1, Math.ceil(String(val).length / 80)); input.value = val; }
    input.id = id;
    input.oninput = () => { object[key] = typeof val === "number" ? Number(input.value) : input.value; state.editDirty = true;
      $("validation").replaceChildren(node("p", "Draft edited. Save a revision to rerun automated checks.")); status("Lesson wording changed. Save a revision to keep the edits."); };
    parent.append(lab, input);
  }
  for (const key of draftKeys) {
    if (!(key in state.draft) || state.draft[key] === null || (Array.isArray(state.draft[key]) && !state.draft[key].length)) continue;
    const group = node("section", undefined, "lesson-group"); group.append(node("h3", titleCase(key)));
    field(group, state.draft, key, titleCase(key)); $("lesson").append(group);
  }
  $("edit-actions").hidden = false; state.editDirty = false;
  requestAnimationFrame(sizeEditors);
}
function showGeneration(id) {
  const generation = state.generations.find((g) => g.event_id === id);
  state.active = generation || null; $("empty-draft").hidden = Boolean(generation); $("edit-actions").hidden = true; $("lesson").replaceChildren();
  if (!generation) { showValidation(null); return; }
  const revisions = state.revisions[id] || [];
  const plan = revisions.at(-1)?.edited_plan || generation.plan;
  showValidation(plan?.validation || generation.validation);
  if (plan) renderDraft(plan);
  else { const details = node("details"); details.append(node("summary", "Saved model response"), node("pre", generation.output_raw || "No response content was returned.")); $("lesson").append(details); }
  showEvidence(state.evidenceByGeneration[id] || state.evidence);
  if (generation.context_snapshot_id !== state.contextId) status("This draft uses an earlier saved context. Generate again to use the current context.");
}
function history(selected) {
  $("history").replaceChildren(new Option("Select a generation", ""));
  state.generations.forEach((g, i) => $("history").append(new Option(`Draft ${i + 1} · ${g.model}`, g.event_id)));
  $("history").value = selected || ""; showGeneration(selected);
}
async function loadSession(id) {
  if (!id) { reset(); return; }
  const data = await api(`/sessions/${id}`);
  state.session = id; localStorage.setItem("praxis.session", id);
  state.contextId = data.context_snapshot?.event_id || null; state.evidence = data.evidence_snapshot;
  state.generations = data.generations; state.revisions = data.revisions; state.evidenceByGeneration = data.evidence_by_generation;
  if (data.context_snapshot) restoreContext(data.context_snapshot.context);
  else {
    const model = value("planning-model"); $("planning-form").reset(); $("planning-model").value = model;
    state.context = null; state.selected.clear(); fillCourses(); selectedView(); state.dirty = true;
  }
  history(state.generations.at(-1)?.event_id); await search(); status("Saved planning session loaded.");
}
function reset() {
  $("planning-form").reset(); state.session = null; state.contextId = null; state.context = null; state.evidence = null;
  state.selected.clear(); state.generations = []; state.revisions = {}; state.evidenceByGeneration = {}; state.draft = null;
  $("sessions").value = ""; localStorage.removeItem("praxis.session"); fillCourses(); selectedView(); history(null);
  $("standards").replaceChildren(); showEvidence(null); state.dirty = true; status("New lesson. Previous saved lessons remain available.");
}
$("planning-form").addEventListener("input", (event) => { if (!["standard-search", "planning-model"].includes(event.target.id)) markDirty(); });
$("subject").onchange = () => { fillCourses(); state.selected.clear(); selectedView(); $("standards").replaceChildren(); };
$("course").onchange = () => { state.selected.clear(); selectedView(); work(search); };
$("search").onclick = () => work(search);
$("standard-search").onkeydown = (event) => { if (event.key === "Enter") { event.preventDefault(); work(search); } };
$("save-context").onclick = () => work(saveContext);
$("new-session").onclick = reset;
$("sessions").onchange = () => work(() => loadSession(value("sessions")));
$("history").onchange = () => showGeneration(value("history"));
$("planning-form").onsubmit = (event) => {
  event.preventDefault(); work(async () => {
    if (!value("planning-model")) throw new Error("Start Ollama and select an installed local model.");
    if (state.dirty || !state.contextId) await saveContext();
    status("Generating a structured lesson. Local models can take several minutes.");
    const generation = await api(`/sessions/${state.session}/generations`, {method: "POST", body: JSON.stringify({context_snapshot_id: state.contextId, model: value("planning-model")})});
    state.generations.push(generation); state.evidenceByGeneration[generation.event_id] = state.evidence;
    history(generation.event_id); status("Model response and validation findings saved.");
  });
};
$("save-revision").onclick = () => work(async () => {
  const event = await api(`/sessions/${state.session}/generations/${state.active.event_id}/revisions`, {method: "POST", body: JSON.stringify({draft: state.draft})});
  (state.revisions[state.active.event_id] ||= []).push(event); showGeneration(state.active.event_id); status("Revision saved. Automated checks were run again.");
});
$("download").onclick = () => {
  if (!state.draft) return;
  function text(value, depth = 0) {
    if (value === null || value === undefined) return "";
    if (Array.isArray(value)) return value.map((v) => text(v, depth + 1)).join("\n\n");
    if (typeof value === "object") return Object.entries(value).map(([k, v]) => `${titleCase(k)}\n${text(v, depth + 1)}`).join("\n\n");
    return String(value);
  }
  const contents = `Praxis lesson draft\n${state.editDirty ? "Contains unsaved, unchecked edits." : "Pedagogical alignment remains unverified."}\n\n${text(state.draft)}`;
  const url = URL.createObjectURL(new Blob([contents], {type: "text/plain;charset=utf-8"}));
  const link = node("a"); link.href = url; link.download = "lesson-draft.txt"; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
};
async function init() {
  try {
    const data = await api("/courses"); state.courses = data.courses; fillCourses();
    $("coverage").textContent = `${data.available_standards} available standards in ${state.courses.filter((c) => c.served).length} courses. Archive: ${data.archived_standards} standards in ${state.courses.length} courses.`;
    state.courses.filter((c) => !c.served).forEach((c) => $("course-holds").append(node("li", `${c.course}: ${c.notes}`)));
    await loadSessions();
    const saved = localStorage.getItem("praxis.session");
    if (saved && [...$("sessions").options].some((o) => o.value === saved)) { $("sessions").value = saved; await loadSession(saved); }
  } catch (error) { status(error.message, true); }
  try {
    const [health, models] = await Promise.all([fetch("/api/health").then((r) => r.json()), fetch("/api/models").then((r) => r.json())]);
    const local = models.filter((m) => !m.is_cloud);
    $("planning-model").replaceChildren(...local.map((m) => new Option(m.name, m.name)));
    if (!local.length) $("planning-model").append(new Option("No local model available", ""));
    else if (local.some((m) => m.name === health.default_model)) $("planning-model").value = health.default_model;
    $("model-status").textContent = health.ollama_reachable ? "Ollama connected. Planning uses local structured outputs." : "Ollama is unavailable. You can still select standards and save context.";
  } catch { $("model-status").textContent = "Could not contact Ollama. Start it and reload to generate."; }
}
init();

function sizeEditors() {
  document.querySelectorAll("#lesson textarea").forEach((el) => {
    el.style.height = "auto"; el.style.height = `${el.scrollHeight + 2}px`;
  });
}
window.addEventListener("resize", sizeEditors);
$("lesson").addEventListener("input", sizeEditors);
