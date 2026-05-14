let currentUploadId = null;
let activeDealId = null;
let selectedContactIds = new Set();
let projects = [];
let currentProject = null;
let krishaEventsLoading = false;
let latestSettings = {};

const $ = (id) => document.getElementById(id);
const tabs = ["dashboard", "settings", "projects", "upload", "krisha", "campaign", "auto", "ai", "crm", "contacts", "logs"];
const baseSettingsIds = [
  "green_api_url",
  "green_media_url",
  "green_id_instance",
  "green_api_token",
  "default_country_code",
  "ai_provider",
    "openai_api_key",
    "openai_base_url",
    "openai_model",
    "groq_api_key",
    "groq_base_url",
    "groq_transcription_model",
    "deepseek_api_key",
    "deepseek_base_url",
    "deepseek_model",
    "audio_transcription_enabled",
    "ai_temperature",
  "bot_name",
  "ai_system_prompt",
  "max_lpr_attempts",
  "send_proposal_after_failed_attempts",
  "typing_enabled",
  "typing_min_seconds",
  "typing_max_seconds",
  "green_history_sync_minutes",
  "handoff_enabled",
  "handoff_phone",
];
const krishaSettingsIds = [
  "krisha_login",
  "krisha_password",
  "krisha_city",
  "krisha_property_type",
  "krisha_keywords",
  "krisha_min_area",
  "krisha_max_area",
  "krisha_min_price",
  "krisha_max_price",
  "krisha_max_pages",
  "krisha_max_contacts",
  "krisha_interval_minutes",
  "krisha_use_browser",
  "krisha_headless",
];
const autoWorkSettingsIds = [
  "auto_campaign_enabled",
  "auto_campaign_time",
  "auto_campaign_timezone",
  "auto_campaign_max_messages",
  "auto_campaign_delay_min_seconds",
  "auto_campaign_delay_max_seconds",
  "auto_campaign_target_kind",
];

function toast(message) {
  const node = $("toast");
  node.textContent = message;
  node.classList.remove("hidden");
  clearTimeout(window.__toastTimer);
  window.__toastTimer = setTimeout(() => node.classList.add("hidden"), 4500);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: options.body instanceof FormData ? {} : {"Content-Type": "application/json"},
    ...options,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401 && (data.login_required || data.setup_required)) {
      window.location.replace(data.setup_required ? "/setup" : "/login");
      return new Promise(() => {});
    }
    throw new Error(data.detail || data.message || `HTTP ${response.status}`);
  }
  return data;
}

function fillSelect(select, columns, includeEmpty = false) {
  select.innerHTML = "";
  if (includeEmpty) {
    const empty = document.createElement("option");
    empty.value = "";
    empty.textContent = "Не выбрано";
    select.appendChild(empty);
  }
  columns.forEach((column) => {
    const option = document.createElement("option");
    option.value = column;
    option.textContent = column;
    select.appendChild(option);
  });
}

function selectLikelyPhoneColumns(select) {
  const preferred = ["whatsapp", "whats app", "wa.me", "вотсап", "ватсап", "вацап"];
  const fallback = ["телефон", "phone"];
  let selected = 0;
  Array.from(select.options).forEach((option) => {
    const label = option.textContent.toLowerCase();
    option.selected = preferred.some((token) => label.includes(token));
    if (option.selected) selected += 1;
  });
  if (!selected) {
    Array.from(select.options).forEach((option) => {
      const label = option.textContent.toLowerCase();
      option.selected = fallback.some((token) => label.includes(token));
    });
  }
}

function selectLikelySignalColumns(select) {
  const preferred = [
    "рубрика",
    "категор",
    "вид деятельности",
    "описан",
    "адрес",
    "город",
    "район",
    "рейтинг",
    "отзыв",
    "сайт",
    "instagram",
    "2gis",
  ];
  Array.from(select.options).forEach((option) => {
    const label = option.textContent.toLowerCase();
    option.selected = preferred.some((token) => label.includes(token));
  });
}

function renderPreview(rows) {
  if (!rows.length) {
    $("preview").innerHTML = "";
    return;
  }
  const columns = Object.keys(rows[0]);
  const head = columns.map((col) => `<th>${escapeHtml(col)}</th>`).join("");
  const body = rows.map((row) => (
    `<tr>${columns.map((col) => `<td>${escapeHtml(row[col] || "")}</td>`).join("")}</tr>`
  )).join("");
  $("preview").innerHTML = `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function activateTab(tabId, updateHash = true) {
  const nextTab = tabs.includes(tabId) ? tabId : "dashboard";
  document.querySelectorAll(".tab-panel").forEach((panel) => {
    panel.classList.toggle("active", panel.id === nextTab);
  });
  document.querySelectorAll(".tab-button").forEach((button) => {
    button.classList.toggle("active", button.dataset.tab === nextTab);
  });
  if (updateHash && window.location.hash !== `#${nextTab}`) {
    history.replaceState(null, "", `#${nextTab}`);
  }
}

function isKeramoProject() {
  return (currentProject?.workflow_type || "") === "keramo_investor";
}

function updateProjectTabs() {
  const keramo = isKeramoProject();
  const uploadButton = document.querySelector('.tab-button[data-tab="upload"]');
  const krishaButton = document.querySelector('.tab-button[data-tab="krisha"]');
  if (uploadButton) uploadButton.classList.toggle("hidden", keramo);
  if (krishaButton) krishaButton.classList.toggle("hidden", !keramo);
  if (keramo && $("upload")?.classList.contains("active")) {
    activateTab("krisha");
  }
  if (!keramo && $("krisha")?.classList.contains("active")) {
    activateTab("upload");
  }
}

