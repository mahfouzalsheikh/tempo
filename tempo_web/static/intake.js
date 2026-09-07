"use strict";
document.querySelectorAll("[data-add-form]").forEach(button => {
  button.addEventListener("click", () => {
    const prefix = button.dataset.addForm;
    const total = document.getElementById(`id_${prefix}-TOTAL_FORMS`);
    const count = Number(total.value);
    if (count >= Number(button.dataset.maxForms)) return;
    const template = document.getElementById(`${prefix}-template`);
    const fragment = document.createElement("div");
    fragment.innerHTML = template.innerHTML.replaceAll("__prefix__", String(count));
    const criterionId = fragment.querySelector('[name$="-criterion_id"]');
    if (criterionId) criterionId.value = `AC-${(globalThis.crypto?.randomUUID?.() || `${Date.now()}-${count}`).slice(0, 20)}`;
    const firstField = fragment.querySelector("textarea, input:not([type=hidden])");
    document.getElementById(`${prefix}-forms`).append(...fragment.childNodes);
    total.value = count + 1;
    button.disabled = count + 1 >= Number(button.dataset.maxForms);
    firstField?.focus();
  });
});

function markDeleted(checkbox) {
  const card = checkbox.closest("fieldset");
  card.querySelectorAll("input, textarea, select").forEach(field => {
    if (field !== checkbox) field.disabled = checkbox.checked;
  });
  card.style.opacity = checkbox.checked ? "0.55" : "1";
}
document.addEventListener("change", event => {
  if (event.target.matches('input[name$="-DELETE"]')) markDeleted(event.target);
});
document.querySelectorAll('input[name$="-DELETE"]:checked').forEach(markDeleted);
