// State shape, pure handlers, and side-effect handlers for the job list.

import { assoc, dissoc, conj, disj, merge, pick, updateIn } from './util.js'

// jobFromData(data) → job
// data is one job as the server sends it (views._job_state), in the initial state
// and in each status poll. job.status describes only the upload (uploading, copying,
// ready, error); UCE progress is on job.embeddings, mapping progress on job.projections.
function jobFromData(j) {
  return {
    ...j,
    embeddings: new Map(j.embeddings.map(e => [e.id, e])),
    projections: new Map(j.projections.map(p => [p.id, p])),
  }
}

// buildInitialState(data) → state
// data comes from the #initial-state JSON block injected by Django.
export function buildInitialState(data) {
  const jobs = new Map(data.jobs.map(j => [j.id, jobFromData(j)]))
  return {
    jobs,
    ui: {
      selected: new Set(),
      openKebab: null,   // {projId, jobId, vizUrl, isPublic, rect}
      openShare: null,   // {projId, jobId, vizUrl, isPublic, rect}
      openExplore: null, // {projId, jobId, vizUrl, rect}
      shareModal: null,  // {url}
    },
    csrf: data.csrf,
    deleteUrl: data.deleteUrl || '',
  }
}

// ── Pure handlers ────────────────────────────────────────────────────────────
// Each handler: (state, params) → newState

export const handlers = {

  POLL_UPDATE(state, { jobId, data }) {
    if (!state.jobs.has(jobId)) return state
    // The poll returns the whole job, so replace it, keeping only the upload
    // progress that the browser tracks.
    return updateIn(state, ['jobs', jobId], job => ({
      ...jobFromData(data),
      ...(data.status === 'uploading' ? pick(job, ['upload_pct']) : {}),
    }))
  },

  REMOVE_JOB(state, { jobId }) {
    const s = assoc(state, 'jobs', dissoc(state.jobs, jobId))
    return updateIn(s, ['ui', 'selected'], sel => disj(sel, jobId))
  },

  SELECT_JOB(state, { jobId }) {
    return updateIn(state, ['ui', 'selected'], sel => conj(sel, jobId))
  },

  DESELECT_JOB(state, { jobId }) {
    return updateIn(state, ['ui', 'selected'], sel => disj(sel, jobId))
  },

  SELECT_ALL(state) {
    return updateIn(state, ['ui', 'selected'], () => new Set(state.jobs.keys()))
  },

  DESELECT_ALL(state) {
    return updateIn(state, ['ui', 'selected'], () => new Set())
  },

  OPEN_KEBAB(state, params) {
    return updateIn(state, ['ui'], ui => merge(ui, { openKebab: params, openShare: null, openExplore: null }))
  },

  OPEN_SHARE(state, params) {
    return updateIn(state, ['ui'], ui => merge(ui, { openShare: params, openKebab: null, openExplore: null }))
  },

  OPEN_EXPLORE(state, params) {
    return updateIn(state, ['ui'], ui => merge(ui, { openExplore: params, openKebab: null, openShare: null }))
  },

  CLOSE_POPOVERS(state) {
    return updateIn(state, ['ui'], ui => merge(ui, { openKebab: null, openShare: null, openExplore: null }))
  },

  OPEN_SHARE_MODAL(state, { url }) {
    return updateIn(state, ['ui'], ui => merge(ui, { openKebab: null, openShare: null, shareModal: { url } }))
  },

  CLOSE_SHARE_MODAL(state) {
    return updateIn(state, ['ui', 'shareModal'], () => null)
  },

  SET_PROJ_RUNNING(state, { jobId, projId }) {
    return updateIn(state, ['jobs', jobId, 'projections', projId, 'status'], () => 'running')
  },

  REMOVE_PROJ(state, { jobId, projId }) {
    return updateIn(state, ['jobs', jobId, 'projections'], projs => dissoc(projs, projId))
  },

  SET_PROJ_PUBLIC(state, { jobId, projId, isPublic }) {
    let s = updateIn(state, ['jobs', jobId, 'projections', projId, 'public'], () => isPublic)
    if (s.ui.openShare?.projId === projId) {
      s = updateIn(s, ['ui', 'openShare', 'isPublic'], () => isPublic)
    }
    return s
  },

  RETRY_UCE(state, { jobId }) {
    if (!state.jobs.has(jobId)) return state
    return updateIn(state, ['jobs', jobId], job => merge(job, {
      embeddings: new Map([...job.embeddings].map(([id, e]) =>
        [id, e.status === 'error' ? { ...e, status: 'pending', error: '' } : e])),
      projections: new Map([...job.projections].map(([id, p]) =>
        [id, p.uce_failed ? { ...p, status: 'pending', uce_failed: false, error: '' } : p])),
    }))
  },

  UPLOAD_PROGRESS(state, { jobId, pct }) {
    if (!state.jobs.has(jobId)) return state
    return updateIn(state, ['jobs', jobId], job => ({ ...job, status: 'uploading', upload_pct: pct }))
  },

  UPLOAD_COMPLETE(state, { jobId }) {
    if (!state.jobs.has(jobId)) return state
    return updateIn(state, ['jobs', jobId], job => ({ ...job, status: 'ready', upload_pct: null }))
  },

  // The upload stopped in this browser. Unless the server recorded the failure, the
  // upload manager resumes it on the next page load, so this is shown only until then.
  UPLOAD_ERROR(state, { jobId, message }) {
    if (!state.jobs.has(jobId)) return state
    return updateIn(state, ['jobs', jobId], job => ({ ...job, status: 'error', error: message || 'Upload failed' }))
  },
}

