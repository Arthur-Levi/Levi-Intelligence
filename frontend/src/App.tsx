import { Activity, ArrowUpRight, Building2, ChartNoAxesCombined, FileSearch, Search, ShieldCheck, Target, Users } from 'lucide-react'

const metrics = [
  { label: 'Empresas descobertas', value: '—', note: 'Aguardando conexão de fontes', icon: Building2 },
  { label: 'Leads qualificados', value: '—', note: 'Score ainda não calibrado', icon: Target },
  { label: 'Diagnósticos gerados', value: '—', note: 'Motor em implementação', icon: FileSearch },
  { label: 'Oportunidades ativas', value: '—', note: 'CRM em implementação', icon: Activity },
]

export default function App() {
  return <main className="shell">
    <aside className="sidebar">
      <div className="brand-mark">LI</div>
      <div className="brand"><strong>LEVI</strong><span>INTELLIGENCE</span></div>
      <div className="workspace"><span className="dot"/> Levi Digital <span className="chevron">⌄</span></div>
      <p className="nav-label">WORKSPACE</p>
      <a className="nav-item active"><ChartNoAxesCombined size={17}/> Visão geral</a>
      <a className="nav-item"><Search size={17}/> Prospecção</a>
      <a className="nav-item"><Building2 size={17}/> Empresas</a>
      <a className="nav-item"><Users size={17}/> Decisores</a>
      <a className="nav-item"><FileSearch size={17}/> Diagnósticos</a>
      <a className="nav-item"><Target size={17}/> Pipeline CRM</a>
      <div className="sidebar-bottom"><ShieldCheck size={16}/> Ambiente de desenvolvimento</div>
    </aside>
    <section className="content">
      <header className="topbar"><div><span className="eyebrow">LEVI DIGITAL / WORKSPACE</span><h1>Visão geral</h1></div><div className="top-right"><span className="env">BUILD 001 · DEV</span><div className="avatar">LI</div></div></header>
      <section className="welcome"><div><span className="eyebrow light">BUSINESS ACQUISITION INTELLIGENCE</span><h2>Transforme dados em<br/><em>oportunidades reais.</em></h2><p>Descubra empresas, compreenda seus negócios e construa relacionamentos comerciais com inteligência.</p><button disabled>＋ Nova prospecção <ArrowUpRight size={16}/></button></div><div className="orb"><div className="orb-core">LI</div><span className="orbit orbit-a"/><span className="orbit orbit-b"/><span className="orbit orbit-c"/></div></section>
      <div className="section-heading"><div><span className="eyebrow">PERFORMANCE</span><h3>Seu ambiente comercial</h3></div><span className="muted">Sem dados operacionais ainda</span></div>
      <div className="metrics">{metrics.map(({label,value,note,icon:Icon})=><article className="metric" key={label}><div className="metric-top"><span>{label}</span><Icon size={18}/></div><strong>{value}</strong><small>{note}</small></article>)}</div>
      <div className="lower-grid"><article className="panel"><div className="panel-head"><div><span className="eyebrow">ACQUISITION ENGINE</span><h3>Fluxo de inteligência</h3></div><span className="status">Em construção</span></div><div className="pipeline"><div><b>01</b><span><strong>Discovery</strong><small>Encontrar empresas-alvo</small></span></div><i/><div><b>02</b><span><strong>Enrichment</strong><small>Verificar dados e fontes</small></span></div><i/><div><b>03</b><span><strong>Analysis</strong><small>Auditar presença digital</small></span></div><i/><div><b>04</b><span><strong>Qualification</strong><small>Priorizar oportunidades</small></span></div></div></article><article className="panel focus"><span className="eyebrow">VERTICAL INICIAL</span><h3>Odontologia</h3><p>Primeiro perfil de mercado: clínicas particulares com potencial de aprimoramento da aquisição de pacientes.</p><div className="tag-row"><span>Brasil</span><span>Dental Copy</span><span>B2B</span></div><div className="focus-foot"><span>Perfil de cliente ideal</span><strong>Em definição</strong></div></article></div>
      <footer>LEVI INTELLIGENCE <span>FOUNDATION BUILD · 0.1.0</span></footer>
    </section>
  </main>
}
