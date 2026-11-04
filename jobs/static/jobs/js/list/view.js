// Pure state → vdom. No side effects here.
import { h } from './superfine.js'

const TERMINAL = new Set(['complete', 'error'])
const QUEUED_BATCH = new Set(['SUBMITTED', 'PENDING', 'RUNNABLE'])

const MI = (name) => h('span', { class: 'material-icons' }, name)

function formatDate(iso) {
  return new Date(iso).toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric',
    hour: 'numeric', minute: '2-digit',
  })
}

function wordBreak(name) {
  // Insert <wbr> around _-. for long filenames
  const parts = name.split(/([_\-.])/)
  return parts.flatMap((p, i) =>
    i % 2 === 1 ? [h('wbr', null), p, h('wbr', null)] : (p ? [p] : [])
  )
}

function ucePhaseLabel(job) {
  if (job.sharded) {
    if (job.n_shards == null) return 'Preprocessing'
    const done = job.shards_complete || 0
    const total = job.n_shards
    if (done >= total) return 'Merging'
    return `Processing · ${done} / ${total} shards`
  }
  const waiting = !job.batch_status || QUEUED_BATCH.has(job.batch_status)
  if (waiting) return 'Waiting for GPU'
  if (job.uce_progress != null) return `Processing · ${job.uce_progress}%`
  if (job.cell_count) return 'Processing'
  return 'Starting'
}

function uceStatusContent(job, dispatch) {
  if (job.status === 'error') {
    return [
      h('span', { class: 'status-error' },
        'Error ',
        h('a', { href: `/jobs/${job.id}/`, class: 'status-error-info', title: 'View error details' },
          MI('help_outline'))),
      job.s3_input_key
        ? h('button', {
            class: 'btn-retry-uce',
            title: 'Retry UCE pipeline',
            onClick: (e) => { e.stopPropagation(); dispatch('RETRY_UCE', { jobId: job.id }) },
          }, 'Retry')
        : null,
    ]
  }
  if (job.status === 'uploading') {
    const pct = job.upload_pct != null ? ` ${job.upload_pct}%` : ''
    return [
      h('span', { class: 'status-uploading' }, `Uploading${pct}`),
      h('button', {
        class: 'btn-cancel-upload',
        title: 'Cancel upload',
        onClick: (e) => { e.stopPropagation(); dispatch('CANCEL_UPLOAD', { jobId: job.id }) },
      }, 'Cancel'),
    ]
  }
  if (job.status === 'running') {
    return [h('span', { class: 'status-running' }, ucePhaseLabel(job))]
  }
  return [h('span', { class: 'status-pending' }, 'Pending')]
}

function projStatusContent(proj, job, dispatch) {
  if (proj.status === 'error') {
    return [
      h('span', { class: 'status-error' },
        'Error ',
        h('a', { href: `/jobs/${job.id}/`, class: 'status-error-info', title: 'View error details' },
          MI('help_outline'))),
    ]
  }
  if (proj.status === 'complete') {
    return [h('span', { class: 'status-complete' }, 'Complete')]
  }
  if (proj.status === 'running') {
    const label = proj.batch_status && QUEUED_BATCH.has(proj.batch_status) ? 'Waiting' : 'Mapping'
    return [h('span', { class: 'status-running' }, label)]
  }
  return [h('span', { class: 'status-pending' }, 'Pending')]
}

