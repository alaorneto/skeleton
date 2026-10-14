(() => {
  'use strict';
  const node = document.getElementById('reference-picker-context');
  if (!node) return;
  const context = JSON.parse(node.textContent);
  const status = document.getElementById('reference-picker-status');
  for (const button of document.querySelectorAll('[data-reference-choice]')) {
    button.hidden = false;
    button.addEventListener('click', async () => {
      const sysid = button.dataset.referenceChoice;
      if (window.opener && !window.opener.closed) {
        window.opener.postMessage({ type: 'skeleton-reference', ...context, sysid }, window.location.origin);
        window.close();
      } else {
        try {
          await navigator.clipboard.writeText(sysid);
          status.textContent = 'UUID copiado. Cole no campo do formulário aberto.';
        } catch {
          status.textContent = `Copie o UUID: ${sysid}`;
        }
      }
    });
  }
})();
