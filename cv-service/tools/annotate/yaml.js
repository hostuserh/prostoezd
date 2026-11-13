/* ParkVision — минимальный YAML (подмножество) для редактора разметки.
 *
 * Зачем свой: инструмент обязан открываться без npm и без сборки,
 * а браузер YAML не умеет. Поддерживается ровно то, что встречается
 * в config/cameras.yml:
 *
 *   - вложенные отображения и списки по отступам;
 *   - элементы списка вида "- id: CAM-001" с продолжением на след. строках;
 *   - flow-последовательности [1, 2] и [[1,2],[3,4]];
 *   - скаляры: строки (в кавычках и без), числа, true/false, null/~;
 *   - комментарии "#" (вне кавычек).
 *
 * НЕ поддерживается: якоря/алиасы, многострочные блоки (| и >),
 * сложные ключи, несколько документов. Их в конфиге нет.
 *
 * ВАЖНО: round-trip НЕ сохраняет комментарии внутри структуры.
 * Шапку файла (комментарии до первой значащей строки) редактор
 * сохраняет отдельно — см. app.js (splitHeader).
 */
(function (global) {
  'use strict';

  // ------------------------------------------------------------------ //
  // Парсер
  // ------------------------------------------------------------------ //

  function stripComment(line) {
    let quote = null;
    for (let i = 0; i < line.length; i++) {
      const ch = line[i];
      if (quote) {
        if (ch === '\\' && quote === '"') { i++; continue; }
        if (ch === quote) quote = null;
      } else if (ch === '"' || ch === "'") {
        quote = ch;
      } else if (ch === '#' && (i === 0 || /\s/.test(line[i - 1]))) {
        return line.slice(0, i);
      }
    }
    return line;
  }

  function tokenize(text) {
    const out = [];
    const raw = text.split(/\r?\n/);
    for (let n = 0; n < raw.length; n++) {
      const line = stripComment(raw[n]).replace(/\s+$/, '');
      if (!line.trim()) continue;
      const indent = line.match(/^ */)[0].length;
      let body = line.slice(indent);
      let item = false;
      if (body === '-' || body.startsWith('- ')) {
        item = true;
        const inner = body === '-' ? '' : body.slice(2);
        const extra = inner.length - inner.replace(/^ +/, '').length;
        out.push({ indent: indent + 2 + extra, item: true, text: inner.trim(), line: n + 1 });
        continue;
      }
      out.push({ indent, item, text: body.trim(), line: n + 1 });
    }
    return out;
  }

  const KEY_RE = /^((?:"(?:[^"\\]|\\.)*")|(?:'(?:[^']|'')*')|[^:]+):(\s+.*|)$/;

  function isKeyLine(text) { return KEY_RE.test(text); }

  function parseTokens(L) {
    function node(i, indent) {
      if (i < L.length && L[i].item && L[i].indent === indent) return seq(i, indent);
      return map(i, indent, false);
    }

    function seq(i, indent) {
      const arr = [];
      while (i < L.length && L[i].indent === indent && L[i].item) {
        if (L[i].text === '') {
          const j = i + 1;
          if (j < L.length && L[j].indent > indent) {
            const r = node(j, L[j].indent);
            arr.push(r.value); i = r.i;
          } else { arr.push(null); i++; }
        } else if (isKeyLine(L[i].text)) {
          const r = map(i, indent, true);
          arr.push(r.value); i = r.i;
        } else {
          arr.push(scalar(L[i].text)); i++;
        }
      }
      return { value: arr, i };
    }

    function map(i, indent, firstIsItem) {
      const obj = {};
      let first = true;
      while (i < L.length && L[i].indent === indent) {
        if (L[i].item && !(first && firstIsItem)) break;
        const m = L[i].text.match(KEY_RE);
        if (!m) {
          throw new Error('строка ' + L[i].line + ': ожидалась пара "ключ: значение", получено ' + JSON.stringify(L[i].text));
        }
        const key = String(scalar(m[1].trim()));
        const rest = m[2].trim();
        if (rest === '') {
          const j = i + 1;
          if (j < L.length && L[j].indent > indent) {
            const r = node(j, L[j].indent);
            obj[key] = r.value; i = r.i;
          } else { obj[key] = null; i++; }
        } else {
          obj[key] = scalar(rest); i++;
        }
        first = false;
      }
      return { value: obj, i };
    }

    if (!L.length) return null;
    return node(0, L[0].indent).value;
  }

  function parse(text) {
    return parseTokens(tokenize(text));
  }

  // -- скаляры --------------------------------------------------------- //

  function scalar(s) {
    s = s.trim();
    if (s === '') return null;
    if (s[0] === '[' || s[0] === '{') return flow(s);
    if (s[0] === '"') return unescapeDouble(s);
    if (s[0] === "'") return s.slice(1, -1).replace(/''/g, "'");
    if (s === 'null' || s === '~' || s === 'Null' || s === 'NULL') return null;
    if (s === 'true' || s === 'True' || s === 'yes' || s === 'on') return true;
    if (s === 'false' || s === 'False' || s === 'no' || s === 'off') return false;
    if (/^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$/.test(s)) return Number(s);
    return s;
  }

  function unescapeDouble(s) {
    let out = '';
    for (let i = 1; i < s.length - 1; i++) {
      if (s[i] === '\\') {
        const c = s[++i];
        out += c === 'n' ? '\n' : c === 't' ? '\t' : c;
      } else out += s[i];
    }
    return out;
  }

  // Разбор flow-коллекции: [1, 2], [[1,2],[3,4]], {lat: 1, lon: 2}
  function flow(s) {
    let p = 0;
    function ws() { while (p < s.length && /\s/.test(s[p])) p++; }
    function value() {
      ws();
      if (s[p] === '[') {
        p++; const arr = [];
        ws();
        if (s[p] === ']') { p++; return arr; }
        for (;;) {
          arr.push(value()); ws();
          if (s[p] === ',') { p++; continue; }
          if (s[p] === ']') { p++; return arr; }
          throw new Error('flow: ожидались "," или "]" в ' + JSON.stringify(s));
        }
      }
      if (s[p] === '{') {
        p++; const obj = {};
        ws();
        if (s[p] === '}') { p++; return obj; }
        for (;;) {
          ws();
          const kStart = p;
          while (p < s.length && s[p] !== ':' && s[p] !== ',' && s[p] !== '}') p++;
          const key = String(scalar(s.slice(kStart, p).trim()));
          if (s[p] !== ':') throw new Error('flow: ожидалось ":" в ' + JSON.stringify(s));
          p++;
          obj[key] = value(); ws();
          if (s[p] === ',') { p++; continue; }
          if (s[p] === '}') { p++; return obj; }
          throw new Error('flow: ожидались "," или "}" в ' + JSON.stringify(s));
        }
      }
      if (s[p] === '"' || s[p] === "'") {
        const q = s[p]; const start = p; p++;
        while (p < s.length) {
          if (s[p] === '\\' && q === '"') { p += 2; continue; }
          if (s[p] === q) { p++; break; }
          p++;
        }
        return scalar(s.slice(start, p));
      }
      const start = p;
      while (p < s.length && !/[,\]\}]/.test(s[p])) p++;
      return scalar(s.slice(start, p).trim());
    }
    return value();
  }

  // ------------------------------------------------------------------ //
  // Дампер
  // ------------------------------------------------------------------ //

  const PLAIN_SAFE = /^[A-Za-z_][A-Za-z0-9_\-./]*$/;

  function quote(str) {
    return '"' + str.replace(/\\/g, '\\\\').replace(/"/g, '\\"').replace(/\n/g, '\\n') + '"';
  }

  function scalarOut(v) {
    if (v === null || v === undefined) return 'null';
    if (typeof v === 'boolean') return v ? 'true' : 'false';
    if (typeof v === 'number') {
      if (!isFinite(v)) throw new Error('нечисловое значение в YAML: ' + v);
      return String(Number.isInteger(v) ? v : Math.round(v * 1e6) / 1e6);
    }
    const s = String(v);
    if (s === '') return '""';
    if (PLAIN_SAFE.test(s) && !['true', 'false', 'null', 'yes', 'no', 'on', 'off', '~'].includes(s)) return s;
    return quote(s);
  }

  function isNumArray(v) {
    return Array.isArray(v) && v.length > 0 && v.every(function (x) { return typeof x === 'number'; });
  }

  // Короткие числовые массивы и массивы точек печатаем в одну строку —
  // так quad читается глазами, а diff остаётся обозримым.
  function flowOut(v) {
    if (isNumArray(v)) return '[' + v.map(scalarOut).join(', ') + ']';
    if (Array.isArray(v) && v.every(isNumArray)) {
      return '[' + v.map(function (p) { return '[' + p.map(scalarOut).join(', ') + ']'; }).join(', ') + ']';
    }
    return null;
  }

  function dump(value, opts) {
    opts = opts || {};
    const keyOrder = opts.keyOrder || {};
    const maxFlow = opts.maxFlow === undefined ? 10 : opts.maxFlow;
    const out = [];

    function orderedKeys(obj, path) {
      const order = keyOrder[path];
      const keys = Object.keys(obj);
      if (!order) return keys;
      const known = order.filter(function (k) { return k in obj; });
      const rest = keys.filter(function (k) { return order.indexOf(k) < 0; });
      return known.concat(rest);
    }

    function emit(v, indent, path) {
      const pad = ' '.repeat(indent);
      if (Array.isArray(v)) {
        if (v.length === 0) { return; }
        for (const el of v) {
          if (el !== null && typeof el === 'object' && !Array.isArray(el)) {
            const keys = orderedKeys(el, path + '[]');
            if (!keys.length) { out.push(pad + '- {}'); continue; }
            let firstLine = true;
            for (const k of keys) {
              const prefix = firstLine ? pad + '- ' : pad + '  ';
              emitPair(prefix, k, el[k], indent + 2, path + '[].' + k);
              firstLine = false;
            }
          } else if (Array.isArray(el)) {
            const f = flowOut(el);
            if (f !== null && el.length <= maxFlow) out.push(pad + '- ' + f);
            else { out.push(pad + '-'); emit(el, indent + 2, path + '[]'); }
          } else {
            out.push(pad + '- ' + scalarOut(el));
          }
        }
        return;
      }
      if (v !== null && typeof v === 'object') {
        for (const k of orderedKeys(v, path)) {
          emitPair(pad, k, v[k], indent, path + '.' + k);
        }
        return;
      }
      out.push(pad + scalarOut(v));
    }

    function emitPair(prefix, key, val, childIndent, path) {
      const k = scalarOut(key) + ':';
      if (Array.isArray(val)) {
        const f = flowOut(val);
        if (f !== null && val.length <= maxFlow) { out.push(prefix + k + ' ' + f); return; }
        if (val.length === 0) { out.push(prefix + k + ' []'); return; }
        out.push(prefix + k);
        emit(val, childIndent, path);
        return;
      }
      if (val !== null && typeof val === 'object') {
        const keys = Object.keys(val);
        if (!keys.length) { out.push(prefix + k + ' {}'); return; }
        // {lat, lon} печатаем инлайном — это одна сущность, а не структура
        if (keys.length <= 2 && keys.every(function (kk) { return typeof val[kk] === 'number'; })) {
          out.push(prefix + k + ' {' + orderedKeys(val, path).map(function (kk) {
            return scalarOut(kk) + ': ' + scalarOut(val[kk]);
          }).join(', ') + '}');
          return;
        }
        out.push(prefix + k);
        emit(val, childIndent + 2, path);
        return;
      }
      out.push(prefix + k + ' ' + scalarOut(val));
    }

    emit(value, 0, '');
    return out.join('\n') + '\n';
  }

  global.PVYaml = { parse: parse, dump: dump, stripComment: stripComment };
})(window);
