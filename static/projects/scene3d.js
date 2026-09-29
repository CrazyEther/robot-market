(() => {
  "use strict";

  window.createPlayback3D = function createPlayback3D(panel, scene) {
    const canvas = panel.querySelector("canvas[data-scene3d]");
    const errorNode = panel.querySelector("[data-3d-error]");
    const resetButton = panel.querySelector("[data-3d-reset]");
    if (!canvas || !canvas.getContext || !scene || !Array.isArray(scene.floors)
        || scene.floors.length === 0) {
      if (errorNode) {
        errorNode.hidden = false;
        errorNode.textContent = "Для 3D-просмотра нет данных о схеме. Доступна схема 2D.";
      }
      return {setState() {}, resize() {}};
    }

    const context = canvas.getContext("2d");
    if (!context) {
      if (errorNode) {
        errorNode.hidden = false;
        errorNode.textContent = "Браузер не поддерживает canvas 2D. Используйте схему 2D.";
      }
      return {setState() {}, resize() {}};
    }

    const floors = scene.floors.map((floor, index) => ({
      ...floor,
      level: index,
      nodes: Array.isArray(floor.nodes) ? floor.nodes : [],
      edges: Array.isArray(floor.edges) ? floor.edges : [],
    }));
    const levelByLabel = new Map(floors.map((floor) => [floor.label, floor.level]));
    const floorByLabel = new Map(floors.map((floor) => [floor.label, floor]));
    const transitions = Array.isArray(scene.transitions) ? scene.transitions : [];

    let yaw = -0.62;
    let pitch = 0.48;
    let zoom = 1;
    let markers = [];
    let width = 0;
    let height = 0;
    let pixelRatio = 1;
    let dragging = false;
    let lastX = 0;
    let lastY = 0;

    function world(x, y, level) {
      return {
        x: (Number(x) - 300) / 300,
        // Keep both axes in the same schematic units; no aspect-ratio distortion.
        depth: (200 - Number(y)) / 300,
        elevation: level * 0.55,
      };
    }

    function project(point) {
      const cosYaw = Math.cos(yaw);
      const sinYaw = Math.sin(yaw);
      const horizontal = point.x * cosYaw + point.depth * sinYaw;
      const depth = -point.x * sinYaw + point.depth * cosYaw;
      const cosPitch = Math.cos(pitch);
      const sinPitch = Math.sin(pitch);
      const vertical = (point.elevation - (floors.length - 1) * 0.275) * cosPitch
        - depth * sinPitch;
      const cameraDepth = depth * cosPitch
        + (point.elevation - (floors.length - 1) * 0.275) * sinPitch;
      const perspective = 3.8 / Math.max(2.1, 3.8 - cameraDepth);
      const scale = Math.min(width, height) * 0.34 * zoom * perspective;
      return {x: width / 2 + horizontal * scale,
        y: height / 2 - vertical * scale, scale, cameraDepth};
    }

    function addLine(queue, a, b, color, lineWidth, dash = []) {
      const start = project(a);
      const end = project(b);
      queue.push({depth: (start.cameraDepth + end.cameraDepth) / 2, paint() {
        context.beginPath();
        context.moveTo(start.x, start.y);
        context.lineTo(end.x, end.y);
        context.strokeStyle = color;
        context.lineWidth = lineWidth;
        context.setLineDash(dash);
        context.stroke();
        context.setLineDash([]);
      }});
    }

    function addPolygon(queue, points, fill, stroke, lineWidth = 1) {
      const projected = points.map(project);
      const depth = projected.reduce((sum, point) => sum + point.cameraDepth, 0)
        / projected.length;
      queue.push({depth, paint() {
        context.beginPath();
        context.moveTo(projected[0].x, projected[0].y);
        for (const point of projected.slice(1)) context.lineTo(point.x, point.y);
        context.closePath();
        if (fill) {
          context.fillStyle = fill;
          context.fill();
        }
        if (stroke) {
          context.strokeStyle = stroke;
          context.lineWidth = lineWidth;
          context.stroke();
        }
      }});
    }

    function addText(queue, point, text, color, align = "left", baseline = "middle",
        weight = "400") {
      const projected = project(point);
      queue.push({depth: projected.cameraDepth + 0.0001, paint() {
        context.font = `${weight} 12px system-ui, sans-serif`;
        context.textAlign = align;
        context.textBaseline = baseline;
        context.lineWidth = 3;
        context.strokeStyle = "#101713";
        context.strokeText(String(text ?? ""), projected.x, projected.y);
        context.fillStyle = color;
        context.fillText(String(text ?? ""), projected.x, projected.y);
      }});
    }

    function addNode(queue, floor, node) {
      const point = world(node.x, node.y, floor.level);
      const projected = project(point);
      const radius = Math.max(3, Math.min(6, projected.scale * 0.025));
      queue.push({depth: projected.cameraDepth, paint() {
        context.beginPath();
        context.arc(projected.x, projected.y, radius, 0, Math.PI * 2);
        context.fillStyle = node.on_route ? "#c7ed83" : "#91ad95";
        context.fill();
      }});
      addText(queue, {...point, x: point.x + 0.025}, node.label, "#f0f5eb");
    }

    function addRobot(queue, marker) {
      const level = levelByLabel.get(marker.floor);
      if (level === undefined) return;
      // The box base is anchored to the same 2D playback position on its current floor.
      const center = world(marker.x, marker.y, level);
      const halfX = 0.075;
      const halfDepth = 0.075;
      const visualHeight = 0.16;
      const x0 = center.x - halfX;
      const x1 = center.x + halfX;
      const d0 = center.depth - halfDepth;
      const d1 = center.depth + halfDepth;
      const z0 = center.elevation;
      const z1 = z0 + visualHeight;
      const waiting = marker.kind === "resource_wait";
      const v = [
        {x: x0, depth: d0, elevation: z0}, {x: x1, depth: d0, elevation: z0},
        {x: x1, depth: d1, elevation: z0}, {x: x0, depth: d1, elevation: z0},
        {x: x0, depth: d0, elevation: z1}, {x: x1, depth: d0, elevation: z1},
        {x: x1, depth: d1, elevation: z1}, {x: x0, depth: d1, elevation: z1},
      ];
      addPolygon(queue, [v[0], v[1], v[5], v[4]], waiting ? "#edbd69" : "#b9dc88", "#263927", 1.2);
      addPolygon(queue, [v[1], v[2], v[6], v[5]], waiting ? "#d99b40" : "#91b866", "#263927", 1.2);
      addPolygon(queue, [v[2], v[3], v[7], v[6]], waiting ? "#b77c30" : "#75994f", "#263927", 1.2);
      addPolygon(queue, [v[3], v[0], v[4], v[7]], waiting ? "#e1ad5b" : "#9fc875", "#263927", 1.2);
      addPolygon(queue, [v[4], v[5], v[6], v[7]], waiting ? "#ffdfa2" : "#e1f5b8", "#263927", 1.2);
      context.font = "12px system-ui, sans-serif";
      const label = `${String(marker.robotId)} · ${String(marker.sourceRow)}${waiting ? " · ожидание" : ""}`;
      addText(queue, {...center, x: center.x + halfX + 0.025,
        elevation: z1 + 0.015}, label, "#ffffff", "left", "bottom", "700");
    }

    function addFloor(queue, planeQueue, floor) {
      const z = floor.level * 0.55;
      const minX = -1.08;
      const maxX = 1.08;
      const minDepth = -0.72;
      const maxDepth = 0.72;
      addPolygon(planeQueue, [
        {x: minX, depth: minDepth, elevation: z},
        {x: maxX, depth: minDepth, elevation: z},
        {x: maxX, depth: maxDepth, elevation: z},
        {x: minX, depth: maxDepth, elevation: z},
      ], "rgba(96, 125, 101, 0.11)", "#506653", 1);
      for (let x = -1000; x <= 1000; x += 200) {
        const xUnit = x / 1000;
        addLine(queue, {x: xUnit, depth: minDepth, elevation: z},
          {x: xUnit, depth: maxDepth, elevation: z}, "rgba(157, 184, 153, 0.22)", 0.7);
      }
      for (let d = -600; d <= 600; d += 200) {
        const depth = d / 1000;
        addLine(queue, {x: minX, depth, elevation: z},
          {x: maxX, depth, elevation: z}, "rgba(157, 184, 153, 0.22)", 0.7);
      }
    }

    function transitionPoint(label, floorLabel, nodeId) {
      const floor = floorByLabel.get(floorLabel || "Общий уровень");
      if (!floor) return null;
      const node = floor.nodes.find((item) => nodeId && item.id === nodeId)
        || floor.nodes.find((item) => item.label === label);
      return node ? world(node.x, node.y, floor.level) : null;
    }

    function addTransition(queue, transition) {
      const from = transitionPoint(transition.from_label, transition.from_floor,
        transition.from_id);
      const to = transitionPoint(transition.to_label, transition.to_floor,
        transition.to_id);
      if (!from || !to) return;
      addLine(queue, from, to, "#83cbd2", 2.4, [5, 4]);
      for (const point of [from, to]) {
        const size = 4;
        addPolygon(queue, [
          {x: point.x - size / 300, depth: point.depth, elevation: point.elevation},
          {x: point.x, depth: point.depth - size / 300, elevation: point.elevation},
          {x: point.x + size / 300, depth: point.depth, elevation: point.elevation},
          {x: point.x, depth: point.depth + size / 300, elevation: point.elevation},
        ], "#83cbd2", "#173a3e", 1);
      }
    }

    function render() {
      if (!width || !height) return;
      context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
      context.clearRect(0, 0, width, height);
      context.fillStyle = "#101713";
      context.fillRect(0, 0, width, height);
      const queue = [];
      const planeQueue = [];
      for (const floor of floors) {
        addFloor(queue, planeQueue, floor);
        for (const edge of floor.edges) {
          const points = edge.path || [edge.start, edge.end];
          for (let i = 1; i < points.length; i++) {
            const start = world(points[i - 1].x, points[i - 1].y, floor.level);
            const end = world(points[i].x, points[i].y, floor.level);
            addLine(queue, start, end, edge.on_route ? "#a8c889" : "#536b59",
              edge.on_route ? 3 : 1.5);
          }
        }
      }
      for (const transition of transitions) addTransition(queue, transition);
      for (const floor of floors) {
        for (const node of floor.nodes) addNode(queue, floor, node);
      }
      for (const marker of markers) addRobot(queue, marker);
      floors.forEach((floor) => {
        const anchor = {x: 1.02, depth: 0.67, elevation: floor.level * 0.55};
        addText(queue, anchor, floor.label ?? "Этаж", "#d7e6d2", "right", "top");
      });
      planeQueue.sort((a, b) => a.depth - b.depth);
      for (const plane of planeQueue) plane.paint();
      queue.sort((a, b) => a.depth - b.depth);
      for (const item of queue) item.paint();
    }

    function resize() {
      const bounds = canvas.getBoundingClientRect();
      if (!bounds.width || !bounds.height) return;
      pixelRatio = Math.min(window.devicePixelRatio || 1, 2);
      width = bounds.width;
      height = bounds.height;
      canvas.width = Math.round(width * pixelRatio);
      canvas.height = Math.round(height * pixelRatio);
      render();
    }

    function reset() {
      yaw = -0.62;
      pitch = 0.48;
      zoom = 1;
      render();
    }

    canvas.addEventListener("pointerdown", (event) => {
      dragging = true;
      lastX = event.clientX;
      lastY = event.clientY;
      canvas.setPointerCapture(event.pointerId);
    });
    canvas.addEventListener("pointermove", (event) => {
      if (!dragging) return;
      yaw += (event.clientX - lastX) * 0.008;
      pitch = Math.max(-0.12, Math.min(1.25, pitch + (event.clientY - lastY) * 0.006));
      lastX = event.clientX;
      lastY = event.clientY;
      render();
    });
    canvas.addEventListener("pointerup", () => { dragging = false; });
    canvas.addEventListener("pointercancel", () => { dragging = false; });
    canvas.addEventListener("wheel", (event) => {
      event.preventDefault();
      zoom = Math.max(0.55, Math.min(2.8, zoom * (event.deltaY < 0 ? 1.1 : 0.9)));
      render();
    }, {passive: false});
    canvas.addEventListener("keydown", (event) => {
      const key = event.key;
      if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "+", "=", "-", "0"].includes(key)) {
        event.preventDefault();
      }
      if (key === "ArrowLeft") yaw -= 0.1;
      else if (key === "ArrowRight") yaw += 0.1;
      else if (key === "ArrowUp") pitch = Math.min(1.25, pitch + 0.08);
      else if (key === "ArrowDown") pitch = Math.max(-0.12, pitch - 0.08);
      else if (key === "+" || key === "=") zoom = Math.min(2.8, zoom * 1.12);
      else if (key === "-") zoom = Math.max(0.55, zoom / 1.12);
      else if (key === "0") reset();
      render();
    });
    if (resetButton) resetButton.addEventListener("click", reset);
    if (typeof ResizeObserver === "function") {
      const observer = new ResizeObserver(resize);
      observer.observe(canvas);
    } else {
      window.addEventListener("resize", resize);
    }
    resize();
    return {
      setState(state) {
        markers = state && Array.isArray(state.markers) ? state.markers : [];
        render();
      },
      resize,
    };
  };
})();

