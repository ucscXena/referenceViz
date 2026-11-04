// Time-travel debugger. Activate with Shift+Ctrl+H.
// createDebugger(handlers, onRender) → { record, toggle, getDisplayState }

import { deepEqual } from './util.js'

const MAX_HISTORY = 200

export function createDebugger(handlers, onRender) {
  let baseState = null
  let history = []       // [{type, params, timestamp}]
  let travelIdx = -1     // -1 = live, >=0 = viewing that history entry
  let panelEl = null
  let displayedState = null

  const openPaths = new Set(['jobs', 'ui'])  // default open at top level

  let dedupEnabled = (() => {
    try { return sessionStorage.getItem('dbg-dedup') !== '0' } catch { return true }
  })()

  // ── Buffer ──────────────────────────────────────────────────────────────────

  function record(prevState, nextState, type, params) {
    if (dedupEnabled && deepEqual(nextState, prevState)) return
    if (baseState === null) baseState = prevState
    if (history.length >= MAX_HISTORY) {
      const dropped = history.shift()
      baseState = (handlers[dropped.type] ?? (s => s))(baseState, dropped.params)
      if (travelIdx > 0) travelIdx--
      // if travelIdx was 0, stays 0 (now points at new first entry)
    }
    history.push({ type, params, timestamp: Date.now() })
    if (panelEl?.classList.contains('dbg-visible')) refreshPanel()
  }

  function replayTo(i) {
    let s = baseState
    for (let j = 0; j <= i; j++)
      s = (handlers[history[j].type] ?? (s => s))(s, history[j].params)
    return s
  }

  function getDisplayState() {
    return travelIdx >= 0 ? replayTo(travelIdx) : null
  }

  function setTravel(i) {
    travelIdx = i
    onRender?.()  // re-render app with replayed or live state
    if (panelEl?.classList.contains('dbg-visible')) refreshPanel()
  }

  // ── Panel ────────────────────────────────────────────────────────────────────

  function toggle() {
    if (!panelEl) panelEl = buildPanel()
    const showing = panelEl.classList.toggle('dbg-visible')
    if (showing) refreshPanel()
    else if (travelIdx >= 0) setTravel(-1)
  }

  function buildPanel() {
    const style = document.createElement('style')
    style.textContent = `
      #dbg-panel {
        position: fixed; top: 0; right: 0; bottom: 0; width: 340px;
        background: #1e1e2e; color: #cdd6f4;
        font-family: 'Consolas','Menlo',monospace; font-size: 11px;
        z-index: 99999; display: flex; flex-direction: column;
        transform: translateX(100%); transition: transform 0.2s ease;
        box-shadow: -4px 0 20px rgba(0,0,0,0.5);
      }
      #dbg-panel.dbg-visible { transform: translateX(0); }
      #dbg-header {
        display: flex; align-items: center; gap: 8px;
        padding: 8px 12px; background: #181825; flex-shrink: 0;
        border-bottom: 1px solid #313244;
      }
      #dbg-title { font-weight: bold; color: #cba6f7; flex: 1; font-size: 12px; }
      #dbg-dedup-label {
        display: flex; align-items: center; gap: 3px;
        font-size: 10px; color: #6c7086; cursor: pointer; user-select: none; flex-shrink: 0;
      }
      #dbg-dedup-label:hover { color: #cdd6f4; }
      #dbg-dedup-cb { accent-color: #cba6f7; cursor: pointer; }
      #dbg-badge {
        font-size: 10px; padding: 2px 6px; border-radius: 3px;
        background: #a6e3a1; color: #1e1e2e; font-weight: bold;
      }
      #dbg-badge.travel { background: #f38ba8; }
      #dbg-close {
        background: none; border: none; color: #6c7086;
        cursor: pointer; font-size: 14px; padding: 0 2px; line-height: 1;
      }
      #dbg-close:hover { color: #f38ba8; }
      #dbg-list { flex: 1; overflow: auto; min-height: 0; }
      .dbg-row {
        display: flex; align-items: baseline; gap: 8px;
        padding: 3px 12px; cursor: pointer;
        border-left: 3px solid transparent; line-height: 1.5;
      }
      .dbg-row:hover { background: #2a2a3d; }
      .dbg-row.dbg-sel { background: #2a2a3d; border-left-color: #cba6f7; }
      .dbg-live-row { color: #a6e3a1; font-style: italic; }
      .dbg-live-row.dbg-sel { border-left-color: #a6e3a1; }
      .dbg-ts { color: #6c7086; font-size: 10px; flex-shrink: 0; }
      .dbg-action { color: #89b4fa; }
      .dbg-row { white-space: nowrap; min-width: max-content; }
      .dbg-p { color: #a6adc8; }
      #dbg-state {
        flex-shrink: 0; height: 240px; overflow-y: auto;
        border-top: 1px solid #313244; padding: 4px 0;
      }
      .dk  { color: #89dceb; }
      .dv  { color: #a6e3a1; }
      .dn  { color: #6c7086; }
      .dtog {
        cursor: pointer; color: #585b70; user-select: none;
        margin-right: 2px; font-size: 10px; flex-shrink: 0;
      }
      .dtog:hover { color: #cdd6f4; }
    `
    document.head.appendChild(style)

    const el = document.createElement('div')
    el.id = 'dbg-panel'
    el.innerHTML = `
      <div id="dbg-header">
        <span id="dbg-title">Debugger</span>
        <label id="dbg-dedup-label">
          <input type="checkbox" id="dbg-dedup-cb"> dedup
        </label>
        <span id="dbg-badge">Live</span>
        <button id="dbg-close">✕</button>
      </div>
      <div id="dbg-list"></div>
      <div id="dbg-state"></div>
    `
    el.addEventListener('click', e => e.stopPropagation())
    el.querySelector('#dbg-close').onclick = toggle
    const dedupCb = el.querySelector('#dbg-dedup-cb')
    dedupCb.checked = dedupEnabled
    dedupCb.addEventListener('change', () => {
      dedupEnabled = dedupCb.checked
      try { sessionStorage.setItem('dbg-dedup', dedupEnabled ? '1' : '0') } catch {}
    })
    document.body.appendChild(el)
    return el
  }

  // ── Action list ──────────────────────────────────────────────────────────────

  function fmtTs(t) {
    const d = new Date(t)
    return [d.getHours(), d.getMinutes(), d.getSeconds()]
      .map(n => String(n).padStart(2, '0')).join(':') +
      '.' + String(d.getMilliseconds()).padStart(3, '0')
  }

  function fmtParams(p) {
    try { return JSON.stringify(p) } catch { return String(p) }
  }

  function refreshPanel() {
    const isTravel = travelIdx >= 0
    panelEl.querySelector('#dbg-badge').textContent = isTravel ? 'Time travel' : 'Live'
    panelEl.querySelector('#dbg-badge').className = isTravel ? 'travel' : ''

    const list = panelEl.querySelector('#dbg-list')
    const frag = document.createDocumentFragment()

    history.forEach((e, i) => {
      const row = document.createElement('div')
      row.className = 'dbg-row' + (i === travelIdx ? ' dbg-sel' : '')
      row.innerHTML =
        `<span class="dbg-ts">${fmtTs(e.timestamp)}</span>` +
        `<span class="dbg-action">${e.type}</span>` +
        `<span class="dbg-p">${fmtParams(e.params)}</span>`
      row.addEventListener('click', () => setTravel(i))
      frag.appendChild(row)
    })

    // Live marker always at bottom — click to return to live
    const liveRow = document.createElement('div')
    liveRow.className = 'dbg-row dbg-live-row' + (!isTravel ? ' dbg-sel' : '')
    liveRow.textContent = '▶ live'
    liveRow.addEventListener('click', () => setTravel(-1))
    frag.appendChild(liveRow)

    list.replaceChildren(frag)
    if (!isTravel) list.scrollTop = list.scrollHeight

    displayedState = history.length
      ? (isTravel ? replayTo(travelIdx) : replayTo(history.length - 1))
      : null
    refreshStateDisplay()
  }

  // ── State tree ────────────────────────────────────────────────────────────────

  function refreshStateDisplay() {
    if (!panelEl) return
    const el = panelEl.querySelector('#dbg-state')
    el.replaceChildren()
    if (!displayedState) return
    for (const [k, v] of Object.entries(displayedState))
      renderNode(el, k, v, k, 0)
  }

  function getEntries(val) {
    if (val instanceof Map) return [...val.entries()].slice(0, 30)
    if (val instanceof Set) return [...val].slice(0, 30).map((v, i) => [i, v])
    if (Array.isArray(val)) return val.slice(0, 30).map((v, i) => [i, v])
    return Object.entries(val).slice(0, 30)
  }

  function collectionSize(val) {
    if (val instanceof Map || val instanceof Set) return val.size
    if (Array.isArray(val)) return val.length
    return Object.keys(val).length
  }

  function summarize(val) {
    if (val instanceof Map) return `Map(${val.size})`
    if (val instanceof Set) {
      const items = [...val].slice(0, 3).map(v => String(v).slice(0, 8))
      return `Set(${val.size}){${items.join(', ')}${val.size > 3 ? '…' : ''}}`
    }
    if (Array.isArray(val)) return `[…${val.length}]`
    const keys = Object.keys(val)
    return `{${keys.slice(0, 3).join(', ')}${keys.length > 3 ? '…' : ''}}`
  }

  function renderNode(container, key, val, path, depth) {
    const complex = val !== null && typeof val === 'object'
    const row = document.createElement('div')
    row.style.cssText =
      `padding-left:${depth * 14 + 8}px; line-height:1.6;` +
      `display:flex; align-items:baseline; gap:3px;`

    if (complex) {
      const open = openPaths.has(path)
      const tog = document.createElement('span')
      tog.className = 'dtog'
      tog.textContent = open ? '▼' : '▶'
      tog.addEventListener('click', (e) => {
        e.stopPropagation()
        openPaths.has(path) ? openPaths.delete(path) : openPaths.add(path)
        refreshStateDisplay()
      })
      row.appendChild(tog)

      const k = document.createElement('span')
      k.className = 'dk'
      k.textContent = String(key).slice(0, 30)
      row.appendChild(k)
      row.appendChild(document.createTextNode(':'))

      if (!open) {
        const sum = document.createElement('span')
        sum.className = 'dn'
        sum.textContent = ' ' + summarize(val)
        row.appendChild(sum)
      }
      container.appendChild(row)

      if (open) {
        for (const [ck, cv] of getEntries(val))
          renderNode(container, ck, cv, `${path}.${ck}`, depth + 1)
        const size = collectionSize(val)
        if (size > 30) {
          const more = document.createElement('div')
          more.style.cssText =
            `padding-left:${(depth + 1) * 14 + 8}px; color:#6c7086;`
          more.textContent = `… ${size - 30} more`
          container.appendChild(more)
        }
      }
    } else {
      const k = document.createElement('span')
      k.className = 'dk'
      k.textContent = String(key).slice(0, 30)
      row.appendChild(k)
      row.appendChild(document.createTextNode(': '))

      const v = document.createElement('span')
      if (val === null || val === undefined) {
        v.className = 'dn'; v.textContent = 'null'
      } else if (typeof val === 'string') {
        v.className = 'dv'; v.textContent = `"${val.slice(0, 60)}"`
      } else {
        v.className = 'dv'; v.textContent = String(val)
      }
      row.appendChild(v)
      container.appendChild(row)
    }
  }

  return { record, toggle, getDisplayState }
}
