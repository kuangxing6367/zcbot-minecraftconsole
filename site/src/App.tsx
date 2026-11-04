import { useEffect, useState } from 'react'
import { Block, Section, footer, repoFile, sections, site } from './content'

function Icon({ name }: { name: 'github' | 'arrow' | 'copy' | 'check' | 'menu' | 'close' }) {
  const common = { width: 16, height: 16, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', strokeWidth: 1.8, strokeLinecap: 'round' as const, strokeLinejoin: 'round' as const }
  if (name === 'github') return (
    <svg {...common} width={18} height={18}>
      <path d="M9 19c-4.3 1.4-4.3-2.5-6-3m12 6v-3.87a3.37 3.37 0 0 0-.94-2.61c3.14-.35 6.44-1.54 6.44-7A5.44 5.44 0 0 0 19 4.77 5.07 5.07 0 0 0 18.91 1S17.73.65 15 2.48a13.38 13.38 0 0 0-7 0C5.27.65 4.09 1 4.09 1A5.07 5.07 0 0 0 4 4.77a5.44 5.44 0 0 0-1.5 3.78c0 5.42 3.3 6.61 6.44 7A3.37 3.37 0 0 0 8 18.13V22" />
    </svg>
  )
  if (name === 'arrow') return <svg {...common}><path d="M5 12h14M13 6l6 6-6 6" /></svg>
  if (name === 'copy') return <svg {...common}><rect x="9" y="9" width="11" height="11" rx="2" /><path d="M5 15V5a2 2 0 0 1 2-2h8" /></svg>
  if (name === 'check') return <svg {...common}><path d="M4 12l5 5L20 6" /></svg>
  if (name === 'menu') return <svg {...common} width={20} height={20}><path d="M4 7h16M4 12h16M4 17h16" /></svg>
  return <svg {...common} width={20} height={20}><path d="M6 6l12 12M18 6L6 18" /></svg>
}

function CodeBlock({ label, lang, code }: { label: string; lang: string; code: string }) {
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(code)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1600)
    } catch {
      setCopied(false)
    }
  }
  return (
    <div className="code-wrap">
      <div className="code-head">
        <span className="code-label">{label}</span>
        <span className="code-lang">{lang}</span>
        <button className="code-copy" onClick={copy} aria-label="复制代码" title="复制代码">
          <Icon name={copied ? 'check' : 'copy'} />
          <span>{copied ? '已复制' : '复制'}</span>
        </button>
      </div>
      <pre><code>{code}</code></pre>
    </div>
  )
}