async function loadSettings() {
  const settings = await api("/api/settings");
  latestSettings = settings;
  Object.entries(settings).forEach(([key, value]) => {
    const input = $(key);
    if (!input) return;
    if (input.type === "checkbox") {
      input.checked = value === true || value === "true" || value === "1";
    } else {
      input.value = value ?? "";
    }
  });
  renderAutoWorkStatus();
}

function settingValue(id) {
  const input = $(id);
  if (!input) return "";
  if (input.type === "checkbox") return input.checked ? "true" : "false";
  return input.value;
}

function settingsPayload(ids) {
  const payload = {};
  ids.forEach((id) => {
    if ($(id)) payload[id] = settingValue(id);
  });
  return payload;
}

async function saveSettings() {
  await api("/api/settings", {method: "POST", body: JSON.stringify(settingsPayload(baseSettingsIds))});
  toast("Настройки сохранены");
}

async function saveKrishaSettings(showToast = true) {
  await api("/api/settings", {method: "POST", body: JSON.stringify(settingsPayload(krishaSettingsIds))});
  if (showToast) toast("Настройки Krisha сохранены");
}

async function saveAutoWorkSettings(showToast = true) {
  await api("/api/settings", {method: "POST", body: JSON.stringify(settingsPayload(autoWorkSettingsIds))});
  await loadSettings();
  if (showToast) toast("Настройки автоработы сохранены");
  await refreshAll();
}

function renderProjectSelect() {
  $("projectSelect").innerHTML = projects.map((project) => (
    `<option value="${project.id}" ${currentProject && Number(currentProject.id) === Number(project.id) ? "selected" : ""}>${escapeHtml(project.name)}</option>`
  )).join("");
  $("activeProjectName").textContent = currentProject ? currentProject.name : "Проект";
  updateProjectTabs();
}

function renderProjectCards() {
  if (!currentProject) {
    $("projectCards").innerHTML = `<div class="project-card active"><strong>Проект не выбран</strong></div>`;
    return;
  }
  $("projectCards").innerHTML = (
    `<div class="project-card active project-card-static">
      <strong>${escapeHtml(currentProject.name)}</strong>
      <span>${escapeHtml(currentProject.product_name || "")}</span>
      <small>${escapeHtml(currentProject.workflow_type || "")}</small>
    </div>`
  );
}

function fillProjectForm() {
  if (!currentProject) return;
  $("project_name").value = currentProject.name || "";
  $("project_product_name").value = currentProject.product_name || "";
  $("project_workflow_type").value = currentProject.workflow_type || "generic_b2b";
  $("project_proposal_filename").value = currentProject.proposal_filename || "";
  $("project_ai_system_prompt").value = currentProject.ai_system_prompt || "";
}

async function loadProjects() {
  const data = await api("/api/projects");
  projects = data.projects || [];
  currentProject = data.current || projects.find((project) => Number(project.id) === Number(data.current_project_id)) || projects[0] || null;
  renderProjectSelect();
  renderProjectCards();
  fillProjectForm();
}

async function switchProject(projectId) {
  if (currentProject && Number(currentProject.id) === Number(projectId)) return;
  await api("/api/projects/current", {method: "POST", body: JSON.stringify({project_id: projectId})});
  selectedContactIds = new Set();
  closeDeal();
  currentUploadId = null;
  await loadProjects();
  await loadSettings();
  await refreshAll();
  toast(`Проект переключен: ${currentProject.name}`);
}

async function saveProject() {
  if (!currentProject) {
    toast("Проект не выбран");
    return;
  }
  const payload = {
    name: $("project_name").value.trim(),
    product_name: $("project_product_name").value.trim(),
    workflow_type: $("project_workflow_type").value,
    proposal_filename: $("project_proposal_filename").value.trim(),
    ai_system_prompt: $("project_ai_system_prompt").value,
  };
  const data = await api(`/api/projects/${currentProject.id}`, {method: "PATCH", body: JSON.stringify(payload)});
  currentProject = data.project;
  await loadProjects();
  await loadSettings();
  toast("Проект сохранен");
}

async function logout() {
  await api("/api/auth/logout", {method: "POST"});
  window.location.replace("/login");
}

async function changePassword() {
  const currentPassword = $("current_password").value;
  const newPassword = $("new_password").value;
  const repeat = $("new_password_repeat").value;
  if (!currentPassword || !newPassword) {
    toast("Заполните текущий и новый пароль");
    return;
  }
  if (newPassword !== repeat) {
    toast("Новый пароль и повтор не совпадают");
    return;
  }
  await api("/api/auth/password", {
    method: "POST",
    body: JSON.stringify({current_password: currentPassword, new_password: newPassword}),
  });
  $("current_password").value = "";
  $("new_password").value = "";
  $("new_password_repeat").value = "";
  toast("Пароль изменен");
}

function metric(label, value) {
  return `<div class="metric"><strong>${value}</strong><span>${label}</span></div>`;
}

function runtimeRow(label, value) {
  return `<div class="kv-row"><span>${label}</span><strong>${escapeHtml(value ?? "—")}</strong></div>`;
}