// ── Effect handlers (-POST!) ─────────────────────────────────────────────────
// Each effect: (prevState, nextState, params, dispatch) → void

export const effects = {

  REFRESH_JOB(prev, next, { jobId }, dispatch) {
    fetch(`/jobs/${jobId}/status/`)
      .then(r => {
        if (r.status === 404) { dispatch('REMOVE_JOB', { jobId }); return null }
        return r.ok ? r.json() : null
      })
      .then(data => { if (data) dispatch('POLL_UPDATE', { jobId, data }) })
      .catch(() => {})
  },

  // Notify once per poll, however many projections changed: a failed UCE run fails
  // every projection waiting on it.
  POLL_UPDATE(prev, next, { jobId }) {
    const before = prev.jobs.get(jobId)
    const after = next.jobs.get(jobId)
    if (!before || !after) return
    if (after.status === 'error' && before.status !== 'error') {
      window.notifyJobError?.(after.filename)
      return
    }
    const changedTo = (status) => [...after.projections.values()].some(p =>
      p.status === status && before.projections.has(p.id) && before.projections.get(p.id).status !== status)
    if (changedTo('error')) window.notifyJobError?.(after.filename)
    if (changedTo('complete')) window.notifyJobComplete?.(after.filename)
  },

  RETRY_UCE(prev, next, { jobId }, dispatch) {
    fetch(`/jobs/${jobId}/retry/`, {
      method: 'POST',
      headers: { 'X-CSRFToken': next.csrf },
    })
      .catch(() => {})
      .then(() => dispatch('REFRESH_JOB', { jobId }))
  },

  CANCEL_UPLOAD(prev, next, { jobId }, dispatch) {
    window.UploadManager?.cancel(jobId)
    fetch(`/jobs/${jobId}/cancel-upload/`, {
      method: 'POST',
      headers: { 'X-CSRFToken': next.csrf },
    })
      .then(r => r.json())
      .then(() => dispatch('REMOVE_JOB', { jobId }))
      .catch(() => {})
  },

  RERUN_PROJ(prev, next, { projId }) {
    fetch(`/jobs/projections/${projId}/rerun/`, {
      method: 'POST',
      headers: { 'X-CSRFToken': next.csrf },
    })
      .then(r => {
        if (!r.ok) return r.json().then(d => { throw new Error(d.error || 'Re-run failed.') })
      })
      .catch(err => {
        alert(err.message || 'Re-run failed.')
        window.location.reload()
      })
  },

  DELETE_PROJ(prev, next, { projId, jobId }, dispatch) {
    fetch(`/jobs/projections/${projId}/delete/`, {
      method: 'POST',
      headers: { 'X-CSRFToken': next.csrf },
    })
      .then(r => {
        if (r.ok) dispatch('REMOVE_PROJ', { projId, jobId })
        else { alert('Delete failed.'); window.location.reload() }
      })
      .catch(() => { alert('Delete failed.'); window.location.reload() })
  },

  GET_SHARE_TOKEN(prev, next, { jobId }, dispatch) {
    fetch(`/jobs/${jobId}/share-token/`, {
      method: 'POST',
      headers: { 'X-CSRFToken': next.csrf },
    })
      .then(r => r.json())
      .then(d => dispatch('OPEN_SHARE_MODAL', { url: d.url }))
      .catch(() => {})
  },

  SET_PROJ_PUBLIC(prev, next, { projId, isPublic }, dispatch) {
    fetch(`/jobs/projections/${projId}/set-public/`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': next.csrf },
      body: JSON.stringify({ public: isPublic }),
    }).catch(() => {})
  },

  COPY_VIZ_LINK(prev, next, { projId, jobId, vizUrl, isPublic }, dispatch) {
    const doShare = () => {
      const url = window.location.origin + vizUrl
      navigator.clipboard.writeText(url).catch(() => {
        const inp = document.createElement('input')
        inp.value = url; document.body.appendChild(inp); inp.select()
        document.execCommand('copy'); inp.remove()
      })
    }
    if (!isPublic) {
      fetch(`/jobs/projections/${projId}/set-public/`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': next.csrf },
        body: JSON.stringify({ public: true }),
      }).then(r => {
        if (r.ok) { dispatch('SET_PROJ_PUBLIC', { projId, jobId, isPublic: true }); doShare() }
      }).catch(() => {})
    } else {
      doShare()
    }
  },

  DISABLE_SHARE(prev, next, { projId, jobId }, dispatch) {
    fetch(`/jobs/projections/${projId}/set-public/`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': next.csrf },
      body: JSON.stringify({ public: false }),
    }).then(r => {
      if (r.ok) dispatch('SET_PROJ_PUBLIC', { projId, jobId, isPublic: false })
    }).catch(() => {})
  },

  DELETE_JOBS(prev, next, { jobIds }, dispatch) {
    const form = new FormData()
    for (const id of jobIds) form.append('job_ids', id)
    fetch(next.deleteUrl, {
      method: 'POST',
      headers: { 'X-CSRFToken': next.csrf },
      body: form,
    })
      .then(() => window.location.reload())
      .catch(() => window.location.reload())
  },

}
