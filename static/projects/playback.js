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
  const viewButtons = [...root.querySelectorAll("[data-view]")];
  const view2d = root.querySelector("[data-view-panel='2d']");
  const view3d = root.querySelector("[data-view-panel='3d']");
  const sceneNode = document.getElementById("playback-scene");
  const layers = new Map([...root.querySelectorAll("[data-robot-layer]")]
    .map((layer) => [layer.dataset.floor, layer]));
  const motion = payload.motion && Array.isArray(payload.motion.stages)
    && Array.isArray(payload.motion.cycles) ? payload.motion : null;
  const resourceStatus = root.querySelector("[data-resource-status]");
  const resourceReservations = Array.isArray(payload.resource_reservations)
    ? payload.resource_reservations : [];
  const routeResources = payload.resource_plan && Array.isArray(payload.resource_plan.resources)
    ? payload.resource_plan.resources : [];
  let scene = null;
  try {
    scene = sceneNode ? JSON.parse(sceneNode.textContent) : null;
  } catch {
    scene = null;
  }
  const tokens = new Map();
  const motionMarkers = new Map();
  let position = 0;
  let clock = Number(payload.initial_at_s);
  let frame = null;
  let lastFrame = null;

  function positionOnPath(path, fraction) {
    const points = Array.isArray(path) && path.length >= 2 ? path : [];
    if (!points.length) return null;
    const segments = [];
    let total = 0;
    for (let index = 1; index < points.length; index++) {
      const start = points[index - 1];
      const end = points[index];
      const length = Math.hypot(Number(end.x) - Number(start.x),
        Number(end.y) - Number(start.y));
      segments.push({start, end, length});
      total += length;
    }
    if (!total) return {x: Number(points[0].x), y: Number(points[0].y),
      headingX: Number(points[1].x), headingY: Number(points[1].y)};
    let left = Math.max(0, Math.min(1, fraction)) * total;
    for (const segment of segments) {
      if (left <= segment.length) {
        const portion = segment.length ? left / segment.length : 0;
        return {
          x: Number(segment.start.x) + (Number(segment.end.x) - Number(segment.start.x)) * portion,
          y: Number(segment.start.y) + (Number(segment.end.y) - Number(segment.start.y)) * portion,
          headingX: Number(segment.end.x), headingY: Number(segment.end.y),
        };
      }
      left -= segment.length;
    }
    const final = points[points.length - 1];
    return {x: Number(final.x), y: Number(final.y),
      headingX: Number(final.x), headingY: Number(final.y)};
  }

  function stop() {
    if (frame !== null) window.cancelAnimationFrame(frame);
    frame = null;
    lastFrame = null;
    play.textContent = "▶";
    play.setAttribute("aria-label", "Воспроизвести события");
  }

  function getMotionMarkers(busy) {
    motionMarkers.clear();
    if (!motion || (!motion.stages.length && !motion.cycles.some(
      (cycle) => Array.isArray(cycle.stages) && cycle.stages.length))) return motionMarkers;
    const visible = new Set();
    for (const cycle of motion.cycles) {
      if (busy[cycle.robot_id] !== cycle.source_row) continue;
      const relative = clock - Number(cycle.start_s);
      if (relative < 0 || relative >= Number(cycle.end_s) - Number(cycle.start_s)) continue;
      const cycleStages = Array.isArray(cycle.stages) ? cycle.stages : motion.stages;
      const stage = cycleStages.find((item) => relative >= Number(item.start_s)
        && relative < Number(item.end_s));
      if (!stage) continue;
      const origin = stage.from;
      const target = stage.to;
      const layer = layers.get(origin.floor);
      if (!layer) continue;
      const fraction = stage.kind === "travel"
        ? (relative - Number(stage.start_s)) / (Number(stage.end_s) - Number(stage.start_s)) : 0;
      const point = stage.kind === "travel" ? positionOnPath(
        stage.path || [origin, target], fraction) : null;
      const x = point ? point.x : Number(origin.x);
      const y = point ? point.y : Number(origin.y);
      const key = `${cycle.robot_id}:${cycle.source_row}`;
      visible.add(key);
      motionMarkers.set(key, {
        key, robotId: cycle.robot_id, sourceRow: cycle.source_row,
        floor: origin.floor, x, y, targetFloor: target.floor,
        targetX: point ? point.headingX : Number(target.x),
        targetY: point ? point.headingY : Number(target.y),
        kind: stage.kind, resourceId: stage.resource_id || null,
        fraction: stage.kind === "elevator"
          ? (relative - Number(stage.start_s)) / (Number(stage.end_s) - Number(stage.start_s))
          : fraction,
      });
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
      token.circle.classList.toggle("is-waiting", stage.kind === "resource_wait");
      token.label.setAttribute("x", String(x));
      token.label.setAttribute("y", String(y));
      token.group.querySelector("title").textContent = stage.kind === "resource_wait"
        ? `${cycle.robot_id} · ожидание участка ${stage.resource_id || "маршрута"}`
        : `${cycle.robot_id} · задание ${cycle.source_row}`;
    }
    for (const [key, token] of tokens) {
      if (!visible.has(key)) {
        token.group.remove();
        tokens.delete(key);
      }
    }
    return motionMarkers;
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
    const markers = getMotionMarkers(busy);
    const counts = new Map();
    for (const claim of resourceReservations) {
      if (Number(claim.start_s) <= clock && clock < Number(claim.end_s)) {
        counts.set(claim.resource_id, (counts.get(claim.resource_id) || 0) + 1);
      }
    }
    if (resourceStatus) {
      const activeResources = routeResources.filter((resource) => counts.has(resource.id))
        .map((resource) => `${resource.id}: ${counts.get(resource.id)} из ${resource.capacity}`);
      const waiting = [...markers.values()].filter((marker) => marker.kind === "resource_wait");
      resourceStatus.textContent = `Заняты участки: ${activeResources.join("; ") || "нет"}. `
        + `Ожидают освобождения: ${waiting.length} роботов.`;
    }
    if (window.playback3d && window.playback3d.setState) {
      window.playback3d.setState({clock, markers: [...markers.values()],
        occupiedResources: Object.fromEntries(counts)});
    }
  }

  function seek(index) {
    stop();
    position = Math.max(0, Math.min(payload.events.length, index));
    clock = Number(position ? payload.events[position - 1].at_s : payload.initial_at_s);
    render();
  }
  function setView(view) {
    const use3d = view === "3d";
    if (view2d) view2d.hidden = use3d;
    if (view3d) view3d.hidden = !use3d;
    for (const button of viewButtons) {
      button.setAttribute("aria-pressed", String(button.dataset.view === view));
    }
    if (use3d && window.playback3d && window.playback3d.resize) {
      window.playback3d.resize();
    }
  }
  for (const button of viewButtons) {
    button.addEventListener("click", () => setView(button.dataset.view));
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
  if (view3d) {
    const errorNode = view3d.querySelector("[data-3d-error]");
    if (typeof window.createPlayback3D === "function") {
      try {
        window.playback3d = window.createPlayback3D(view3d, scene);
      } catch {
        if (errorNode) {
          errorNode.hidden = false;
          errorNode.textContent = "3D-просмотр недоступен. Используйте схему 2D.";
        }
      }
    } else if (errorNode) {
      errorNode.hidden = false;
      errorNode.textContent = "Модуль 3D-просмотра не загрузился. Используйте схему 2D.";
    }
  }
  window.addEventListener("resize", () => {
    if (window.playback3d && window.playback3d.resize) window.playback3d.resize();
  });
  render();
})();