function renderKrishaParserStatus(state = {}) {
  const node = $("krishaParserStatus");
  if (!node) return;
  const errors = Array.isArray(state.source_errors) ? state.source_errors.length : 0;
  node.innerHTML = [
    runtimeRow("Статус фона", state.status || "idle"),
    runtimeRow("Циклов", state.processed || 0),
    runtimeRow("Найдено в последнем цикле", state.found || 0),
    runtimeRow("Лимит номеров", state.max_contacts || "—"),
    runtimeRow("Новых / обновлено", `${state.imported || 0} / ${state.updated || 0}`),
    runtimeRow("Последний запуск", formatDateTime(state.last_run_at)),
    runtimeRow("Ошибки источников", errors),
    runtimeRow("Последняя ошибка", state.last_error || "нет"),
  ].join("");
}

function formatDateTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function formatShortDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function displayName(contact) {
  return contact.company || contact.name || contact.phone || contact.chat_id || `#${contact.id}`;
}

function contactLine(contact) {
  const parts = [contact.phone, contact.name, contact.kind].filter(Boolean);
  return parts.join(" · ");
}

function contactMeta(contact) {
  return parsePayload(contact.meta_json);
}

function salesSignalPreview(contact) {
  const meta = contactMeta(contact);
  return meta.sales_angle || meta.sales_signal || "";
}

function statusBadge(contact) {
  if (contact.status === "interested") return "<span class=\"pill\">зацепка</span>";
  if (contact.status === "not_interested") return "<span class=\"pill warn\">не сейчас</span>";
  if (contact.status === "opt_out" || contact.status === "no_whatsapp") return `<span class="pill bad">${escapeHtml(contact.status)}</span>`;
  if (contact.status === "error") return "<span class=\"pill bad\">ошибка</span>";
  if (contact.proposal_sent === 1) return "<span class=\"pill\">КП</span>";
  if (contact.kind === "lpr") return "<span class=\"pill\">ответственный</span>";
  return `<span class="pill warn">${escapeHtml(contact.status || "new")}</span>`;
}

function renderStats(data) {
  if (data.project) {
    currentProject = data.project;
    $("activeProjectName").textContent = currentProject.name || "Проект";
    updateProjectTabs();
  }
  const s = data.stats;
  $("metrics").innerHTML = [
    metric("Всего контактов", s.total),
    metric("WhatsApp найден", s.whatsapp_ok),
    metric("Отправлено", s.sent),
    metric("Ответили", s.replied),
    metric("Ответственные", s.lpr),
    metric("КП отправлено", s.proposal_sent),
    metric("Зацепки", s.interested),
    metric("Не сейчас", s.not_interested),
    metric("Отказы", s.opt_out),
    metric("Ошибки", s.errors),
  ].join("");

  const campaign = data.campaign || {};
  $("runtimeStatus").innerHTML = [
    runtimeRow("Проверка WhatsApp", `${data.runtime.check.status} ${data.runtime.check.processed || 0}/${data.runtime.check.total || 0}`),
    runtimeRow("Кампания", campaign.status ? `${campaign.status}: ${campaign.sent_count || 0}/${campaign.max_messages || 0}` : data.runtime.campaign.status),
    runtimeRow("Авторабота", data.runtime.auto_work?.status || "idle"),
    runtimeRow("AI polling", `${data.runtime.ai.status}, обработано: ${data.runtime.ai.processed || 0}`),
    runtimeRow("Krisha-парсер", data.runtime.krisha_parser?.status || "idle"),
    runtimeRow("Последняя ошибка", data.runtime.ai.last_error || data.runtime.campaign.last_error || data.runtime.check.last_error || "нет"),
  ].join("");

  $("aiState").textContent = data.runtime.ai.status;
  renderCampaignStatus(data);
  renderAutoWorkStatus(data);
  renderAiStatus(data);
  renderKrishaParserStatus(data.runtime.krisha_parser || {});
  $("recentMessages").innerHTML = data.recent_messages.map((message) => (
    `<div class="message ${message.direction}">
      <small>${escapeHtml(message.direction)} · ${escapeHtml(message.phone || message.chat_id)} · ${escapeHtml(message.created_at)}</small>
      <div>${escapeHtml(message.text || "")}</div>
    </div>`
  )).join("") || "<p class=\"note\">Сообщений пока нет</p>";
}

function renderDealCard(deal) {
  const signal = salesSignalPreview(deal);
  const preview = deal.last_message_text || signal || "Сообщений пока нет";
  const messageMeta = deal.message_count ? `${deal.message_count} сообщ. · ${formatShortDate(deal.last_message_at)}` : "нет сообщений";
  return (
    `<button class="deal-card" type="button" data-contact-id="${deal.id}">
      <span class="deal-card-title">${escapeHtml(displayName(deal))}</span>
      <span class="deal-card-sub">${escapeHtml(contactLine(deal))}</span>
      <span class="deal-card-preview">${escapeHtml(preview)}</span>
      <span class="deal-card-bottom">
        ${statusBadge(deal)}
        <small>${escapeHtml(messageMeta)}</small>
      </span>
    </button>`
  );
}

function renderCrmBoard(data) {
  $("crmBoard").innerHTML = data.columns.map((column) => (
    `<section class="kanban-column">
      <header>
        <strong>${escapeHtml(column.title)}</strong>
        <span>${column.deals.length}</span>
      </header>
      <div class="kanban-list">
        ${column.deals.map(renderDealCard).join("") || "<p class=\"note empty-column\">Нет сделок</p>"}
      </div>
    </section>`
  )).join("");
  document.querySelectorAll(".deal-card").forEach((card) => {
    card.addEventListener("click", () => openDeal(Number(card.dataset.contactId)).catch((e) => toast(e.message)));
  });
}

