// Multipart S3 upload manager.
// Persists state in IndexedDB. Coordinates tabs via Web Locks. Broadcasts
// progress via BroadcastChannel. Included in base.html so any page resumes
// a pending upload automatically when the user navigates back into the app.

(function () {
  const SUPPORTED = !!(window.indexedDB && navigator.locks);
  const PART_SIZE = 8 * 1024 * 1024; // 8 MB (S3 min is 5 MB for non-final parts)
  const CONCURRENCY = 4;
  const DB_NAME = 'uce-uploads';
  const STORE = 'pending';

  const bc = new BroadcastChannel('upload-progress');

  function getCsrf() {
    const m = document.cookie.match(/csrftoken=([^;]+)/);
    return m ? m[1] : '';
  }

  // ── IndexedDB helpers ────────────────────────────────────────────────────

  function openDb() {
    return new Promise((resolve, reject) => {
      const req = indexedDB.open(DB_NAME, 1);
      req.onupgradeneeded = e => e.target.result.createObjectStore(STORE, {keyPath: 'uploadId'});
      req.onsuccess = e => resolve(e.target.result);
      req.onerror = e => reject(e.target.error);
    });
  }

  function dbGet(db, uploadId) {
    return new Promise((resolve, reject) => {
      const req = db.transaction(STORE).objectStore(STORE).get(uploadId);
      req.onsuccess = e => resolve(e.target.result);
      req.onerror = e => reject(e.target.error);
    });
  }

  function dbPut(db, record) {
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, 'readwrite');
      tx.objectStore(STORE).put(record);
      tx.oncomplete = () => resolve();
      tx.onerror = e => reject(e.target.error);
      tx.onabort = e => reject(e.target.error);
    });
  }

  function dbDelete(db, uploadId) {
    return new Promise((resolve, reject) => {
      const req = db.transaction(STORE, 'readwrite').objectStore(STORE).delete(uploadId);
      req.onsuccess = () => resolve();
      req.onerror = e => reject(e.target.error);
    });
  }

  function dbGetAll(db) {
    return new Promise((resolve, reject) => {
      const req = db.transaction(STORE).objectStore(STORE).getAll();
      req.onsuccess = e => resolve(e.target.result);
      req.onerror = e => reject(e.target.error);
    });
  }

  // ── Django API calls ──────────────────────────────────────────────────────

  function post(url, body) {
    return fetch(url, {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-CSRFToken': getCsrf()},
      body: JSON.stringify(body),
    });
  }

  async function signParts(uploadId, key, partNumbers) {
    const resp = await post('/jobs/upload/sign/', {uploadId, key, partNumbers});
    if (!resp.ok) throw new Error(`sign-parts failed: ${resp.status}`);
    const data = await resp.json();
    return data.urls; // {partNumber: presigned-url}
  }

  async function listCompletedParts(uploadId, key) {
    const resp = await post('/jobs/upload/sign/', {uploadId, key, list: true});
    if (resp.status === 404) throw Object.assign(new Error('NoSuchUpload'), {code: 'NoSuchUpload'});
    if (!resp.ok) throw new Error(`list-parts failed: ${resp.status}`);
    const data = await resp.json();
    return data.parts; // [{PartNumber, ETag}]
  }

  async function completeUpload(jobId, uploadId, key, parts, refId, mixedPrecision) {
    const resp = await post('/jobs/upload/complete/', {jobId, uploadId, key, parts, refId, mixedPrecision});
    if (!resp.ok) throw new Error(`complete failed: ${resp.status}`);
  }

  async function abortUpload(jobId, uploadId, key) {
    await post('/jobs/upload/abort/', {jobId, uploadId, key}).catch(() => {});
  }

  // ── Upload loop ───────────────────────────────────────────────────────────

  async function runUpload(db, state) {
    const {uploadId, key, jobId, file, refId, mixedPrecision} = state;
    let etags = Object.assign({}, state.etags);

    console.log('[upload-manager] runUpload: job', jobId, 'file valid?', !!(file && typeof file.slice === 'function'));
    if (!file || typeof file.slice !== 'function') {
      // File reference lost (browser was fully closed and reopened).
      console.warn('[upload-manager] file reference lost for job', jobId);
      await abortUpload(jobId, uploadId, key);
      await dbDelete(db, uploadId);
      bc.postMessage({type: 'lost', jobId});
      return;
    }

    const totalParts = Math.ceil(file.size / PART_SIZE);

    // Reconcile with S3 — it is the authoritative source of what's committed.
    console.log('[upload-manager] listing completed parts for', uploadId);
    try {
      const s3Parts = await listCompletedParts(uploadId, key);
      console.log('[upload-manager] S3 has', s3Parts.length, 'completed parts');
      for (const p of s3Parts) etags[p.PartNumber] = p.ETag;
    } catch (e) {
      if (e.code === 'NoSuchUpload') {
        console.warn('[upload-manager] multipart upload expired/missing:', uploadId);
        await dbDelete(db, uploadId);
        bc.postMessage({type: 'expired', jobId});
        return;
      }
      // Network glitch — leave state alone and let next page load retry.
      console.error('[upload-manager] list-parts error (will retry on next load):', e);
      throw e;
    }

    // Sign all missing parts in one call, then upload with bounded concurrency.
    const missing = [];
    for (let i = 1; i <= totalParts; i++) {
      if (!etags[i]) missing.push(i);
    }

    if (missing.length > 0) {
      const urls = await signParts(uploadId, key, missing);
      const queue = [...missing];

      const uploadPart = async partNum => {
        const offset = (partNum - 1) * PART_SIZE;
        const slice = file.slice(offset, Math.min(offset + PART_SIZE, file.size));
        const putResp = await fetch(urls[partNum], {method: 'PUT', body: slice});
        if (!putResp.ok) throw new Error(`part ${partNum} PUT failed: ${putResp.status}`);
        const etag = putResp.headers.get('ETag');
        if (!etag) throw new Error('ETag missing — add ETag to S3 CORS ExposeHeaders');
        etags[partNum] = etag;
        await dbPut(db, {...state, etags: Object.assign({}, etags)});
        bc.postMessage({type: 'progress', jobId, done: Object.keys(etags).length, total: totalParts});
      };

      await Promise.all(
        Array.from({length: Math.min(CONCURRENCY, missing.length)}, async () => {
          while (queue.length > 0) await uploadPart(queue.shift());
        })
      );
    }

    // All parts confirmed — complete the multipart upload.
    const parts = Object.entries(etags)
      .map(([n, e]) => ({PartNumber: parseInt(n, 10), ETag: e}))
      .sort((a, b) => a.PartNumber - b.PartNumber);

    await completeUpload(jobId, uploadId, key, parts, refId, mixedPrecision);
    await dbDelete(db, uploadId);
    bc.postMessage({type: 'complete', jobId});
  }

  async function tryResume(db, state) {
    // ifAvailable: true — skip immediately if another tab already holds the lock.
    await navigator.locks.request(
      `uce-upload-${state.uploadId}`,
      {ifAvailable: true},
      async lock => {
        if (!lock) {
          console.log('[upload-manager] lock busy for', state.uploadId, '— skipping');
          return;
        }
        console.log('[upload-manager] lock acquired for', state.uploadId);
        try {
          await runUpload(db, state);
        } catch (e) {
          console.error('[upload-manager] upload error:', e);
          bc.postMessage({type: 'error', jobId: state.jobId, message: e.message});
        }
      }
    );
  }

  // ── Init: resume any uploads pending from a previous page ────────────────

  async function init() {
    const db = await openDb();
    const pending = await dbGetAll(db);
    console.log('[upload-manager] init: pending uploads =', pending.length, pending.map(s => s.jobId));
    for (const state of pending) tryResume(db, state);
  }

  if (SUPPORTED) {
    init().catch(e => console.error('[upload-manager] init error:', e));
  }

  // ── Public API (used by create page) ─────────────────────────────────────

  window.UploadManager = {
    supported: SUPPORTED,
    async start(file, jobId, uploadId, key, {refId = null, mixedPrecision = 'bf16'} = {}) {
      if (!SUPPORTED) {
        throw new Error(
          'Background uploads require IndexedDB and Web Locks (secure context — HTTPS or localhost). ' +
          'Access the app over HTTPS or via http://localhost.'
        );
      }
      const db = await openDb();
      const state = {uploadId, key, jobId, file, refId, mixedPrecision, etags: {}};
      await dbPut(db, state);
      // Don't tryResume here — it would race with the imminent navigation and be
      // cancelled mid-flight. The next page's init() picks it up with no lock contention.
    },
  };
})();
