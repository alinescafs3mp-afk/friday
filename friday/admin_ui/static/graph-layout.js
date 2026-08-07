// Раскладка графа: силовая симуляция с квадродеревом Барнса-Хата.
//
// Файл намеренно отделён от `app.js` и не знает ни про DOM, ни про глобальные
// переменные экрана. Причина не в чистоте: раскладку надо МЕРИТЬ, а измерять её
// через браузер значит мерить заодно разметку, вставку в DOM и перерисовку.
// Отдельный файл запускается в Node как есть — то есть прибор проверяет ровно то,
// что поставляется, а не пересобранную вручную копию.
//
// Почему Барнс-Хат. Прежняя раскладка считала отталкивание по ВСЕМ парам узлов на
// 260 шагов, то есть O(n²) синхронно на главном потоке. Замер боевой функции:
// 150 узлов — 22 мс, 1000 — 499 мс, 4500 — 15 114 мс. Последнее означает вкладку,
// которая не отвечает пятнадцать секунд. Квадродерево заменяет далёкую группу
// узлов одной точкой с массой, и цена падает до O(n log n).
//
// Почему шаг за кадр, а не один прогон. Один прогон обязан закончиться до первой
// отрисовки, поэтому его стоимость человек видит как зависание. Кадровый шаг
// стоит доли миллисекунды, картина оседает на глазах, и — главное — перетаскивание
// узла становится вводом в живую симуляцию: соседи откликаются, а не стоят.