async function loadCrmBoard() {
  const params = new URLSearchParams();
  if ($("crmSearch").value.trim()) params.set("q", $("crmSearch").value.trim());
  const data = await api(`/api/crm/kanban?${params}`);
  renderCrmBoard(data);
}

function parsePayload(value) {
  try {
    return JSON.parse(value || "{}");
  } catch {
    return {};
  }
}

function messageText(message) {
  const payload = parsePayload(message.payload_json);
  const type = payload?.messageData?.typeMessage || payload?.typeMessage || "";
  const body = message.text || "";
  if (type === "contactMessage") return body || "Контакт";
  if (type === "contactsArrayMessage") return body || "Контакты";
  return body;
}

function renderWhatsappMessage(message) {
  const directionLabel = message.direction === "out" ? "Вы" : "Клиент";
  return (
    `<div class="wa-row ${message.direction}">
      <div class="wa-bubble">
        <div class="wa-text">${escapeHtml(messageText(message)).replaceAll("\n", "<br>") || "<span class=\"muted-text\">без текста</span>"}</div>
        <div class="wa-meta">${escapeHtml(directionLabel)} · ${escapeHtml(formatShortDate(message.created_at))}</div>
      </div>
    </div>`
  );
}

async function openDeal(contactId) {
  activeDealId = contactId;
  const data = await api(`/api/contacts/${contactId}/messages`);
  const contact = data.contact;
  const meta = contactMeta(contact);
  $("dealStageLabel").textContent = `${contact.kind || "contact"} · ${contact.status || ""}`;
  $("dealTitle").textContent = displayName(contact);
  $("dealSubtitle").textContent = contactLine(contact);
  $("dealMeta").innerHTML = [
    runtimeRow("Телефон", contact.phone || "—"),
    runtimeRow("WhatsApp", contact.whatsapp_exists === 1 ? "есть" : contact.whatsapp_exists === 0 ? "нет" : "не проверен"),
    runtimeRow("Этап", `${contact.status || "—"} / ${contact.stage || "—"}`),
    runtimeRow("Источник", contact.source || "—"),
    runtimeRow("Организация", contact.company || "—"),
    runtimeRow("Имя", contact.name || "—"),
    runtimeRow("Сигнал", meta.sales_signal || "—"),
    runtimeRow("Угол атаки", meta.sales_angle || "—"),
  ].join("");
  $("dealMessages").innerHTML = data.messages.map(renderWhatsappMessage).join("") || "<p class=\"note empty-dialog\">Сообщений по этой сделке пока нет</p>";
  $("dealModal").classList.remove("hidden");
  requestAnimationFrame(() => {
    $("dealMessages").scrollTop = $("dealMessages").scrollHeight;
  });
}

function closeDeal() {
  activeDealId = null;
  $("dealModal").classList.add("hidden");
}

function renderCampaignStatus(data) {
  const campaign = data.campaign || {};
  $("campaignStatusPanel").innerHTML = [
    runtimeRow("Статус", campaign.status || data.runtime.campaign.status || "idle"),
    runtimeRow("Отправлено", campaign.max_messages ? `${campaign.sent_count || 0}/${campaign.max_messages}` : "0"),
    runtimeRow("Ошибок", campaign.error_count ?? 0),
    runtimeRow("Задержка", campaign.delay_min_seconds ? `${campaign.delay_min_seconds}-${campaign.delay_max_seconds} сек` : "—"),
    runtimeRow("Запущена", campaign.started_at ? formatDateTime(campaign.started_at) : "—"),
    runtimeRow("Последняя ошибка", data.runtime.campaign.last_error || "нет"),
  ].join("");
}

function renderAutoWorkStatus(data = null) {
  const node = $("autoWorkStatusPanel");
  if (!node) return;
  if (data?.auto_work_settings) {
    latestSettings = {...latestSettings, ...data.auto_work_settings};
  }
  const runtime = data?.runtime?.auto_work || {};
  const enabled = latestSettings.auto_campaign_enabled === true || latestSettings.auto_campaign_enabled === "true" || latestSettings.auto_campaign_enabled === "1";
  const nextTime = latestSettings.auto_campaign_time || "10:00";
  const timezone = latestSettings.auto_campaign_timezone || "Asia/Qyzylorda";
  const limit = latestSettings.auto_campaign_max_messages || "25";
  const delayMin = latestSettings.auto_campaign_delay_min_seconds || "40";
  const delayMax = latestSettings.auto_campaign_delay_max_seconds || "120";
  node.innerHTML = [
    runtimeRow("Статус", runtime.status || "idle"),
    runtimeRow("Автозапуск", enabled ? "включен" : "выключен"),
    runtimeRow("Ежедневное время", `${nextTime} (${timezone})`),
    runtimeRow("Лимит в день", limit),
    runtimeRow("Задержка", `${delayMin}-${delayMax} сек`),
    runtimeRow("Последний запуск", latestSettings.auto_campaign_last_run_date || "—"),
    runtimeRow("Последняя проверка", formatDateTime(runtime.last_check_at)),
    runtimeRow("Последнее действие", runtime.last_action || "—"),
    runtimeRow("Последняя ошибка", runtime.last_error || "нет"),
  ].join("");
}

function renderAiStatus(data) {
  $("aiStatusPanel").innerHTML = [
    runtimeRow("Статус", data.runtime.ai.status || "idle"),
    runtimeRow("Обработано уведомлений", data.runtime.ai.processed || 0),
    runtimeRow("Ответили", data.stats.replied),
    runtimeRow("Ответственных найдено", data.stats.lpr),
    runtimeRow("КП отправлено", data.stats.proposal_sent),
    runtimeRow("Зацепки", data.stats.interested),
    runtimeRow("Последняя ошибка", data.runtime.ai.last_error || "нет"),
  ].join("");
}

