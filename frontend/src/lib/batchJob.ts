// Batch grabs that run as server-side jobs (ADR-0024): send to pipeline
// and tentative bulk approve. The POST starts the job and returns at
// once; the GET polls it. Every MAM request is paced, so a batch takes
// minutes, longer than a reverse proxy waits for one request.
//
// `runBatchJob` hides the polling: it resolves with the finished job's
// `result`, which is exactly what these endpoints used to return in one
// response, so callers handle it as before.

import { api } from "../api";

export interface BatchJob<T> {
  job_id: string;
  done: boolean;
  total: number;
  completed: number;
  rows: Record<string, unknown>[];
  result: T | null;
  error: string | null;
}

const POLL_MS = 1500;

export async function runBatchJob<T>(
  startPath: string,
  body: unknown,
  onProgress?: (job: BatchJob<T>) => void,
): Promise<T> {
  let job = await api.post<BatchJob<T>>(startPath, body);
  onProgress?.(job);
  while (!job.done) {
    await new Promise((r) => setTimeout(r, POLL_MS));
    // A poll that fails (404 after a restart, network error) throws to
    // the caller, which already reports a failed send.
    job = await api.get<BatchJob<T>>(`${startPath}/${job.job_id}`);
    onProgress?.(job);
  }
  if (job.error || job.result === null) {
    throw new Error(job.error || "The batch finished without a result");
  }
  return job.result;
}
