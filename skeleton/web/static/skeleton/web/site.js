"use strict";

// Native sections are the no-JavaScript interface; tabs are progressive only.
for (const navigation of document.querySelectorAll("[data-tabs]")) {
  const container = navigation.parentElement;
  const buttons = [...navigation.querySelectorAll("[data-tab]")];
  const panels = new Map();
  for (const button of buttons) {
    const id = button.dataset.tab;
    const sections = [...container.querySelectorAll("[data-tab-section]")].filter(section => section.dataset.tabSection === id);
    if (!sections.length) continue;
    const panel = document.createElement("div");
    panel.id = "panel-" + id;
    panel.setAttribute("role", "tabpanel");
    panel.setAttribute("aria-labelledby", button.id);
    panel.tabIndex = 0;
    sections[0].before(panel);
    for (const section of sections) panel.append(section);
    button.setAttribute("role", "tab");
    button.setAttribute("aria-controls", panel.id);
    panels.set(id, panel);
  }
  const select = (id, focus = false) => {
    for (const button of buttons) {
      const active = button.dataset.tab === id;
      button.setAttribute("aria-selected", String(active));
      button.tabIndex = active ? 0 : -1;
      const panel = panels.get(button.dataset.tab);
      if (panel) panel.hidden = !active;
      if (active && focus) button.focus();
    }
  };
  navigation.hidden = false;
  navigation.setAttribute("role", "tablist");
  select(panels.has(navigation.dataset.activeTab) ? navigation.dataset.activeTab : buttons[0].dataset.tab);
  for (const button of buttons) {
    button.addEventListener("click", () => select(button.dataset.tab));
    button.addEventListener("keydown", event => {
      const index = buttons.indexOf(button);
      let next;
      if (event.key === "ArrowRight") next = (index + 1) % buttons.length;
      else if (event.key === "ArrowLeft") next = (index + buttons.length - 1) % buttons.length;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = buttons.length - 1;
      else return;
      event.preventDefault();
      select(buttons[next].dataset.tab, true);
    });
  }
  const reveal = target => {
    for (const [id, panel] of panels) if (panel.contains(target)) select(id);
  };
  let revealingInvalid = false;
  container.addEventListener("invalid", event => {
    if (revealingInvalid) return;
    revealingInvalid = true;
    reveal(event.target);
    setTimeout(() => { revealingInvalid = false; }, 0);
  }, true);
  container.addEventListener("click", event => {
    const link = event.target.closest("[data-error-summary] a[href^='#']");
    if (!link) return;
    const target = document.getElementById(link.hash.slice(1));
    if (target) {
      reveal(target);
      event.preventDefault();
      target.focus();
      target.scrollIntoView({block: "center"});
    }
  });
}

for (const field of document.querySelectorAll(".field")) {
  const checkbox = field.querySelector(".change-control input[type='checkbox']");
  const input = [...field.querySelectorAll("input,textarea,select")].find(element => element !== checkbox);
  if (!checkbox || !input) continue;
  const update = () => { input.disabled = !checkbox.checked; };
  checkbox.addEventListener("change", update);
  update();
}

const summary = document.querySelector("[data-error-summary]");
if (summary) summary.focus();

let picker = null;
for (const link of document.querySelectorAll("[data-reference-picker]")) {
  link.addEventListener("click", event => {
    if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    const popup = window.open(link.href, "skeleton-reference", "popup,width=760,height=650");
    if (!popup) return; // Normal navigation remains the fallback.
    picker = {popup, tableKey: link.dataset.tableKey, fieldName: link.dataset.fieldName,
              record: link.dataset.record || null, inputId: link.dataset.inputId};
    event.preventDefault();
  });
}
window.addEventListener("message", event => {
  if (!picker || event.origin !== location.origin || event.source !== picker.popup) return;
  const message = event.data;
  if (!message || message.type !== "skeleton-reference" || message.table_key !== picker.tableKey ||
      message.field_name !== picker.fieldName || message.record !== picker.record ||
      typeof message.sysid !== "string" || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(message.sysid)) return;
  const input = document.getElementById(picker.inputId);
  if (!input) return;
  const checkbox = input.closest(".field")?.querySelector(".change-control input[type='checkbox']");
  if (checkbox) { checkbox.checked = true; checkbox.dispatchEvent(new Event("change", {bubbles: true})); }
  input.value = message.sysid;
  input.dispatchEvent(new Event("change", {bubbles: true}));
  input.focus();
  picker.popup.close();
  picker = null;
});

// Link tabs (e.g. update steps) scroll horizontally on narrow screens: keep the current one visible.
for (const current of document.querySelectorAll(".link-tabs [aria-current]")) {
  const strip = current.parentElement;
  strip.scrollLeft = Math.max(0, current.offsetLeft - strip.offsetLeft - 16);
}

