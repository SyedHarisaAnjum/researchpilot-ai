import { useState } from 'react'
import { api } from '../services/api'

const MIN_LENGTH = 10
const MAX_LENGTH = 2000

const DEPTHS = [
  { value: 'quick', label: 'Quick', detail: '1 round · ~4 pages · about 2 min' },
  { value: 'standard', label: 'Standard', detail: 'Up to 3 rounds · ~10 pages · about 5 min' },
  { value: 'deep', label: 'Deep', detail: 'Up to 4 rounds · ~24 pages · up to 15 min' },
]

export default function ResearchForm({ onCreated }) {
  const [question, setQuestion] = useState('')
  const [depth, setDepth] = useState('standard')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState(null)

  const trimmed = question.trim()
  const tooShort = trimmed.length > 0 && trimmed.length < MIN_LENGTH

  async function handleSubmit(event) {
    event.preventDefault()
    if (trimmed.length < MIN_LENGTH) {
      setError(`Please write a question of at least ${MIN_LENGTH} characters.`)
      return
    }
    setSubmitting(true)
    setError(null)
    try {
      const job = await api.createResearch(trimmed, depth)
      onCreated(job)
    } catch (err) {
      setError(err.message)
      setSubmitting(false)
    }
  }

  return (
    <form className="card research-form" onSubmit={handleSubmit} noValidate>
      <label className="field">
        <span className="field__label">Research question</span>
        <textarea
          className="input textarea"
          rows={4}
          maxLength={MAX_LENGTH}
          placeholder="Compare the best free LLM APIs for agentic research in 2026…"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) handleSubmit(e)
          }}
          disabled={submitting}
          aria-invalid={tooShort}
          autoFocus
        />
        <span className={`field__hint ${tooShort ? 'field__hint--error' : ''}`}>
          {tooShort ? `At least ${MIN_LENGTH} characters` : 'Be specific: what you want compared, explained or checked.'}
          <span className="field__count">{question.length}/{MAX_LENGTH}</span>
        </span>
      </label>

      <fieldset className="field">
        <legend className="field__label">Research depth</legend>
        <div className="segmented" role="radiogroup">
          {DEPTHS.map((option) => (
            <label key={option.value} className={`segmented__option ${depth === option.value ? 'is-selected' : ''}`}>
              <input
                type="radio"
                name="depth"
                value={option.value}
                checked={depth === option.value}
                onChange={() => setDepth(option.value)}
                disabled={submitting}
              />
              <span className="segmented__label">{option.label}</span>
              <span className="segmented__detail">{option.detail}</span>
            </label>
          ))}
        </div>
      </fieldset>

      {error && (
        <div className="alert alert--error" role="alert">
          {error}
        </div>
      )}

      <div className="research-form__actions">
        <button type="submit" className="button button--primary" disabled={submitting || trimmed.length < MIN_LENGTH}>
          {submitting ? 'Starting…' : 'Start research'}
        </button>
        <span className="muted small">Ctrl + Enter to submit</span>
      </div>
    </form>
  )
}
