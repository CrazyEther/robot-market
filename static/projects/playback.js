(() => {
  "use strict";

  const root = document.querySelector("[data-playback]");
  const dataNode = document.getElementById("playback-data");
  if (!root || !dataNode) return;

  let payload;
  try {
    payload = JSON.parse(dataNode.textContent);
  } catch {
    return;
  }
  if (!payload || !Array.isArray(payload.events) || !Array.isArray(payload.availability)
      || payload.events.length !== payload.availability.length || !payload.initial) return;

  const slider = root.querySelector("[data-position]");
  const previous = root.querySelector("[data-step-back]");
  const next = root.querySelector("[data-step-next]");
  const play = root.querySelector("[data-toggle-play]");
  const speed = root.querySelector("[data-speed]");
  const current = root.querySelector("[data-current]");
  const queued = root.querySelector("[data-queued]");
  const active = root.querySelector("[data-active]");
  const completed = root.querySelector("[data-completed]");
  const delivered = root.querySelector("[data-delivered]");
  const available = root.querySelector("[data-available]");
  const charging = root.querySelector("[data-charging]");
  const inactive = root.querySelector("[data-inactive]");
  const layers = new Map([...root.querySelectorAll("[data-robot-layer]")]
    .map((layer) => [layer.dataset.floor, layer]));
  const motion = payload.motion && Array.isArray(payload.motion.stages)
    && Array.isArray(payload.motion.cycles) ? payload.motion : null;
  const tokens = new Map();
  let position = 0;
  let clock = Number(payload.initial_at_s);
  let frame = null;
  let lastFrame = null;

  function stop() {
    if (frame !== null) window.cancelAnimationFrame(frame);
    frame = null;
    lastFrame = null;
    play.textContent = "▶";
    play.setAttribute("aria-label", "Воспроизвести события");
  }

  function renderMotion(busy) {
    if (!motion || !motion.stages.length) return;
    const visible = new Set();
    for (const cycle of motion.cycles) {
      if (busy[cycle.robot_id] !== cycle.source_row) continue;
      const relative = clock - Number(cycle.start_s);
      if (relative < 0 || relative >= Number(cycle.end_s) - Number(cycle.start_s)) continue;
      const stage = motion.stages.find((item) => relative >= Number(item.start_s)
        && relative < Number(item.end_s));
      if (!stage) continue;
      const origin = stage.from;
      const target = stage.to;
      const layer = layers.get(origin.floor);
      if (!layer) continue;
      const fraction = stage.kind === "travel"
        ? (relative - Number(stage.start_s)) / (Number(stage.end_s) - Number(stage.start_s)) : 0;
      const x = Number(origin.x) + (Number(target.x) - Number(origin.x)) * fraction;
      const y = Number(origin.y) + (Number(target.y) - Number(origin.y)) * fraction;
      const key = `${cycle.robot_id}:${cycle.source_row}`;
      visible.add(key);
      let token = tokens.get(key);
      if (!token) {
        const group = document.createElementNS("http://www.w3.org/2000/svg", "g");
        const circle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
        const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
        const title = document.createElementNS("http://www.w3.org/2000/svg", "title");
        circle.setAttribute("r", "10");
        circle.setAttribute("class", "playback-robot");
        label.setAttribute("class", "playback-robot-label");
        label.setAttribute("dx", "14");
        label.setAttribute("dy", "-9");
        label.textContent = cycle.robot_id;
        title.textContent = `${cycle.robot_id} · задание ${cycle.source_row}`;
        group.append(circle, label, title);
        token = {group, circle, label};
        tokens.set(key, token);
      }
      if (token.group.parentNode !== layer) layer.append(token.group);
      token.circle.setAttribute("cx", String(x));
      token.circle.setAttribute("cy", String(y));
      token.label.setAttribute("x", String(x));
      token.label.setAttribute("y", String(y));
    }
    for (const [key, token] of tokens) {
      if (!visible.has(key)) {
        token.group.remove();
        tokens.delete(key);
      }
    }
  }

  function render() {
    let arrivals = payload.initial.arrivals;
    let started = payload.initial.started;
    let handedOff = payload.initial.delivered;
    let finished = payload.initial.completed;
    const busy = {...payload.initial.active};
    for (let index = 0; index < position; index += 1) {
      const event = payload.events[index];
      if (event.type === "arrival") arrivals += 1;
      if (event.type === "start") {
        started += 1;
        busy[event.robot_id] = event.source_row;
      }
      if (event.type === "complete") {
        finished += 1;
        delete busy[event.robot_id];
      }
      if (event.type === "handoff") handedOff += 1;
    }
    queued.textContent = String(arrivals - started);
    active.textContent = String(Object.keys(busy).length);
    completed.textContent = String(finished);
    if (delivered) delivered.textContent = String(handedOff);
    root.classList.toggle("has-active-route", Object.keys(busy).length > 0);
    const state = position === 0 ? payload.initial_availability
      : payload.availability[position - 1];
    available.textContent = String(state.available ?? 0);
    charging.textContent = String(state.charging ?? 0);
    inactive.textContent = String((state.maintenance ?? 0) + (state.downtime ?? 0));
    slider.value = String(position);
    previous.disabled = position === 0;
    next.disabled = position === payload.events.length;
    if (position === 0) {
      current.textContent = `${payload.initial_at_s} с · перед первым событием страницы`;
    } else {
      const event = payload.events[position - 1];
      const label = {arrival: "Поступило задание", start: "Начат цикл",
        handoff: "Груз передан", complete: "Завершён цикл",
        calendar_state: "Состояние робота", period_end: "Конец периода"}[event.type] || event.type;
      const detail = event.type === "calendar_state"
        ? ` · ${event.robot_id}: ${event.state_label} · строка календаря ${event.source_row}`
        : event.type === "period_end" ? "" : ` · строка задания ${event.source_row}`;
      current.textContent = `${event.at_s} с · ${label}${detail}`;
    }
    renderMotion(busy);
  }

  function seek(index) {
    stop();
    position = Math.max(0, Math.min(payload.events.length, index));
    clock = Number(position ? payload.events[position - 1].at_s : payload.initial_at_s);
    render();
  }
  previous.addEventListener("click", () => seek(position - 1));
  next.addEventListener("click", () => seek(position + 1));
  slider.addEventListener("input", () => seek(Number(slider.value)));
  function advance(timestamp) {
    if (lastFrame !== null) {
      clock += (timestamp - lastFrame) * Number(speed.value) / 1000;
    }
    lastFrame = timestamp;
    const end = Number(payload.events.at(-1).at_s);
    clock = Math.min(clock, end);
    while (position < payload.events.length && Number(payload.events[position].at_s) <= clock) {
      position += 1;
    }
    render();
    if (clock >= end) { stop(); return; }
    frame = window.requestAnimationFrame(advance);
  }
  play.addEventListener("click", () => {
    if (frame !== null) { stop(); return; }
    if (!payload.events.length) return;
    if (position === payload.events.length) {
      position = 0;
      clock = Number(payload.initial_at_s);
    }
    play.textContent = "Ⅱ";
    play.setAttribute("aria-label", "Остановить воспроизведение");
    frame = window.requestAnimationFrame(advance);
  });
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    play.disabled = true;
    play.title = "Используйте кнопки перехода между событиями";
  }
  render();
})();
