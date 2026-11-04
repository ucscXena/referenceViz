import { patch } from './superfine.js'
import { buildInitialState, handlers, effects } from './state.js'
import { renderJobs, renderPopovers } from './view.js'
import { createDebugger } from './debug.js'

// ── Bootstrap ─────────────────────────────────────────────────────────────────

const rawData = JSON.parse(document.getElementById('initial-state').textContent)

// Read CSRF token from the form's hidden input (always present in list.html)
rawData.csrf = document.querySelector('[name=csrfmiddlewaretoken]')?.value
  || (document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/) || [])[1]
  || ''

// Read delete URL from form action so Django's URL resolver is authoritative
rawData.deleteUrl = document.getElementById('job-list-form')?.action || ''

let state = buildInitialState(rawData)

const tbodyEl = document.getElementById('job-tbody')
const popoverRootEl = document.getElementById('popover-root')

const dbg = createDebugger(handlers, () => render())
document.addEventListener('keydown', (e) => {
  if (e.shiftKey && e.ctrlKey && e.key === 'H') dbg.toggle()
})

// ── Dispatch ──────────────────────────────────────────────────────────────────

function render() {
  const s = dbg.getDisplayState() ?? state
  patch(tbodyEl, renderJobs(s, dispatch))
  patch(popoverRootEl, renderPopovers(s, dispatch))
  // Keep delete button in sync
  const btn = document.querySelector('.btn-delete')
  if (btn) btn.disabled = s.ui.selected.size === 0
}

function dispatch(actionType, params = {}) {
  const prev = state
  state = (handlers[actionType] ?? (s => s))(state, params)
  dbg.record(prev, state, actionType, params)
  render()
  effects[actionType]?.(prev, state, params, dispatch)
}

// Initial render
render()

// ── Checkbox handling ─────────────────────────────────────────────────────────
// Clicks on dynamically-rendered checkboxes bubble up to tbody

tbodyEl.addEventListener('change', (e) => {
  const cb = e.target.closest('input[type="checkbox"][name="job_ids"]')
  if (!cb) return
  const jobId = cb.value
  dispatch(cb.checked ? 'SELECT_JOB' : 'DESELECT_JOB', { jobId })
})

// ── Delete form ───────────────────────────────────────────────────────────────

const listForm = document.getElementById('job-list-form')
if (listForm) {
  listForm.addEventListener('submit', (e) => {
    e.preventDefault()
    if (!confirm('Delete the selected mappings?')) return
    dispatch('DELETE_JOBS', { jobIds: [...state.ui.selected] })
  })
}

// ── Close popovers on outside click ──────────────────────────────────────────

document.addEventListener('click', () => dispatch('CLOSE_POPOVERS'))

// ── Polling ───────────────────────────────────────────────────────────────────

const TERMINAL = new Set(['complete', 'error'])
const projLastStatus = new Map()

function activeJobIds() {
  const ids = []
  for (const job of state.jobs.values()) {
    if (!TERMINAL.has(job.status)) { ids.push(job.id); continue }
    // Also poll jobs with non-terminal projections
    for (const proj of job.projections.values()) {
      if (!TERMINAL.has(proj.status)) { ids.push(job.id); break }
    }
  }
  return ids
}

function pollJob(jobId) {
  fetch(`/jobs/${jobId}/status/`)
    .then(r => {
      if (r.status === 404) { dispatch('REMOVE_JOB', { jobId }); return null }
      return r.json()
    })
    .then(data => {
      if (!data) return
      const job = state.jobs.get(jobId)
      if (!job) return
      const prevStatus = job.status

      // Reload when UCE completes with projections (structure change)
      if (data.status === 'complete' && prevStatus !== 'complete' &&
          (data.projections || []).length > 0) {
        window.location.reload(); return
      }
      // Reload when upload finishes (projection rows appear)
      if (prevStatus === 'uploading' && data.status !== 'uploading') {
        window.location.reload(); return
      }

      dispatch('POLL_UPDATE', { jobId, data })

      // Notifications
      if (data.status === 'error' && prevStatus !== 'error') {
        window.notifyJobError?.(job.filename)
      }
      for (const pd of (data.projections || [])) {
        const prev = projLastStatus.get(pd.id)
        if (prev !== undefined && prev !== pd.status) {
          if (pd.status === 'complete') window.notifyJobComplete?.(job.filename)
          if (pd.status === 'error') window.notifyJobError?.(job.filename)
        }
        projLastStatus.set(pd.id, pd.status)
      }
    })
    .catch(() => {})
}

