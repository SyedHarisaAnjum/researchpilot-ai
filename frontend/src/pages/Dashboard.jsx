import { useNavigate } from 'react-router-dom'
import ResearchForm from '../components/ResearchForm'
import ResearchHistory from '../components/ResearchHistory'

export default function Dashboard() {
  const navigate = useNavigate()

  return (
    <div className="page dashboard">
      <section className="hero">
        <h1>What do you want to research?</h1>
        <p className="muted">Explore a topic and generate an evidence-backed report with checked citations.</p>
      </section>
      <ResearchForm onCreated={(job) => navigate(`/research/${job.id}`)} />
      <ResearchHistory />
    </div>
  )
}
