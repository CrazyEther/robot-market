(() => {
  "use strict";
  const form = document.querySelector("[data-floor-plan-calibration]");
  const source = document.getElementById("floor-plan-options");
  if (!form || !source) return;
  const images = JSON.parse(source.textContent);
  const picker = form.querySelector("[data-plan-preview]");
  const details = form.querySelector("[data-plan-details]");
  const status = form.querySelector("[data-pick-status]");
  const select = form.elements.namedItem("plan_id");
  let anchor = "a";
  function update() {
    const plan = images.find((item) => item.id === select.value);
    picker.hidden = !plan;
    if (!plan) return;
    picker.src = plan.url;
    picker.alt = `${plan.floor}: ${plan.filename}`;
    details.textContent = `${plan.width} × ${plan.height} пикселей; источник: ${plan.source}`;
  }
  for (const button of form.querySelectorAll("[data-pick-anchor]")) {
    button.addEventListener("click", () => {
      anchor = button.dataset.pickAnchor;
      status.textContent = anchor === "a" ? "Выбрана точка A" : "Выбрана точка Б";
    });
  }
  picker.addEventListener("click", (event) => {
    if (!picker.naturalWidth || !picker.naturalHeight) return;
    const rect = picker.getBoundingClientRect();
    const x = Math.max(0, Math.min(picker.naturalWidth,
      (event.clientX - rect.left) * picker.naturalWidth / rect.width));
    const y = Math.max(0, Math.min(picker.naturalHeight,
      (event.clientY - rect.top) * picker.naturalHeight / rect.height));
    form.elements.namedItem(`pixel_${anchor}_x`).value = x.toFixed(2);
    form.elements.namedItem(`pixel_${anchor}_y`).value = y.toFixed(2);
    status.textContent = `${anchor === "a" ? "A" : "Б"}: ${x.toFixed(2)}, ${y.toFixed(2)} пикс.`;
  });
  select.addEventListener("change", update);
  if (images.length && !select.value) select.value = images[0].id;
  update();
})();
