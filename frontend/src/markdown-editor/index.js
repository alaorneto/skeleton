// O editor de texto formatado (#825): aprimora `textarea[data-markdown-editor]`.
//
// O <textarea> continua sendo o campo do formulário (sem JavaScript, é o que a pessoa usa). O
// editor o esconde, mostra a faixa de opções e as abas que o servidor renderizou no
// <template data-markdown-chrome> ao lado dele (com os textos já traduzidos), e grava nele o
// Markdown a cada edição. Alternar de modo sem editar não altera o valor.
import { COMMANDS, activeVisual, commandsFor, linkAtVisual, setLinkVisual } from "./commands.js";
import { isAllowedLink, validateFeatures } from "./dialect.js";
import { VisualMode } from "./visual-mode.js";

// O modo Markdown (CodeMirror) é carregado só quando alguém o abre: o modo visual é o padrão.
let sourceModule = null;
function loadSource() {
  sourceModule ??= import("./source-mode.js");
  return sourceModule;
}

const STORAGE_KEY = "skeleton.markdown-editor.mode";
const MODES = ["visual", "source"];
const MAC = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);

function storedMode() {
  try {
    const mode = window.localStorage.getItem(STORAGE_KEY);
    return MODES.includes(mode) ? mode : "visual";
  } catch {
    return "visual";
  }
}

function storeMode(mode) {
  try {
    window.localStorage.setItem(STORAGE_KEY, mode);
  } catch {
    // Sem armazenamento (modo privado, política): a escolha vale só nesta página.
  }
}

function rovingFocus(container, items, { onMove } = {}) {
  container.addEventListener("keydown", event => {
    const index = items.indexOf(document.activeElement);
    if (index < 0) return;
    let next;
    if (event.key === "ArrowRight") next = (index + 1) % items.length;
    else if (event.key === "ArrowLeft") next = (index + items.length - 1) % items.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = items.length - 1;
    else return;
    event.preventDefault();
    for (const item of items) item.tabIndex = -1;
    items[next].tabIndex = 0;
    items[next].focus();
    if (onMove) onMove(items[next]);
  });
}

class MarkdownEditor {
  constructor(textarea, chrome) {
    this.textarea = textarea;
    this.features = validateFeatures((textarea.dataset.markdownFeatures || "").split(/\s+/).filter(Boolean));
    this.commands = commandsFor(this.features);
    this.visual = new VisualMode({ features: this.features });
    this.root = chrome.content.firstElementChild.cloneNode(true);
    this.surface = this.root.querySelector("[data-markdown-surface]");
    this.error = this.root.querySelector("[data-markdown-error]");
    this.buttons = [...this.root.querySelectorAll("[data-command]")];
    this.tabs = [...this.root.querySelectorAll("[data-mode]")];
    this.dialog = this.root.querySelector("[data-markdown-link-dialog]");
    this.view = null;
    this.mode = null;
    this.source = null;
    this.pending = null;

    const id = textarea.id || `markdown-${Math.random().toString(36).slice(2)}`;
    textarea.id = id;
    this.surface.id = `${id}-markdown-surface`;
    this.error.id = `${id}-markdown-error`;
    const label = document.querySelector(`label[for="${CSS.escape(id)}"]`);
    if (label && !label.id) label.id = `${id}-label`;
    this.labelId = label ? label.id : null;
    for (const tab of this.tabs) {
      tab.id = `${id}-mode-${tab.dataset.mode}`;
      tab.setAttribute("aria-controls", this.surface.id);
    }
    if (textarea.rows) this.root.style.setProperty("--markdown-rows", String(textarea.rows));
    if (MAC) for (const button of this.buttons) button.title = button.title.replace(/Ctrl\+/g, "⌘").replace(/Alt\+/g, "⌥");

    textarea.before(this.root);
    this.root.append(textarea);
    textarea.classList.add("markdown-editor-source");
    textarea.tabIndex = -1;
    textarea.dataset.markdownEnhanced = "true";

    this.wireToolbar();
    this.wireTabs();
    this.wireDialog();
    this.wireTextarea();
    this.setMode(storedMode(), { focus: false });
  }

  // ------------------------------------------------------------- acessibilidade do conteúdo

