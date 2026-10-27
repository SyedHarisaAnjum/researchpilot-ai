import { useCallback, useEffect, useRef, useState } from 'react'
import { api, subscribeToEvents, TERMINAL_STATUSES } from '../services/api'

const POLL_MS = 3000

/**
 * Everything about one research job, loaded from the backend: the job record, its
 * progress events (live over SSE), and the report and sources once it has finished.
 * If the event stream drops for good, it falls back to polling.
 * Mount it with key={jobId} so switching jobs starts from fresh state.
 */
export function useResearchJob(jobId) {
  const [job, setJob] = useState(null)
  const [events, setEvents] = useState([])
  const [report, setReport] = useState(null)
  const [sources, setSources] = useState([])
  const [error, setError] = useState(null)
  const [resultsError, setResultsError] = useState(null)
  const [streamLost, setStreamLost] = useState(false)
  const resultsLoaded = useRef(false)

  const loadJob = useCallback(async () => {
    try {
      const data = await api.getResearch(jobId)
      setJob(data)
      setError(null)
      return data
    } catch (err) {
      setError(err)
      return null
    }
  }, [jobId])

  const loadResults = useCallback(async (status) => {
    if (resultsLoaded.current) return
    resultsLoaded.current = true
    // Only completed jobs have a report; skip the request (and its 404) otherwise.
    const [reportResult, sourcesResult] = await Promise.allSettled([
      status === 'completed' ? api.getReport(jobId) : Promise.resolve(null),
      api.getSources(jobId),
    ])
    if (reportResult.status === 'fulfilled') setReport(reportResult.value)
    else if (reportResult.reason?.status !== 404) setResultsError(reportResult.reason)
    if (sourcesResult.status === 'fulfilled') setSources(sourcesResult.value)
  }, [jobId])

  useEffect(() => {
    let active = true
    let closeStream = null
    let pollTimer = null

    const finish = async () => {
      if (!active) return
      const data = await loadJob()
      if (active && data) await loadResults(data.status)
    }

    const startPolling = () => {
      pollTimer = setInterval(async () => {
        const data = await loadJob()
        if (data && TERMINAL_STATUSES.has(data.status)) {
          clearInterval(pollTimer)
          loadResults(data.status)
        }
      }, POLL_MS)
    }

    api
      .getResearch(jobId)
      .then((data) => {
        if (!active) return
        setJob(data)
        if (TERMINAL_STATUSES.has(data.status)) loadResults(data.status)
        // Replays every stored event, then streams new ones until the job ends.
        closeStream = subscribeToEvents(jobId, {
          // On every (re)connect: the server's current view of the job.
          onState: (snapshot) => {
            if (!active) return
            setJob((previous) => ({ ...previous, ...snapshot }))
          },
          onEvent: (event) => {
            if (!active) return
            setEvents((previous) => (previous.some((e) => e.id === event.id) ? previous : [...previous, event]))
            // Progress events carry the stage, percentage and counts, so no extra request is needed.
            if (event.kind === 'started' || event.kind === 'progress') {
              setJob((previous) => ({
                ...previous,
                status: 'running',
                stage: event.stage ?? previous.stage,
                progress: Math.max(previous.progress, event.progress ?? 0),
                stats: event.stats ?? previous.stats,
              }))
            }
          },
          onEnd: finish,
          onConnectionError: () => {
            if (!active) return
            setStreamLost(true)
            startPolling()
          },
        })
      })
      .catch((err) => active && setError(err))

    return () => {
      active = false
      closeStream?.()
      clearInterval(pollTimer)
    }
  }, [jobId, loadJob, loadResults])

  const cancel = useCallback(async () => {
    const data = await api.cancelResearch(jobId)
    setJob(data)
    return data
  }, [jobId])

  return { job, events, report, sources, error, resultsError, streamLost, cancel }
}