async function loadStats() {
  const data = await api("/api/stats");
  renderStats(data);
}

function statusPill(contact) {
  if (contact.whatsapp_exists === 1) return "<span class=\"pill\">есть</span>";
  if (contact.whatsapp_exists === 0) return "<span class=\"pill bad\">нет</span>";
  return "<span class=\"pill warn\">не проверен</span>";
}

async function loadContacts() {
  const params = new URLSearchParams();
  if ($("kindFilter").value) params.set("kind", $("kindFilter").value);
  if ($("statusFilter").value) params.set("status", $("statusFilter").value);
  if ($("searchContacts").value.trim()) params.set("q", $("searchContacts").value.trim());
  const data = await api(`/api/contacts?${params}`);
  selectedContactIds = new Set(data.contacts.filter((contact) => selectedContactIds.has(contact.id)).map((contact) => contact.id));
  $("contactsBody").innerHTML = data.contacts.map((contact) => (
    `<tr>
      <td class="table-checkbox"><input class="contact-select" type="checkbox" value="${contact.id}" ${selectedContactIds.has(contact.id) ? "checked" : ""} aria-label="Выбрать контакт ${contact.id}"></td>
      <td>${contact.id}</td>
      <td>${escapeHtml(contact.kind)}</td>
      <td>${escapeHtml(contact.phone)}</td>
      <td>${escapeHtml(contact.name || "")}</td>
      <td>${escapeHtml(contact.company || "")}</td>
      <td>${statusPill(contact)}</td>
      <td>${escapeHtml(contact.status)}<br><small>${escapeHtml(contact.stage || "")}</small></td>
      <td>${escapeHtml(contact.source || "")}</td>
      <td>${escapeHtml(contact.last_error || "")}</td>
    </tr>`
  )).join("") || "<tr><td colspan=\"10\">Контактов пока нет</td></tr>";
  document.querySelectorAll(".contact-select").forEach((input) => {
    input.addEventListener("change", () => {
      const contactId = Number(input.value);
      if (input.checked) {
        selectedContactIds.add(contactId);
      } else {
        selectedContactIds.delete(contactId);
      }
      renderContactSelectionState();
    });
  });
  renderContactSelectionState();
}

function renderContactSelectionState() {
  const checkboxes = Array.from(document.querySelectorAll(".contact-select"));
  const visibleIds = checkboxes.map((input) => Number(input.value));
  const allSelected = visibleIds.length > 0 && visibleIds.every((contactId) => selectedContactIds.has(contactId));
  $("selectAllContactsCheckbox").checked = allSelected;
  $("selectedContactsCount").textContent = selectedContactIds.size ? `Выбрано: ${selectedContactIds.size}` : "Ничего не выбрано";
}

function toggleVisibleContactsSelection(selected) {
  document.querySelectorAll(".contact-select").forEach((input) => {
    const contactId = Number(input.value);
    input.checked = selected;
    if (selected) {
      selectedContactIds.add(contactId);
    } else {
      selectedContactIds.delete(contactId);
    }
  });
  renderContactSelectionState();
}

async function deleteContacts(payload) {
  return api("/api/contacts/delete", {method: "POST", body: JSON.stringify(payload)});
}

async function deleteSelectedContacts() {
  const contactIds = Array.from(selectedContactIds);
  if (!contactIds.length) {
    toast("Сначала выделите контакты");
    return;
  }
  if (!window.confirm(`Удалить выбранные контакты: ${contactIds.length}?`)) return;
  const data = await deleteContacts({contact_ids: contactIds});
  selectedContactIds = new Set();
  if (activeDealId && contactIds.includes(activeDealId)) closeDeal();
  toast(`Удалено контактов: ${data.deleted_contacts}`);
  await refreshAll();
}

async function deleteFilteredContacts() {
  const payload = {
    kind: $("kindFilter").value || null,
    status: $("statusFilter").value || null,
    q: $("searchContacts").value.trim() || null,
    delete_all_matching: true,
  };
  const scope = payload.kind || payload.status || payload.q ? "контакты по текущему фильтру" : "все контакты";
  if (!window.confirm(`Удалить ${scope}?`)) return;
  const data = await deleteContacts(payload);
  selectedContactIds = new Set();
  closeDeal();
  toast(`Удалено контактов: ${data.deleted_contacts}`);
  await refreshAll();
}

async function deleteFilteredDeals() {
  const q = $("crmSearch").value.trim();
  const scope = q ? `сделки по фильтру «${q}»` : "все сделки";
  if (!window.confirm(`Удалить ${scope}?`)) return;
  const data = await deleteContacts({q: q || null, delete_all_matching: true});
  selectedContactIds = new Set();
  closeDeal();
  toast(`Удалено сделок: ${data.deleted_contacts}`);
  await refreshAll();
}

async function deleteActiveDeal() {
  if (!activeDealId) {
    toast("Сделка не выбрана");
    return;
  }
  if (!window.confirm(`Удалить сделку ${$("dealTitle").textContent}?`)) return;
  const data = await api(`/api/contacts/${activeDealId}`, {method: "DELETE"});
  selectedContactIds.delete(activeDealId);
  closeDeal();
  toast(`Удалено сделок: ${data.deleted_contacts}`);
  await refreshAll();
}