function downloadCellContent(proj, job, dispatch) {
  if (proj.status === 'complete' && proj.s3_uri) {
    const vizUrl = `/visualization/${proj.reference_id}/?overlay=${encodeURIComponent(proj.s3_uri)}`
    return [
      h('div', { class: 'action-btns' },
        h('button', {
          type: 'button',
          class: 'action-btn explore-btn',
          'data-viz-url': vizUrl,
          'data-job-id': job.id,
          title: 'Explore results',
          onClick: (e) => {
            e.stopPropagation()
            const rect = e.currentTarget.getBoundingClientRect()
            dispatch('OPEN_EXPLORE', { projId: proj.id, jobId: job.id, vizUrl, rect })
          },
        },
          MI('explore'), 'Explore'),
        h('a', {
          href: `/jobs/projections/${proj.id}/download/`,
          class: 'action-btn',
          title: 'Download results',
        },
          MI('download'), 'Download'),
        h('button', {
          type: 'button',
          class: 'action-btn kebab-btn',
          title: 'More options',
          onClick: (e) => {
            e.stopPropagation()
            const rect = e.currentTarget.getBoundingClientRect()
            dispatch('OPEN_KEBAB', { projId: proj.id, jobId: job.id, vizUrl, isPublic: proj.public, isComplete: true, canRerun: true, rect })
          },
        },
          MI('more_vert')),
      ),
    ]
  }
  if (proj.status === 'error') {
    return [
      h('button', {
        type: 'button',
        class: 'action-btn kebab-btn',
        title: 'More options',
        onClick: (e) => {
          e.stopPropagation()
          const rect = e.currentTarget.getBoundingClientRect()
          dispatch('OPEN_KEBAB', { projId: proj.id, jobId: job.id, vizUrl: null, isPublic: false, isComplete: false, canRerun: !!proj.can_rerun, rect })
        },
      }, MI('more_vert')),
    ]
  }
  return ['—']
}

function renderJobRows(job, isAlt, selected, dispatch) {
  const rows = []
  const projs = [...job.projections.values()]
  const pcount = projs.length

  if (pcount === 0) {
    rows.push(h('tr', { key: `j-${job.id}`, 'data-job-id': job.id, 'data-job-status': job.status, class: isAlt ? 'job-row-alt' : null },
      h('td', { class: 'col-check' },
        h('input', { type: 'checkbox', name: 'job_ids', value: job.id, class: 'job-checkbox', checked: selected.has(job.id) })),
      h('td', { class: 'job-file-cell' },
        h('span', { class: 'job-filename' }, ...wordBreak(job.filename)),
        job.cell_count ? [h('br', null), h('span', { class: 'cell-count' }, `${job.cell_count} cells`)] : null),
      h('td', { class: 'col-date' },
        h('time', { datetime: job.created_at }, formatDate(job.created_at))),
      h('td', { class: 'no-projections' }, '—'),
      h('td', { class: 'uce-status-cell' }, ...uceStatusContent(job, dispatch)),
      h('td', { class: 'no-projections' }, '—'),
    ))
    return rows
  }

  const isComplete = job.status === 'complete'

  projs.forEach((proj, i) => {
    const isFirst = i === 0
    const rowKey = isFirst ? `j-${job.id}` : `p-${proj.id}`
    const rowClass = isAlt ? 'job-row-alt' : null

    if (isFirst) {
      if (isComplete) {
        rows.push(h('tr', { key: rowKey, 'data-job-id': job.id, 'data-job-status': job.status, class: rowClass },
          h('td', { rowspan: pcount, class: 'col-check' },
            h('input', { type: 'checkbox', name: 'job_ids', value: job.id, class: 'job-checkbox', checked: selected.has(job.id) })),
          h('td', { rowspan: pcount, class: 'job-file-cell' },
            h('span', { class: 'job-filename' }, ...wordBreak(job.filename)),
            job.cell_count ? [h('br', null), h('span', { class: 'cell-count' }, `${job.cell_count} cells`)] : null),
          h('td', { rowspan: pcount, class: 'col-date' },
            h('time', { datetime: job.created_at }, formatDate(job.created_at))),
          h('td', null,
            proj.reference_name,
            proj.reference_version ? [h('br', null), h('span', { class: 'ref-version-label' }, proj.reference_version)] : null),
          h('td', { class: 'proj-status-cell', 'data-proj-id': proj.id }, ...projStatusContent(proj, job, dispatch)),
          h('td', { class: 'proj-download-cell', 'data-proj-id': proj.id }, ...downloadCellContent(proj, job, dispatch)),
        ))
      } else {
        rows.push(h('tr', { key: rowKey, 'data-job-id': job.id, 'data-job-status': job.status, class: rowClass },
          h('td', { rowspan: pcount, class: 'col-check' },
            h('input', { type: 'checkbox', name: 'job_ids', value: job.id, class: 'job-checkbox', checked: selected.has(job.id) })),
          h('td', { rowspan: pcount, class: 'job-file-cell' },
            h('span', { class: 'job-filename' }, ...wordBreak(job.filename)),
            job.cell_count ? [h('br', null), h('span', { class: 'cell-count' }, `${job.cell_count} cells`)] : null),
          h('td', { rowspan: pcount, class: 'col-date' },
            h('time', { datetime: job.created_at }, formatDate(job.created_at))),
          h('td', null,
            proj.reference_name,
            proj.reference_version ? [h('br', null), h('span', { class: 'ref-version-label' }, proj.reference_version)] : null),
          h('td', { rowspan: pcount, class: 'uce-status-cell' }, ...uceStatusContent(job, dispatch)),
          h('td', { rowspan: pcount }, '—'),
        ))
      }
    } else {
      if (isComplete) {
        rows.push(h('tr', { key: rowKey, 'data-proj-row': proj.id, class: isAlt ? 'job-row-alt' : null },
          h('td', null,
            proj.reference_name,
            proj.reference_version ? [h('br', null), h('span', { class: 'ref-version-label' }, proj.reference_version)] : null),
          h('td', { class: 'proj-status-cell', 'data-proj-id': proj.id }, ...projStatusContent(proj, job, dispatch)),
          h('td', { class: 'proj-download-cell', 'data-proj-id': proj.id }, ...downloadCellContent(proj, job, dispatch)),
        ))
      } else {
        rows.push(h('tr', { key: rowKey, 'data-proj-row': proj.id, class: isAlt ? 'job-row-alt' : null },
          h('td', null,
            proj.reference_name,
            proj.reference_version ? [h('br', null), h('span', { class: 'ref-version-label' }, proj.reference_version)] : null),
        ))
      }
    }
  })

  return rows
}

