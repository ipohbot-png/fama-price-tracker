/* FAMA Price Tracker — dashboard app.
 * Static, no build step. Hash routing. Relative paths only (GitHub Pages subpath).
 *
 * Contract B v2 (DESIGN.md §4):
 *   latest.json and latest_state/<STATE>.json share one shape:
 *     { date: <headline archive date>, rows: [{ id, name, unit, level, price,
 *       date, n, dod, dod_from, wow, wow_from, reliable }] }
 *   - `date` on a ROW is that reading's own date and MAY be older than the
 *     document's headline `date`. The UI discloses it whenever they differ.
 *   - `dod` / `wow` are ABSOLUTE RM changes against the readings dated `dod_from`
 *     / `wow_from`. Those gaps are NOT guaranteed to be 1 and 7 days, so the UI
 *     labels every change "vs <date>" and never claims "day on day".
 *   - `n` = number of market rows behind the mean.
 *   - `reliable:false` = the comparison is distorted by a basket change; the row
 *     stays in the table but is excluded from Top Movers.
 *   catalog.json products carry only { id, name, kategori, unit, grades }.
 * Percentages are derived as dod / (price - dod).
 */
(function () {
  'use strict';

  // ------------------------------------------------------------- constants --
  var DATA = 'data/';
  var LEVELS = ['Ladang', 'Borong', 'Runcit'];
  var LEVEL_SUB = { Ladang: 'farm-gate', Borong: 'wholesale', Runcit: 'retail' };
  // Categorical slots in fixed order — colour follows the entity, never its rank.
  var LEVEL_SLOT = { Ladang: 1, Borong: 2, Runcit: 3 };
  var RANGES = [
    { id: '7', label: '7D', days: 7 },
    { id: '14', label: '14D', days: 14 },
    { id: '30', label: '30D', days: 30 },
    { id: 'all', label: 'All', days: null }
  ];
  var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  var LS = { scope: 'fama.scope', theme: 'fama.theme', focus: 'fama.focusLevel' };

  // ----------------------------------------------------------------- state --
  var store = { catalog: null, latest: null, meta: null, error: null };
  var seriesCache = {};        // slug -> Promise<seriesDoc>
  var stateLatestCache = {};   // stateName -> Promise<{date, rows}>
  var charts = [];             // live ECharts instances for the current view

  var ui = {
    scope: read(LS.scope, 'MY'),
    theme: read(LS.theme, 'auto'),
    focusLevel: read(LS.focus, 'Runcit'),
    query: '',
    sort: { key: 'name', dir: 1 },
    detail: { slug: null, state: null, range: '30', table: false },
    compare: { ids: [], level: 'Runcit', state: null, range: '30', table: false }
  };

  function read(k, dflt) {
    try { return localStorage.getItem(k) || dflt; } catch (e) { return dflt; }
  }
  function write(k, v) {
    try { localStorage.setItem(k, v); } catch (e) { /* private mode */ }
  }

  // ------------------------------------------------------------- DOM utils --
  function h(tag, attrs, kids) {
    var e = document.createElement(tag), k, v;
    if (attrs) {
      for (k in attrs) {
        if (!Object.prototype.hasOwnProperty.call(attrs, k)) continue;
        v = attrs[k];
        if (v == null || v === false) continue;
        if (k === 'class') e.className = v;
        else if (k === 'text') e.textContent = v;      // data goes through here
        else if (k === 'html') e.innerHTML = v;        // literal markup only
        else if (k.slice(0, 2) === 'on') e.addEventListener(k.slice(2), v);
        else e.setAttribute(k, v === true ? '' : String(v));
      }
    }
    if (kids != null) add(e, kids);
    return e;
  }
  function add(parent, kids) {
    if (kids == null) return parent;
    if (Array.isArray(kids)) { kids.forEach(function (c) { add(parent, c); }); return parent; }
    parent.appendChild(typeof kids === 'string' ? document.createTextNode(kids) : kids);
    return parent;
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function $(sel, root) { return (root || document).querySelector(sel); }

  // ------------------------------------------------------------ formatting --
  function fmtRM(v, unit) {
    if (v == null || isNaN(v)) return '—';
    return 'RM ' + Number(v).toFixed(2) + (unit ? ' / ' + unitShort(unit) : '');
  }
  function unitShort(u) {
    if (!u) return '';
    var l = String(u).toLowerCase();
    if (l.indexOf('kilogram') === 0 || l === 'kg') return 'kg';
    if (l.indexOf('biji') === 0) return 'biji';
    if (l.indexOf('ikat') === 0) return 'ikat';
    return u;
  }
  function fmtDate(iso) {
    if (!iso) return '—';
    var p = String(iso).split('-');
    if (p.length !== 3) return iso;
    return Number(p[2]) + ' ' + MONTHS[Number(p[1]) - 1] + ' ' + p[0];
  }
  function fmtDateShort(iso) {
    if (!iso) return '—';
    var p = String(iso).split('-');
    if (p.length !== 3) return iso;
    return Number(p[2]) + ' ' + MONTHS[Number(p[1]) - 1];
  }
  function daysBetween(aIso, bIso) {
    var a = Date.parse(aIso + 'T00:00:00Z'), b = Date.parse(bIso + 'T00:00:00Z');
    if (isNaN(a) || isNaN(b)) return null;
    return Math.round((b - a) / 86400000);
  }
  /* Calendar arithmetic on ISO dates — the 7-day rule is days, not array steps. */
  function shiftDays(iso, delta) {
    var t = Date.parse(iso + 'T00:00:00Z');
    if (isNaN(t)) return null;
    return new Date(t + delta * 86400000).toISOString().slice(0, 10);
  }
  function todayIso() {
    var d = new Date();
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' +
      String(d.getDate()).padStart(2, '0');
  }

  /* Delta cue: arrow glyph + sign + colour. Never colour alone.
     Up = price rose (costlier) = red; down = price fell = green. */
  function deltaInfo(abs, price) {
    if (abs == null || price == null) return { dir: 'na', pct: null, abs: null };
    var prev = price - abs;
    var pct = (prev > 0.00001) ? (abs / prev) * 100 : null;
    var dir = abs > 0.0049 ? 'up' : (abs < -0.0049 ? 'down' : 'flat');
    return { dir: dir, pct: pct, abs: abs };
  }

  function gapPhrase(fromIso, atIso) {
    if (!fromIso || !atIso) return '';
    var g = daysBetween(fromIso, atIso);
    if (g == null) return '';
    return ' (' + g + ' day' + (g === 1 ? '' : 's') + ' earlier)';
  }

  /* ONE honest phrasing for every change figure in the app.
     opts = { from: 'YYYY-MM-DD'|null,   // dod_from / wow_from — the ACTUAL comparison date
              at:   'YYYY-MM-DD'|null,   // the reading's own date
              kind: 'prev' | 'week' }    // only used to word the "nothing to compare" title
     The visible label is "vs 23 Jul", never "day on day" — the source guarantees
     neither a 1-day nor a 7-day gap, so the UI states the date, not a period. */
  function deltaCell(abs, price, opts) {
    opts = opts || {};
    var kindWord = opts.kind === 'week' ? 'about a week back' : 'an earlier day';
    var d = deltaInfo(abs, price);
    if (d.dir === 'na') {
      return h('span', {
        class: 'delta', 'data-dir': 'na', text: '—',
        title: 'No reading from ' + kindWord + ' to compare against'
      });
    }
    var arrow = d.dir === 'up' ? '▲' : d.dir === 'down' ? '▼' : '–';
    var word = d.dir === 'up' ? 'up' : d.dir === 'down' ? 'down' : 'unchanged';
    var pctTxt = d.pct == null ? '' : (d.pct > 0 ? '+' : '') + d.pct.toFixed(1) + '%';
    var absTxt = (d.abs > 0 ? '+' : '') + d.abs.toFixed(2);
    var vsTxt = opts.from ? 'vs ' + fmtDateShort(opts.from) : '';
    var full = word + ' ' + absTxt + ' RM' + (opts.from
      ? ' versus the reading of ' + fmtDate(opts.from) + gapPhrase(opts.from, opts.at)
      : ' — no comparison date published');

    return h('span', { class: 'deltawrap' }, [
      h('span', { class: 'delta', 'data-dir': d.dir, title: full }, [
        h('span', { class: 'delta__arrow', 'aria-hidden': 'true', text: arrow }),
        ' ' + (pctTxt || absTxt)
      ]),
      pctTxt ? h('span', { class: 'muted num', text: ' ' + absTxt }) : null,
      vsTxt ? h('span', { class: 'delta__vs', title: full, text: vsTxt }) : null,
      h('span', { class: 'sr-only', text: ' ' + full + '. ' })
    ]);
  }

  /* Muted date chip — discloses a reading date that is OLDER than the headline
     date. Returns null when there is nothing worth disclosing. */
  function dateChip(iso, headlineIso) {
    if (!iso || !headlineIso || iso === headlineIso) return null;
    var gap = daysBetween(iso, headlineIso);
    return h('span', {
      class: 'datechip', text: fmtDateShort(iso),
      title: 'Reading of ' + fmtDate(iso) +
        (gap ? ' — ' + gap + ' day' + (gap === 1 ? '' : 's') + ' before ' + fmtDate(headlineIso) : '')
    });
  }

  /* "22 markets" — how many source rows are behind a mean. */
  function nLabel(n) {
    if (n == null || isNaN(n)) return null;
    return Number(n) === 1 ? '1 market' : Number(n) + ' markets';
  }

  // ---------------------------------------------------------------- theming --
  function tokens() {
    var cs = getComputedStyle(document.documentElement);
    function g(n) { return cs.getPropertyValue(n).trim(); }
    return {
      surface: g('--surface-1'), plane: g('--plane'),
      text: g('--text-primary'), text2: g('--text-secondary'), muted: g('--text-muted'),
      grid: g('--grid'), axis: g('--axis'), border: g('--border'),
      series: [g('--series-1'), g('--series-2'), g('--series-3'), g('--series-4')]
    };
  }
  function slotColor(n) { return tokens().series[(n - 1) % 4]; }

  function applyTheme() {
    if (ui.theme === 'auto') document.documentElement.removeAttribute('data-theme');
    else document.documentElement.setAttribute('data-theme', ui.theme);
    $('#theme-toggle').querySelectorAll('button').forEach(function (b) {
      b.setAttribute('aria-pressed', String(b.dataset.theme === ui.theme));
    });
  }

  // ------------------------------------------------------------- data load --
  function getJSON(path) {
    return fetch(path, { cache: 'no-cache' }).then(function (r) {
      if (!r.ok) throw new Error(path + ' → HTTP ' + r.status);
      return r.json();
    });
  }
  function loadSeries(slug) {
    if (!seriesCache[slug]) {
      seriesCache[slug] = getJSON(DATA + 'series/' + encodeURIComponent(slug) + '.json');
    }
    return seriesCache[slug];
  }

  /* State scope reads the aggregator's per-state snapshot — same shape as
     latest.json (Contract B v2). ONE request; no series scanning, no fallback.
     If the file is missing the caller shows the standard error card. */
  function loadStateLatest(stateName) {
    if (!stateLatestCache[stateName]) {
      stateLatestCache[stateName] =
        getJSON(DATA + 'latest_state/' + encodeURIComponent(stateName) + '.json');
    }
    return stateLatestCache[stateName];
  }

  /* Latest non-null reading of a series, plus the two changes the UI shows.
     Both comparisons are CALENDAR based and both carry their own date, matching
     the aggregator's latest.json rule exactly (DESIGN.md §4):
       dod / dod_from = the previous non-null point, whatever its date
       wow / wow_from = the nearest non-null point dated on or before (date − 7 days);
                        null when the archive has none — never a silent fallback to
                        arr[0], which used to be mislabelled "week on week".
     `dates` is ascending ISO and parallel to `arr`. */
  function pointsFor(dates, arr) {
    var idx = -1, i;
    for (i = arr.length - 1; i >= 0; i--) { if (arr[i] != null) { idx = i; break; } }
    if (idx < 0) {
      return { price: null, date: null, dod: null, dod_from: null, wow: null, wow_from: null };
    }
    var price = arr[idx], date = dates[idx];
    var prev = null, prevFrom = null, wk = null, wkFrom = null;
    for (i = idx - 1; i >= 0; i--) {
      if (arr[i] != null) { prev = arr[i]; prevFrom = dates[i]; break; }
    }
    var cutoff = shiftDays(date, -7);
    if (cutoff) {
      for (i = idx - 1; i >= 0; i--) {
        if (dates[i] > cutoff) continue;     // still inside the 7-day window
        if (arr[i] == null) continue;        // no reading collected that day
        wk = arr[i]; wkFrom = dates[i]; break;
      }
    }
    return {
      price: round2(price),
      date: date,
      dod: prev == null ? null : round2(price - prev),
      dod_from: prevFrom,
      wow: wk == null ? null : round2(price - wk),
      wow_from: wkFrom
    };
  }
  function round2(x) { return Math.round(x * 100) / 100; }

  // -------------------------------------------------------------- charting --
  function disposeCharts() {
    charts.forEach(function (c) { try { c.dispose(); } catch (e) {} });
    charts = [];
  }
  function makeChart(dom) {
    var c = echarts.init(dom, null, { renderer: 'canvas' });
    charts.push(c);
    return c;
  }
  window.addEventListener('resize', function () {
    charts.forEach(function (c) { try { c.resize(); } catch (e) {} });
  });

  function tooltipNode(rows, title) {
    var box = h('div');
    box.appendChild(h('div', {
      text: title,
      style: 'font-size:11px;opacity:.75;margin-bottom:5px'
    }));
    rows.forEach(function (r) {
      var line = h('div', { style: 'display:flex;align-items:center;gap:7px;margin-top:3px' });
      line.appendChild(h('span', {
        style: 'width:14px;height:3px;border-radius:2px;flex:none;background:' + r.color
      }));
      // value leads (Strong), series name follows (secondary)
      line.appendChild(h('strong', { text: r.value, style: 'font-variant-numeric:tabular-nums' }));
      line.appendChild(h('span', { text: r.name, style: 'opacity:.75;font-size:12px' }));
      box.appendChild(line);
    });
    return box;
  }

  function baseLineOption(t, dates, unit) {
    return {
      animation: false,
      grid: { left: 4, right: 8, top: 10, bottom: 4, containLabel: true },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'line', lineStyle: { color: t.axis, width: 1, type: 'solid' } },
        backgroundColor: t.surface,
        borderColor: t.axis,
        borderWidth: 1,
        padding: [8, 10],
        textStyle: { color: t.text, fontSize: 13, fontFamily: 'system-ui, -apple-system, "Segoe UI", sans-serif' },
        extraCssText: 'border-radius:8px;box-shadow:0 4px 16px rgba(0,0,0,.18);',
        formatter: function (params) {
          if (!params || !params.length) return '';
          var rows = [];
          params.forEach(function (p) {
            if (p.value == null) return;
            rows.push({ color: p.color, name: p.seriesName, value: 'RM ' + Number(p.value).toFixed(2) });
          });
          if (!rows.length) rows.push({ color: 'transparent', name: 'no reading', value: '—' });
          return tooltipNode(rows, fmtDate(dates[params[0].dataIndex]));
        }
      },
      xAxis: {
        type: 'category', data: dates, boundaryGap: false,
        axisLine: { lineStyle: { color: t.axis, width: 1 } },
        axisTick: { show: false },
        splitLine: { show: false },
        axisLabel: {
          color: t.muted, fontSize: 11, hideOverlap: true,
          formatter: function (v) { return fmtDateShort(v); }
        }
      },
      yAxis: {
        type: 'value', scale: true,
        axisLine: { show: false },
        axisTick: { show: false },
        splitLine: { lineStyle: { color: t.grid, width: 1, type: 'solid' } },
        axisLabel: {
          color: t.muted, fontSize: 11,
          formatter: function (v) { return v < 10 ? v.toFixed(2) : v.toFixed(0); }
        },
        name: 'RM / ' + unitShort(unit), nameGap: 10,
        nameTextStyle: { color: t.muted, fontSize: 11, align: 'left' },
        nameLocation: 'end'
      },
      series: []
    };
  }

  function lineSeries(t, name, data, color, withEndLabel) {
    // Sparse series (weekly-surveyed products: readings every ~7 days, nulls
    // between) would be invisible as a plain line: an isolated point has no
    // neighbour to form a segment with, and symbols are hidden by default.
    // For those, show dots at the actual readings and connect them with a
    // DASHED line — the dashes signal "no collection on the days between",
    // while dense daily series keep solid lines with honest gaps.
    var isolated = data.some(function (v, i) {
      return v != null &&
        (i === 0 || data[i - 1] == null) &&
        (i === data.length - 1 || data[i + 1] == null);
    });
    return {
      name: name, type: 'line', data: data,
      connectNulls: isolated,       // sparse: bridge uncollected days (dashed below);
                                    // dense: null = no reading → gap, never zero
      showSymbol: isolated,
      symbol: 'circle',
      symbolSize: isolated ? 7 : 9, // ≥8px marker on hover; visible dots when sparse
      sampling: 'lttb',
      lineStyle: { width: isolated ? 1.5 : 2, cap: 'round', join: 'round', color: color,
                   type: isolated ? [4, 4] : 'solid' },
      itemStyle: { color: color, borderColor: t.surface, borderWidth: 2 }, // 2px surface ring
      emphasis: { focus: 'series', scale: 1.1 },
      endLabel: withEndLabel ? {
        show: true, distance: 6, fontSize: 11,
        color: t.text2,             // text token, never the series colour
        formatter: function (p) { return p.value == null ? '' : Number(p.value).toFixed(2); }
      } : { show: false },
      labelLayout: withEndLabel ? { moveOverlap: 'shiftY' } : undefined
    };
  }

  function legendEl(items, asBar) {
    return h('div', { class: 'legend', role: 'list' },
      items.map(function (it) {
        return h('div', { class: 'legend__item', role: 'listitem' }, [
          h('span', {
            class: 'legend__key' + (asBar ? ' legend__key--bar' : ''),
            style: 'background:' + it.color, 'aria-hidden': 'true'
          }),
          h('span', { text: it.label })
        ]);
      })
    );
  }

  // ---------------------------------------------------------- table twin ----
  function seriesTable(dates, seriesList, unit) {
    var wrap = h('div', { class: 'tablewrap', style: 'max-height:340px;overflow:auto' });
    var tbl = h('table', { class: 'data data--dense' });
    var thead = h('thead');
    var hr = h('tr', null, [h('th', { text: 'Date' })]);
    seriesList.forEach(function (s) { hr.appendChild(h('th', { class: 'n', text: s.name })); });
    thead.appendChild(hr);
    tbl.appendChild(thead);
    var tb = h('tbody');
    for (var i = dates.length - 1; i >= 0; i--) {
      var any = seriesList.some(function (s) { return s.data[i] != null; });
      if (!any) continue;
      var tr = h('tr', null, [h('td', { 'data-label': 'Date', text: fmtDate(dates[i]) })]);
      /* eslint-disable no-loop-func */
      (function (idx) {
        seriesList.forEach(function (s) {
          tr.appendChild(h('td', {
            class: 'n', 'data-label': s.name,
            text: s.data[idx] == null ? '—' : Number(s.data[idx]).toFixed(2)
          }));
        });
      })(i);
      tb.appendChild(tr);
    }
    tbl.appendChild(tb);
    wrap.appendChild(tbl);
    wrap.appendChild(h('p', { class: 'card__note', style: 'margin:8px 0 0',
      text: 'All values in RM per ' + unitShort(unit) + '. “—” = no price collected that day.' }));
    return wrap;
  }

  function tableToggle(getState, setState) {
    return h('button', {
      type: 'button', class: 'btn', 'aria-pressed': String(getState()),
      text: getState() ? 'Hide table' : 'Show table',
      onclick: function () { setState(!getState()); }
    });
  }

  // ------------------------------------------------------------ empty state --
  function empty(icon, title, msg) {
    return h('div', { class: 'empty' }, [
      h('div', { class: 'empty__icon', 'aria-hidden': 'true', text: icon }),
      h('div', { class: 'empty__title', text: title }),
      msg ? h('p', { text: msg }) : null
    ]);
  }

  function sampleBanner() {
    if (!store.meta || !store.meta.sample) return document.createTextNode('');
    return h('div', { class: 'banner' }, [
      h('span', { 'aria-hidden': 'true', text: '⚠️' }),
      h('div', null, [
        h('strong', { text: 'Sample data. ' }),
        'These figures were generated to build and test the dashboard — they are not real FAMA prices. ' +
        'They disappear as soon as the scraper writes real files to site/data/.'
      ])
    ]);
  }

  // ================================================================ VIEWS ====

  // ------------------------------------------------------------- overview ---
  function overviewRows(latest) {
    var byId = {};
    (latest.rows || []).forEach(function (r) {
      var e = byId[r.id] || (byId[r.id] = {});
      e[r.level] = r;
    });
    return store.catalog.products.map(function (p) {
      return {
        id: p.id, name: p.name, unit: p.unit, kategori: p.kategori || '',
        levels: byId[p.id] || {}
      };
    });
  }

  function renderOverview(root) {
    root.appendChild(sampleBanner());

    var scopeLabel = ui.scope === 'MY' ? 'All Malaysia' : titleCase(ui.scope);
    var moversCard = h('section', { class: 'card' });
    var tableCard = h('section', { class: 'card' });
    root.appendChild(moversCard);
    root.appendChild(tableCard);

    function paint(latest) {
      clear(moversCard); clear(tableCard);

      if (!latest || !latest.rows || !latest.rows.length) {
        moversCard.appendChild(empty('📭', 'No prices for ' + scopeLabel,
          'The archive has no readings for this scope yet. Switch back to All Malaysia.'));
        if (tableCard.parentNode) tableCard.parentNode.removeChild(tableCard);
        return;
      }

      var rows = overviewRows(latest);
      var focus = ui.focusLevel;
      var headline = latest.date || null;

      // ---- top movers strip: biggest change vs each row's OWN previous reading.
      // reliable:false rows are excluded — their change reflects a changed basket
      // of reporting markets, not a real price move. They stay in the table below.
      var movers = [], suppressed = 0;
      rows.forEach(function (r) {
        var rec = r.levels[focus];
        if (!rec || rec.price == null || rec.dod == null) return;
        var d = deltaInfo(rec.dod, rec.price);
        if (d.pct == null || Math.abs(d.pct) < 0.05) return;
        if (rec.reliable === false) { suppressed++; return; }
        movers.push({ row: r, rec: rec, pct: d.pct });
      });
      movers.sort(function (a, b) { return Math.abs(b.pct) - Math.abs(a.pct); });
      var top = movers.slice(0, 10);   // fewer than 3 is fine — we show what exists

      moversCard.appendChild(h('div', { class: 'card__head' }, [
        h('h2', { text: 'Top movers' }),
        h('span', { class: 'card__note',
          text: 'Biggest change vs each product’s previous reading · ' +
            focus + ' (' + LEVEL_SUB[focus] + ') · ' + scopeLabel })
      ]));

      if (!top.length) {
        moversCard.appendChild(empty('😴', 'No movement',
          'No product changed price at ' + focus + ' level since its previous reading.' +
          (suppressed ? ' (' + suppressed + ' change' + (suppressed === 1 ? ' was' : 's were') +
            ' set aside — the set of reporting markets changed.)' : '')));
      } else {
        moversCard.appendChild(h('div', { class: 'movers' }, top.map(function (m) {
          var meta = ['per ' + unitShort(m.row.unit), nLabel(m.rec.n)].filter(Boolean).join(' · ');
          return h('a', {
            class: 'mover', href: '#/p/' + encodeURIComponent(m.row.id),
            title: m.row.name + ' — ' + fmtRM(m.rec.price, m.row.unit) +
              ' on ' + fmtDate(m.rec.date) +
              (nLabel(m.rec.n) ? ', mean of ' + nLabel(m.rec.n) : '')
          }, [
            h('div', { class: 'mover__name', text: m.row.name }),
            h('div', { class: 'mover__meta', text: meta }),
            h('div', { class: 'mover__price num' }, [
              fmtRM(m.rec.price),
              dateChip(m.rec.date, headline)
            ]),
            h('div', { style: 'margin-top:2px;font-size:.8125rem' },
              deltaCell(m.rec.dod, m.rec.price,
                { from: m.rec.dod_from, at: m.rec.date, kind: 'prev' }))
          ]);
        })));
        if (suppressed) {
          moversCard.appendChild(h('p', { class: 'card__note', style: 'margin:8px 0 0',
            text: suppressed + ' product' + (suppressed === 1 ? '' : 's') +
              ' left out: the set of reporting markets changed between the two readings, ' +
              'so the difference would describe the basket, not the price.' }));
        }
      }

      // ---- filter row (one row, above the table it scopes)
      tableCard.appendChild(h('div', { class: 'card__head' }, [
        h('h2', { text: 'Prices — ' + scopeLabel }),
        h('span', { class: 'card__note', id: 'ov-count' })
      ]));

      var searchInput = h('input', {
        class: 'field field--search', type: 'search', id: 'ov-search',
        placeholder: 'Search product, category or unit…',
        'aria-label': 'Search products',
        value: ui.query,
        oninput: function () { ui.query = this.value; drawTable(); }
      });

      var levelSeg = h('div', { class: 'seg seg--sm', role: 'group', 'aria-label': 'Level for change columns' },
        LEVELS.map(function (lv) {
          return h('button', {
            type: 'button', 'data-level': lv, text: lv,
            'aria-pressed': String(lv === ui.focusLevel),
            onclick: function () {
              ui.focusLevel = lv; write(LS.focus, lv); render();
            }
          });
        }));

      tableCard.appendChild(h('div', { class: 'filters' }, [
        searchInput,
        h('label', { for: 'ov-search', class: 'sr-only', text: 'Search products' }),
        h('span', { class: 'card__note', text: 'Change vs:' }),
        levelSeg
      ]));

      var tableHost = h('div');
      tableCard.appendChild(tableHost);
      tableCard.appendChild(h('p', { class: 'card__note', style: 'margin:10px 0 0' },
        [
          'Each cell is that product’s most recent reading on or before ' + fmtDate(headline) +
          '. A muted date beside a price means that reading is older than ' + fmtDate(headline) +
          '. Prices are means across all reporting markets, and every change states the date ' +
          'it is measured against — the gap is not always one day, or seven. ',
          h('span', { class: 'delta', 'data-dir': 'up', text: '▲ red' }),
          ' = price rose, ',
          h('span', { class: 'delta', 'data-dir': 'down', text: '▼ green' }),
          ' = price fell.'
        ]));

      function drawTable() {
        var q = ui.query.trim().toLowerCase();
        var shown = rows.filter(function (r) {
          if (!q) return true;
          return (r.name + ' ' + r.kategori + ' ' + r.unit).toLowerCase().indexOf(q) >= 0;
        });

        var key = ui.sort.key, dir = ui.sort.dir;
        shown = shown.slice().sort(function (a, b) {
          var va, vb;
          if (key === 'name') { va = a.name; vb = b.name; return va < vb ? -dir : va > vb ? dir : 0; }
          if (key === 'dod' || key === 'wow') {
            var ra = a.levels[ui.focusLevel], rb = b.levels[ui.focusLevel];
            va = ra ? deltaInfo(ra[key], ra.price).pct : null;
            vb = rb ? deltaInfo(rb[key], rb.price).pct : null;
          } else {
            va = a.levels[key] ? a.levels[key].price : null;
            vb = b.levels[key] ? b.levels[key].price : null;
          }
          if (va == null && vb == null) return 0;
          if (va == null) return 1;      // missing always sinks
          if (vb == null) return -1;
          return (va - vb) * dir;
        });

        var cnt = $('#ov-count');
        if (cnt) cnt.textContent = shown.length + ' of ' + rows.length + ' products';

        clear(tableHost);
        if (!shown.length) {
          tableHost.appendChild(empty('🔍', 'No products match “' + ui.query + '”',
            'Try a shorter word — names are in Bahasa Malaysia (e.g. “ayam”, “cili”, “ikan”).'));
          return;
        }

        var tbl = h('table', { class: 'data' });
        var head = h('tr');
        head.appendChild(sortTh('Product', 'name', ''));
        head.appendChild(h('th', { text: 'Unit' }));
        LEVELS.forEach(function (lv) {
          head.appendChild(sortTh(lv, lv, 'n', LEVEL_SUB[lv]));
        });
        // Honest headers: the gap to the comparison reading is data, not a promise.
        head.appendChild(sortTh('Δ prev', 'dod', 'n', ui.focusLevel,
          'Change against this product’s previous reading, whenever that was — each cell ' +
          'names the comparison date. Not necessarily one day.'));
        head.appendChild(sortTh('Δ 7-day', 'wow', 'n', ui.focusLevel,
          'Change against the nearest reading dated on or before seven calendar days earlier — ' +
          'each cell names the comparison date. “—” means the archive has none.'));
        tbl.appendChild(h('thead', null, head));

        var tb = h('tbody');
        shown.forEach(function (r) {
          var tr = h('tr');
          tr.appendChild(h('td', { class: 'c-name' }, [
            h('a', { href: '#/p/' + encodeURIComponent(r.id), text: r.name }),
            h('span', { class: 'rowunit', text: 'per ' + unitShort(r.unit) })
          ]));
          tr.appendChild(h('td', { class: 'c-unit muted', text: unitShort(r.unit) }));
          LEVELS.forEach(function (lv) {
            var rec = r.levels[lv];
            var td = h('td', { class: 'n c-price', 'data-label': lv });
            if (!rec || rec.price == null) {
              td.appendChild(h('span', { class: 'muted', text: '—' }));
            } else {
              td.setAttribute('title', 'RM ' + Number(rec.price).toFixed(2) +
                ' — reading of ' + fmtDate(rec.date) +
                (nLabel(rec.n) ? ', mean of ' + nLabel(rec.n) : ''));
              td.appendChild(document.createTextNode(Number(rec.price).toFixed(2)));
              var chip = dateChip(rec.date, headline);
              if (chip) td.appendChild(chip);
            }
            tr.appendChild(td);
          });
          var f = r.levels[ui.focusLevel];
          tr.appendChild(h('td', { class: 'n c-change', 'data-label': 'Δ prev · ' + ui.focusLevel },
            f ? deltaCell(f.dod, f.price, { from: f.dod_from, at: f.date, kind: 'prev' })
              : h('span', { class: 'muted', text: '—' })));
          tr.appendChild(h('td', { class: 'n c-change', 'data-label': 'Δ 7-day · ' + ui.focusLevel },
            f ? deltaCell(f.wow, f.price, { from: f.wow_from, at: f.date, kind: 'week' })
              : h('span', { class: 'muted', text: '—' })));
          tb.appendChild(tr);
        });
        tbl.appendChild(tb);
        tableHost.appendChild(h('div', { class: 'tablewrap' }, tbl));
      }

      function sortTh(label, key, cls, sub, tip) {
        var active = ui.sort.key === key;
        var th = h('th', {
          class: 'sortable ' + (cls || ''),
          title: tip || null,
          'aria-sort': active ? (ui.sort.dir === 1 ? 'ascending' : 'descending') : 'none',
          onclick: function () {
            if (ui.sort.key === key) ui.sort.dir = -ui.sort.dir;
            else { ui.sort.key = key; ui.sort.dir = key === 'name' ? 1 : -1; }
            drawTable();
          }
        }, [
          label,
          sub ? h('span', { class: 'muted', style: 'font-weight:400', text: ' · ' + sub }) : null,
          tip ? h('span', { class: 'sr-only', text: '. ' + tip }) : null,
          h('span', { class: 'caret', 'aria-hidden': 'true', text: active ? (ui.sort.dir === 1 ? ' ▲' : ' ▼') : ' ' })
        ]);
        return th;
      }

      drawTable();
    }

    if (ui.scope === 'MY') {
      paint(store.latest);
      return;
    }

    // State scope = ONE file: data/latest_state/<STATE>.json. No fallback scan.
    moversCard.appendChild(empty('⏳', 'Loading ' + scopeLabel + ' prices…', null));
    if (tableCard.parentNode) tableCard.parentNode.removeChild(tableCard);

    loadStateLatest(ui.scope).then(function (res) {
      if (routeOf() !== 'overview' || ui.scope === 'MY') return;
      if (!tableCard.parentNode) root.appendChild(tableCard);
      paint(res);
    }).catch(function (err) {
      if (routeOf() !== 'overview' || ui.scope === 'MY') return;
      clear(moversCard);
      moversCard.appendChild(empty('⚠️', 'Could not load ' + scopeLabel + ' prices',
        String((err && err.message) || err) +
        ' — this view needs site/data/latest_state/' + ui.scope + '.json. ' +
        'Re-run the aggregator, or switch back to All Malaysia.'));
    });
  }

  function titleCase(s) {
    return String(s).toLowerCase().replace(/\b[a-z]/g, function (c) { return c.toUpperCase(); });
  }

  // -------------------------------------------------------- product detail --
  function renderDetail(root, slug) {
    var prod = null;
    store.catalog.products.some(function (p) { if (p.id === slug) { prod = p; return true; } return false; });

    root.appendChild(h('a', { class: 'linkback', href: '#/', text: '← All products' }));
    root.appendChild(sampleBanner());

    if (!prod) {
      root.appendChild(h('section', { class: 'card' },
        empty('🤷', 'Product not found', 'No product with id “' + slug + '” is in the catalog.')));
      return;
    }

    var card = h('section', { class: 'card' });
    root.appendChild(card);
    card.appendChild(h('div', { class: 'card__head' }, [
      h('h2', { text: prod.name }),
      h('span', { class: 'card__note',
        text: [prod.kategori, 'per ' + unitShort(prod.unit),
          prod.grades && prod.grades.length ? 'grade ' + prod.grades.join('/') : null]
          .filter(Boolean).join(' · ') })
    ]));

    var tilesHost = h('div', { class: 'tiles' });
    var chartCard = h('section', { class: 'card' });
    root.appendChild(chartCard);
    card.appendChild(tilesHost);

    loadSeries(slug).then(function (doc) {
      if (routeOf() !== 'product') return;
      var stateNames = (store.catalog.states || []).filter(function (s) {
        return doc.by_state && doc.by_state[s];
      });
      if (ui.detail.slug !== slug) {
        ui.detail.slug = slug;
        ui.detail.state = (ui.scope !== 'MY' && stateNames.indexOf(ui.scope) >= 0) ? ui.scope : 'MY';
      }
      var headline = (doc.dates && doc.dates.length) ? doc.dates[doc.dates.length - 1] : null;

      function currentBlock() {
        return ui.detail.state === 'MY' ? doc.national : (doc.by_state[ui.detail.state] || {});
      }
      function sliced() {
        var r = RANGES.filter(function (x) { return x.id === ui.detail.range; })[0] || RANGES[3];
        var n = doc.dates.length;
        var from = r.days == null ? 0 : Math.max(0, n - r.days);
        var block = currentBlock();
        return {
          dates: doc.dates.slice(from),
          data: LEVELS.map(function (lv) {
            return { level: lv, arr: (block[lv] || []).slice(from) };
          })
        };
      }

      function paint() {
        clear(tilesHost); clear(chartCard);
        var block = currentBlock();
        var scopeName = ui.detail.state === 'MY' ? 'All Malaysia' : titleCase(ui.detail.state);

        // ---- summary tiles: latest reading + both changes, each with its own date
        LEVELS.forEach(function (lv) {
          var arr = block[lv] || [];
          var p = pointsFor(doc.dates, arr);
          var tile = h('div', { class: 'tile' }, [
            h('div', { class: 'tile__label' }, [
              h('span', { class: 'tile__key', 'aria-hidden': 'true',
                style: 'background:' + slotColor(LEVEL_SLOT[lv]) }),
              h('span', { text: lv + ' · ' + LEVEL_SUB[lv] })
            ])
          ]);
          if (p.price == null) {
            tile.appendChild(h('div', { class: 'tile__value tile__value--empty', text: 'Not collected' }));
            tile.appendChild(h('div', { class: 'tile__delta muted',
              text: 'No ' + lv + ' price for ' + scopeName }));
          } else {
            tile.appendChild(h('div', { class: 'tile__value' }, [
              fmtRM(p.price),
              dateChip(p.date, headline)
            ]));
            tile.appendChild(h('div', { class: 'tile__delta' }, [
              h('span', { class: 'tile__deltakey', text: 'prev' }),
              deltaCell(p.dod, p.price, { from: p.dod_from, at: p.date, kind: 'prev' })
            ]));
            tile.appendChild(h('div', { class: 'tile__delta' }, [
              h('span', { class: 'tile__deltakey', text: '7-day' }),
              deltaCell(p.wow, p.price, { from: p.wow_from, at: p.date, kind: 'week' })
            ]));
            tile.appendChild(h('div', { class: 'tile__delta muted',
              text: 'reading of ' + fmtDate(p.date) }));
          }
          tilesHost.appendChild(tile);
        });

        // ---- filter row
        chartCard.appendChild(h('div', { class: 'card__head' }, [
          h('h3', { text: 'Price history — ' + scopeName }),
          h('span', { class: 'card__note', text: 'RM per ' + unitShort(doc.unit) })
        ]));

        var stateSel = h('select', {
          class: 'field', 'aria-label': 'State',
          onchange: function () { ui.detail.state = this.value; paint(); }
        });
        stateSel.appendChild(h('option', { value: 'MY', text: 'All Malaysia' }));
        stateNames.forEach(function (s) {
          stateSel.appendChild(h('option', { value: s, text: titleCase(s) }));
        });
        stateSel.value = ui.detail.state;

        var rangeSeg = h('div', { class: 'seg seg--sm', role: 'group', 'aria-label': 'Date range' },
          RANGES.map(function (r) {
            return h('button', {
              type: 'button', text: r.label, 'aria-pressed': String(r.id === ui.detail.range),
              onclick: function () { ui.detail.range = r.id; paint(); }
            });
          }));

        chartCard.appendChild(h('div', { class: 'filters' }, [
          h('span', { class: 'card__note', text: 'State:' }), stateSel,
          h('span', { class: 'card__note', text: 'Range:' }), rangeSeg,
          tableToggle(function () { return ui.detail.table; },
            function (v) { ui.detail.table = v; paint(); })
        ]));

        var s = sliced();
        var live = s.data.filter(function (d) {
          return d.arr.some(function (v) { return v != null; });
        });

        if (!live.length) {
          chartCard.appendChild(empty('📉', 'No readings in this window',
            scopeName + ' has no ' + (ui.detail.range === 'all' ? '' : 'recent ') +
            'price data for this product. Try a longer range or All Malaysia.'));
          return;
        }

        var t = tokens();
        var host = h('div', { class: 'chart' });
        chartCard.appendChild(host);

        var opt = baseLineOption(t, s.dates, doc.unit);
        opt.grid.right = 34;   // room for the end labels
        opt.series = live.map(function (d) {
          return lineSeries(t, d.level, d.arr, slotColor(LEVEL_SLOT[d.level]), true);
        });
        var chart = makeChart(host);
        chart.setOption(opt);

        chartCard.appendChild(legendEl(live.map(function (d) {
          return { color: slotColor(LEVEL_SLOT[d.level]), label: d.level + ' (' + LEVEL_SUB[d.level] + ')' };
        })));

        var missing = LEVELS.filter(function (lv) {
          return live.every(function (d) { return d.level !== lv; });
        });
        if (missing.length) {
          chartCard.appendChild(h('p', { class: 'card__note', style: 'margin:8px 0 0',
            text: 'Not collected for ' + scopeName + ': ' + missing.join(', ') +
              '. Solid line breaks mean no price was collected that day; dashed lines join dots of weekly readings across uncollected days.' }));
        } else {
          chartCard.appendChild(h('p', { class: 'card__note', style: 'margin:8px 0 0',
            text: 'Solid line breaks mean no price was collected that day; dashed lines join dots of weekly readings across uncollected days.' }));
        }

        if (ui.detail.table) {
          chartCard.appendChild(seriesTable(s.dates, live.map(function (d) {
            return { name: d.level, data: d.arr };
          }), doc.unit));
        }
      }

      paint();
    }).catch(function (err) {
      if (routeOf() !== 'product') return;
      clear(chartCard);
      chartCard.appendChild(empty('⚠️', 'Could not load price history', String(err.message || err)));
    });
  }

  // ------------------------------------------------------------- compare ----
  function renderCompare(root) {
    root.appendChild(sampleBanner());
    var card = h('section', { class: 'card' });
    var chartCard = h('section', { class: 'card' });
    root.appendChild(card);
    root.appendChild(chartCard);

    var prods = store.catalog.products;
    if (ui.compare.state == null) ui.compare.state = (ui.scope === 'MY' ? 'MY' : ui.scope);
    if (!ui.compare.ids.length) {
      ui.compare.ids = prods.slice(0, 2).map(function (p) { return p.id; });
    }

    function paintPicker() {
      clear(card);
      card.appendChild(h('div', { class: 'card__head' }, [
        h('h2', { text: 'Compare products' }),
        h('span', { class: 'card__note',
          text: ui.compare.ids.length + ' of 4 selected (pick 2–4)' })
      ]));

      var search = h('input', {
        class: 'field field--search', type: 'search', placeholder: 'Filter product list…',
        'aria-label': 'Filter product list', value: ui.compareQuery || '',
        oninput: function () { ui.compareQuery = this.value; paintPicker(); }
      });

      var levelSeg = h('div', { class: 'seg seg--sm', role: 'group', 'aria-label': 'Price level' },
        LEVELS.map(function (lv) {
          return h('button', {
            type: 'button', text: lv, 'aria-pressed': String(lv === ui.compare.level),
            onclick: function () { ui.compare.level = lv; paintPicker(); paintChart(); }
          });
        }));

      var allStates = store.catalog.states || [];
      var stateSel = h('select', { class: 'field', 'aria-label': 'State',
        onchange: function () { ui.compare.state = this.value; paintChart(); } });
      stateSel.appendChild(h('option', { value: 'MY', text: 'All Malaysia' }));
      allStates.forEach(function (s) { stateSel.appendChild(h('option', { value: s, text: titleCase(s) })); });
      stateSel.value = ui.compare.state;

      var rangeSeg = h('div', { class: 'seg seg--sm', role: 'group', 'aria-label': 'Date range' },
        RANGES.map(function (r) {
          return h('button', {
            type: 'button', text: r.label, 'aria-pressed': String(r.id === ui.compare.range),
            onclick: function () { ui.compare.range = r.id; paintPicker(); paintChart(); }
          });
        }));

      card.appendChild(h('div', { class: 'filters' }, [
        search,
        h('span', { class: 'card__note', text: 'Level:' }), levelSeg,
        h('span', { class: 'card__note', text: 'State:' }), stateSel,
        h('span', { class: 'card__note', text: 'Range:' }), rangeSeg
      ]));

      var q = (ui.compareQuery || '').trim().toLowerCase();
      var list = prods.filter(function (p) {
        if (ui.compare.ids.indexOf(p.id) >= 0) return true;
        if (!q) return true;
        return (p.name + ' ' + (p.kategori || '')).toLowerCase().indexOf(q) >= 0;
      });

      if (!list.length) {
        card.appendChild(empty('🔍', 'No products match', 'Try a shorter search word.'));
        return;
      }

      card.appendChild(h('div', { class: 'picker' }, list.map(function (p) {
        var i = ui.compare.ids.indexOf(p.id);
        var on = i >= 0;
        var full = !on && ui.compare.ids.length >= 4;
        return h('button', {
          type: 'button', class: 'chip', 'aria-pressed': String(on),
          disabled: full, title: full ? 'Deselect one first (max 4)' : null,
          onclick: function () {
            if (on) ui.compare.ids.splice(i, 1);
            else if (ui.compare.ids.length < 4) ui.compare.ids.push(p.id);
            paintPicker(); paintChart();
          }
        }, [
          h('span', { class: 'chip__key', 'aria-hidden': 'true',
            style: on ? 'background:' + slotColor(i + 1) : '' }),
          h('span', { text: p.name })
        ]);
      })));
    }

    function paintChart() {
      clear(chartCard);
      var ids = ui.compare.ids.slice();
      if (ids.length < 2) {
        chartCard.appendChild(empty('➕', 'Pick at least two products',
          'Select 2–4 products above to overlay their ' + ui.compare.level + ' prices.'));
        return;
      }
      chartCard.appendChild(h('div', { class: 'card__head' }, [
        h('h3', { text: ui.compare.level + ' price — ' +
          (ui.compare.state === 'MY' ? 'All Malaysia' : titleCase(ui.compare.state)) }),
        h('span', { class: 'card__note', text: 'One level, one axis' })
      ]));
      var host = h('div', { class: 'chart chart--loading' });
      chartCard.appendChild(host);

      Promise.all(ids.map(loadSeries)).then(function (docs) {
        if (routeOf() !== 'compare') return;
        host.classList.remove('chart--loading');

        var units = {};
        docs.forEach(function (d) { units[unitShort(d.unit)] = true; });
        var unitList = Object.keys(units);

        var r = RANGES.filter(function (x) { return x.id === ui.compare.range; })[0] || RANGES[3];
        // union of dates across the picked products (they share the archive calendar)
        var dateSet = {};
        docs.forEach(function (d) { d.dates.forEach(function (x) { dateSet[x] = 1; }); });
        var dates = Object.keys(dateSet).sort();
        if (r.days != null) dates = dates.slice(Math.max(0, dates.length - r.days));

        var seriesList = docs.map(function (doc, i) {
          var block = ui.compare.state === 'MY' ? doc.national : ((doc.by_state || {})[ui.compare.state] || {});
          var arr = block[ui.compare.level] || [];
          var idx = {};
          doc.dates.forEach(function (dt, j) { idx[dt] = arr[j] == null ? null : arr[j]; });
          return {
            name: doc.name,
            // colour follows the entity's slot in the selection, fixed order, never cycled
            color: slotColor(i + 1),
            data: dates.map(function (dt) { return dt in idx ? idx[dt] : null; })
          };
        });

        var live = seriesList.filter(function (s) {
          return s.data.some(function (v) { return v != null; });
        });
        if (!live.length) {
          clear(host);
          host.classList.remove('chart');
          chartCard.appendChild(empty('📉', 'No data for this combination',
            'None of the selected products has ' + ui.compare.level + ' prices for ' +
            (ui.compare.state === 'MY' ? 'All Malaysia' : titleCase(ui.compare.state)) + ' in this window.'));
          return;
        }

        var t = tokens();
        var opt = baseLineOption(t, dates, docs[0].unit);
        opt.yAxis.name = unitList.length === 1 ? 'RM / ' + unitList[0] : 'RM';
        // No end labels here: up to 4 product lines can converge — legend + tooltip
        // + table view carry identity instead of stacked detached labels.
        opt.series = live.map(function (s) { return lineSeries(t, s.name, s.data, s.color, false); });
        var chart = makeChart(host);
        chart.setOption(opt);

        chartCard.appendChild(legendEl(live.map(function (s) {
          return { color: s.color, label: s.name };
        })));

        var notes = [];
        if (unitList.length > 1) {
          notes.push('⚠️ Mixed units (' + unitList.join(', ') + ') — the lines are not directly comparable.');
        }
        var dropped = seriesList.length - live.length;
        if (dropped > 0) notes.push(dropped + ' selected product(s) have no data here and are not drawn.');
        notes.push('Solid line breaks mean no price was collected that day; dashed lines join dots of weekly readings across uncollected days.');
        chartCard.appendChild(h('p', { class: 'card__note', style: 'margin:8px 0 0', text: notes.join(' ') }));

        chartCard.appendChild(h('div', { class: 'filters', style: 'margin:10px 0 0' }, [
          tableToggle(function () { return ui.compare.table; },
            function (v) { ui.compare.table = v; paintChart(); })
        ]));
        if (ui.compare.table) {
          chartCard.appendChild(seriesTable(dates, live, unitList.length === 1 ? docs[0].unit : ''));
        }
      }).catch(function (err) {
        if (routeOf() !== 'compare') return;
        clear(chartCard);
        chartCard.appendChild(empty('⚠️', 'Could not load series', String(err.message || err)));
      });
    }

    paintPicker();
    paintChart();
  }

  // --------------------------------------------------------------- about ----
  function renderAbout(root) {
    root.appendChild(sampleBanner());
    var meta = store.meta || {};
    var ut = meta.update_times || {};
    var byHour = ut.by_hour || {};

    var card = h('section', { class: 'card about' });
    root.appendChild(card);
    card.appendChild(h('h2', { text: 'About this tracker' }));
    card.appendChild(h('p', { text:
      'FAMA (Lembaga Pemasaran Pertanian Persekutuan) publishes daily Malaysian farm-produce ' +
      'prices at three market levels. This dashboard reads them from an archive kept in the ' +
      'project repository and renders them as static files — no server, no login.' }));

    card.appendChild(h('h3', { text: 'The three price levels' }));
    var lv = h('ul');
    lv.appendChild(h('li', null, [h('strong', { text: 'Ladang' }), ' — farm-gate: what the grower is paid.']));
    lv.appendChild(h('li', null, [h('strong', { text: 'Borong' }), ' — wholesale: pasar borong / distributor level.']));
    lv.appendChild(h('li', null, [h('strong', { text: 'Runcit' }), ' — retail: what you pay at the market.']));
    card.appendChild(lv);
    card.appendChild(h('p', { text:
      'Not every product is collected at every level — imported goods and most fish have no ' +
      'farm-gate price, so those cells read “—” and the chart line is simply absent.' }));

    card.appendChild(h('h3', { text: 'Where the numbers come from' }));
    card.appendChild(h('p', { text: meta.source ||
      'FAMA “Harga Pasaran Terkini” public Power BI report.' }));
    var src = h('ul');
    src.appendChild(h('li', { text:
      'The FAMA report exposes a rolling ~30-day window only — older days drop out of the ' +
      'source entirely. This project scrapes it twice daily and commits every day to git, so ' +
      'the repository is the long-term archive; history here goes back further than FAMA’s own page.' }));
    src.appendChild(h('li', { text:
      'A national figure is the mean of every reporting market’s price for that product, level ' +
      'and day (mean over rows, not a mean of state means).' }));
    card.appendChild(src);

    card.appendChild(h('h3', { text: 'How the change figures are worked out' }));
    card.appendChild(h('p', { text:
      'FAMA does not collect every product in every market every day. A product’s newest ' +
      'reading is therefore often older than the newest date in the archive, and the reading ' +
      'before it is rarely “yesterday”. One rule is applied everywhere — overview table, top ' +
      'movers and the product tiles — and every figure names the date it compares against.' }));
    var rule = h('ul');
    rule.appendChild(h('li', null, [
      h('strong', { text: 'Price' }),
      ' — the most recent reading on or before the headline date. When that reading is older ' +
      'than the headline date, its own date is shown in muted type beside the price.'
    ]));
    rule.appendChild(h('li', null, [
      h('strong', { text: 'Δ prev' }),
      ' — the price minus the previous reading that actually exists, whatever its date. The ' +
      'gap is frequently more than one day, so the cell reads “vs 23 Jul” rather than “day on day”.'
    ]));
    rule.appendChild(h('li', null, [
      h('strong', { text: 'Δ 7-day' }),
      ' — the price minus the nearest reading dated on or before seven calendar days earlier ' +
      '(calendar days, not seven rows back). If the archive holds no such reading the cell shows ' +
      '“—”; it is never quietly compared against the oldest point available and still called a week.'
    ]));
    rule.appendChild(h('li', null, [
      h('strong', { text: 'Market count' }),
      ' — every price is a mean over the markets that reported it that day, shown as “22 markets”. ' +
      'When that set changes sharply between the two readings, the difference describes the ' +
      'basket rather than the market, so the product is kept out of Top movers. It still appears ' +
      'in the table, with its change and comparison date.'
    ]));
    card.appendChild(rule);

    card.appendChild(h('h3', { text: 'Data status' }));
    var kv = h('dl', { class: 'kv' });
    function row(k, v) { kv.appendChild(h('dt', { text: k })); kv.appendChild(h('dd', { text: v })); }
    row('Latest price date', fmtDate(store.catalog.dates && store.catalog.dates.max));
    row('Earliest archived', fmtDate(store.catalog.dates && store.catalog.dates.min));
    row('Products tracked', String(store.catalog.products.length));
    row('States covered', String((store.catalog.states || []).length));
    if (meta.row_count) row('Archived rows', Number(meta.row_count).toLocaleString('en-MY'));
    row('Files generated at', meta.generated_at_utc ? String(meta.generated_at_utc) + ' (UTC)' : 'unknown');
    if (meta.sample) row('Data kind', 'SAMPLE — generated for UI development, not real FAMA prices');
    card.appendChild(kv);

    // ---- update-time histogram
    var hist = h('section', { class: 'card' });
    root.appendChild(hist);
    hist.appendChild(h('div', { class: 'card__head' }, [
      h('h3', { text: 'When FAMA enters prices' }),
      h('span', { class: 'card__note', text: ut.median_entry_local ? 'median ' + ut.median_entry_local : '' })
    ]));

    var hours = [], counts = [], total = 0;
    for (var i = 0; i < 24; i++) {
      var key = String(i).padStart(2, '0');
      hours.push(key + ':00');
      var c = Number(byHour[key] || 0);
      counts.push(c); total += c;
    }

    if (!total) {
      hist.appendChild(empty('🕓', 'No update-time data',
        'meta.json has no update_times.by_hour histogram yet.'));
    } else {
      var t = tokens();
      var host = h('div', { class: 'chart' });
      hist.appendChild(host);
      var c2 = makeChart(host);
      c2.setOption({
        animation: false,
        grid: { left: 4, right: 8, top: 10, bottom: 4, containLabel: true },
        tooltip: {
          trigger: 'item',
          backgroundColor: t.surface, borderColor: t.axis, borderWidth: 1, padding: [8, 10],
          textStyle: { color: t.text, fontSize: 13, fontFamily: 'system-ui, -apple-system, "Segoe UI", sans-serif' },
          extraCssText: 'border-radius:8px;box-shadow:0 4px 16px rgba(0,0,0,.18);',
          formatter: function (p) {
            return tooltipNode([{
              color: t.series[0],
              name: 'rows entered',
              value: Number(p.value).toLocaleString('en-MY')
            }], p.name + ' – ' + p.name.replace(/^(\d+)/, function (m) {
              return String((Number(m) + 1) % 24).padStart(2, '0');
            }));
          }
        },
        xAxis: {
          type: 'category', data: hours,
          axisLine: { lineStyle: { color: t.axis, width: 1 } },
          axisTick: { show: false },
          axisLabel: { color: t.muted, fontSize: 11, interval: 2,
            formatter: function (v) { return v.slice(0, 2); } }
        },
        yAxis: {
          type: 'value', min: 0,                 // bars grow from a single baseline
          axisLine: { show: false }, axisTick: { show: false },
          splitLine: { lineStyle: { color: t.grid, width: 1, type: 'solid' } },
          axisLabel: { color: t.muted, fontSize: 11,
            formatter: function (v) { return v >= 1000 ? (v / 1000) + 'k' : v; } }
        },
        series: [{
          type: 'bar', data: counts, name: 'Rows entered',
          barMaxWidth: 24, barCategoryGap: '30%',
          itemStyle: { color: t.series[0], borderRadius: [4, 4, 0, 0] },  // 4px rounded data-end
          emphasis: { itemStyle: { color: t.series[0], opacity: 0.82 } }
        }]
      });
      hist.appendChild(h('p', { class: 'card__note', style: 'margin:8px 0 0',
        text: (ut.note || '') + ' Hour of day (local, 24h) that rows were entered into the FAMA system.' }));

      var showTbl = h('div', { class: 'filters', style: 'margin:10px 0 0' });
      var tblHost = h('div');
      var open = false;
      showTbl.appendChild(h('button', {
        type: 'button', class: 'btn', text: 'Show table', 'aria-pressed': 'false',
        onclick: function () {
          open = !open;
          this.textContent = open ? 'Hide table' : 'Show table';
          this.setAttribute('aria-pressed', String(open));
          clear(tblHost);
          if (!open) return;
          var tbl = h('table', { class: 'data data--dense' });
          tbl.appendChild(h('thead', null, h('tr', null, [
            h('th', { text: 'Hour' }), h('th', { class: 'n', text: 'Rows entered' })
          ])));
          var tb = h('tbody');
          hours.forEach(function (hr, j) {
            tb.appendChild(h('tr', null, [
              h('td', { 'data-label': 'Hour', text: hr }),
              h('td', { class: 'n', 'data-label': 'Rows', text: counts[j].toLocaleString('en-MY') })
            ]));
          });
          tbl.appendChild(tb);
          tblHost.appendChild(h('div', { class: 'tablewrap' }, tbl));
        }
      }));
      hist.appendChild(showTbl);
      hist.appendChild(tblHost);
    }
  }

  // ================================================================ ROUTER ===
  function routeOf() {
    var hash = location.hash.replace(/^#/, '');
    if (hash.indexOf('/p/') === 0) return 'product';
    if (hash === '/compare') return 'compare';
    if (hash === '/about') return 'about';
    return 'overview';
  }
  function slugOf() {
    var hash = location.hash.replace(/^#/, '');
    return hash.indexOf('/p/') === 0 ? decodeURIComponent(hash.slice(3)) : null;
  }

  function render() {
    disposeCharts();
    var root = $('#view');
    clear(root);

    var route = routeOf();
    document.querySelectorAll('#tabs a').forEach(function (a) {
      var on = (a.dataset.route === route) || (route === 'product' && a.dataset.route === 'overview');
      if (on) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
    });

    if (store.error) {
      root.appendChild(h('section', { class: 'card' },
        empty('⚠️', 'Could not load the data files', store.error)));
      return;
    }
    if (!store.catalog) return;

    if (route === 'product') renderDetail(root, slugOf());
    else if (route === 'compare') renderCompare(root);
    else if (route === 'about') renderAbout(root);
    else renderOverview(root);
  }

  // ================================================================== BOOT ===
  function paintHeader() {
    var maxDate = (store.catalog && store.catalog.dates && store.catalog.dates.max) ||
      (store.latest && store.latest.date) || null;
    $('#brand-date').textContent = maxDate ? 'prices to ' + fmtDate(maxDate) : '';

    var fr = $('#freshness');
    if (!maxDate) { fr.hidden = true; return; }
    var age = daysBetween(maxDate, todayIso());
    var state = 'unknown', txt = 'Latest ' + fmtDate(maxDate);
    if (age != null) {
      if (age <= 0) { state = 'fresh'; txt = 'Updated today'; }
      else if (age === 1) { state = 'fresh'; txt = 'Updated yesterday'; }
      else if (age <= 3) { state = 'aging'; txt = age + ' days behind'; }
      else { state = 'stale'; txt = age + ' days behind'; }
    }
    fr.hidden = false;
    fr.setAttribute('data-state', state);
    fr.setAttribute('title', 'Most recent price date: ' + fmtDate(maxDate));
    $('#freshness-text').textContent = txt;
  }

  function wireChrome() {
    // scope toggle — persisted, default All Malaysia
    var seg = $('#scope-toggle');
    seg.querySelectorAll('button').forEach(function (b) {
      b.setAttribute('aria-pressed', String(b.dataset.scope === ui.scope));
      b.addEventListener('click', function () {
        if (ui.scope === b.dataset.scope) return;
        ui.scope = b.dataset.scope;
        write(LS.scope, ui.scope);
        seg.querySelectorAll('button').forEach(function (o) {
          o.setAttribute('aria-pressed', String(o.dataset.scope === ui.scope));
        });
        ui.detail.slug = null;                  // re-seed the detail state selector
        ui.compare.state = ui.scope === 'MY' ? 'MY' : ui.scope;
        render();
      });
    });

    $('#theme-toggle').querySelectorAll('button').forEach(function (b) {
      b.addEventListener('click', function () {
        ui.theme = b.dataset.theme;
        write(LS.theme, ui.theme);
        applyTheme();
        render();                                // charts re-read the theme tokens
      });
    });

    // OS theme flip while in Auto
    if (window.matchMedia) {
      var mq = window.matchMedia('(prefers-color-scheme: dark)');
      var onFlip = function () { if (ui.theme === 'auto') render(); };
      if (mq.addEventListener) mq.addEventListener('change', onFlip);
      else if (mq.addListener) mq.addListener(onFlip);
    }

    window.addEventListener('hashchange', render);
  }

  applyTheme();
  wireChrome();

  Promise.all([
    getJSON(DATA + 'catalog.json'),
    getJSON(DATA + 'latest.json').catch(function () { return { date: null, rows: [] }; }),
    getJSON(DATA + 'meta.json').catch(function () { return {}; })
  ]).then(function (res) {
    store.catalog = res[0];
    store.latest = res[1];
    store.meta = res[2];
    if (!store.catalog || !Array.isArray(store.catalog.products)) {
      throw new Error('catalog.json has no products array');
    }
    paintHeader();
    render();
  }).catch(function (err) {
    store.error = (err && err.message ? err.message : String(err)) +
      ' — the dashboard needs site/data/catalog.json. Run the aggregator, then reload.';
    $('#brand-date').textContent = '';
    $('#freshness').hidden = true;
    render();
  });
})();
