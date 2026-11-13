/* ParkVision — редактор разметки ROI. Vanilla JS, без сборки и без зависимостей.
 *
 * Модель разметки гибридная и менять её нельзя:
 *   spots — дискретные места ТОЛЬКО там, где на асфальте есть краска.
 *           quad = наземный след места (4 точки, по часовой, старт — ближний
 *           к камере левый угол). Это НЕ силуэт машины.
 *   zones — полигон + capacity для грунтовых/хаотичных площадок, где
 *           физических мест не существует. ID отдельных мест там не выдумываем.
 *
 * Координаты пиксельные, в системе эталонного кадра камеры (frame_size).
 */
(function () {
  'use strict';

  // --------------------------------------------------------------- //
  // Константы
  // --------------------------------------------------------------- //

  // Синхронизировано с cvsvc/config.py: MIN_SPOT_LONG_SIDE_PX.
  // Квад короче — детектор машину не увидит, валидатор такое место отклонит.
  var MIN_SPOT_LONG_SIDE_PX = 28;

  var COLORS = {
    spot: '#4ade80', spotOff: '#7c8894', spotBad: '#f87171',
    zone: '#fbbf24', overlay: '#f472b6', tile: '#38bdf8',
    sel: '#4ea1ff', pending: '#ffffff'
  };

  var KEY_ORDER = {
    '': ['version', 'defaults', 'cameras'],
    '.cameras[]': ['id', 'name', 'parking_lot_id', 'playlist_url', 'frame_size',
      'reference_frame', 'poll_interval_s', 'range_bytes', 'footprint_offset_px',
      'overlay_exclude', 'tiles', 'spots', 'zones'],
    '.cameras[].spots[]': ['id', 'type', 'enabled', 'quad', 'lat', 'lon', 'approach', 'notes'],
    '.cameras[].zones[]': ['id', 'name', 'capacity', 'polygon', 'lat', 'lon', 'entrance', 'notes'],
    '.cameras[].spots[].approach': ['lat', 'lon'],
    '.cameras[].zones[].entrance': ['lat', 'lon']
  };

  var CAM_NOTES = {
    'CAM-003': '<b>Бочкообразная дисторсия.</b> Прямой ряд мест выглядит в кадре ДУГОЙ. ' +
      'Не ставьте квады линейной интерполяцией между краями ряда — крайние места уедут ' +
      'с асфальта. Ставьте каждый квад отдельно по краске, либо ведите ряд по дуге ' +
      '(клавиша <kbd>D</kbd> дублирует место со сдвигом — после дубля обязательно ' +
      'подвиньте углы по картинке).',
    'CAM-001': 'Краски на асфальте нет — только <b>зоны</b>. Мачта ЛЭП перекрывает часть ' +
      'обзора: то, что под ней, не размечаем или помечаем <code>enabled: false</code>. ' +
      'Нижняя половина кадра в глубокой тени.',
    'CAM-002': 'Худшая камера: авто 25–45 px, перекрытия стройкой и деревьями. ' +
      '<b>Только зоны</b>, дискретных мест здесь не делать вообще.'
  };

  // --------------------------------------------------------------- //
  // Состояние
  // --------------------------------------------------------------- //

  var S = {
    doc: null,            // разобранный YAML целиком
    header: '',           // комментарии-шапка исходного файла (round-trip их не хранит)
    camIdx: 0,
    img: null,
    imgLabel: '',
    view: { scale: 1, x: 0, y: 0 },
    mode: 'select',
    pending: null,        // {kind, pts}
    rect: null,           // {kind, x0, y0, x1, y1}
    sel: null,            // {kind, i, v}
    drag: null,
    space: false,
    fitted: false,
    hasServer: false
  };

  var $ = function (id) { return document.getElementById(id); };
  var canvas = $('canvas');
  var ctx = canvas.getContext('2d');

  // --------------------------------------------------------------- //
  // Утилиты
  // --------------------------------------------------------------- //

  function cam() { return S.doc && S.doc.cameras ? S.doc.cameras[S.camIdx] : null; }
  function frameSize() { var c = cam(); return (c && c.frame_size) ? c.frame_size : [1920, 1080]; }
  function r1(v) { return Math.round(v * 10) / 10; }
  function clamp(v, a, b) { return v < a ? a : v > b ? b : v; }

  function dist(a, b) { return Math.hypot(a[0] - b[0], a[1] - b[1]); }

  function longSide(quad) {
    var m = 0;
    for (var i = 0; i < 4; i++) m = Math.max(m, dist(quad[i], quad[(i + 1) % 4]));
    return m;
  }
  function shortSide(quad) {
    var m = Infinity;
    for (var i = 0; i < 4; i++) m = Math.min(m, dist(quad[i], quad[(i + 1) % 4]));
    return m;
  }

  function polyArea(pts) {
    var a = 0;
    for (var i = 0; i < pts.length; i++) {
      var p = pts[i], q = pts[(i + 1) % pts.length];
      a += p[0] * q[1] - q[0] * p[1];
    }
    return Math.abs(a) / 2;
  }

  function centroid(pts) {
    var sx = 0, sy = 0;
    pts.forEach(function (p) { sx += p[0]; sy += p[1]; });
    return [sx / pts.length, sy / pts.length];
  }

  function pointInPoly(pt, pts) {
    var inside = false;
    for (var i = 0, j = pts.length - 1; i < pts.length; j = i++) {
      var xi = pts[i][0], yi = pts[i][1], xj = pts[j][0], yj = pts[j][1];
      if ((yi > pt[1]) !== (yj > pt[1]) &&
          pt[0] < (xj - xi) * (pt[1] - yi) / (yj - yi) + xi) inside = !inside;
    }
    return inside;
  }

  function rectPts(r) { return [[r[0], r[1]], [r[2], r[1]], [r[2], r[3]], [r[0], r[3]]]; }

  function setStatus(msg, kind) {
    var el = $('status');
    el.textContent = msg || '';
    el.className = 'status' + (kind ? ' ' + kind : '');
  }

  var hintTimer = null;
  function flash(msg) {
    var el = $('hint');
    el.textContent = msg;
    el.classList.add('show');
    clearTimeout(hintTimer);
    hintTimer = setTimeout(function () { el.classList.remove('show'); }, 2200);
  }

  // --------------------------------------------------------------- //
  // Преобразования координат
  // --------------------------------------------------------------- //

  function toImage(sx, sy) {
    return [(sx - S.view.x) / S.view.scale, (sy - S.view.y) / S.view.scale];
  }
  function toScreen(ix, iy) {
    return [ix * S.view.scale + S.view.x, iy * S.view.scale + S.view.y];
  }
  function eventPos(e) {
    var r = canvas.getBoundingClientRect();
    return [e.clientX - r.left, e.clientY - r.top];
  }

  // Если редактор отрисовался в ещё не разложенном контейнере (ширина 0),
  // вписать нечего — запоминаем это и доделаем на первом же resize.
  function fitView() {
    var fs = frameSize();
    var w = canvas.clientWidth, h = canvas.clientHeight;
    if (!w || !h) { S.fitted = false; return; }
    var s = Math.min(w / fs[0], h / fs[1]) * 0.96;
    S.view.scale = s;
    S.view.x = (w - fs[0] * s) / 2;
    S.view.y = (h - fs[1] * s) / 2;
    S.fitted = true;
    draw();
  }

  function zoomAt(sx, sy, factor) {
    var before = toImage(sx, sy);
    S.view.scale = clamp(S.view.scale * factor, 0.03, 40);
    var after = toImage(sx, sy);
    S.view.x += (after[0] - before[0]) * S.view.scale;
    S.view.y += (after[1] - before[1]) * S.view.scale;
    draw();
  }

  // --------------------------------------------------------------- //
  // Отрисовка
  // --------------------------------------------------------------- //

  var rafPending = false;
  function draw() {
    if (rafPending) return;
    rafPending = true;
    requestAnimationFrame(function () { rafPending = false; render(); });
  }

  function resizeCanvas() {
    var dpr = window.devicePixelRatio || 1;
    var w = canvas.clientWidth, h = canvas.clientHeight;
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    if (!S.fitted && w && h) { fitView(); return; }
    draw();
  }

  function render() {
    var dpr = window.devicePixelRatio || 1;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, canvas.clientWidth, canvas.clientHeight);

    var c = cam();
    if (!c) return;
    var fs = frameSize();
    var v = S.view;

    ctx.save();
    ctx.translate(v.x, v.y);
    ctx.scale(v.scale, v.scale);

    // кадр
    if (S.img) {
      ctx.imageSmoothingEnabled = v.scale < 2;
      ctx.drawImage(S.img, 0, 0, fs[0], fs[1]);
    } else {
      ctx.fillStyle = '#1a1f26';
      ctx.fillRect(0, 0, fs[0], fs[1]);
    }
    ctx.lineWidth = 2 / v.scale;
    ctx.strokeStyle = '#4b5563';
    ctx.strokeRect(0, 0, fs[0], fs[1]);

    // tiles (под всем остальным)
    (c.tiles || []).forEach(function (r, i) {
      drawPoly(rectPts(r), COLORS.tile, isSel('tile', i), 'tile ' + i, true);
    });
    (c.overlay_exclude || []).forEach(function (r, i) {
      drawPoly(rectPts(r), COLORS.overlay, isSel('overlay', i), 'overlay ' + i, true);
    });
    (c.zones || []).forEach(function (z, i) {
      drawPoly(z.polygon || [], COLORS.zone, isSel('zone', i),
        (z.id || '?') + ' · ' + (z.capacity != null ? z.capacity : '?') + ' мест', false);
    });
    (c.spots || []).forEach(function (sp, i) {
      var q = sp.quad || [];
      var col = sp.enabled === false ? COLORS.spotOff
        : (q.length === 4 && longSide(q) < MIN_SPOT_LONG_SIDE_PX ? COLORS.spotBad : COLORS.spot);
      drawPoly(q, col, isSel('spot', i), sp.id || '?', false);
      // жирная точка на quad[0] — ближний к камере левый угол.
      // Порядок обхода значим: по нему детектор понимает, где «перёд» места.
      if (q.length === 4) {
        ctx.fillStyle = col;
        ctx.beginPath();
        ctx.arc(q[0][0], q[0][1], 4 / v.scale, 0, Math.PI * 2);
        ctx.fill();
      }
    });

    // незавершённый объект
    if (S.pending && S.pending.pts.length) {
      drawPoly(S.pending.pts, COLORS.pending, false, '', true, true);
    }
    if (S.rect) {
      var r = S.rect;
      drawPoly(rectPts([Math.min(r.x0, r.x1), Math.min(r.y0, r.y1),
        Math.max(r.x0, r.x1), Math.max(r.y0, r.y1)]), COLORS.pending, false, '', true, true);
    }

    ctx.restore();
    updateHud();
  }

  function isSel(kind, i) { return S.sel && S.sel.kind === kind && S.sel.i === i; }

  function drawPoly(pts, color, selected, label, dashed, open) {
    if (!pts || pts.length < 2) {
      if (pts && pts.length === 1) {
        ctx.fillStyle = color;
        ctx.beginPath();
        ctx.arc(pts[0][0], pts[0][1], 4 / S.view.scale, 0, Math.PI * 2);
        ctx.fill();
      }
      return;
    }
    var v = S.view;
    ctx.beginPath();
    ctx.moveTo(pts[0][0], pts[0][1]);
    for (var i = 1; i < pts.length; i++) ctx.lineTo(pts[i][0], pts[i][1]);
    if (!open) ctx.closePath();

    ctx.fillStyle = hexA(color, selected ? 0.3 : 0.14);
    if (!open) ctx.fill();

    ctx.setLineDash(dashed ? [8 / v.scale, 6 / v.scale] : []);
    ctx.lineWidth = (selected ? 3 : 2) / v.scale;
    ctx.strokeStyle = selected ? COLORS.sel : color;
    ctx.stroke();
    ctx.setLineDash([]);

    // вершины
    var hr = 5 / v.scale;
    for (var j = 0; j < pts.length; j++) {
      ctx.beginPath();
      ctx.rect(pts[j][0] - hr, pts[j][1] - hr, hr * 2, hr * 2);
      ctx.fillStyle = selected ? COLORS.sel : color;
      ctx.fill();
      ctx.lineWidth = 1 / v.scale;
      ctx.strokeStyle = '#0d1014';
      ctx.stroke();
    }

    if (label) {
      var c0 = centroid(pts);
      var size = clamp(14 / v.scale, 6 / v.scale, 1e9);
      ctx.font = '600 ' + size + 'px -apple-system, sans-serif';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      var tw = ctx.measureText(label).width;
      ctx.fillStyle = 'rgba(13,16,20,.72)';
      ctx.fillRect(c0[0] - tw / 2 - 3 / v.scale, c0[1] - size * 0.65,
        tw + 6 / v.scale, size * 1.3);
      ctx.fillStyle = selected ? COLORS.sel : color;
      ctx.fillText(label, c0[0], c0[1]);
    }
  }

  function hexA(hex, a) {
    var n = parseInt(hex.slice(1), 16);
    return 'rgba(' + ((n >> 16) & 255) + ',' + ((n >> 8) & 255) + ',' + (n & 255) + ',' + a + ')';
  }

  var lastCursor = [0, 0];
  function updateHud() {
    var fs = frameSize();
    var parts = [
      'кадр ' + fs[0] + '×' + fs[1],
      'x ' + Math.round(lastCursor[0]) + '  y ' + Math.round(lastCursor[1])
    ];
    if (S.pending) parts.push('точек: ' + S.pending.pts.length + (S.pending.kind === 'spot' ? '/4' : ''));
    $('hud').textContent = parts.join('   ·   ');
    $('zoomLabel').textContent = Math.round(S.view.scale * 100) + '%';
  }

  // --------------------------------------------------------------- //
  // Хит-тест
  // --------------------------------------------------------------- //

  function objects() {
    var c = cam();
    if (!c) return [];
    var out = [];
    (c.spots || []).forEach(function (o, i) { out.push({ kind: 'spot', i: i, pts: o.quad || [] }); });
    (c.zones || []).forEach(function (o, i) { out.push({ kind: 'zone', i: i, pts: o.polygon || [] }); });
    (c.overlay_exclude || []).forEach(function (r, i) { out.push({ kind: 'overlay', i: i, pts: rectPts(r) }); });
    (c.tiles || []).forEach(function (r, i) { out.push({ kind: 'tile', i: i, pts: rectPts(r) }); });
    return out;
  }

  function hitVertex(ip) {
    var tol = 8 / S.view.scale;
    var objs = objects();
    // выделенный объект имеет приоритет — иначе на плотной разметке не попасть
    objs.sort(function (a, b) {
      return (isSel(b.kind, b.i) ? 1 : 0) - (isSel(a.kind, a.i) ? 1 : 0);
    });
    for (var k = 0; k < objs.length; k++) {
      var o = objs[k];
      for (var j = 0; j < o.pts.length; j++) {
        if (dist(ip, o.pts[j]) <= tol) return { kind: o.kind, i: o.i, v: j };
      }
    }
    return null;
  }

  function hitBody(ip) {
    var objs = objects();
    for (var k = objs.length - 1; k >= 0; k--) {
      if (objs[k].pts.length >= 3 && pointInPoly(ip, objs[k].pts)) {
        return { kind: objs[k].kind, i: objs[k].i, v: null };
      }
    }
    return null;
  }

  // --------------------------------------------------------------- //
  // Правка геометрии
  // --------------------------------------------------------------- //

  function getPts(sel) {
    var c = cam();
    if (!c || !sel) return null;
    if (sel.kind === 'spot') return (c.spots[sel.i] || {}).quad;
    if (sel.kind === 'zone') return (c.zones[sel.i] || {}).polygon;
    if (sel.kind === 'overlay') return rectPts(c.overlay_exclude[sel.i]);
    if (sel.kind === 'tile') return rectPts(c.tiles[sel.i]);
    return null;
  }

  // Прямоугольники хранятся как [x1,y1,x2,y2]; таскание угла правит нужную пару.
  function moveRectVertex(rect, vi, ip) {
    var fs = frameSize();
    var x = Math.round(clamp(ip[0], 0, fs[0])), y = Math.round(clamp(ip[1], 0, fs[1]));
    // вершины: 0=(x1,y1) 1=(x2,y1) 2=(x2,y2) 3=(x1,y2)
    if (vi === 0) { rect[0] = x; rect[1] = y; }
    else if (vi === 1) { rect[2] = x; rect[1] = y; }
    else if (vi === 2) { rect[2] = x; rect[3] = y; }
    else { rect[0] = x; rect[3] = y; }
    if (rect[2] <= rect[0]) rect[2] = rect[0] + 1;
    if (rect[3] <= rect[1]) rect[3] = rect[1] + 1;
  }

  function moveVertex(sel, ip) {
    var c = cam(), fs = frameSize();
    var x = clamp(ip[0], 0, fs[0]), y = clamp(ip[1], 0, fs[1]);
    if (sel.kind === 'overlay') return moveRectVertex(c.overlay_exclude[sel.i], sel.v, ip);
    if (sel.kind === 'tile') return moveRectVertex(c.tiles[sel.i], sel.v, ip);
    var pts = getPts(sel);
    if (!pts) return;
    pts[sel.v] = [Math.round(x), Math.round(y)];
  }

  function moveObject(sel, dx, dy) {
    var c = cam(), fs = frameSize();
    if (sel.kind === 'overlay' || sel.kind === 'tile') {
      var r = sel.kind === 'overlay' ? c.overlay_exclude[sel.i] : c.tiles[sel.i];
      dx = clamp(dx, -r[0], fs[0] - r[2]);
      dy = clamp(dy, -r[1], fs[1] - r[3]);
      r[0] = Math.round(r[0] + dx); r[1] = Math.round(r[1] + dy);
      r[2] = Math.round(r[2] + dx); r[3] = Math.round(r[3] + dy);
      return;
    }
    var pts = getPts(sel);
    if (!pts) return;
    var minX = Math.min.apply(null, pts.map(function (p) { return p[0]; }));
    var minY = Math.min.apply(null, pts.map(function (p) { return p[1]; }));
    var maxX = Math.max.apply(null, pts.map(function (p) { return p[0]; }));
    var maxY = Math.max.apply(null, pts.map(function (p) { return p[1]; }));
    dx = clamp(dx, -minX, fs[0] - maxX);
    dy = clamp(dy, -minY, fs[1] - maxY);
    for (var i = 0; i < pts.length; i++) {
      pts[i] = [Math.round(pts[i][0] + dx), Math.round(pts[i][1] + dy)];
    }
  }

  // --------------------------------------------------------------- //
  // Создание объектов
  // --------------------------------------------------------------- //

  // Ведущие нули значимы: шаблон "S-3" + счётчик "01" даёт S-301, как в спеке.
  function nextId(which) {
    var tpl = $(which === 'spot' ? 'spotTpl' : 'zoneTpl').value || (which === 'spot' ? 'S-' : 'Z-');
    var fld = $(which === 'spot' ? 'spotNext' : 'zoneNext');
    var raw = String(fld.value || '1').trim();
    var pad = /^\d+$/.test(raw) ? raw.length : 1;
    var n = parseInt(raw, 10);
    if (!isFinite(n)) n = 1;
    var all = allIds();
    var id;
    do { id = tpl + String(n).padStart(pad, '0'); n++; } while (all.indexOf(id) >= 0);
    fld.value = String(n).padStart(pad, '0');
    return id;
  }

  function allIds() {
    var out = [];
    (S.doc.cameras || []).forEach(function (c) {
      (c.spots || []).forEach(function (s) { out.push(s.id); });
      (c.zones || []).forEach(function (z) { out.push(z.id); });
    });
    return out;
  }

  function baseGeo() {
    var la = parseFloat($('baseLat').value), lo = parseFloat($('baseLon').value);
    if (isFinite(la) && isFinite(lo)) return { lat: la, lon: lo };
    // наследуем от последнего объекта этой камеры, чтобы не плодить null
    var c = cam();
    var last = (c.spots || [])[(c.spots || []).length - 1] || (c.zones || [])[(c.zones || []).length - 1];
    if (last && typeof last.lat === 'number') return { lat: last.lat, lon: last.lon };
    return null;
  }

  function createSpot(pts) {
    var c = cam();
    var g = baseGeo();
    var sp = {
      id: nextId('spot'),
      type: 'standard',
      enabled: true,
      quad: pts.map(function (p) { return [Math.round(p[0]), Math.round(p[1])]; }),
      lat: g ? g.lat : null,
      lon: g ? g.lon : null,
      approach: { lat: g ? g.lat : null, lon: g ? g.lon : null }
    };
    c.spots = c.spots || [];
    c.spots.push(sp);
    S.sel = { kind: 'spot', i: c.spots.length - 1, v: null };
    if (longSide(sp.quad) < MIN_SPOT_LONG_SIDE_PX) {
      flash('Длинная сторона ' + Math.round(longSide(sp.quad)) + ' px < ' +
        MIN_SPOT_LONG_SIDE_PX + ' px — валидатор отклонит это место');
    }
    if (!g) flash('Поставьте lat/lon по 2ГИС — без них валидатор ругнётся');
    refresh();
  }

  function createZone(pts) {
    var c = cam();
    var g = baseGeo();
    var z = {
      id: nextId('zone'),
      name: 'Зона',
      capacity: Math.max(1, pts.length),
      polygon: pts.map(function (p) { return [Math.round(p[0]), Math.round(p[1])]; }),
      lat: g ? g.lat : null,
      lon: g ? g.lon : null,
      entrance: { lat: g ? g.lat : null, lon: g ? g.lon : null }
    };
    c.zones = c.zones || [];
    c.zones.push(z);
    S.sel = { kind: 'zone', i: c.zones.length - 1, v: null };
    flash('Поставьте capacity — сколько машин реально помещается в зону');
    refresh();
  }

  function createRect(kind, r) {
    var c = cam();
    var fs = frameSize();
    var rect = [
      Math.round(clamp(Math.min(r.x0, r.x1), 0, fs[0])),
      Math.round(clamp(Math.min(r.y0, r.y1), 0, fs[1])),
      Math.round(clamp(Math.max(r.x0, r.x1), 0, fs[0])),
      Math.round(clamp(Math.max(r.y0, r.y1), 0, fs[1]))
    ];
    if (rect[2] - rect[0] < 2 || rect[3] - rect[1] < 2) return;
    if (kind === 'overlay') { c.overlay_exclude = c.overlay_exclude || []; c.overlay_exclude.push(rect); S.sel = { kind: 'overlay', i: c.overlay_exclude.length - 1, v: null }; }
    else { c.tiles = c.tiles || []; c.tiles.push(rect); S.sel = { kind: 'tile', i: c.tiles.length - 1, v: null }; }
    refresh();
  }

  function duplicateSpot() {
    var c = cam();
    if (!S.sel || S.sel.kind !== 'spot') { flash('Дублирование работает только для мест'); return; }
    var src = c.spots[S.sel.i];
    var q = src.quad;
    // сдвиг на собственную ширину вдоль «верхней» стороны квада
    var dx = q[3][0] - q[0][0], dy = q[3][1] - q[0][1];
    var copy = JSON.parse(JSON.stringify(src));
    copy.id = nextId('spot');
    copy.quad = q.map(function (p) { return [Math.round(p[0] + dx), Math.round(p[1] + dy)]; });
    c.spots.push(copy);
    S.sel = { kind: 'spot', i: c.spots.length - 1, v: null };
    refresh();
    flash('Дубль поставлен линейно. На этой камере есть дисторсия — подвиньте углы по картинке.');
  }

  function deleteSel() {
    var c = cam();
    if (!S.sel) return;
    var arr = S.sel.kind === 'spot' ? c.spots
      : S.sel.kind === 'zone' ? c.zones
        : S.sel.kind === 'overlay' ? c.overlay_exclude : c.tiles;
    arr.splice(S.sel.i, 1);
    S.sel = null;
    refresh();
  }

  // --------------------------------------------------------------- //
  // События мыши
  // --------------------------------------------------------------- //

  canvas.addEventListener('wheel', function (e) {
    e.preventDefault();
    var p = eventPos(e);
    zoomAt(p[0], p[1], Math.pow(0.9988, e.deltaY));
  }, { passive: false });

  canvas.addEventListener('mousedown', function (e) {
    var p = eventPos(e);
    var ip = toImage(p[0], p[1]);

    if (e.button === 1 || e.button === 2 || S.space) {
      S.drag = { type: 'pan', sx: p[0], sy: p[1], vx: S.view.x, vy: S.view.y };
      canvas.classList.add('panning');
      e.preventDefault();
      return;
    }
    if (e.button !== 0) return;

    if (S.mode === 'overlay' || S.mode === 'tile') {
      S.rect = { kind: S.mode, x0: ip[0], y0: ip[1], x1: ip[0], y1: ip[1] };
      S.drag = { type: 'rect' };
      return;
    }

    if (S.mode === 'select') {
      var hv = hitVertex(ip);
      if (hv) {
        S.sel = hv;
        S.drag = { type: 'vertex', sel: hv };
        refreshSide();
        draw();
        return;
      }
      var hb = hitBody(ip);
      if (hb) {
        S.sel = hb;
        S.drag = { type: 'move', sel: hb, last: ip };
        refreshSide();
        draw();
        return;
      }
      S.sel = null;
      S.drag = { type: 'pan', sx: p[0], sy: p[1], vx: S.view.x, vy: S.view.y };
      canvas.classList.add('panning');
      refreshSide();
      draw();
      return;
    }

    // режимы рисования: клик = точка. Таскание вершины уже созданного — тоже работает.
    var hv2 = hitVertex(ip);
    if (hv2 && !S.pending) {
      S.sel = hv2;
      S.drag = { type: 'vertex', sel: hv2 };
      refreshSide();
      draw();
      return;
    }
    S.drag = { type: 'click', sx: p[0], sy: p[1], moved: false };
  });

  window.addEventListener('mousemove', function (e) {
    var p = eventPos(e);
    var ip = toImage(p[0], p[1]);
    lastCursor = ip;

    if (!S.drag) { updateHud(); return; }
    if (S.drag.type === 'pan') {
      S.view.x = S.drag.vx + (p[0] - S.drag.sx);
      S.view.y = S.drag.vy + (p[1] - S.drag.sy);
      draw();
    } else if (S.drag.type === 'vertex') {
      moveVertex(S.drag.sel, ip);
      draw();
      refreshSide();
    } else if (S.drag.type === 'move') {
      moveObject(S.drag.sel, ip[0] - S.drag.last[0], ip[1] - S.drag.last[1]);
      S.drag.last = ip;
      draw();
      refreshSide();
    } else if (S.drag.type === 'rect' && S.rect) {
      S.rect.x1 = ip[0]; S.rect.y1 = ip[1];
      draw();
    } else if (S.drag.type === 'click') {
      if (Math.hypot(p[0] - S.drag.sx, p[1] - S.drag.sy) > 4) S.drag.moved = true;
    }
    updateHud();
  });

  window.addEventListener('mouseup', function (e) {
    if (!S.drag) return;
    var d = S.drag;
    S.drag = null;
    canvas.classList.remove('panning');

    if (d.type === 'rect' && S.rect) {
      var r = S.rect; S.rect = null;
      createRect(r.kind, r);
      return;
    }
    if (d.type === 'click' && !d.moved) {
      var p = eventPos(e);
      addPendingPoint(toImage(p[0], p[1]));
    }
    if (d.type === 'vertex' || d.type === 'move') { refresh(); }
  });

  // Замыкание зоны двойным кликом.
  //
  // Точку-дубль от второго клика НЕ вычищаем здесь: сколько mouseup прилетит
  // перед dblclick — один или два — зависит от того, настоящая это мышь или
  // синтетика, и угадывать бессмысленно. Дубль гасится в addPendingPoint,
  // так что к этому моменту лишней точки уже нет.
  canvas.addEventListener('dblclick', function (e) {
    if (S.mode !== 'zone' || !S.pending || !S.pending.pts.length) return;
    e.preventDefault();
    closeZone();
  });

  canvas.addEventListener('contextmenu', function (e) { e.preventDefault(); });

  function addPendingPoint(ip) {
    var fs = frameSize();
    if (ip[0] < 0 || ip[1] < 0 || ip[0] > fs[0] || ip[1] > fs[1]) return;
    if (S.mode !== 'spot' && S.mode !== 'zone') return;
    if (!S.pending || S.pending.kind !== S.mode) S.pending = { kind: S.mode, pts: [] };
    // Второй клик двойного клика приходит сюда как обычная точка поверх
    // предыдущей. Гасим её здесь: иначе зона получает вырожденное ребро,
    // а место — две совпавшие вершины.
    var prev = S.pending.pts[S.pending.pts.length - 1];
    if (prev && dist(ip, prev) * S.view.scale < 5) return;
    S.pending.pts.push([ip[0], ip[1]]);
    if (S.mode === 'spot' && S.pending.pts.length === 4) {
      var pts = S.pending.pts;
      S.pending = null;
      createSpot(pts);
      return;
    }
    draw();
  }

  function closeZone() {
    if (!S.pending || S.pending.pts.length < 3) {
      flash('Зоне нужно минимум 3 точки');
      return;
    }
    var pts = S.pending.pts;
    S.pending = null;
    if (polyArea(pts) < 1) { flash('Вырожденный полигон — отменено'); draw(); return; }
    createZone(pts);
  }

  // --------------------------------------------------------------- //
  // Клавиатура
  // --------------------------------------------------------------- //

  window.addEventListener('keydown', function (e) {
    var t = e.target;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT')) {
      if (e.key === 'Escape') t.blur();
      return;
    }
    if (e.code === 'Space') { S.space = true; e.preventDefault(); return; }
    var k = e.key.toLowerCase();
    if (k === 'v') setMode('select');
    else if (k === 's') setMode('spot');
    else if (k === 'z' && !e.metaKey && !e.ctrlKey) setMode('zone');
    else if (k === 'o') setMode('overlay');
    else if (k === 't') setMode('tile');
    else if (k === 'f') fitView();
    else if (k === 'd') duplicateSpot();
    else if (e.key === 'Enter') { if (S.mode === 'zone') closeZone(); }
    else if (e.key === 'Escape') { S.pending = null; S.rect = null; draw(); }
    else if (e.key === 'Delete' || e.key === 'Backspace') {
      if (S.pending && S.pending.pts.length) { S.pending.pts.pop(); draw(); }
      else { deleteSel(); }
      e.preventDefault();
    }
  });
  window.addEventListener('keyup', function (e) {
    if (e.code === 'Space') S.space = false;
  });

  function setMode(m) {
    S.mode = m;
    S.pending = null;
    S.rect = null;
    Array.prototype.forEach.call($('modes').children, function (b) {
      b.classList.toggle('active', b.dataset.mode === m);
    });
    canvas.className = m === 'select' ? 'mode-select' : '';
    if (m === 'spot') flash('Место: 4 клика по углам НАЗЕМНОГО СЛЕДА, по часовой от ближнего к камере левого угла');
    if (m === 'zone') flash('Зона: клики по контуру, двойной клик или Enter замыкает');
    draw();
  }

  // --------------------------------------------------------------- //
  // Боковая панель
  // --------------------------------------------------------------- //

  function refresh() { refreshSide(); draw(); updatePreview(); }

  function refreshSide() {
    renderList();
    renderProps();
    renderCounts();
  }

  function renderCounts() {
    var c = cam();
    if (!c) { $('counts').textContent = ''; return; }
    $('counts').textContent = (c.spots || []).length + ' мест · ' + (c.zones || []).length + ' зон';
  }

  function renderList() {
    var c = cam();
    var ul = $('objList');
    ul.innerHTML = '';
    if (!c) return;

    function group(title) {
      var li = document.createElement('li');
      li.className = 'grouphdr';
      li.textContent = title;
      ul.appendChild(li);
    }
    function row(kind, i, color, id, meta, bad, off) {
      var li = document.createElement('li');
      if (isSel(kind, i)) li.className = 'sel';
      if (off) li.className += ' off';
      var dot = document.createElement('span');
      dot.className = 'dot'; dot.style.background = color;
      var name = document.createElement('span');
      name.className = 'oid'; name.textContent = id;
      var m = document.createElement('span');
      m.className = 'meta' + (bad ? ' bad' : ''); m.textContent = meta;
      li.appendChild(dot); li.appendChild(name); li.appendChild(m);
      li.onclick = function () {
        S.sel = { kind: kind, i: i, v: null };
        focusOn(getPts(S.sel));
        refreshSide();
      };
      ul.appendChild(li);
    }

    if ((c.spots || []).length) {
      group('Места');
      c.spots.forEach(function (sp, i) {
        var q = sp.quad || [];
        var ls = q.length === 4 ? longSide(q) : 0;
        var bad = q.length === 4 && sp.enabled !== false && ls < MIN_SPOT_LONG_SIDE_PX;
        var meta = (q.length === 4 ? Math.round(ls) + '×' + Math.round(shortSide(q)) + ' px' : 'нет квада');
        if (typeof sp.lat !== 'number') { meta = 'нет lat/lon'; bad = true; }
        row('spot', i, sp.enabled === false ? COLORS.spotOff : COLORS.spot,
          sp.id || '(без id)', meta, bad, sp.enabled === false);
      });
    }
    if ((c.zones || []).length) {
      group('Зоны');
      c.zones.forEach(function (z, i) {
        var bad = typeof z.lat !== 'number' || !(z.capacity > 0);
        row('zone', i, COLORS.zone, z.id || '(без id)',
          (z.polygon || []).length + ' точек · cap ' + (z.capacity != null ? z.capacity : '—'), bad, false);
      });
    }
    if ((c.overlay_exclude || []).length) {
      group('Overlay exclude');
      c.overlay_exclude.forEach(function (r, i) {
        row('overlay', i, COLORS.overlay, 'overlay ' + i,
          (r[2] - r[0]) + '×' + (r[3] - r[1]) + ' px', false, false);
      });
    }
    if ((c.tiles || []).length) {
      group('Tiles');
      c.tiles.forEach(function (r, i) {
        row('tile', i, COLORS.tile, 'tile ' + i,
          (r[2] - r[0]) + '×' + (r[3] - r[1]) + ' px', false, false);
      });
    }
    if (!ul.children.length) {
      var li = document.createElement('li');
      li.className = 'grouphdr';
      li.textContent = 'пусто — нарисуйте место (S) или зону (Z)';
      ul.appendChild(li);
    }
  }

  function focusOn(pts) {
    if (!pts || !pts.length) { draw(); return; }
    var c0 = centroid(pts);
    var w = canvas.clientWidth, h = canvas.clientHeight;
    S.view.x = w / 2 - c0[0] * S.view.scale;
    S.view.y = h / 2 - c0[1] * S.view.scale;
    draw();
  }

  function field(label, value, onChange, opts) {
    opts = opts || {};
    var row = document.createElement('div');
    row.className = 'row';
    var lab = document.createElement('label');
    lab.textContent = label;
    row.appendChild(lab);
    var inp;
    if (opts.options) {
      inp = document.createElement('select');
      opts.options.forEach(function (o) {
        var opt = document.createElement('option');
        opt.value = o; opt.textContent = o;
        inp.appendChild(opt);
      });
      inp.value = value;
      inp.onchange = function () { onChange(inp.value); };
    } else if (opts.checkbox) {
      inp = document.createElement('input');
      inp.type = 'checkbox';
      inp.checked = !!value;
      inp.onchange = function () { onChange(inp.checked); };
    } else {
      inp = document.createElement('input');
      inp.type = opts.number ? 'number' : 'text';
      if (opts.number) inp.step = opts.step || 'any';
      inp.value = (value === null || value === undefined) ? '' : value;
      inp.onchange = function () {
        if (opts.number) {
          var v = parseFloat(inp.value);
          onChange(isFinite(v) ? v : null);
        } else onChange(inp.value);
      };
    }
    row.appendChild(inp);
    return row;
  }

  function renderProps() {
    var box = $('props');
    box.innerHTML = '';
    var c = cam();
    if (!c || !S.sel) {
      box.innerHTML = '<p class="muted">Ничего не выделено.</p>';
      return;
    }

    if (S.sel.kind === 'spot') {
      var sp = c.spots[S.sel.i];
      if (!sp) { S.sel = null; return renderProps(); }
      box.appendChild(field('id', sp.id, function (v) { sp.id = v; refresh(); }));
      box.appendChild(field('type', sp.type || 'standard', function (v) { sp.type = v; refresh(); },
        { options: ['standard', 'disabled', 'electric'] }));
      box.appendChild(field('enabled', sp.enabled !== false, function (v) { sp.enabled = v; refresh(); },
        { checkbox: true }));
      box.appendChild(field('lat', sp.lat, function (v) { sp.lat = v; refresh(); }, { number: true, step: '0.000001' }));
      box.appendChild(field('lon', sp.lon, function (v) { sp.lon = v; refresh(); }, { number: true, step: '0.000001' }));
      var sub = document.createElement('div');
      sub.className = 'sub'; sub.textContent = 'Точка подъезда';
      box.appendChild(sub);
      sp.approach = sp.approach || { lat: null, lon: null };
      box.appendChild(field('approach.lat', sp.approach.lat, function (v) { sp.approach.lat = v; refresh(); }, { number: true, step: '0.000001' }));
      box.appendChild(field('approach.lon', sp.approach.lon, function (v) { sp.approach.lon = v; refresh(); }, { number: true, step: '0.000001' }));
      box.appendChild(field('notes', sp.notes || '', function (v) { if (v) sp.notes = v; else delete sp.notes; refresh(); }));

      var q = sp.quad || [];
      if (q.length === 4) {
        var ls = longSide(q), ss = shortSide(q);
        var info = document.createElement('p');
        var bad = ls < MIN_SPOT_LONG_SIDE_PX;
        info.className = bad ? 'warn' : 'good';
        info.innerHTML = 'Длинная сторона: <b>' + ls.toFixed(1) + ' px</b> (короткая ' + ss.toFixed(1) + ' px)' +
          (bad ? '<br>⚠ меньше ' + MIN_SPOT_LONG_SIDE_PX + ' px — детектор машину не увидит, ' +
            'валидатор отклонит. Переведите участок в зону или снимите enabled.' : '');
        box.appendChild(info);
      } else {
        var w = document.createElement('p');
        w.className = 'warn';
        w.textContent = 'В кваде ' + q.length + ' точек вместо 4.';
        box.appendChild(w);
      }
      addGeoHint(box);
      addDeleteBtn(box);
      return;
    }

    if (S.sel.kind === 'zone') {
      var z = c.zones[S.sel.i];
      if (!z) { S.sel = null; return renderProps(); }
      box.appendChild(field('id', z.id, function (v) { z.id = v; refresh(); }));
      box.appendChild(field('name', z.name || '', function (v) { z.name = v; refresh(); }));
      box.appendChild(field('capacity', z.capacity, function (v) { z.capacity = v === null ? null : Math.round(v); refresh(); }, { number: true, step: '1' }));
      box.appendChild(field('lat', z.lat, function (v) { z.lat = v; refresh(); }, { number: true, step: '0.000001' }));
      box.appendChild(field('lon', z.lon, function (v) { z.lon = v; refresh(); }, { number: true, step: '0.000001' }));
      var sub2 = document.createElement('div');
      sub2.className = 'sub'; sub2.textContent = 'Точка въезда';
      box.appendChild(sub2);
      z.entrance = z.entrance || { lat: null, lon: null };
      box.appendChild(field('entrance.lat', z.entrance.lat, function (v) { z.entrance.lat = v; refresh(); }, { number: true, step: '0.000001' }));
      box.appendChild(field('entrance.lon', z.entrance.lon, function (v) { z.entrance.lon = v; refresh(); }, { number: true, step: '0.000001' }));
      var p = document.createElement('p');
      p.className = 'muted';
      p.textContent = 'Точек: ' + (z.polygon || []).length + ' · площадь ' +
        Math.round(polyArea(z.polygon || [])) + ' px². capacity — сколько машин реально помещается.';
      box.appendChild(p);
      addGeoHint(box);
      addDeleteBtn(box);
      return;
    }

    var r = S.sel.kind === 'overlay' ? c.overlay_exclude[S.sel.i] : c.tiles[S.sel.i];
    if (!r) { S.sel = null; return renderProps(); }
    ['x1', 'y1', 'x2', 'y2'].forEach(function (nm, idx) {
      box.appendChild(field(nm, r[idx], function (v) {
        if (v !== null) { r[idx] = Math.round(v); refresh(); }
      }, { number: true, step: '1' }));
    });
    var note = document.createElement('p');
    note.className = 'muted';
    note.textContent = S.sel.kind === 'overlay'
      ? 'Область оверлея исключается и из детекта, и из freeze-хеша: тикающий таймкод иначе маскирует зависшую картинку.'
      : 'Тайл детектора: прогоняем модель только по областям парковок, а не по всему кадру.';
    box.appendChild(note);
    addDeleteBtn(box);
  }

  function addGeoHint(box) {
    var p = document.createElement('p');
    p.className = 'muted';
    p.innerHTML = 'lat/lon ставятся руками по 2ГИС. Валидатор принимает только рамку Владивостока ' +
      '(42.8–43.5 / 131.5–132.3).';
    box.appendChild(p);
  }

  function addDeleteBtn(box) {
    var b = document.createElement('button');
    b.className = 'danger';
    b.textContent = 'Удалить';
    b.onclick = deleteSel;
    box.appendChild(b);
  }

  // --------------------------------------------------------------- //
  // Камеры и кадры
  // --------------------------------------------------------------- //

  function renderCameraSelect() {
    var sel = $('cameraSelect');
    sel.innerHTML = '';
    (S.doc.cameras || []).forEach(function (c, i) {
      var o = document.createElement('option');
      o.value = String(i);
      o.textContent = c.id + ' — ' + (c.name || '');
      sel.appendChild(o);
    });
    sel.value = String(S.camIdx);
  }

  function onCameraChange() {
    S.camIdx = parseInt($('cameraSelect').value, 10) || 0;
    S.sel = null;
    S.pending = null;
    var c = cam();

    var warn = $('warnCam');
    if (c && CAM_NOTES[c.id]) {
      warn.innerHTML = '<b>' + c.id + '.</b> ' + CAM_NOTES[c.id];
      warn.classList.remove('hidden');
    } else warn.classList.add('hidden');

    // шаблон id по номеру камеры: CAM-003 -> S-3 / Z-3
    if (c) {
      var m = /(\d+)\s*$/.exec(c.id);
      if (m) {
        var n = parseInt(m[1], 10);
        $('spotTpl').value = 'S-' + n;
        $('zoneTpl').value = 'Z-' + n;
        $('spotNext').value = String(((c.spots || []).length) + 1).padStart(2, '0');
        $('zoneNext').value = String(((c.zones || []).length) + 1).padStart(2, '0');
      }
    }

    loadFrameList();
    refresh();
    loadImage(defaultImagePath());
    fitView();
  }

  function defaultImagePath() {
    var c = cam();
    return c && c.reference_frame ? c.reference_frame : null;
  }

  function loadFrameList() {
    var c = cam();
    var sel = $('frameSelect');
    sel.innerHTML = '';
    if (!c) return;

    function add(value, label) {
      var o = document.createElement('option');
      o.value = value; o.textContent = label;
      sel.appendChild(o);
    }
    if (c.reference_frame) add(c.reference_frame, 'эталон · ' + c.reference_frame);

    fetch('api/frames?camera=' + encodeURIComponent(c.id))
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (data) {
        S.hasServer = true;
        (data.images || []).forEach(function (im) {
          if (im.path === c.reference_frame) return;
          add(im.path, im.label);
        });
        sel.value = defaultImagePath() || (data.images[0] && data.images[0].path) || '';
      })
      .catch(function () {
        // работаем из-под обычного python -m http.server: берём только эталон
        S.hasServer = false;
        sel.value = defaultImagePath() || '';
      });
  }

  function imageUrl(path) {
    return S.hasServer ? 'api/image?path=' + encodeURIComponent(path) : '../../' + path;
  }

  function loadImage(path) {
    if (!path) { S.img = null; S.imgLabel = ''; draw(); return; }
    var img = new Image();
    img.onload = function () {
      S.img = img;
      S.imgLabel = path;
      var fs = frameSize();
      if (img.naturalWidth !== fs[0] || img.naturalHeight !== fs[1]) {
        setStatus('кадр ' + img.naturalWidth + '×' + img.naturalHeight +
          ' ≠ frame_size ' + fs[0] + '×' + fs[1] + ' — координаты поедут', 'err');
      } else {
        setStatus(path + ' · ' + img.naturalWidth + '×' + img.naturalHeight, 'ok');
      }
      if (!S.fitted) fitView(); else draw();
    };
    img.onerror = function () {
      S.img = null;
      setStatus('не удалось загрузить ' + path + ' (запустите server.py)', 'err');
      draw();
    };
    img.src = imageUrl(path);
  }

  // --------------------------------------------------------------- //
  // Импорт / экспорт YAML
  // --------------------------------------------------------------- //

  // Комментарии round-trip не переживают — шапку файла сохраняем отдельно.
  function splitHeader(text) {
    var lines = text.split(/\r?\n/);
    var head = [];
    for (var i = 0; i < lines.length; i++) {
      var t = lines[i].trim();
      if (t === '' || t[0] === '#') head.push(lines[i]);
      else break;
    }
    while (head.length && head[head.length - 1].trim() === '') head.pop();
    return head.join('\n');
  }

  function normalizeDoc(doc) {
    (doc.cameras || []).forEach(function (c) {
      c.spots = c.spots || [];
      c.zones = c.zones || [];
      c.overlay_exclude = c.overlay_exclude || [];
      c.tiles = c.tiles || [];
      c.spots.forEach(function (s) {
        s.approach = s.approach || { lat: null, lon: null };
        if (s.enabled === undefined) s.enabled = true;
        if (!s.type) s.type = 'standard';
      });
      c.zones.forEach(function (z) { z.entrance = z.entrance || { lat: null, lon: null }; });
    });
    return doc;
  }

  function loadYamlText(text, label) {
    var doc;
    try {
      doc = PVYaml.parse(text);
    } catch (err) {
      setStatus('YAML не разобран: ' + err.message, 'err');
      return false;
    }
    if (!doc || !doc.cameras || !doc.cameras.length) {
      setStatus('в файле нет списка cameras', 'err');
      return false;
    }
    S.doc = normalizeDoc(doc);
    S.header = splitHeader(text);
    S.camIdx = 0;
    renderCameraSelect();
    onCameraChange();
    setStatus('загружено: ' + label, 'ok');
    return true;
  }

  function exportYaml() {
    // Пустые списки не печатаем ключом с [] там, где это мусор, —
    // но overlay_exclude/tiles/spots/zones оставляем явными: так конфиг читается.
    var body = PVYaml.dump(S.doc, { keyOrder: KEY_ORDER, maxFlow: 12 });
    var stamp = new Date().toISOString().replace('T', ' ').slice(0, 19);
    var head = S.header ? S.header + '\n' : '';
    head += '# Отредактировано в tools/annotate (' + stamp + ' UTC).\n' +
      '# Комментарии внутри структуры редактор не сохраняет — пишите пояснения\n' +
      '# в поле notes места/зоны или в шапку этого файла.\n';
    return head + '\n' + body;
  }

  function updatePreview() {
    if (!S.doc) return;
    try {
      $('yamlPreview').value = exportYaml();
    } catch (err) {
      $('yamlPreview').value = 'ошибка генерации YAML: ' + err.message;
    }
  }

  function showProblems(payload) {
    var box = $('problems');
    box.classList.remove('hidden', 'ok');
    box.innerHTML = '';
    var p = document.createElement('p');
    if (payload.ok) {
      box.classList.add('ok');
      p.textContent = payload.message || 'Конфиг валиден.';
      box.appendChild(p);
      return;
    }
    p.textContent = payload.message || 'Валидатор нашёл проблемы:';
    box.appendChild(p);
    var ul = document.createElement('ul');
    (payload.problems || []).forEach(function (t) {
      var li = document.createElement('li');
      li.textContent = t;
      ul.appendChild(li);
    });
    box.appendChild(ul);
  }

  function postConfig(url, force) {
    var text;
    try { text = exportYaml(); } catch (err) {
      setStatus('не смог сгенерировать YAML: ' + err.message, 'err');
      return;
    }
    fetch(url + (force ? '?force=1' : ''), {
      method: 'POST',
      headers: { 'Content-Type': 'text/yaml; charset=utf-8' },
      body: text
    }).then(function (r) {
      return r.json().then(function (j) { return { status: r.status, body: j }; });
    }).then(function (res) {
      showProblems(res.body);
      if (res.body.ok) setStatus(res.body.message || 'готово', 'ok');
      else setStatus('валидатор нашёл ' + (res.body.problems || []).length + ' проблем(ы)', 'err');
    }).catch(function (err) {
      setStatus('сервер недоступен (' + err.message + ') — запустите tools/annotate/server.py', 'err');
      showProblems({ ok: false, message: 'Нет связи с server.py. Скачайте YAML кнопкой и положите в config/cameras.yml вручную.', problems: [] });
    });
  }

  function download() {
    var text;
    try { text = exportYaml(); } catch (err) {
      setStatus('не смог сгенерировать YAML: ' + err.message, 'err');
      return;
    }
    var blob = new Blob([text], { type: 'text/yaml;charset=utf-8' });
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'cameras.yml';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 1000);
  }

  // --------------------------------------------------------------- //
  // Инициализация
  // --------------------------------------------------------------- //

  function bind() {
    $('cameraSelect').onchange = onCameraChange;
    $('frameSelect').onchange = function () { loadImage($('frameSelect').value); };
    $('btnReloadFrames').onclick = loadFrameList;
    $('fileImage').onchange = function (e) {
      var f = e.target.files[0];
      if (!f) return;
      var url = URL.createObjectURL(f);
      var img = new Image();
      img.onload = function () {
        S.img = img;
        var fs = frameSize();
        setStatus(f.name + ' · ' + img.naturalWidth + '×' + img.naturalHeight +
          (img.naturalWidth !== fs[0] ? ' ≠ frame_size!' : ''),
          img.naturalWidth !== fs[0] ? 'err' : 'ok');
        draw();
      };
      img.src = url;
    };
    $('fileYaml').onchange = function (e) {
      var f = e.target.files[0];
      if (!f) return;
      f.text().then(function (t) { loadYamlText(t, f.name); updatePreview(); });
    };

    Array.prototype.forEach.call($('modes').children, function (b) {
      b.onclick = function () { setMode(b.dataset.mode); };
    });

    $('btnZoomIn').onclick = function () { zoomAt(canvas.clientWidth / 2, canvas.clientHeight / 2, 1.25); };
    $('btnZoomOut').onclick = function () { zoomAt(canvas.clientWidth / 2, canvas.clientHeight / 2, 0.8); };
    $('btnZoomFit').onclick = fitView;

    $('btnLoadServer').onclick = loadFromServer;
    $('btnValidate').onclick = function () { postConfig('api/validate', false); };
    $('btnExport').onclick = download;
    $('btnSave').onclick = function () {
      if (!confirm('Записать config/cameras.yml? Предыдущая версия уйдёт в config/backups/.')) return;
      postConfig('api/config', false);
    };

    ['spotTpl', 'zoneTpl', 'spotNext', 'zoneNext', 'baseLat', 'baseLon'].forEach(function (id) {
      var el = $(id);
      if (el) el.onchange = function () { try { localStorage.setItem('pv.' + id, el.value); } catch (e) {} };
      if (el) { try { var v = localStorage.getItem('pv.' + id); if (v !== null && (id === 'baseLat' || id === 'baseLon')) el.value = v; } catch (e) {} }
    });

    window.addEventListener('resize', resizeCanvas);
  }

  function loadFromServer() {
    fetch('api/config')
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.text(); })
      .then(function (t) { S.hasServer = true; loadYamlText(t, 'config/cameras.yml (сервер)'); updatePreview(); })
      .catch(function () {
        // запасной путь: отдача статики из корня cv-service
        fetch('../../config/cameras.yml')
          .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.text(); })
          .then(function (t) { loadYamlText(t, 'config/cameras.yml (статика)'); updatePreview(); })
          .catch(function (err) {
            setStatus('не нашёл cameras.yml: ' + err.message + ' — импортируйте файл вручную', 'err');
          });
      });
  }

  // Поля базовых координат добавляем из JS, чтобы разметка HTML
  // не расходилась с логикой автоподстановки.
  function injectGeoDefaults() {
    var host = document.querySelector('#side .panel .idtpl');
    var div = document.createElement('div');
    div.className = 'idtpl';
    div.innerHTML = 'Базовые <label>lat <input type="number" id="baseLat" step="0.000001" size="9"></label>' +
      '<label>lon <input type="number" id="baseLon" step="0.000001" size="9"></label>';
    host.parentNode.insertBefore(div, host);
    var hint = document.createElement('p');
    hint.className = 'muted';
    hint.style.fontSize = '11px';
    hint.textContent = 'Подставляются новым объектам. Уточняйте каждую точку по 2ГИС.';
    host.parentNode.insertBefore(hint, div.nextSibling);
  }

  injectGeoDefaults();
  bind();
  resizeCanvas();
  setMode('select');
  loadFromServer();
  setStatus('загружаю config/cameras.yml…');
})();
