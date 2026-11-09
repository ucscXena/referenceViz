// Horizontal bar charts for reference detail pages, drawn with Observable Plot.
(function () {
  const BAR_GAP = 8;
  const BAR_HEIGHT = 24;
  const MARGIN_LEFT = 136;
  const TEXT_PADDING = 8;
  const TICKS = 6;
  const MAX_BAR_COUNT = 4;
  const BAR_COLOR = '#2081A2';
  const GRID_COLOR = '#E1E3E5';
  const INK = '#212B36';
  const WHITE = '#FFFFFF';
  const collator = new Intl.Collator('en', {numeric: true, sensitivity: 'base'});

  function sortPoints(points, order) {
    return [...points].sort(order.length
      ? (a, b) => order.indexOf(a.label) - order.indexOf(b.label)
      : (a, b) => b.count - a.count || collator.compare(a.label, b.label));
  }

  // Rounds up to the next "nice" axis limit.
  function getStop(n) {
    if (n <= 10) return 10;
    const magnitude = Math.pow(10, Math.floor(Math.log10(n)));
    for (const multiplier of [1, 1.5, 2, 2.5, 5, 7.5, 10]) {
      if (n <= magnitude * multiplier) return magnitude * multiplier;
    }
    return magnitude * 10;
  }

  function formatCountSize(value) {
    const units = ['k', 'M', 'G', 'T', 'P', 'E'];
    let val = value || 0;
    let power = 0;
    while (val >= 1000) {
      val /= 1000;
      power += 1;
    }
    return power ? `${val.toFixed(1)}${units[power - 1]}` : `${val}`;
  }

  function percentage(count, total) {
    if (!total || !count) return '0';
    const rounded = Math.round((count / total) * 1000) / 10;
    return rounded < 0.1 ? '< 0.1' : rounded.toFixed(1);
  }

  // Moves a count label outside its bar when it doesn't fit inside.
  function renderCountText(index, scales, values, dimensions, context, next) {
    const g = next(index, scales, values, dimensions, context);
    requestAnimationFrame(() => {
      g.querySelectorAll('text').forEach(text => {
        const ctm = text.getCTM();
        const box = text.getBBox();
        if (!ctm || ctm.e - box.width - TEXT_PADDING >= MARGIN_LEFT) return;
        const match = text.getAttribute('transform')?.match(/translate\(\s*([-\d.]+)[,\s]+([-\d.]+)\s*\)/);
        const [tx, ty] = match ? [parseFloat(match[1]), parseFloat(match[2])] : [0, 0];
        text.setAttribute('transform', `translate(${tx + box.width + TEXT_PADDING * 2}, ${ty})`);
        if (text.getAttribute('fill') === WHITE) text.setAttribute('fill', INK);
      });
    });
    return g;
  }

  function plotOptions(visible, all, width) {
    const total = all.reduce((sum, d) => sum + d.count, 0);
    const stop = all.length ? getStop(Math.max(...all.map(d => d.count))) : 0;
    const step = getStop(stop) / (TICKS - 1);
    const ticks = Array.from({length: TICKS}, (_, i) => i * step);
    const label = d => d.label;
    const count = d => `${d.count.toLocaleString('en-US')} (${percentage(d.count, total)}%)`;
    return {
      height: (visible.length + 2) * BAR_HEIGHT + (visible.length - 1) * BAR_GAP + 32,
      margin: 0,
      marginBottom: 32,
      marginLeft: MARGIN_LEFT,
      marks: [
        Plot.axisX({className: 'x-axis', tickFormat: formatCountSize, tickPadding: 8, tickSize: 0, ticks}),
        Plot.gridX({stroke: GRID_COLOR, strokeOpacity: 1, ticks}),
        Plot.barX(visible, {className: 'x-bar', fill: BAR_COLOR, rx1: 0, rx2: 4, x: 'count', y: 'label'}),
        Plot.text(visible, {
          className: 'text-category-label', dx: -TEXT_PADDING, dy: -2, fill: INK, lineHeight: 0.8125,
          lineWidth: (MARGIN_LEFT - TEXT_PADDING) / 13, text: label, textAnchor: 'end',
          textOverflow: 'ellipsis', title: label, x: 0, y: 'label',
        }),
        Plot.text(visible, {
          className: 'text-count', dx: -TEXT_PADDING, dy: -2, fill: () => WHITE, fontWeight: 500,
          lineHeight: 0.8125, render: renderCountText, text: count, textAnchor: 'end', x: 'count', y: 'label',
        }),
      ],
      style: {dominantBaseline: 'middle', font: 'inherit', overflow: 'visible'},
      width,
      x: {domain: [0, stop], grid: false, label: null, line: false},
      y: {
        domain: visible.map(label),
        label: null,
        line: false,
        paddingInner: BAR_GAP / (BAR_HEIGHT + BAR_GAP),
        paddingOuter: BAR_HEIGHT / (BAR_HEIGHT + BAR_GAP),
        tickFormat: () => '',
        tickSize: 0,
        type: 'band',
      },
    };
  }

  function mountChart(section, spec, paper) {
    const points = sortPoints(spec.points, spec.order);
    const expandable = spec.expandable !== false && points.length > MAX_BAR_COUNT;
    let expanded = !expandable;

    const heading = document.createElement('span');
    heading.className = 'ref-section-title';
    heading.textContent = spec.label;
    const plot = document.createElement('div');
    plot.className = 'ref-chart';
    section.append(heading, plot);

    let button = null;
    if (expandable) {
      button = document.createElement('button');
      button.type = 'button';
      button.className = 'ref-chart-toggle';
      button.addEventListener('click', () => {
        expanded = !expanded;
        draw();
        if (!expanded) section.scrollIntoView({behavior: 'smooth'});
      });
      section.append(button);
    }

    function draw() {
      const visible = expanded ? points : points.slice(0, MAX_BAR_COUNT);
      plot.replaceChildren(Plot.plot(plotOptions(visible, points, paper.offsetWidth - 40)));
      if (button) {
        button.textContent = expanded ? 'Show less' : `Show ${points.length - MAX_BAR_COUNT} additional results`;
      }
    }
    return draw;
  }

  const data = document.getElementById('reference-charts-data');
  const paper = document.getElementById('reference-charts');
  if (!data || !paper) return;

  const draws = JSON.parse(data.textContent).map(spec => {
    const section = document.createElement('div');
    section.className = 'ref-section';
    paper.append(section);
    return mountChart(section, spec, paper);
  });

  // Count labels are placed by measuring their text, so the chart fonts must be
  // loaded before the first draw. A failed load still draws, with fallback metrics.
  let width = 0;
  const startDrawing = () => new ResizeObserver(() => {
    if (paper.offsetWidth === width) return;
    width = paper.offsetWidth;
    draws.forEach(draw => draw());
  }).observe(paper);
  Promise.all(['400 13px Inter', '500 13px Inter'].map(font => document.fonts.load(font)))
    .finally(startDrawing);
})();
