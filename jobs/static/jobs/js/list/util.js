export function deepEqual(a, b) {
  if (a === b) return true
  if (a === null || b === null) return false
  if (typeof a !== 'object' || typeof b !== 'object') return false
  if (a instanceof Map && b instanceof Map) {
    if (a.size !== b.size) return false
    for (const [k, v] of a) {
      if (!b.has(k) || !deepEqual(v, b.get(k))) return false
    }
    return true
  }
  if (a instanceof Set && b instanceof Set) {
    if (a.size !== b.size) return false
    for (const v of a) if (!b.has(v)) return false
    return true
  }
  if (Array.isArray(a) && Array.isArray(b)) {
    if (a.length !== b.length) return false
    for (let i = 0; i < a.length; i++) if (!deepEqual(a[i], b[i])) return false
    return true
  }
  if (Array.isArray(a) !== Array.isArray(b)) return false
  const ka = Object.keys(a), kb = Object.keys(b)
  if (ka.length !== kb.length) return false
  for (const k of ka) {
    if (!Object.prototype.hasOwnProperty.call(b, k) || !deepEqual(a[k], b[k])) return false
  }
  return true
}

// assoc(obj, key, val) → same obj if deepEqual(val, existing), else new obj/Map with key set
export function assoc(obj, key, val) {
  const old = obj instanceof Map ? obj.get(key) : obj[key]
  if (deepEqual(val, old)) return obj
  if (obj instanceof Map) { const m = new Map(obj); m.set(key, val); return m }
  return { ...obj, [key]: val }
}

// dissoc(obj, key) → same obj if key absent, else new obj/Map with key removed
export function dissoc(obj, key) {
  if (obj instanceof Map) {
    if (!obj.has(key)) return obj
    const m = new Map(obj); m.delete(key); return m
  }
  if (!Object.prototype.hasOwnProperty.call(obj, key)) return obj
  const { [key]: _, ...rest } = obj
  return rest
}

// conj(set, val) → same Set if already member, else new Set with val added
export function conj(set, val) {
  if (set.has(val)) return set
  const s = new Set(set); s.add(val); return s
}

// disj(set, val) → same Set if not member, else new Set with val removed
export function disj(set, val) {
  if (!set.has(val)) return set
  const s = new Set(set); s.delete(val); return s
}

// pick(obj, keys) → plain object with only the keys that exist in obj
export function pick(obj, keys) {
  const result = {}
  for (const k of keys) if (k in obj) result[k] = obj[k]
  return result
}

// merge(obj, updates) → obj with each key-value in updates applied via assoc,
//   identity preserved where all values are deepEqual to existing
export function merge(obj, updates) {
  let result = obj
  for (const [k, v] of Object.entries(updates)) result = assoc(result, k, v)
  return result
}

// updateIn(obj, path, fn) → obj with fn applied at path, identity preserved if unchanged
export function updateIn(obj, [k, ...rest], fn) {
  const old = obj instanceof Map ? obj.get(k) : obj[k]
  const val = rest.length ? updateIn(old, rest, fn) : fn(old)
  return assoc(obj, k, val)
}
