const form = document.getElementById("authForm");
const errorNode = document.getElementById("authError");
const titleNode = document.getElementById("authTitle");
const noteNode = document.getElementById("authNote");
const submitNode = document.getElementById("authSubmit");
const eyebrowNode = document.getElementById("authEyebrow");

let mode = window.location.pathname === "/setup" ? "setup" : "login";

function showError(message) {
  errorNode.textContent = message;
  errorNode.classList.remove("hidden");
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: {"Content-Type": "application/json"},
    ...options,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
  return data;
}

function renderMode() {
  if (mode === "setup") {
    eyebrowNode.textContent = "Первый запуск";
    titleNode.textContent = "Создать администратора";
    noteNode.textContent = "Эта учетная запись будет единственной админ-учеткой для панели.";
    submitNode.textContent = "Создать и войти";
    document.getElementById("password").autocomplete = "new-password";
  } else {
    eyebrowNode.textContent = "Админка";
    titleNode.textContent = "Вход";
    noteNode.textContent = "Введите логин и пароль администратора.";
    submitNode.textContent = "Войти";
  }
}

async function init() {
  const status = await api("/api/auth/status");
  if (!status.configured && mode !== "setup") {
    window.location.replace("/setup");
    return;
  }
  if (status.configured && mode === "setup") {
    window.location.replace("/login");
    return;
  }
  if (status.authenticated) {
    window.location.replace("/");
    return;
  }
  renderMode();
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  errorNode.classList.add("hidden");
  const username = document.getElementById("username").value.trim();
  const password = document.getElementById("password").value;
  try {
    const path = mode === "setup" ? "/api/auth/setup" : "/api/auth/login";
    await api(path, {method: "POST", body: JSON.stringify({username, password})});
    window.location.replace("/");
  } catch (error) {
    showError(error.message);
  }
});

init().catch((error) => showError(error.message));
