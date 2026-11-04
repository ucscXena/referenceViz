// Minimal virtual DOM — h(tag, props, ...children) + patch(container, vnodes[])
const TEXT = '#text'

// Stable per-element listener that reads current handler from element property.
function listener(e) {
  this[`_on${e.type}`]?.(e)
}

function setProp(el, key, oldVal, newVal) {
  if (key === 'key') return
  if (key === 'class') {
    el.className = newVal ?? ''
    return
  }
  if (key === 'style') {
    if (typeof newVal === 'object') {
      const oldObj = typeof oldVal === 'object' ? (oldVal || {}) : {}
      for (const k in { ...oldObj, ...(newVal || {}) }) {
        el.style[k] = newVal?.[k] ?? ''
      }
    } else {
      el.setAttribute('style', newVal || '')
    }
    return
  }
  if (key.length > 2 && key[0] === 'o' && key[1] === 'n') {
    const evt = key.slice(2).toLowerCase()
    const sk = `_on${evt}`
    if (!oldVal && newVal) el.addEventListener(evt, listener)
    else if (oldVal && !newVal) el.removeEventListener(evt, listener)
    el[sk] = newVal || null
    return
  }
  // Set DOM IDL properties directly (handles boolean props like checked/disabled correctly)
  if (key !== 'list' && key !== 'form' && key in el && typeof newVal !== 'object') {
    if (newVal == null || newVal === false) {
      el[key] = typeof el[key] === 'boolean' ? false : ''
    } else {
      el[key] = newVal === true ? true : newVal
    }
    return
  }
  // Attribute-only: data-*, rowspan, colspan, custom attributes
  if (newVal == null || newVal === false) {
    el.removeAttribute(key)
  } else {
    el.setAttribute(key, newVal === true ? '' : String(newVal))
  }
}

function makeNode(vdom) {
  if (vdom.tag === TEXT) return document.createTextNode(vdom.text)
  const el = document.createElement(vdom.tag)
  for (const k in vdom.props) setProp(el, k, null, vdom.props[k])
  for (const c of vdom.children) el.appendChild(makeNode(c))
  return el
}

function patchNode(el, ov, nv) {
  if (nv.tag === TEXT) {
    if (ov.tag === TEXT) {
      if (ov.text !== nv.text) el.nodeValue = nv.text
      return el
    }
    const tn = document.createTextNode(nv.text)
    if (el.parentNode) el.parentNode.replaceChild(tn, el)
    return tn
  }
  if (ov.tag !== nv.tag) {
    const newEl = makeNode(nv)
    if (el.parentNode) el.parentNode.replaceChild(newEl, el)
    return newEl
  }
  const op = ov.props, np = nv.props
  for (const k in { ...op, ...np }) {
    if (op[k] !== np[k]) setProp(el, k, op[k], np[k])
  }
  patchChildren(el, ov.children, nv.children)
  return el
}

function patchChildren(parent, oldCh, newCh) {
  if (!oldCh.length && !newCh.length) return
  const domNodes = [...parent.childNodes]
  const oldByKey = new Map()
  const oldUnkeyed = []
  for (let i = 0; i < oldCh.length; i++) {
    const k = oldCh[i].key
    if (k != null) oldByKey.set(k, { ov: oldCh[i], el: domNodes[i] })
    else oldUnkeyed.push({ ov: oldCh[i], el: domNodes[i] })
  }

  const result = []
  let ukIdx = 0
  for (const nv of newCh) {
    if (nv.key != null && oldByKey.has(nv.key)) {
      const { ov, el } = oldByKey.get(nv.key)
      oldByKey.delete(nv.key)
      result.push(patchNode(el, ov, nv))
    } else if (nv.key == null && ukIdx < oldUnkeyed.length) {
      const { ov, el } = oldUnkeyed[ukIdx++]
      result.push(patchNode(el, ov, nv))
    } else {
      result.push(makeNode(nv))
    }
  }

  for (const { el } of oldByKey.values()) el.remove()
  while (ukIdx < oldUnkeyed.length) oldUnkeyed[ukIdx++].el.remove()

  // Insert/reorder backwards using insertBefore.
  // Check parentNode too: new nodes aren't in parent yet, so nextSibling can't
  // be compared against ref (which may itself be a not-yet-inserted new node).
  let ref = null
  for (let i = result.length - 1; i >= 0; i--) {
    if (result[i].parentNode !== parent || result[i].nextSibling !== ref)
      parent.insertBefore(result[i], ref)
    ref = result[i]
  }
}

export function h(tag, props, ...raw) {
  props = props || {}
  return { tag, props, key: props.key ?? null, children: flatChildren(raw) }
}

function flatChildren(raw) {
  const out = []
  for (const c of raw) {
    if (c == null || c === false || c === true) continue
    if (Array.isArray(c)) { out.push(...flatChildren(c)); continue }
    if (typeof c === 'string' || typeof c === 'number')
      out.push({ tag: TEXT, text: String(c), props: {}, key: null, children: [] })
    else out.push(c)
  }
  return out
}

// patch(container, vnodes) - renders vnodes as children of container
export function patch(container, vnodes) {
  patchChildren(container, container._ch || [], vnodes)
  container._ch = vnodes
}