function BlockView({ block }: { block: Block }) {
  switch (block.kind) {
    case 'p':
      return <p className="prose">{block.text}</p>
    case 'note':
      return (
        <div className={`note note-${block.tone}`}>
          <div className="note-title"><span className="note-dot" />{block.title}</div>
          <p>{block.text}</p>
        </div>
      )
    case 'code':
      return <CodeBlock label={block.label} lang={block.lang} code={block.code} />
    case 'cards':
      return (
        <div className="card-grid">
          {block.items.map((it) => (
            <article className="card" key={it.tag + it.title}>
              <span className="card-tag">{it.tag}</span>
              <h3>{it.title}</h3>
              <p>{it.desc}</p>
            </article>
          ))}
        </div>
      )
    case 'facts':
      return (
        <dl className="facts">
          {block.items.map((it) => (
            <div className="fact" key={it.k}>
              <dt>{it.k}</dt><dd>{it.v}</dd>
            </div>
          ))}
        </dl>
      )
    case 'table':
      return (
        <div className="table-wrap">
          <table>
            <thead><tr>{block.head.map((h) => <th key={h}>{h}</th>)}</tr></thead>
            <tbody>
              {block.rows.map((row, i) => (
                <tr key={i}>{row.map((cell, j) => <td key={j}>{cell}</td>)}</tr>
              ))}
            </tbody>
          </table>
        </div>
      )
    case 'steps':
      return (
        <ol className="steps">
          {block.items.map((it, i) => (
            <li key={it.title}>
              <span className="step-index">{String(i + 1).padStart(2, '0')}</span>
              <div className="step-body">
                <h3>{it.title}</h3>
                <p>{it.text}</p>
                {it.code && <CodeBlock label={`步骤 ${i + 1}`} lang={it.lang ?? 'text'} code={it.code} />}
              </div>
            </li>
          ))}
        </ol>
      )
    case 'flow':
      return (
        <div className="flow">
          {block.nodes.map((n, i) => (
            <div className="flow-unit" key={n.title}>
              <div className="flow-node">
                <span className="flow-title">{n.title}</span>
                <span className="flow-sub">{n.sub}</span>
              </div>
              {i < block.nodes.length - 1 && (
                <div className="flow-edge">
                  <span>{block.edges[i]}</span>
                  <i className="flow-line" />
                </div>
              )}
            </div>
          ))}
        </div>
      )
    case 'links':
      return (
        <div className="link-list">
          {block.items.map((it) => (
            <a className="link-row" key={it.href} href={it.href} target="_blank" rel="noreferrer">
              <span className="link-name">{it.label}</span>
              <span className="link-desc">{it.desc}</span>
              <Icon name="arrow" />
            </a>
          ))}
        </div>
      )
  }
}

function SectionView({ section, index }: { section: Section; index: number }) {
  return (
    <section id={section.id} className="doc-section reveal" data-index={index}>
      <header className="section-head">
        <span className="kicker">{section.kicker}</span>
        <h2>{section.title}</h2>
        {section.lead && <p className="lead">{section.lead}</p>}
      </header>
      <div className="section-body">
        {section.blocks.map((b, i) => <BlockView key={i} block={b} />)}
      </div>
    </section>
  )
}