async function resetSalesData() {
  const message = "Очистить тестовую базу? Будут удалены все контакты, ответственные, диалоги и кампании. Настройки, ключи, КП и Excel-файлы останутся.";
  if (!window.confirm(message)) return;
  const second = "Подтвердите очистку еще раз: это действие нельзя отменить.";
  if (!window.confirm(second)) return;
  const data = await api("/api/contacts/reset", {method: "POST"});
  selectedContactIds = new Set();
  closeDeal();
  toast(`База очищена: контактов ${data.deleted_contacts}, сообщений ${data.deleted_messages}, кампаний ${data.deleted_campaigns}`);
  await refreshAll();
}

function logLevelPill(level) {
  if (level === "error") return "<span class=\"pill bad\">error</span>";
  if (level === "warning") return "<span class=\"pill warn\">warning</span>";
  return "<span class=\"pill\">info</span>";
}

function renderEventFeed(nodeId, logs) {
  const node = $(nodeId);
  if (!node) return;
  node.innerHTML = logs.map((item) => (
    `<div class="event-item ${escapeHtml(item.level)}">
      <div class="event-time">${escapeHtml(formatDateTime(item.created_at))}</div>
      <div class="event-body">
        <strong>${escapeHtml(item.category)} · ${escapeHtml(item.level)}</strong>
        <p>${escapeHtml(item.message)}</p>
        <small>${escapeHtml(item.phone || item.company || item.name || "")}</small>
      </div>
    </div>`
  )).join("") || "<p class=\"note\">Событий пока нет</p>";
}

async function loadLogs() {
  const params = new URLSearchParams();
  if ($("logLevelFilter").value) params.set("level", $("logLevelFilter").value);
  if ($("logCategoryFilter").value) params.set("category", $("logCategoryFilter").value);
  if ($("searchLogs").value.trim()) params.set("q", $("searchLogs").value.trim());
  const data = await api(`/api/logs?${params}`);
  $("logsBody").innerHTML = data.logs.map((item) => (
    `<tr>
      <td>${escapeHtml(item.created_at)}</td>
      <td>${logLevelPill(item.level)}</td>
      <td>${escapeHtml(item.category)}</td>
      <td>${escapeHtml(item.message)}</td>
      <td>${escapeHtml(item.phone || "")}<br><small>${escapeHtml(item.company || item.name || "")}</small></td>
    </tr>`
  )).join("") || "<tr><td colspan=\"5\">Логов пока нет</td></tr>";
}

async function loadKrishaEvents() {
  if (!$("krishaEvents") || krishaEventsLoading) return;
  krishaEventsLoading = true;
  try {
    const [krisha, imports, whatsapp] = await Promise.all([
      api("/api/logs?category=krisha&limit=80"),
      api("/api/logs?category=import&limit=30"),
      api("/api/logs?category=whatsapp&limit=20"),
    ]);
    const logs = [
      ...krisha.logs,
      ...imports.logs.filter((item) => item.message.toLowerCase().includes("krisha")),
      ...whatsapp.logs.filter((item) => item.message.toLowerCase().includes("krisha")),
    ].sort((a, b) => b.id - a.id).slice(0, 80);
    renderEventFeed("krishaEvents", logs);
  } finally {
    krishaEventsLoading = false;
  }
}

async function loadScopedEvents() {
  const [campaign, ai, autoWork] = await Promise.all([
    api("/api/logs?category=campaign&limit=25"),
    api("/api/logs?category=ai&limit=25"),
    api("/api/logs?category=auto_work&limit=25"),
  ]);
  const [messages, proposals, handoff, lpr, contacts, whatsapp] = await Promise.all([
    api("/api/logs?category=message&limit=25"),
    api("/api/logs?category=proposal&limit=25"),
    api("/api/logs?category=handoff&limit=25"),
    api("/api/logs?category=lpr&limit=25"),
    api("/api/logs?category=contact&limit=25"),
    api("/api/logs?category=whatsapp&limit=25"),
  ]);

  const campaignLogs = [
    ...campaign.logs,
    ...messages.logs.filter((item) => item.message.includes("Отправлено сообщение")),
    ...proposals.logs,
    ...whatsapp.logs,
  ].sort((a, b) => b.id - a.id).slice(0, 18);

  const aiLogs = [
    ...ai.logs,
    ...handoff.logs,
    ...lpr.logs,
    ...contacts.logs,
    ...messages.logs.filter((item) => item.message.includes("Получено входящее")),
    ...proposals.logs,
  ].sort((a, b) => b.id - a.id).slice(0, 18);

  renderEventFeed("campaignEvents", campaignLogs);
  renderEventFeed("autoWorkEvents", autoWork.logs || []);
  renderEventFeed("aiEvents", aiLogs);
  await loadKrishaEvents();
}

async function uploadExcel() {
  const file = $("excelFile").files[0];
  if (!file) {
    toast("Выберите Excel файл");
    return;
  }
  const form = new FormData();
  form.append("file", file);
  const data = await api("/api/upload/excel", {method: "POST", body: form});
  currentUploadId = data.upload_id;
  fillSelect($("phoneColumn"), data.columns);
  selectLikelyPhoneColumns($("phoneColumn"));
  fillSelect($("companyColumn"), data.columns, true);
  fillSelect($("nameColumn"), data.columns, true);
  fillSelect($("signalColumns"), data.columns);
  selectLikelySignalColumns($("signalColumns"));
  $("columnChooser").classList.remove("hidden");
  renderPreview(data.preview);
  toast("Файл прочитан, выберите столбцы с номерами");
}