  contentAttributes() {
    const describedBy = [this.textarea.getAttribute("aria-describedby"), this.error.hidden ? null : this.error.id].filter(Boolean).join(" ");
    const attributes = { role: "textbox", "aria-multiline": "true", spellcheck: "true", autocorrect: "on", autocapitalize: "sentences" };
    if (this.labelId) attributes["aria-labelledby"] = this.labelId;
    if (describedBy) attributes["aria-describedby"] = describedBy;
    if (this.textarea.required) attributes["aria-required"] = "true";
    if (this.textarea.getAttribute("aria-invalid") === "true" || !this.error.hidden) attributes["aria-invalid"] = "true";
    return attributes;
  }

  // ------------------------------------------------------------- modos

  setMode(mode, { focus = true } = {}) {
    if (mode === this.mode) return;
    if (mode === "source" && !this.source) {
      const ticket = (this.pending = {});
      loadSource().then(
        module => {
          this.source = module;
          if (this.pending === ticket) this.setMode(mode, { focus });
        },
        () => {
          // Sem o módulo (rede), o editor fica no modo visual.
          if (this.pending === ticket && !this.view) this.setMode("visual", { focus });
        },
      );
      return;
    }
    this.pending = null;
    const markdown = this.textarea.value;
    if (this.view) this.view.destroy();
    this.surface.replaceChildren();
    this.mode = mode;
    this.root.dataset.mode = mode;
    for (const tab of this.tabs) {
      const selected = tab.dataset.mode === mode;
      tab.setAttribute("aria-selected", String(selected));
      tab.tabIndex = selected ? 0 : -1;
      if (selected) this.surface.setAttribute("aria-labelledby", tab.id);
    }
    const onChange = value => this.write(value);
    const onUpdate = () => this.refreshToolbar();
    if (mode === "source") {
      this.view = this.source.createSourceEditor({
        parent: this.surface,
        doc: markdown,
        features: this.features,
        keys: this.sourceKeys(),
        contentAttributes: this.contentAttributes(),
        onChange,
        onUpdate,
      });
    } else {
      this.view = this.visual.mount({
        parent: this.surface,
        markdown,
        keys: this.visualKeys(),
        attributes: { ...this.contentAttributes(), class: "markdown" },
        onChange,
        onUpdate,
      });
    }
    this.refreshToolbar();
    if (focus) this.view.focus();
  }

  write(value) {
    if (this.textarea.value === value) return;
    this.textarea.value = value;
    this.textarea.dispatchEvent(new Event("input", { bubbles: true }));
    if (!this.error.hidden && value.trim()) this.showError("");
  }

  focus() {
    if (this.view) this.view.focus();
  }

  // ------------------------------------------------------------- comandos e atalhos

  run(id) {
    const command = COMMANDS[id];
    if (!command || !this.commands.some(([name]) => name === id)) return false;
    if (command.dialog) {
      this.openLinkDialog();
      return true;
    }
    let done;
    if (this.mode === "source") done = this.source.runSource(this.view, this.source.SOURCE_COMMANDS[id]);
    else done = command.visual(this.visual.schema)(this.view.state, this.view.dispatch);
    this.focus();
    return done;
  }

  sourceKeys() {
    const keys = [];
    for (const [id, command] of this.commands) {
      for (const key of command.keys) keys.push({ key, run: () => this.run(id), preventDefault: true });
      for (const [key, [name]] of Object.entries(command.levels || {})) {
        keys.push({ key, run: view => this.source.runSource(view, this.source.SOURCE_COMMANDS[name]), preventDefault: true });
      }
    }
    return keys;
  }

  visualKeys() {
    const keys = {};
    for (const [id, command] of this.commands) {
      for (const key of command.keys) keys[key] = () => this.run(id);
      for (const [key, [, visual]] of Object.entries(command.levels || {})) keys[key] = visual(this.visual.schema);
    }
    return keys;
  }

  refreshToolbar() {
    if (!this.view) return;
    // CodeMirror expõe `hasFocus` como propriedade; o ProseMirror, como método.
    const focused = typeof this.view.hasFocus === "function" ? this.view.hasFocus() : this.view.hasFocus;
    this.root.toggleAttribute("data-focused", focused);
    // Fora do editor nenhum botão aparece ligado: o estado é o da seleção de quem está editando.
    const active = !focused ? new Set() : this.mode === "source" ? this.source.activeSource(this.view.state) : activeVisual(this.view.state);
    for (const button of this.buttons) button.setAttribute("aria-pressed", String(active.has(button.dataset.command)));
  }

