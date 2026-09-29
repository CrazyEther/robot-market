/* Spatial rendering of an attested schematic playback scene.
 * The geometry of machines and floor separation are illustrative; event times
 * and robot positions are supplied by the immutable simulation playback.
 */
import * as THREE from "./vendor/three.module.js";

const FLOOR_GAP = 1.45;
const UNITS_PER_PIXEL = 1 / 95;

function floorPoint(x, y, level) {
  return new THREE.Vector3(
    (Number(x) - 300) * UNITS_PER_PIXEL,
    level * FLOOR_GAP + 0.10,
    (Number(y) - 200) * UNITS_PER_PIXEL,
  );
}

function makeLabel(text, options = {}) {
  const surface = document.createElement("canvas");
  const ctx = surface.getContext("2d");
  surface.width = 512;
  surface.height = 80;
  ctx.clearRect(0, 0, 512, 80);
  ctx.fillStyle = options.background || "rgba(15, 30, 27, 0.94)";
  ctx.fillRect(0, 0, 512, 80);
  ctx.fillStyle = options.color || "#f2f9e8";
  ctx.font = "600 32px system-ui, sans-serif";
  ctx.textBaseline = "middle";
  ctx.fillText(String(text).slice(0, 32), 18, 40, 475);
  const texture = new THREE.CanvasTexture(surface);
  texture.colorSpace = THREE.SRGBColorSpace;
  const material = new THREE.SpriteMaterial({map: texture, transparent: true,
    depthWrite: false, depthTest: false});
  const sprite = new THREE.Sprite(material);
  sprite.scale.set(options.width || 1.7, 0.265, 1);
  sprite.renderOrder = 7;
  return sprite;
}

function makeRobot(facility) {
  const robot = new THREE.Group();
  robot.scale.setScalar(1.32);
  const bodyMaterial = new THREE.MeshStandardMaterial({color: 0xa8e15e, roughness: 0.35,
    metalness: 0.25, emissive: 0x1f370c, emissiveIntensity: 0.3});
  const trim = new THREE.MeshStandardMaterial({color: 0x263f35, roughness: 0.65,
    metalness: 0.4});
  const glass = new THREE.MeshStandardMaterial({color: 0x5d8e9d, roughness: 0.23,
    metalness: 0.4});
  const body = new THREE.Mesh(new THREE.BoxGeometry(0.40, 0.18, 0.54), bodyMaterial);
  body.position.y = 0.23;
  robot.add(body);
  const top = new THREE.Mesh(new THREE.BoxGeometry(0.29, 0.12, 0.27), glass);
  top.position.set(0, 0.38, -0.06);
  robot.add(top);
  for (const side of [-1, 1]) {
    for (const front of [-0.17, 0.17]) {
      const wheel = new THREE.Mesh(new THREE.CylinderGeometry(0.079, 0.079, 0.065, 14), trim);
      wheel.rotation.z = Math.PI / 2;
      wheel.position.set(side * 0.22, 0.11, front);
      robot.add(wheel);
    }
  }
  const beacon = new THREE.Mesh(new THREE.SphereGeometry(0.042, 12, 10),
    new THREE.MeshStandardMaterial({color: 0xf3bd62, emissive: 0xf2a63a,
      emissiveIntensity: 0.7}));
  beacon.position.set(0, 0.47, -0.04);
  robot.add(beacon);
  if (facility === "warehouse") {
    // Symbolic pallet forks, independent of the selected manufacturer's dimensions.
    for (const side of [-0.14, 0.14]) {
      const fork = new THREE.Mesh(new THREE.BoxGeometry(0.05, 0.028, 0.35), trim);
      fork.position.set(side, 0.13, 0.38);
      robot.add(fork);
    }
  } else if (facility === "hospital") {
    const tray = new THREE.Mesh(new THREE.BoxGeometry(0.37, 0.035, 0.45), trim);
    tray.position.set(0, 0.51, 0.03);
    robot.add(tray);
  } else if (facility === "airport") {
    const hitch = new THREE.Mesh(new THREE.BoxGeometry(0.11, 0.06, 0.26), trim);
    hitch.position.set(0, 0.17, -0.36);
    robot.add(hitch);
  }
  robot.userData.bodyMaterial = bodyMaterial;
  robot.userData.label = makeLabel("Робот");
  robot.userData.label.position.set(0, 0.79, 0);
  robot.add(robot.userData.label);
  return robot;
}