export default function App() {
  const [active, setActive] = useState(sections[0].id)
  const [open, setOpen] = useState(false)
  const [progress, setProgress] = useState(0)

  useEffect(() => {
    const onScroll = () => {
      const max = document.documentElement.scrollHeight - window.innerHeight
      setProgress(max > 0 ? (window.scrollY / max) * 100 : 0)
    }
    onScroll()
    window.addEventListener('scroll', onScroll, { passive: true })
    return () => window.removeEventListener('scroll', onScroll)
  }, [])

  useEffect(() => {
    const obs = new IntersectionObserver(
      (entries) => {
        const visible = entries.filter((e) => e.isIntersecting).sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)
        if (visible[0]) setActive(visible[0].target.id)
      },
      { rootMargin: '-15% 0px -75% 0px', threshold: 0 },
    )
    sections.forEach((s) => {
      const el = document.getElementById(s.id)
      if (el) obs.observe(el)
    })
    const revealObs = new IntersectionObserver(
      (entries) => entries.forEach((e) => { if (e.isIntersecting) { e.target.classList.add('shown'); revealObs.unobserve(e.target) } }),
      { rootMargin: '0px 0px -8% 0px', threshold: 0.04 },
    )
    document.querySelectorAll('.reveal').forEach((el) => revealObs.observe(el))
    return () => { obs.disconnect(); revealObs.disconnect() }
  }, [])

  const go = (id: string) => {
    setOpen(false)
    document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }

  return (
    <div className="app">
      <div className="bg-grid" aria-hidden="true" />
      <div className="bg-glow bg-glow-a" aria-hidden="true" />
      <div className="bg-glow bg-glow-b" aria-hidden="true" />
      <div className="progress" style={{ width: `${progress}%` }} />

      <header className="topbar">
        <button className="menu-btn" onClick={() => setOpen(!open)} aria-label="切换目录">
          <Icon name={open ? 'close' : 'menu'} />
        </button>
        <a className="brand" href="#top" onClick={(e) => { e.preventDefault(); window.scrollTo({ top: 0, behavior: 'smooth' }) }}>
          <span className="brand-mark" aria-hidden="true"><span /><span /><span /></span>
          <span className="brand-name">{site.name}</span>
          <span className="brand-badge">DOCS</span>
        </a>
        <div className="topbar-right">
          <span className="version-chip">{site.version}</span>
          <a className="domain-chip" href={site.origin}>{site.domain}</a>
          <a className="gh-btn" href={site.repo} target="_blank" rel="noreferrer">
            <Icon name="github" /><span>源码</span>
          </a>
        </div>
      </header>

      <aside className={`sidebar ${open ? 'open' : ''}`}>
        <div className="side-inner">
          <div className="side-title">目录</div>
          <nav>
            {sections.map((s, i) => (
              <button
                key={s.id}
                className={`side-link ${active === s.id ? 'active' : ''}`}
                onClick={() => go(s.id)}
              >
                <span className="side-num">{String(i).padStart(2, '0')}</span>
                <span>{s.label}</span>
              </button>
            ))}
          </nav>
          <div className="side-foot">
            <a href={repoFile('README.md')} target="_blank" rel="noreferrer">仓库 README ↗</a>
            <span>{site.framework}</span>
          </div>
        </div>
      </aside>

      {open && <button className="scrim" onClick={() => setOpen(false)} aria-label="关闭目录" />}

      <main id="top">
        <div className="hero">
          <div className="hero-inner">
            <div className="hero-copy">
              <span className="hero-kicker">PROJECT DOCUMENTATION · {site.version}</span>
              <h1>{site.name}<span className="hero-dot">.</span></h1>
              <p className="hero-tagline">{site.tagline}</p>
              <p className="hero-summary">{site.summary}</p>
              <div className="hero-actions">
                <a className="btn btn-primary" href="#start" onClick={(e) => { e.preventDefault(); go('start') }}>
                  快速开始 <Icon name="arrow" />
                </a>
                <a className="btn btn-ghost" href={site.repo} target="_blank" rel="noreferrer">
                  <Icon name="github" /> 查看源码
                </a>
              </div>
              <div className="hero-meta">
                <span>{site.framework}</span><i />
                <span>{site.agent}</span><i />
                <span>私有 TCP · 非 HTTP 隧道</span>
              </div>
            </div>
            <div className="hero-visual" aria-hidden="true">
              <div className="glass-panel">
                <div className="panel-head"><span className="panel-dot" />控制器状态<span className="panel-live">IDLE</span></div>
                <div className="panel-row"><span>服务器</span><b>等待接入</b></div>
                <div className="panel-row"><span>TPS</span><b>--</b></div>
                <div className="panel-row"><span>在线人数</span><b>--</b></div>
                <div className="spark"><i /><i /><i /><i /><i /><i /><i /><i /><i /><i /><i /><i /></div>
                <div className="panel-foot">mc_link · 25599 · notice.mc_heartbeat</div>
              </div>
              <div className="glass-float">
                <span className="float-label">FRP tcp 隧道</span>
                <span className="float-value">64B FRAME / HMAC-SHA256</span>
              </div>
            </div>
          </div>
          <div className="hero-rule" />
        </div>

        <div className="content">
          {sections.map((s, i) => <SectionView key={s.id} section={s} index={i} />)}
        </div>

        <footer className="footer">
          <div className="footer-top">
            <span className="footer-brand">{site.name}<span className="hero-dot">.</span></span>
            <div className="footer-links">
              {footer.links.map((l) => <a key={l.href} href={l.href} target="_blank" rel="noreferrer">{l.label} ↗</a>)}
              <a href={site.origin}>{site.domain}</a>
            </div>
          </div>
          <div className="footer-bottom">
            <span>{footer.note}</span>
            <span>{site.version} · {site.framework}</span>
          </div>
        </footer>
      </main>
    </div>
  )
}