export function renderJobs(state, dispatch) {
  const rows = []
  let alt = false
  for (const job of state.jobs.values()) {
    alt = !alt
    rows.push(...renderJobRows(job, alt, state.ui.selected, dispatch))
  }
  if (!rows.length) {
    rows.push(h('tr', null,
      h('td', { colspan: '6', class: 'empty-row' }, 'No mappings yet.')))
  }
  return rows
}

// ── Popovers ─────────────────────────────────────────────────────────────────

function popoverStyle(rect, alignRight = false) {
  const top = rect.bottom + window.scrollY + 4
  if (alignRight) {
    const right = document.documentElement.clientWidth - rect.right
    return { position: 'absolute', top: `${top}px`, right: `${right}px`, display: 'flex' }
  }
  return { position: 'absolute', top: `${top}px`, left: `${rect.left + window.scrollX}px`, display: 'flex' }
}

function kebabPopover(ui, dispatch) {
  const kb = ui.openKebab
  if (!kb) return null
  return h('div', {
    class: 'share-popover',
    style: popoverStyle(kb.rect, true),
  },
    kb.isComplete
      ? h('button', {
          type: 'button', class: 'popover-btn kebab-share',
          onClick: (e) => { e.stopPropagation(); dispatch('OPEN_SHARE', { ...kb }) },
        }, MI('share'), 'Share')
      : null,
    kb.isComplete
      ? h('div', { class: 'popover-divider' })
      : null,
    (kb.isComplete || kb.canRerun)
      ? h('button', {
          type: 'button', class: 'popover-btn kebab-rerun',
          onClick: (e) => {
            e.stopPropagation()
            dispatch('CLOSE_POPOVERS')
            if (!confirm('Re-run this mapping with the current pipeline version? The existing results will be replaced.')) return
            dispatch('SET_PROJ_RUNNING', { projId: kb.projId, jobId: kb.jobId })
            dispatch('RERUN_PROJ', { projId: kb.projId })
          },
        }, MI('refresh'), 'Re-run mapping')
      : null,
    h('button', {
      type: 'button', class: 'popover-btn popover-stop kebab-delete',
      onClick: (e) => {
        e.stopPropagation()
        dispatch('CLOSE_POPOVERS')
        if (!confirm('Delete this mapping? This cannot be undone.')) return
        dispatch('DELETE_PROJ', { projId: kb.projId, jobId: kb.jobId })
      },
    }, MI('delete_outline'), 'Delete'),
  )
}

