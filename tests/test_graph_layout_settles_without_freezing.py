"""Раскладка графа: не вешает вкладку, не хуже точной силы, узнаваема.

Проверяется ПОСТАВЛЯЕМЫЙ `friday/admin_ui/static/graph-layout.js`, а не его копия
в тесте: раскладка вынесена в отдельный файл именно затем, чтобы её можно было
прогнать в Node и померить ровно то, что уедет в браузер.

Замер до правки (боевая функция, вырезанная из `app.js`): 150 узлов — 22 мс,
1000 — 499 мс, 4500 — 15 114 мс, и всё это ОДНИМ синхронным прогоном до первой
отрисовки. Пороги предложения 42 объявлены заранее и здесь закреплены.

Почему проба скорости стоит на 4500, а не на 1000. Прежняя попарная раскладка
делала 260 шагов за 499 мс при 1000 узлах, то есть 1.9 мс на шаг — в бюджет кадра
(16.7 мс) она укладывалась. Вкладку вешал не шаг, а 260 шагов подряд. Поэтому
мутацию «вернуть попарное отталкивание» ловит только тот размер, где сам шаг
выходит за бюджет: при 4500 узлах он стоит 58 мс.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

LAYOUT = pathlib.Path("friday/admin_ui/static/graph-layout.js")

# Общий пролог: загрузить поставляемый файл и построить связный граф степени 3.
PRELUDE = """
const layout = require(%s);
const W = 1200, H = 700;
function corpus(n, degree) {
  degree = degree || 3;
  const nodes = Array.from({length: n}, (_, i) => ({id: 'e' + i}));
  const edges = [];
  for (let i = 1; i < n; i++) {
    for (let d = 0; d < degree; d++) {
      edges.push({source: 'e' + i, target: 'e' + ((i * 7919 + d * 104729) %% i), weight: 1});
    }
  }
  return {nodes, edges};
}
function quality(sim, edges) {
  const at = new Map(sim.nodes.map(n => [n.id, n]));
  const lengths = [];
  for (const e of edges) {
    const a = at.get(e.source), b = at.get(e.target);
    if (a && b) lengths.push(Math.hypot(a.x - b.x, a.y - b.y));
  }
  lengths.sort((x, y) => x - y);
  let overlaps = 0, pairs = 0;
  const ns = sim.nodes;
  for (let i = 0; i < ns.length; i++) {
    for (let j = i + 1; j < ns.length; j++) {
      pairs++;
      if (Math.hypot(ns[i].x - ns[j].x, ns[i].y - ns[j].y) < 16) overlaps++;
    }
  }
  return {median: lengths[Math.floor(lengths.length / 2)] || 0,
          overlapPct: 100 * overlaps / Math.max(1, pairs)};
}
"""


def run(body: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node не установлен — браузерную половину проверить нечем")
    assert LAYOUT.exists(), "раскладка не найдена там, где её подключает index.html"
    script = (PRELUDE % json.dumps(str(LAYOUT.resolve()))) + body
    done = subprocess.run(  # noqa: S603
        [node, "-e", script], capture_output=True, text=True, timeout=300, check=False
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return json.loads(done.stdout)


def test_no_single_frame_blocks_the_tab_on_a_large_graph():
    """Мутация: вернуть попарное отталкивание вместо квадродерева — при 4500 узлах
    шаг стоит 58 мс против бюджета кадра 16.7 мс, и проба краснеет."""

    measured = run("""
      const {nodes, edges} = corpus(4500);
      const sim = layout.createSimulation({nodes, edges, width: W, height: H});
      const frames = [];
      for (let i = 0; i < 300; i++) {
        const t = process.hrtime.bigint();
        const movement = sim.step();
        frames.push(Number(process.hrtime.bigint() - t) / 1e6);
        if (sim.quiet(movement)) break;
      }
      // Первые кадры включают разогрев JIT и меряют компилятор, а не алгоритм.
      const settled = frames.slice(3).sort((a, b) => a - b);
      process.stdout.write(JSON.stringify({
        frames: frames.length,
        p95: settled[Math.floor(settled.length * 0.95)] || 0,
        worst: settled[settled.length - 1] || 0,
      }));
    """)

    assert measured["frames"] > 10, "симуляция закончилась подозрительно рано"
    assert measured["p95"] <= 16.7, (
        f"кадр стоит {measured['p95']:.1f} мс при бюджете 16.7 — вкладка встанет: "
        "раскладка снова считает все пары"
    )


def test_the_approximation_is_not_worse_than_the_exact_force():
    """Пороги объявлены в предложении 42 ДО замера: медиана длины ребра в пределах
    ±25% от точной силы, доля перекрытий выше точной не более чем на 2 п.п.

    Мутация: θ=1e9 — дерево не раскрывается никогда, вся картина считается одной
    точкой, и оба числа уезжают."""

    measured = run("""
      const {nodes, edges} = corpus(300);
      const approx = layout.createSimulation({nodes, edges, width: W, height: H});
      approx.settle();
      // theta=0 означает «раскрывать всегда», то есть ровно прежнюю точную силу.
      const exact = layout.createSimulation({nodes, edges, width: W, height: H, theta: 0});
      exact.settle();
      const a = quality(approx, edges), e = quality(exact, edges);
      process.stdout.write(JSON.stringify({
        drift: 100 * (a.median - e.median) / e.median,
        overlapGap: a.overlapPct - e.overlapPct,
      }));
    """)

    assert abs(measured["drift"]) <= 25, (
        f"медиана длины ребра разошлась с точной силой на {measured['drift']:.1f}%"
    )
    assert measured["overlapGap"] <= 2, (
        f"узлы налезают друг на друга на {measured['overlapGap']:.2f} п.п. чаще точной силы"
    )


def test_the_same_graph_settles_into_the_same_picture():
    """Случайного старта в проекте нет намеренно: человек должен узнавать свой граф.

    Мутация: заменить круговой старт на `Math.random()` — проба краснеет."""

    measured = run("""
      const {nodes, edges} = corpus(300);
      const first = layout.createSimulation({nodes, edges, width: W, height: H});
      first.settle();
      const second = layout.createSimulation({nodes, edges, width: W, height: H});
      second.settle();
      process.stdout.write(JSON.stringify({
        same: first.nodes.every((n, i) => n.x === second.nodes[i].x && n.y === second.nodes[i].y),
      }));
    """)

    assert measured["same"], "два прогона одного графа дали разные картины"


def test_a_pinned_node_holds_while_the_rest_keep_settling():
    """Дефект, который это закрывает: прежний `saveLayout` писал координаты ВСЕХ
    узлов вида, при следующем открытии все получали `fixed`, и раскладка не делала
    ни шага — одно перетаскивание молча замораживало картину целиком.

    Мутация: закреплять все узлы вместо одного — второй assert краснеет."""

    measured = run("""
      const {nodes, edges} = corpus(120);
      const sim = layout.createSimulation({nodes, edges, width: W, height: H});
      sim.pin('e7', 300, 300);
      const before = sim.nodes.map(n => ({id: n.id, x: n.x, y: n.y}));
      sim.settle();
      const at = sim.byId;
      const pinned = at.get('e7');
      let movedOthers = 0;
      for (const was of before) {
        if (was.id === 'e7') continue;
        const now = at.get(was.id);
        if (Math.hypot(now.x - was.x, now.y - was.y) > 1) movedOthers++;
      }
      process.stdout.write(JSON.stringify({
        pinnedX: pinned.x, pinnedY: pinned.y, movedOthers, total: before.length - 1,
      }));
    """)

    assert (measured["pinnedX"], measured["pinnedY"]) == (300, 300), "закреплённый узел уехал"
    assert measured["movedOthers"] > measured["total"] * 0.5, (
        "остальные узлы не укладываются вокруг закреплённого — картина замерла целиком"
    )


def test_dragging_a_node_moves_its_neighbours():
    """Ради этого раскладка и стала живой: прежде перетаскивание двигало только сам
    узел и его собственные линии, соседи стояли, и граф читался как нарисованная
    схема, а не как ткань.

    Мутация: убрать `reheat` из захвата — остывшая симуляция не сдвинет соседей,
    и проба краснеет."""

    measured = run("""
      const {nodes, edges} = corpus(150);
      const sim = layout.createSimulation({nodes, edges, width: W, height: H});
      sim.settle();
      const neighbours = edges.filter(e => e.source === 'e5' || e.target === 'e5')
        .map(e => (e.source === 'e5' ? e.target : e.source));
      const before = new Map(neighbours.map(id => [id, {...sim.byId.get(id)}]));
      // Человек утащил узел в дальний угол.
      sim.hold('e5', 60, 60);
      sim.reheat(30);
      for (let i = 0; i < 30; i++) sim.step();
      let moved = 0;
      for (const [id, was] of before) {
        const now = sim.byId.get(id);
        if (Math.hypot(now.x - was.x, now.y - was.y) > 1) moved++;
      }
      process.stdout.write(JSON.stringify({moved, neighbours: before.size}));
    """)

    assert measured["neighbours"] > 0, "у выбранного узла нет соседей — проба проверяет не то"
    assert measured["moved"] == measured["neighbours"], (
        "соседи не откликнулись на перетаскивание: симуляция не ожила"
    )