(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.FridayGraphLayout = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  // Константы силы взяты из прежней раскладки без изменений: они подобраны на
  // настоящем графе, и менять их заодно с алгоритмом значило бы получить другую
  // картинку и не знать, что её изменило — алгоритм или коэффициент.
  const REPULSION = 3000;
  const SPRING_LENGTH = 110;
  const SPRING_STIFFNESS = 0.007;
  const CENTERING = 0.0015;
  const DAMPING = 0.82;
  const MAX_SPRING_WEIGHT = 4;

  // Сколько кадров занимает остывание. Прежний прогон делал ровно 260 шагов с
  // линейным охлаждением; здесь столько же, только они растянуты по кадрам.
  const COOLING_FRAMES = 260;

  // Ниже этого суммарного смещения за кадр картина считается устоявшейся и
  // симуляция засыпает. Без этого вкладка жгла бы кадры вечно.
  const QUIET_MOVEMENT = 0.35;

  // θ квадродерева. 0 означает «всегда раскрывать клетку», то есть точную силу и
  // прежнюю квадратичную цену; чем больше, тем грубее приближение.
  //
  // Значение НЕ выбрано на глаз. Пороги объявлены до замера (предложение 42):
  // медиана длины ребра в пределах ±25% от точной силы и доля перекрывшихся пар
  // узлов не выше точной более чем на 2 процентных пункта. Перебор на n=300
  // против точной силы (медиана 397.4, перекрытий 7.759%):
  //
  //   θ=0.9 → +24.7%, +2.642 п.п. — порог по перекрытиям НЕ взят
  //   θ=0.8 → +28.8%, +2.999 п.п. — не взят ни один
  //   θ=0.7 → +22.0%, +2.062 п.п. — не взят по перекрытиям
  //   θ=0.6 → +1.8%,  +0.118 п.п. — взяты оба
  //
  // Цена при этом та же: на 4500 узлах p95 кадра 2.91 мс против 2.70 мс у θ=0.7.
  // Порог не двигался под результат — подбирался параметр.
  const THETA = 0.6;

  function buildTree(nodes, minX, minY, size) {
    // Квадродерево строится массивами, а не объектами: на тысячах узлов это
    // разница между «незаметно» и «заметно», а читаемость страдает мало.
    const capacity = Math.max(64, nodes.length * 4);
    const childIndex = new Int32Array(capacity * 4).fill(-1);
    const mass = new Float64Array(capacity);
    const centerX = new Float64Array(capacity);
    const centerY = new Float64Array(capacity);
    const bodyIndex = new Int32Array(capacity).fill(-1);
    const cellSize = new Float64Array(capacity);
    const cellX = new Float64Array(capacity);
    const cellY = new Float64Array(capacity);

    let used = 1;
    cellSize[0] = size;
    cellX[0] = minX;
    cellY[0] = minY;

    function allocate(x, y, half) {
      const index = used++;
      if (index >= capacity) return -1;
      cellSize[index] = half;
      cellX[index] = x;
      cellY[index] = y;
      return index;
    }

    for (let i = 0; i < nodes.length; i++) {
      const node = nodes[i];
      let cell = 0;
      let guard = 0;
      // Глубина ограничена: два узла в одной точке иначе делили бы клетку
      // бесконечно. При исчерпании глубины они просто живут в одной клетке —
      // приближение, но не зависание.
      while (guard++ < 40) {
        if (bodyIndex[cell] === -1 && mass[cell] === 0) {
          bodyIndex[cell] = i;
          mass[cell] = 1;
          centerX[cell] = node.x;
          centerY[cell] = node.y;
          break;
        }
        if (bodyIndex[cell] !== -1) {
          // В клетке уже сидит одиночное тело — разделить и опустить обоих ниже.
          const resident = bodyIndex[cell];
          bodyIndex[cell] = -1;
          const half = cellSize[cell] / 2;
          const rx = nodes[resident].x >= cellX[cell] + half ? 1 : 0;
          const ry = nodes[resident].y >= cellY[cell] + half ? 1 : 0;
          const slot = ry * 2 + rx;
          let child = childIndex[cell * 4 + slot];
          if (child === -1) {
            child = allocate(cellX[cell] + rx * half, cellY[cell] + ry * half, half);
            if (child === -1) break;
            childIndex[cell * 4 + slot] = child;
          }
          bodyIndex[child] = resident;
          mass[child] = 1;
          centerX[child] = nodes[resident].x;
          centerY[child] = nodes[resident].y;
        }
        // Центр масс клетки накапливается на спуске.
        centerX[cell] = (centerX[cell] * mass[cell] + node.x) / (mass[cell] + 1);
        centerY[cell] = (centerY[cell] * mass[cell] + node.y) / (mass[cell] + 1);
        mass[cell] += 1;

        const half = cellSize[cell] / 2;
        const nx = node.x >= cellX[cell] + half ? 1 : 0;
        const ny = node.y >= cellY[cell] + half ? 1 : 0;
        const slot = ny * 2 + nx;
        let child = childIndex[cell * 4 + slot];
        if (child === -1) {
          child = allocate(cellX[cell] + nx * half, cellY[cell] + ny * half, half);
          if (child === -1) break;
          childIndex[cell * 4 + slot] = child;
        }
        cell = child;
      }
    }

    return { childIndex, mass, centerX, centerY, bodyIndex, cellSize, used };
  }

  function applyRepulsion(tree, nodes, index, theta) {
    const node = nodes[index];
    const { childIndex, mass, centerX, centerY, bodyIndex, cellSize } = tree;
    // Обход итеративный: рекурсия на глубине сорока клетках тысячу раз за кадр
    // стоит заметно дороже собственного стека.
    const stack = [0];
    let vx = 0;
    let vy = 0;
    while (stack.length) {
      const cell = stack.pop();
      const cellMass = mass[cell];
      if (cellMass === 0) continue;
      if (bodyIndex[cell] === index) continue;
      let dx = node.x - centerX[cell];
      let dy = node.y - centerY[cell];
      let distance = Math.sqrt(dx * dx + dy * dy);
      if (distance < 0.01) distance = 0.01;
      if (bodyIndex[cell] !== -1 || cellSize[cell] / distance < theta) {
        // Либо одиночное тело, либо группа достаточно далёкая, чтобы считать её
        // одной точкой: именно здесь и экономится квадратичность.
        const push = (REPULSION * cellMass) / (distance * distance);
        vx += (dx / distance) * push;
        vy += (dy / distance) * push;
        continue;
      }
      for (let slot = 0; slot < 4; slot++) {
        const child = childIndex[cell * 4 + slot];
        if (child !== -1) stack.push(child);
      }
    }
    node.vx += vx;
    node.vy += vy;
  }

  function createSimulation(options) {
    const width = options.width;
    const height = options.height;
    const margin = options.margin === undefined ? 40 : options.margin;
    const theta = options.theta === undefined ? THETA : options.theta;
    const exact = theta <= 0;

    // Стартовое положение по кругу, а не случайное: случайный старт даёт разную
    // картинку при каждом открытии, и человек не узнаёт свой же граф.
    const nodes = options.nodes.map(function (source, index, all) {
      const angle = (2 * Math.PI * index) / Math.max(1, all.length);
      return {
        id: source.id,
        x: width / 2 + Math.cos(angle) * Math.min(width, height) * 0.36,
        y: height / 2 + Math.sin(angle) * Math.min(width, height) * 0.36,
        vx: 0,
        vy: 0,
        fixed: false,
      };
    });

    const byId = new Map();
    for (let i = 0; i < nodes.length; i++) byId.set(nodes[i].id, nodes[i]);

    const links = [];
    for (let i = 0; i < options.edges.length; i++) {
      const edge = options.edges[i];
      const source = byId.get(edge.source);
      const target = byId.get(edge.target);
      if (!source || !target || source === target) continue;
      links.push({
        source: source,
        target: target,
        weight: Math.min(MAX_SPRING_WEIGHT, edge.weight || 1),
      });
    }

    let frame = 0;

    function bounds() {
      let minX = Infinity;
      let minY = Infinity;
      let maxX = -Infinity;
      let maxY = -Infinity;
      for (let i = 0; i < nodes.length; i++) {
        if (nodes[i].x < minX) minX = nodes[i].x;
        if (nodes[i].y < minY) minY = nodes[i].y;
        if (nodes[i].x > maxX) maxX = nodes[i].x;
        if (nodes[i].y > maxY) maxY = nodes[i].y;
      }
      if (!isFinite(minX)) return { minX: 0, minY: 0, size: 1 };
      const size = Math.max(maxX - minX, maxY - minY, 1) * 1.01;
      return { minX: minX, minY: minY, size: size };
    }

    function step() {
      if (!nodes.length) return 0;
      // Охлаждение повторяет прежнее линейное, только по кадрам. Ниже нуля не
      // опускается: отрицательный множитель разогнал бы картину вместо остывания.
      const cool = Math.max(0, 1 - frame / COOLING_FRAMES);
      frame++;

      if (exact) {
        // Точная сила оставлена намеренно и только для прибора: проба качества
        // сравнивает приближение именно с ней, а не с рассуждением о ней.
        for (let i = 0; i < nodes.length; i++) {
          const a = nodes[i];
          for (let j = i + 1; j < nodes.length; j++) {
            const b = nodes[j];
            let dx = a.x - b.x;
            let dy = a.y - b.y;
            let distance = Math.sqrt(dx * dx + dy * dy);
            if (distance < 0.01) distance = 0.01;
            const push = REPULSION / (distance * distance);
            const ux = dx / distance;
            const uy = dy / distance;
            a.vx += ux * push;
            a.vy += uy * push;
            b.vx -= ux * push;
            b.vy -= uy * push;
          }
        }
      } else {
        const box = bounds();
        const tree = buildTree(nodes, box.minX, box.minY, box.size);
        for (let i = 0; i < nodes.length; i++) applyRepulsion(tree, nodes, i, theta);
      }

      for (let i = 0; i < links.length; i++) {
        const link = links[i];
        let dx = link.target.x - link.source.x;
        let dy = link.target.y - link.source.y;
        let distance = Math.sqrt(dx * dx + dy * dy);
        if (distance < 0.01) distance = 0.01;
        const pull = (distance - SPRING_LENGTH) * SPRING_STIFFNESS * link.weight;
        const ux = dx / distance;
        const uy = dy / distance;
        link.source.vx += ux * pull;
        link.source.vy += uy * pull;
        link.target.vx -= ux * pull;
        link.target.vy -= uy * pull;
      }

      let movement = 0;
      for (let i = 0; i < nodes.length; i++) {
        const node = nodes[i];
        if (node.fixed) {
          node.vx = 0;
          node.vy = 0;
          continue;
        }
        node.vx += (width / 2 - node.x) * CENTERING;
        node.vy += (height / 2 - node.y) * CENTERING;
        const dx = node.vx * cool;
        const dy = node.vy * cool;
        node.x += dx;
        node.y += dy;
        node.vx *= DAMPING;
        node.vy *= DAMPING;
        if (node.x < margin) node.x = margin;
        if (node.x > width - margin) node.x = width - margin;
        if (node.y < margin / 1.33) node.y = margin / 1.33;
        if (node.y > height - margin / 1.33) node.y = height - margin / 1.33;
        movement += Math.abs(dx) + Math.abs(dy);
      }
      return movement;
    }

    return {
      nodes: nodes,
      links: links,
      step: step,
      get frame() {
        return frame;
      },
      // Устоялась ли картина. Спрашивается кадровым циклом, чтобы не жечь кадры
      // на неподвижном графе.
      quiet: function (movement) {
        return frame >= COOLING_FRAMES && movement < QUIET_MOVEMENT;
      },
      settle: function (maxFrames) {
        let movement = 0;
        const limit = maxFrames === undefined ? COOLING_FRAMES + 40 : maxFrames;
        for (let i = 0; i < limit; i++) {
          movement = step();
          if (this.quiet(movement)) break;
        }
        return movement;
      },
      // Перетаскивание: узел под пальцем закреплён и работает как ввод в
      // симуляцию, поэтому соседи откликаются, а не стоят на месте.
      hold: function (id, x, y) {
        const node = byId.get(id);
        if (!node) return;
        node.fixed = true;
        node.x = x;
        node.y = y;
        node.vx = 0;
        node.vy = 0;
      },
      pin: function (id, x, y) {
        const node = byId.get(id);
        if (!node) return;
        node.fixed = true;
        if (x !== undefined) node.x = x;
        if (y !== undefined) node.y = y;
      },
      release: function (id) {
        const node = byId.get(id);
        if (node) node.fixed = false;
      },
      // Разогрев: после ввода человека картина должна снова ожить, иначе
      // перетаскивание в остывшем графе не двигало бы соседей вовсе.
      reheat: function (frames) {
        const back = frames === undefined ? 60 : frames;
        frame = Math.max(0, frame - back);
      },
      byId: byId,
    };
  }

  return { createSimulation: createSimulation, THETA: THETA, COOLING_FRAMES: COOLING_FRAMES };
});