function pollAll() {
  const ids = activeJobIds()
  if (!ids.length) return
  ids.forEach(pollJob)
  setTimeout(pollAll, 30000)
}

if (activeJobIds().length) pollAll()

// ── BroadcastChannel for upload progress ─────────────────────────────────────

if (window.BroadcastChannel) {
  const bc = new BroadcastChannel('upload-progress')
  bc.onmessage = ({ data }) => {
    if (data.type === 'progress') {
      const pct = Math.round(100 * data.done / data.total)
      dispatch('UPLOAD_PROGRESS', { jobId: data.jobId, pct })
    } else if (data.type === 'complete') {
      dispatch('UPLOAD_COMPLETE', { jobId: data.jobId })
      fetch(`/jobs/${data.jobId}/status/`)
        .then(r => r.ok ? r.json() : null)
        .then(d => { if (d) dispatch('POLL_UPDATE', { jobId: data.jobId, data: d }) })
        .catch(() => {})
    } else if (data.type === 'error') {
      dispatch('UPLOAD_ERROR', { jobId: data.jobId })
    } else if (data.type === 'lost') {
      dispatch('UPLOAD_ERROR', { jobId: data.jobId })
    }
  }
}

// ── Notifications ─────────────────────────────────────────────────────────────

;(function () {
  const PREF_KEY = 'notifyOnComplete'
  const btn = document.getElementById('btn-notify')
  const icon = document.getElementById('notify-icon')
  if (!btn) return

  function isEnabled() {
    return Notification.permission === 'granted' && localStorage.getItem(PREF_KEY) === '1'
  }

  function updateButton() {
    const denied = Notification.permission === 'denied'
    icon.textContent = isEnabled() ? 'notifications' : 'notifications_none'
    btn.classList.toggle('btn-notify--active', isEnabled())
    btn.title = denied
      ? 'Notifications blocked — check browser settings'
      : isEnabled() ? 'Notifications on — click to turn off' : 'Notify me when a job completes'
    btn.disabled = denied
  }

  btn.addEventListener('click', () => {
    if (Notification.permission === 'denied') return
    if (isEnabled()) {
      localStorage.removeItem(PREF_KEY); updateButton()
    } else if (Notification.permission === 'granted') {
      localStorage.setItem(PREF_KEY, '1'); updateButton()
    } else {
      Notification.requestPermission().then(r => {
        if (r === 'granted') localStorage.setItem(PREF_KEY, '1')
        updateButton()
      })
    }
  })
  updateButton()

  function logNotify(event, label) {
    const log = JSON.parse(sessionStorage.getItem('notifyLog') || '[]')
    log.push({ t: new Date().toISOString(), event, label: label || null })
    sessionStorage.setItem('notifyLog', JSON.stringify(log))
  }

  function fireNotification(title, filename) {
    const n = new Notification(title, { body: filename || 'Your job has finished.', icon: '/static/favicons/favicon-32x32.png' })
    n.addEventListener('show', () => logNotify('show', filename))
    n.addEventListener('error', () => logNotify('error', filename))
  }

  window.notifyJobComplete = (filename) => {
    logNotify('attempt:complete', filename)
    if (!isEnabled()) { logNotify('skipped', filename); return }
    fireNotification('Cell mapping complete', filename)
  }
  window.notifyJobError = (filename) => {
    logNotify('attempt:error', filename)
    if (!isEnabled()) { logNotify('skipped', filename); return }
    fireNotification('Cell mapping failed', filename)
  }
})()

// ── Email notifications ───────────────────────────────────────────────────────

;(function () {
  const btn = document.getElementById('btn-email-notify')
  const icon = document.getElementById('email-notify-icon')
  if (!btn) return
  const userEmail = rawData.user_email
  let enabled = rawData.email_on_complete

  function updateButton() {
    icon.textContent = enabled ? 'mail' : 'mail_outline'
    btn.classList.toggle('btn-notify--active', enabled)
    btn.title = enabled
      ? `Email notifications on (${userEmail}) — click to turn off`
      : `Email me at ${userEmail} when a job completes`
  }

  btn.addEventListener('click', () => {
    fetch('/jobs/preferences/email-notifications/', {
      method: 'POST',
      headers: { 'X-CSRFToken': rawData.csrf },
    })
      .then(r => r.json())
      .then(d => { enabled = d.email_on_complete; updateButton() })
      .catch(() => {})
  })
  updateButton()
})()