function updateLabel(robot, value, waiting) {
  if (robot.userData.labelText === value) return;
  const prior = robot.userData.label;
  robot.remove(prior);
  prior.material.map.dispose();
  prior.material.dispose();
  const label = makeLabel(value, {
    color: waiting ? "#ffdaa0" : "#f2f9e8",
    background: waiting ? "rgba(75, 48, 20, 0.94)" : "rgba(15, 30, 27, 0.94)",
  });
  label.position.set(0, 0.79, 0);
  robot.add(label);
  robot.userData.label = label;
  robot.userData.labelText = value;
}

function segment(group, a, b, material, thickness) {
  const distance = a.distanceTo(b);
  if (distance < 0.00001) return;
  const beam = new THREE.Mesh(new THREE.CylinderGeometry(thickness, thickness,
    distance, 8), material);
  beam.position.copy(a).add(b).multiplyScalar(0.5);
  beam.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0),
    b.clone().sub(a).normalize());
  group.add(beam);
}

export function createWebGL3D(canvas, context, data, facility = "warehouse") {
  if (!context || !Array.isArray(data.floors) || !data.floors.length) {
    throw new Error("No WebGL2 context or measured floors");
  }
  const renderer = new THREE.WebGLRenderer({canvas, context, antialias: true,
    powerPreference: "high-performance"});
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.35;
  renderer.setClearColor(0x091713, 1);

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x091713);
  scene.fog = new THREE.Fog(0x091713, 13, 30);
  scene.add(new THREE.HemisphereLight(0xdff3e3, 0x243c30, 2.0));
  const sun = new THREE.DirectionalLight(0xeaf9de, 2.2);
  sun.position.set(-4, 10, 6);
  scene.add(sun);
  const fill = new THREE.DirectionalLight(0x6cb7b1, 0.9);
  fill.position.set(5, 3, -5);
  scene.add(fill);
  const camera = new THREE.PerspectiveCamera(48, 1, 0.1, 100);
  const root = new THREE.Group();
  scene.add(root);

  const floorLevels = new Map(data.floors.map((floor, i) => [floor.label, i]));
  const floorTextures = [];
  const floorSurfaces = [];
  let disposed = false;
  const nodeIndex = new Map();
  const pathMaterial = new THREE.MeshStandardMaterial({color: 0xa4d97e,
    emissive: 0x284b16, emissiveIntensity: 0.6});
  const inactiveMaterial = new THREE.MeshStandardMaterial({color: 0x506f64});
  const resourceMaterials = new Map();
  function materialForResource(id) {
    if (!resourceMaterials.has(id)) resourceMaterials.set(id,
      new THREE.MeshStandardMaterial({color: 0x927c52,
        emissive: 0x37250d, emissiveIntensity: 0.35}));
    return resourceMaterials.get(id);
  }
  // Upper levels must remain translucent so robots on lower levels are visible.
  const floorMaterial = new THREE.MeshStandardMaterial({color: 0x17392e,
    metalness: 0.28, roughness: 0.74, transparent: true, opacity: 0.30,
    depthWrite: false, side: THREE.DoubleSide});
  const nodeMaterial = new THREE.MeshStandardMaterial({color: 0xc4efa2,
    emissive: 0x4c8031, emissiveIntensity: 0.35});
  const offNodeMaterial = new THREE.MeshStandardMaterial({color: 0x6b8b80});
  for (const [level, floor] of data.floors.entries()) {
    const elevation = level * FLOOR_GAP;
    const slab = new THREE.Mesh(new THREE.BoxGeometry(7.55, 0.10, 5.10), floorMaterial);
    slab.position.y = elevation - 0.075;
    root.add(slab);
    const grid = new THREE.GridHelper(7.4, 18, 0x53786b, 0x304d40);
    grid.position.y = elevation - 0.016;
    grid.material.transparent = true;
    grid.material.opacity = 0.45;
    grid.material.depthWrite = false;
    root.add(grid);
    if (floor.plan && floor.plan.url) {
      // Same calibrated viewport as 2D: one private, sanitized PNG per floor.
      const {x, y, width, height, url} = floor.plan;
      const texture = new THREE.TextureLoader().load(url, () => {
        if (!disposed) render();
      });
      texture.colorSpace = THREE.SRGBColorSpace;
      floorTextures.push(texture);
      const material = new THREE.MeshBasicMaterial({map: texture, transparent: true,
        opacity: 0.72, depthWrite: false, side: THREE.DoubleSide});
      const sheet = new THREE.Mesh(new THREE.PlaneGeometry(
        Number(width) * UNITS_PER_PIXEL, Number(height) * UNITS_PER_PIXEL), material);
      sheet.rotation.x = -Math.PI / 2;
      sheet.position.set((Number(x) + Number(width) / 2 - 300) * UNITS_PER_PIXEL,
        elevation + 0.014,
        (Number(y) + Number(height) / 2 - 200) * UNITS_PER_PIXEL);
      root.add(sheet);
      floorSurfaces.push(sheet);
    }
    for (const node of floor.nodes || []) {
      const point = floorPoint(node.x, node.y, level);
      nodeIndex.set(`${floor.label}\u0000${node.id ?? node.label}`, point);
      const marker = new THREE.Mesh(new THREE.SphereGeometry(0.063, 12, 10),
        node.on_route ? nodeMaterial : offNodeMaterial);
      marker.position.copy(point);
      root.add(marker);
      const label = makeLabel(node.label, {width: 1.5, color: "#d6eee2"});
      label.position.copy(point).add(new THREE.Vector3(0, 0.32, 0));
      root.add(label);
    }
    for (const edge of floor.edges || []) {
      const points = edge.path || [edge.start, edge.end];
      for (let i = 1; i < points.length; i++) {
        const a = floorPoint(points[i - 1].x, points[i - 1].y, level);
        const b = floorPoint(points[i].x, points[i].y, level);
        segment(root, a, b, edge.resource_id ? materialForResource(edge.resource_id)
          : edge.on_route ? pathMaterial : inactiveMaterial,
        edge.on_route ? 0.029 : 0.013);
      }
    }
    const title = makeLabel(floor.label || `Уровень ${level + 1}`, {width: 1.85});
    title.position.set(-3.0, elevation + 0.16, -2.20);
    root.add(title);
  }
  for (const transition of data.transitions || []) {
    const a = nodeIndex.get(`${transition.from_floor || "Общий уровень"}\u0000${transition.from_id ?? transition.from_label}`);
    const b = nodeIndex.get(`${transition.to_floor || "Общий уровень"}\u0000${transition.to_id ?? transition.to_label}`);
    if (a && b) segment(root, a, b, pathMaterial, 0.032);
  }
  const center = new THREE.Vector3(0, (data.floors.length - 1) * FLOOR_GAP / 2, 0);
  let yaw = -0.8;
  let pitch = 0.65;
  let radius = Math.max(8.8, 6 + data.floors.length * 1.4);
  let dragging = false;
  let pointerX = 0;
  let pointerY = 0;
  let width = 0;
  let height = 0;
  const robots = new Map();

  function render() {
    if (!width || !height) return;
    camera.position.set(center.x + radius * Math.cos(pitch) * Math.sin(yaw),
      center.y + radius * Math.sin(pitch),
      center.z + radius * Math.cos(pitch) * Math.cos(yaw));
    camera.lookAt(center);
    renderer.render(scene, camera);
  }

  function resize() {
    const bounds = canvas.getBoundingClientRect();
    const nextWidth = Math.floor(bounds.width);
    const nextHeight = Math.floor(bounds.height);
    if (nextWidth <= 0 || nextHeight <= 0) return;
    width = nextWidth;
    height = nextHeight;
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.setSize(width, height, false);
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
    render();
  }

  function reset() {
    yaw = -0.8;
    pitch = 0.65;
    radius = Math.max(8.8, 6 + data.floors.length * 1.4);
    render();
  }

  function setState(state) {
    for (const [id, material] of resourceMaterials) {
      const occupied = Number(state?.occupiedResources?.[id]) > 0;
      material.color.setHex(occupied ? 0xffd07a : 0x927c52);
      material.emissive.setHex(occupied ? 0x946016 : 0x37250d);
      material.emissiveIntensity = occupied ? 0.85 : 0.35;
    }
    const active = new Set();
    for (const marker of state?.markers || []) {
      if (!floorLevels.has(marker.floor)) continue;
      active.add(marker.key);
      let robot = robots.get(marker.key);
      if (!robot) {
        robot = makeRobot(facility);
        root.add(robot);
        robots.set(marker.key, robot);
      }
      const start = floorPoint(marker.x, marker.y, floorLevels.get(marker.floor));
      const vertical = marker.kind === "elevator" && floorLevels.has(marker.targetFloor);
      const end = vertical ? floorPoint(marker.targetX, marker.targetY,
        floorLevels.get(marker.targetFloor)) : start;
      const fraction = vertical ? Math.max(0, Math.min(1, Number(marker.fraction) || 0)) : 0;
      robot.position.copy(start.lerp(end, fraction));
      if (marker.kind === "travel") {
        const dx = Number(marker.targetX) - Number(marker.x);
        const dz = Number(marker.targetY) - Number(marker.y);
        if (Math.abs(dx) + Math.abs(dz) > 0.0001) {
          robot.rotation.y = Math.atan2(dx, dz);
        }
      }
      const waiting = marker.kind === "resource_wait";
      robot.userData.bodyMaterial.color.setHex(waiting ? 0xefac4e : 0xa8e15e);
      robot.userData.bodyMaterial.emissive.setHex(waiting ? 0x794419 : 0x1f370c);
      updateLabel(robot, `${marker.robotId} · ${marker.sourceRow}${waiting ? " · ожидание" : ""}`,
        waiting);
    }
    for (const [key, robot] of robots) {
      if (!active.has(key)) {
        root.remove(robot);
        robot.traverse((node) => {
          // Three.js sprites share one module-level quad geometry. Disposing
          // it when a robot leaves would corrupt unrelated floor text.
          if (node.geometry && !node.isSprite) node.geometry.dispose();
          if (node.material) {
            if (node.material.map) node.material.map.dispose();
            node.material.dispose();
          }
        });
        robots.delete(key);
      }
    }
    render();
  }

  canvas.addEventListener("pointerdown", (event) => {
    dragging = true;
    pointerX = event.clientX;
    pointerY = event.clientY;
    canvas.setPointerCapture(event.pointerId);
  });
  canvas.addEventListener("pointermove", (event) => {
    if (!dragging) return;
    yaw += (event.clientX - pointerX) * 0.008;
    pitch = Math.max(0.10, Math.min(1.35,
      pitch + (event.clientY - pointerY) * 0.006));
    pointerX = event.clientX;
    pointerY = event.clientY;
    render();
  });
  canvas.addEventListener("pointerup", () => { dragging = false; });
  canvas.addEventListener("pointercancel", () => { dragging = false; });
  canvas.addEventListener("wheel", (event) => {
    event.preventDefault();
    radius = Math.max(3.2, Math.min(32, radius * (event.deltaY < 0 ? 0.91 : 1.10)));
    render();
  }, {passive: false});
  canvas.addEventListener("keydown", (event) => {
    const key = event.key;
    if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "+", "=", "-", "0"].includes(key)) return;
    event.preventDefault();
    if (key === "ArrowLeft") yaw -= 0.12;
    if (key === "ArrowRight") yaw += 0.12;
    if (key === "ArrowUp") pitch = Math.min(1.35, pitch + 0.10);
    if (key === "ArrowDown") pitch = Math.max(0.10, pitch - 0.10);
    if (key === "+" || key === "=") radius = Math.max(3.2, radius * 0.9);
    if (key === "-") radius = Math.min(32, radius * 1.1);
    if (key === "0") reset();
    render();
  });
  const resetButton = canvas.closest("[data-view-panel]")?.querySelector("[data-3d-reset]");
  if (resetButton) resetButton.addEventListener("click", reset);
  const observer = typeof ResizeObserver === "function" ? new ResizeObserver(resize) : null;
  if (observer) observer.observe(canvas);
  return {setState, resize, reset, renderer, robotCount: () => robots.size,
    robotAt: (key) => robots.get(key)?.position.toArray(),
    robotYawAt: (key) => robots.get(key)?.rotation.y,
    dispose() {
      disposed = true;
      if (observer) observer.disconnect();
      if (resetButton) resetButton.removeEventListener("click", reset);
      for (const mesh of floorSurfaces) {
        mesh.geometry.dispose();
        mesh.material.dispose();
      }
      for (const texture of floorTextures) texture.dispose();
      renderer.dispose();
    }};
}
