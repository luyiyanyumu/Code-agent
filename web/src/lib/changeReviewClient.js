import { request } from '../api.js'
const base = pid => `/api/v1/code/projects/${encodeURIComponent(pid)}/change-proposals`
const batches = pid => `/api/v1/code/projects/${encodeURIComponent(pid)}/change-batches`
export const changeReviews = {
  list: pid => request('GET', base(pid)),
  detail: (pid, id) => request('GET', `${base(pid)}/${encodeURIComponent(id)}`),
  create: (pid, body) => request('POST', base(pid), body),
  decide: (pid, id, action, ids) => request('POST', `${base(pid)}/${encodeURIComponent(id)}/${action}`, ids ? { hunk_ids: ids } : {}),
  listBatches: pid => request('GET', batches(pid)),
  detailBatch: (pid, id) => request('GET', `${batches(pid)}/${encodeURIComponent(id)}`),
  createBatch: (pid, body) => request('POST', batches(pid), body),
  decideBatch: (pid, id, action) => request('POST', `${batches(pid)}/${encodeURIComponent(id)}/${action}`, {}),
}