async function importExcel() {
  if (!currentUploadId) {
    toast("Сначала загрузите файл");
    return;
  }
  const payload = {
    upload_id: currentUploadId,
    phone_column: $("phoneColumn").value,
    phone_columns: Array.from($("phoneColumn").selectedOptions).map((option) => option.value),
    company_column: $("companyColumn").value || null,
    name_column: $("nameColumn").value || null,
    signal_columns: Array.from($("signalColumns").selectedOptions).map((option) => option.value),
  };
  const data = await api("/api/import", {method: "POST", body: JSON.stringify(payload)});
  const checkText = data.check_started ? "Проверка WhatsApp запущена." : "Проверка WhatsApp ждет GreenAPI.";
  toast(`Импортировано: ${data.imported}. Пропущено строк: ${data.skipped_rows}. ${checkText}`);
  await refreshAll();
}

function numberOrNull(id) {
  const value = $(id).value;
  if (value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function renderKrishaPreview(rows) {
  if (!rows.length) {
    $("krishaPreview").classList.add("hidden");
    $("krishaPreview").innerHTML = "";
    return;
  }
  $("krishaPreview").classList.remove("hidden");
  const body = rows.map((row) => (
    `<tr>
      <td>${escapeHtml(row.phone || "")}</td>
      <td>${escapeHtml(row.title || "")}</td>
      <td>${escapeHtml(row.area || "")}</td>
      <td>${escapeHtml(row.price || "")}</td>
      <td>${row.source_url ? `<a href="${escapeHtml(row.source_url)}" target="_blank" rel="noreferrer">ссылка</a>` : ""}</td>
    </tr>`
  )).join("");
  $("krishaPreview").innerHTML = (
    `<table>
      <thead>
        <tr><th>Телефон</th><th>Объект</th><th>м²</th><th>Цена</th><th>Источник</th></tr>
      </thead>
      <tbody>${body}</tbody>
    </table>`
  );
}

function buildKrishaPayload() {
  return {
    source_text: "",
    city: $("krisha_city").value.trim() || null,
    property_type: $("krisha_property_type").value,
    keywords: $("krisha_keywords").value.trim() || null,
    min_area: numberOrNull("krisha_min_area"),
    max_area: numberOrNull("krisha_max_area"),
    min_price: numberOrNull("krisha_min_price"),
    max_price: numberOrNull("krisha_max_price"),
    max_pages: Number($("krisha_max_pages").value || 1),
    max_contacts: numberOrNull("krisha_max_contacts"),
    import_contacts: true,
  };
}

async function parseKrisha() {
  await saveKrishaSettings(false);
  const payload = buildKrishaPayload();
  const data = await api("/api/krisha/import", {method: "POST", body: JSON.stringify(payload)});
  const checkText = data.check_started ? "Проверка WhatsApp запущена." : "Проверка WhatsApp ждет GreenAPI.";
  const errors = (data.source_errors || []).length ? ` Ошибки источников: ${(data.source_errors || []).length}.` : "";
  $("krishaResult").innerHTML = [
    runtimeRow("Найдено номеров", data.found),
    runtimeRow("Лимит номеров", data.max_contacts || "—"),
    runtimeRow("Новых контактов", data.imported),
    runtimeRow("Обновлено", data.updated),
    runtimeRow("WhatsApp", checkText),
  ].join("");
  renderKrishaPreview(data.preview || []);
  toast(`Krisha: найдено ${data.found}, импортировано ${data.imported}, обновлено ${data.updated}.${errors}`);
  await refreshAll();
}

async function startKrishaParser() {
  await saveKrishaSettings(false);
  const data = await api("/api/krisha/parser/start", {method: "POST"});
  renderKrishaParserStatus(data.state || {});
  toast("Фоновый Krisha-парсер запущен");
  await refreshAll();
}

async function stopKrishaParser() {
  const data = await api("/api/krisha/parser/stop", {method: "POST"});
  renderKrishaParserStatus(data.state || {});
  toast("Фоновый Krisha-парсер остановлен");
  await refreshAll();
}

async function checkWhatsappAgain() {
  await api("/api/check-whatsapp", {method: "POST"});
  toast("Проверка WhatsApp запущена");
  await refreshAll();
}

async function startCampaign() {
  const payload = {
    max_messages: Number($("max_messages").value),
    delay_min_seconds: Number($("delay_min_seconds").value),
    delay_max_seconds: Number($("delay_max_seconds").value),
    target_kind: "lead",
  };
  await api("/api/campaign/start", {method: "POST", body: JSON.stringify(payload)});
  toast("Рассылка запущена");
  await refreshAll();
}

async function pauseCampaign() {
  await api("/api/campaign/pause", {method: "POST"});
  toast("Кампания поставлена на паузу");
  await refreshAll();
}

async function resumeCampaign() {
  await api("/api/campaign/resume", {method: "POST"});
  toast("Кампания продолжена");
  await refreshAll();
}

async function stopCampaign() {
  await api("/api/campaign/stop", {method: "POST"});
  toast("Кампания остановлена");
  await refreshAll();
}

async function toggleAi(enabled) {
  await api("/api/ai/toggle", {method: "POST", body: JSON.stringify({enabled})});
  toast(enabled ? "AI-режим включен" : "AI-режим выключен");
  await refreshAll();
}

async function refreshAll() {
  await Promise.all([loadStats(), loadContacts(), loadCrmBoard(), loadLogs(), loadScopedEvents()]);
  if (activeDealId) {
    openDeal(activeDealId).catch(() => closeDeal());
  }
}

function bindEvents() {
  document.querySelectorAll(".tab-button").forEach((button) => {
    button.addEventListener("click", () => activateTab(button.dataset.tab));
  });
  window.addEventListener("hashchange", () => activateTab(window.location.hash.slice(1), false));
  $("saveSettingsBtn").addEventListener("click", () => saveSettings().catch((e) => toast(e.message)));
  $("saveProjectBtn").addEventListener("click", () => saveProject().catch((e) => toast(e.message)));
  $("projectSelect").addEventListener("change", () => switchProject(Number($("projectSelect").value)).catch((e) => toast(e.message)));
  $("logoutBtn").addEventListener("click", () => logout().catch((e) => toast(e.message)));
  $("changePasswordBtn").addEventListener("click", () => changePassword().catch((e) => toast(e.message)));
  $("refreshBtn").addEventListener("click", () => refreshAll().catch((e) => toast(e.message)));
  $("refreshCrmBtn").addEventListener("click", () => loadCrmBoard().catch((e) => toast(e.message)));
  $("deleteFilteredDealsBtn").addEventListener("click", () => deleteFilteredDeals().catch((e) => toast(e.message)));
  $("closeDealBtn").addEventListener("click", () => closeDeal());
  $("deleteDealBtn").addEventListener("click", () => deleteActiveDeal().catch((e) => toast(e.message)));
  $("dealModalBackdrop").addEventListener("click", () => closeDeal());
  $("crmSearch").addEventListener("input", () => {
    clearTimeout(window.__crmSearchTimer);
    window.__crmSearchTimer = setTimeout(() => loadCrmBoard().catch((e) => toast(e.message)), 250);
  });
  $("uploadBtn").addEventListener("click", () => uploadExcel().catch((e) => toast(e.message)));
  $("resetSalesDataBtn").addEventListener("click", () => resetSalesData().catch((e) => toast(e.message)));
  $("importBtn").addEventListener("click", () => importExcel().catch((e) => toast(e.message)));
  $("checkWhatsappBtn").addEventListener("click", () => checkWhatsappAgain().catch((e) => toast(e.message)));
  $("saveKrishaSettingsBtn").addEventListener("click", () => saveKrishaSettings().catch((e) => toast(e.message)));
  $("parseKrishaBtn").addEventListener("click", () => parseKrisha().catch((e) => toast(e.message)));
  $("startKrishaParserBtn").addEventListener("click", () => startKrishaParser().catch((e) => toast(e.message)));
  $("stopKrishaParserBtn").addEventListener("click", () => stopKrishaParser().catch((e) => toast(e.message)));
  $("checkWhatsappKrishaBtn").addEventListener("click", () => checkWhatsappAgain().catch((e) => toast(e.message)));
  $("startCampaignBtn").addEventListener("click", () => startCampaign().catch((e) => toast(e.message)));
  $("pauseCampaignBtn").addEventListener("click", () => pauseCampaign().catch((e) => toast(e.message)));
  $("resumeCampaignBtn").addEventListener("click", () => resumeCampaign().catch((e) => toast(e.message)));
  $("stopCampaignBtn").addEventListener("click", () => stopCampaign().catch((e) => toast(e.message)));
  $("saveAutoWorkSettingsBtn").addEventListener("click", () => saveAutoWorkSettings().catch((e) => toast(e.message)));
  $("enableAiBtn").addEventListener("click", () => toggleAi(true).catch((e) => toast(e.message)));
  $("disableAiBtn").addEventListener("click", () => toggleAi(false).catch((e) => toast(e.message)));
  $("selectAllContactsBtn").addEventListener("click", () => toggleVisibleContactsSelection(true));
  $("clearSelectedContactsBtn").addEventListener("click", () => toggleVisibleContactsSelection(false));
  $("selectAllContactsCheckbox").addEventListener("change", (event) => toggleVisibleContactsSelection(event.target.checked));
  $("deleteSelectedContactsBtn").addEventListener("click", () => deleteSelectedContacts().catch((e) => toast(e.message)));
  $("deleteFilteredContactsBtn").addEventListener("click", () => deleteFilteredContacts().catch((e) => toast(e.message)));
  $("resetSalesDataContactsBtn").addEventListener("click", () => resetSalesData().catch((e) => toast(e.message)));
  $("kindFilter").addEventListener("change", () => loadContacts().catch((e) => toast(e.message)));
  $("statusFilter").addEventListener("change", () => loadContacts().catch((e) => toast(e.message)));
  $("searchContacts").addEventListener("input", () => {
    clearTimeout(window.__contactSearchTimer);
    window.__contactSearchTimer = setTimeout(() => loadContacts().catch((e) => toast(e.message)), 250);
  });
  $("refreshLogsBtn").addEventListener("click", () => loadLogs().catch((e) => toast(e.message)));
  $("logLevelFilter").addEventListener("change", () => loadLogs().catch((e) => toast(e.message)));
  $("logCategoryFilter").addEventListener("change", () => loadLogs().catch((e) => toast(e.message)));
  $("searchLogs").addEventListener("input", () => {
    clearTimeout(window.__logSearchTimer);
    window.__logSearchTimer = setTimeout(() => loadLogs().catch((e) => toast(e.message)), 250);
  });
}

async function init() {
  bindEvents();
  activateTab(window.location.hash.slice(1) || "dashboard", false);
  await loadProjects();
  await loadSettings();
  await refreshAll();
  setInterval(() => refreshAll().catch(() => {}), 5000);
  setInterval(() => {
    if (isKeramoProject()) loadKrishaEvents().catch(() => {});
  }, 1500);
}

init().catch((e) => toast(e.message));