  wireToolbar() {
    const toolbar = this.root.querySelector("[role=toolbar]");
    this.buttons.forEach((button, index) => {
      button.tabIndex = index === 0 ? 0 : -1;
      // O botão não tira o foco do editor (a seleção continua lá).
      button.addEventListener("mousedown", event => event.preventDefault());
      button.addEventListener("click", () => this.run(button.dataset.command));
    });
    if (toolbar) rovingFocus(toolbar, this.buttons);
  }

  wireTabs() {
    const list = this.root.querySelector("[role=tablist]");
    for (const tab of this.tabs) {
      tab.addEventListener("click", () => {
        storeMode(tab.dataset.mode);
        this.setMode(tab.dataset.mode);
      });
    }
    if (list) {
      rovingFocus(list, this.tabs, {
        onMove: tab => {
          storeMode(tab.dataset.mode);
          this.setMode(tab.dataset.mode, { focus: false });
        },
      });
    }
  }

  // ------------------------------------------------------------- link

  wireDialog() {
    if (!this.dialog) return;
    const field = name => this.dialog.querySelector(`[data-link-${name}]`);
    this.linkText = field("text");
    this.linkHref = field("href");
    this.linkError = field("error");
    this.linkRemove = field("remove");
    field("apply").addEventListener("click", () => this.applyLink(this.linkHref.value.trim()));
    this.linkRemove.addEventListener("click", () => this.applyLink(""));
    field("cancel").addEventListener("click", () => this.dialog.close());
    for (const input of [this.linkText, this.linkHref]) {
      input.addEventListener("keydown", event => {
        if (event.key !== "Enter") return;
        event.preventDefault(); // Enter aqui não envia o formulário do registro.
        this.applyLink(this.linkHref.value.trim());
      });
    }
    this.linkHref.addEventListener("input", () => this.linkInvalid(false));
    this.dialog.addEventListener("close", () => this.focus());
  }

  linkInvalid(invalid) {
    this.linkError.hidden = !invalid;
    if (invalid) this.linkHref.setAttribute("aria-invalid", "true");
    else this.linkHref.removeAttribute("aria-invalid");
  }

  openLinkDialog() {
    if (!this.dialog) return;
    this.link = this.mode === "source" ? this.source.linkAtSource(this.view.state) : linkAtVisual(this.view.state);
    this.linkText.value = this.link.text;
    this.linkHref.value = this.link.href;
    this.linkRemove.hidden = !this.link.existing;
    this.linkInvalid(false);
    this.dialog.showModal();
    (this.link.text ? this.linkHref : this.linkText).focus();
  }

  applyLink(href) {
    if (href && !isAllowedLink(href)) {
      this.linkInvalid(true);
      this.linkHref.focus();
      return;
    }
    const text = this.linkText.value.replace(/\s+/g, " ").trim();
    if (this.mode === "source") this.view.dispatch(this.source.setLinkSource(this.view.state, this.link, href, text));
    else this.view.dispatch(setLinkVisual(this.view.state, this.link, href, text).scrollIntoView());
    this.dialog.close();
  }

  // ------------------------------------------------------------- formulário

  showError(message) {
    this.error.textContent = message;
    this.error.hidden = !message;
    this.root.toggleAttribute("data-invalid", Boolean(message) || this.textarea.getAttribute("aria-invalid") === "true");
    if (this.view) {
      const mode = this.mode;
      this.mode = null;
      this.setMode(mode, { focus: Boolean(message) });
    }
  }

  wireTextarea() {
    const textarea = this.textarea;
    this.root.toggleAttribute("data-invalid", textarea.getAttribute("aria-invalid") === "true");
    // O rótulo e a validação do navegador focam o textarea escondido: o foco vai para o editor.
    textarea.addEventListener("focus", () => this.focus());
    textarea.addEventListener("invalid", event => {
      event.preventDefault();
      this.showError(textarea.validationMessage);
      this.root.scrollIntoView({ block: "center" });
    });
    const form = textarea.form;
    if (form) {
      form.addEventListener("reset", () =>
        setTimeout(() => {
          const mode = this.mode;
          this.mode = null;
          this.setMode(mode, { focus: false });
        }),
      );
    }
  }
}

function enhance(root = document) {
  for (const textarea of root.querySelectorAll("textarea[data-markdown-editor]")) {
    if (textarea.dataset.markdownEnhanced || textarea.disabled || textarea.readOnly) continue;
    const chrome = textarea.parentElement.querySelector(`template[data-markdown-chrome="${CSS.escape(textarea.id)}"]`);
    if (!chrome) continue;
    new MarkdownEditor(textarea, chrome);
  }
}

enhance();