// WebGL2 owns a separate canvas. Keeping the established Canvas renderer as a
// fallback means restricted browsers still have a working spatial schematic.
(() => {
  "use strict";
  const createFallback = window.createPlayback3D;
  window.createPlayback3D = function createPlayback3D(panel, scene) {
    const fallback = createFallback(panel, scene);
    const original = panel.querySelector("canvas[data-scene3d]");
    if (!original || !scene || !Array.isArray(scene.floors) || !scene.floors.length) {
      return fallback;
    }
    const surface = document.createElement("canvas");
    surface.tabIndex = 0;
    surface.setAttribute("aria-label", "Пространственное положение роботов и маршрута");
    surface.setAttribute("data-webgl-scene", "");
    let gl = null;
    let engine = null;
    let lastState = {markers: []};
    let failed = false;
    let requested = false;
    const errorNode = panel.querySelector("[data-3d-error]");
    function showFallback() {
      if (failed) return;
      failed = true;
      if (engine) engine.dispose();
      engine = null;
      surface.remove();
      original.style.display = "";
      fallback.setState(lastState);
      fallback.resize();
      if (errorNode) {
        errorNode.hidden = false;
        errorNode.textContent = "Аппаратный 3D-просмотр недоступен. Работает схематическое отображение.";
      }
    }
    surface.addEventListener("webglcontextlost", (event) => {
      event.preventDefault();
      showFallback();
    });
    function activate() {
      if (requested || failed || panel.hidden) return;
      requested = true;
      try {
        gl = surface.getContext("webgl2", {antialias: true});
      } catch {
        gl = null;
      }
      if (!gl) { showFallback(); return; }
      import("./webgl3d.js").then(({createWebGL3D}) => {
        if (failed) return;
        const instance = createWebGL3D(surface, gl, scene, panel.dataset.facility || "warehouse");
        original.after(surface);
        original.style.display = "none";
        engine = instance;
        engine.resize();
        engine.setState(lastState);
      }).catch(showFallback);
    }
    activate();
    return {
      setState(state) {
        lastState = state;
        if (engine) engine.setState(state);
        else fallback.setState(state);
      },
      resize() {
        activate();
        if (engine) engine.resize();
        else fallback.resize();
      },
      // Browser smoke can verify engine initialization without reading UI internals.
      get isWebGL() { return Boolean(engine); },
      robotAt(key) { return engine?.robotAt(key) ?? null; },
      robotYawAt(key) { return engine?.robotYawAt(key) ?? null; },
    };
  };
})();