// Web push opt-in (#56): the section stays hidden without JavaScript or browser support.
for (const section of document.querySelectorAll("[data-push-optin]")) {
  if (!("serviceWorker" in navigator) || !("PushManager" in window) || !window.isSecureContext) continue;
  const enable = section.querySelector("[data-push-enable]");
  const disable = section.querySelector("[data-push-disable]");
  const status = section.querySelector("[data-push-status]");
  const token = document.querySelector("input[name=csrfmiddlewaretoken]")?.value || "";
  const post = (url, body) => fetch(url, {
    method: "POST",
    credentials: "same-origin",
    headers: {"Content-Type": "application/json", "X-CSRFToken": token},
    body: JSON.stringify(body),
  });
  const key = section.dataset.pushKey.replace(/-/g, "+").replace(/_/g, "/");
  const applicationServerKey = Uint8Array.from(atob(key + "=".repeat((4 - key.length % 4) % 4)), c => c.charCodeAt(0));
  const show = subscribed => {
    enable.hidden = subscribed;
    disable.hidden = !subscribed;
  };
  section.hidden = false;
  navigator.serviceWorker.register(section.dataset.worker, {scope: "/"}).then(registration => {
    registration.pushManager.getSubscription().then(current => show(Boolean(current)));
    enable.addEventListener("click", async () => {
      try {
        const subscription = await registration.pushManager.subscribe({userVisibleOnly: true, applicationServerKey});
        const response = await post(section.dataset.subscribeUrl, subscription.toJSON());
        if (!response.ok) throw new Error(String(response.status));
        status.textContent = "Push ativado neste navegador.";
        show(true);
      } catch (error) {
        status.textContent = "Não foi possível ativar o push neste navegador.";
      }
    });
    disable.addEventListener("click", async () => {
      const current = await registration.pushManager.getSubscription();
      if (current) {
        await post(section.dataset.unsubscribeUrl, {endpoint: current.endpoint});
        await current.unsubscribe();
      }
      status.textContent = "Push desativado neste navegador.";
      show(false);
    });
  });
}

// Colour swatches: applied through the CSSOM, which a strict style-src allows (inline style attributes are not, #266).
for (const swatch of document.querySelectorAll("[data-swatch]")) {
  swatch.style.background = swatch.dataset.swatch;
}

// Double-submit guard (#474): a second submit of the same POST form is ignored until the page
// changes. Buttons are marked aria-disabled, never disabled: a disabled button would not send its
// name/value (forms with several actions). A form that answers with a file on the same page opts
// out with data-allow-resubmit.
for (const form of document.querySelectorAll("form")) {
  if ((form.getAttribute("method") || "get").toLowerCase() !== "post") continue;
  if (form.hasAttribute("data-allow-resubmit")) continue;
  form.addEventListener("submit", event => {
    if (event.defaultPrevented) return;
    if (form.dataset.submitted === "1") {
      event.preventDefault();
      return;
    }
    form.dataset.submitted = "1";
    // Disable after the browser has collected the form data, or the clicked button's value is lost.
    setTimeout(() => {
      for (const button of form.querySelectorAll("button[type=submit], input[type=submit]")) {
        button.setAttribute("aria-disabled", "true");
        button.classList.add("is-submitting");
      }
    }, 0);
  });
}
window.addEventListener("pageshow", () => {
  for (const form of document.querySelectorAll("form[data-submitted]")) {
    delete form.dataset.submitted;
    for (const button of form.querySelectorAll("[aria-disabled=true].is-submitting")) {
      button.removeAttribute("aria-disabled");
      button.classList.remove("is-submitting");
    }
  }
});

// Organization identifier availability while the person types (#765). Only guidance: the server
// checks again when the form is saved, and without JavaScript the form works the same. One request
// per pause in typing; a newer request aborts the previous one.
for (const input of document.querySelectorAll("input[data-slug-check]")) {
  const status = document.createElement("p");
  status.id = input.id + "_availability";
  status.className = "helptext";
  status.setAttribute("aria-live", "polite");
  const field = input.closest(".form-field");
  const help = field ? field.querySelector(".helptext") : null;
  (help || input).after(status);
  input.setAttribute("aria-describedby", [input.getAttribute("aria-describedby"), status.id].filter(Boolean).join(" "));
  const refused = new Set(["taken", "format", "reserved"]);
  const show = (state, text) => {
    status.dataset.state = state;
    status.textContent = text;
    status.classList.toggle("text-fg-success", state === "available");
    status.classList.toggle("text-fg-danger", refused.has(state));
    if (refused.has(state)) input.setAttribute("aria-invalid", "true");
    else if (!field || !field.querySelector(".errorlist")) input.removeAttribute("aria-invalid");
  };
  let timer = 0;
  let controller = null;
  let last = input.value.trim();
  const check = async () => {
    const value = input.value.trim();
    if (value === last) return;
    last = value;
    if (controller) controller.abort();
    controller = null;
    if (!value) {
      show("", "");
      return;
    }
    controller = new AbortController();
    show("checking", input.dataset.slugChecking || "");
    try {
      const url = new URL(input.dataset.slugCheck, window.location.origin);
      url.searchParams.set("slug", value);
      const response = await fetch(url, {
        headers: { Accept: "application/json" },
        credentials: "same-origin",
        signal: controller.signal,
      });
      const body = await response.json();
      if (value !== input.value.trim()) return;
      show(response.ok ? body.status : "", response.ok || response.status === 429 ? body.message : "");
    } catch (error) {
      if (error.name !== "AbortError") show("", "");
    }
  };
  input.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(check, 350);
  });
  input.addEventListener("change", () => {
    clearTimeout(timer);
    check();
  });
}