function sharePopover(ui, dispatch) {
  const sh = ui.openShare
  if (!sh) return null
  return h('div', {
    class: 'share-popover',
    style: popoverStyle(sh.rect, true),
    onClick: (e) => e.stopPropagation(),
  },
    h('button', {
      type: 'button', class: 'popover-btn popover-share',
      title: 'Copy a link to this visualization',
      onClick: (e) => {
        e.stopPropagation()
        dispatch('CLOSE_POPOVERS')
        dispatch('COPY_VIZ_LINK', { projId: sh.projId, jobId: sh.jobId, vizUrl: sh.vizUrl, isPublic: sh.isPublic })
      },
    }, 'Link to visualization'),
    h('button', {
      type: 'button', class: 'popover-btn popover-disable',
      title: 'Remove access to this visualization',
      disabled: !sh.isPublic,
      onClick: (e) => {
        e.stopPropagation()
        dispatch('CLOSE_POPOVERS')
        dispatch('DISABLE_SHARE', { projId: sh.projId, jobId: sh.jobId })
      },
    }, 'Disable visualization link'),
    h('div', { class: 'popover-divider' }),
    h('button', {
      type: 'button', class: 'popover-btn popover-send',
      title: 'Copy a link that lets another user import a copy of these results',
      onClick: (e) => {
        e.stopPropagation()
        dispatch('CLOSE_POPOVERS')
        dispatch('GET_SHARE_TOKEN', { jobId: sh.jobId })
      },
    }, 'Share mapping results'),
  )
}

function explorePopover(ui, dispatch) {
  const ex = ui.openExplore
  if (!ex) return null
  return h('div', {
    class: 'share-popover',
    style: popoverStyle(ex.rect, false),
    onClick: (e) => e.stopPropagation(),
  },
    h('button', {
      type: 'button', class: 'popover-btn explore-umap',
      title: 'View mapping of cells onto the reference',
      onClick: (e) => {
        e.stopPropagation()
        dispatch('CLOSE_POPOVERS')
        window.location.href = ex.vizUrl
      },
    }, 'UMAP'),
    h('button', {
      type: 'button', class: 'popover-btn explore-summary',
      title: 'Analyze results with AI assistant',
      onClick: (e) => {
        e.stopPropagation()
        dispatch('CLOSE_POPOVERS')
        window.location.href = `/jobs/${ex.jobId}/`
      },
    }, 'Analyze'),
  )
}

function shareModal(ui, dispatch) {
  const sm = ui.shareModal
  if (!sm) return null
  return h('div', {
    class: 'share-mapping-overlay',
    style: 'display: flex',
    onClick: (e) => { if (e.target === e.currentTarget) dispatch('CLOSE_SHARE_MODAL') },
  },
    h('div', { class: 'share-mapping-modal' },
      h('div', { class: 'share-mapping-header' },
        h('h3', { class: 'share-mapping-title' }, 'Share mapping results'),
        h('button', {
          type: 'button', class: 'share-mapping-close', title: 'Close',
          onClick: () => dispatch('CLOSE_SHARE_MODAL'),
        }, MI('close')),
      ),
      h('p', { class: 'share-mapping-desc' },
        'This link allows another user to import a copy of these results into their own account. The recipient will need a UCSC Brain Explorer account.'),
      h('div', { class: 'share-mapping-link-row' },
        h('input', { type: 'text', class: 'share-mapping-input', readonly: true, value: sm.url }),
        h('button', {
          type: 'button', class: 'share-mapping-copy', title: 'Copy link',
          onClick: (e) => {
            const input = e.currentTarget.closest('.share-mapping-modal').querySelector('.share-mapping-input')
            navigator.clipboard.writeText(input.value).catch(() => {
              const inp = document.createElement('input')
              inp.value = input.value; document.body.appendChild(inp); inp.select()
              document.execCommand('copy'); inp.remove()
            })
            const icon = e.currentTarget.querySelector('.material-icons')
            icon.textContent = 'check'
            setTimeout(() => { icon.textContent = 'content_copy' }, 2000)
          },
        }, MI('content_copy')),
      ),
    ),
  )
}

export function renderPopovers(state, dispatch) {
  return [
    kebabPopover(state.ui, dispatch),
    sharePopover(state.ui, dispatch),
    explorePopover(state.ui, dispatch),
    shareModal(state.ui, dispatch),
  ].filter(Boolean)
}
