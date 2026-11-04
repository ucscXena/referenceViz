// State shape, pure handlers, and side-effect handlers for the job list.

import { assoc, dissoc, conj, disj, merge, pick, updateIn } from './util.js'

// buildInitialState(data) → state
// data comes from the #initial-state JSON block injected by Django.
export function buildInitialState(data) {
  const jobs = new Map()
  for (const j of data.jobs) {
    jobs.set(j.id, {
      id: j.id,
      filename: j.filename,
      status: j.status,
      cell_count: j.cell_count,
      s3_input_key: j.s3_input_key,
      created_at: j.created_at,
      projections: new Map(
        j.projections.map(p => [p.id, { ...p }])
      ),
    })
  }
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

const JOB_EXTRA = ['batch_status', 'sharded', 'n_shards', 'shards_complete', 'uce_progress', 'has_upload']
const PROJ_KEYS = ['batch_status', 's3_uri', 'has_download', 'reference_id', 'can_rerun']

// ── Pure handlers ────────────────────────────────────────────────────────────
// Each handler: (state, params) → newState

export const handlers = {

  POLL_UPDATE(state, { jobId, data }) {
    if (!state.jobs.has(jobId)) return state
    return updateIn(state, ['jobs', jobId], job => merge(job, {
      status: data.status,
      cell_count: data.cell_count ?? job.cell_count,
      ...pick(data, JOB_EXTRA),
      projections: (data.projections || []).reduce(
        (map, pd) => assoc(map, pd.id, merge(map.get(pd.id) || {}, { id: pd.id, status: pd.status, ...pick(pd, PROJ_KEYS) })),
        job.projections
      ),
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
    return updateIn(state, ['jobs', jobId, 'status'], () => 'pending')
  },

  UPLOAD_PROGRESS(state, { jobId, pct }) {
    if (!state.jobs.has(jobId)) return state
    return updateIn(state, ['jobs', jobId], job => ({ ...job, status: 'uploading', upload_pct: pct }))
  },

  UPLOAD_COMPLETE(state, { jobId }) {
    if (!state.jobs.has(jobId)) return state
    return updateIn(state, ['jobs', jobId], job => ({ ...job, status: 'pending', upload_pct: null }))
  },

  UPLOAD_ERROR(state, { jobId }) {
    if (!state.jobs.has(jobId)) return state
    return updateIn(state, ['jobs', jobId, 'status'], () => 'error')
  },
}

// ── Effect handlers (-POST!) ─────────────────────────────────────────────────
// Each effect: (prevState, nextState, params, dispatch) → void

export const effects = {

  RETRY_UCE(prev, next, { jobId }, dispatch) {
    fetch(`/jobs/${jobId}/retry/`, {
      method: 'POST',
      headers: { 'X-CSRFToken': next.csrf },
    })
      .then(r => r.json())
      .then(d => {
        if (d.status !== 'queued') {
          // Roll back to error on failure (re-fetch from server on next poll)
          const job = prev.jobs.get(jobId)
          if (job) dispatch('POLL_UPDATE', { jobId, data: { status: 'error', projections: [] } })
        }
      })
      .catch(() => {})
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
