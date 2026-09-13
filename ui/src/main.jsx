import React, { useEffect, useState, useCallback } from 'react'
import { createRoot } from 'react-dom/client'
import './style.css'

const api = async (path, opts) => {
  const r = await fetch(`/api${path}`, {
    headers: { 'Content-Type': 'application/json' }, ...opts,
  })
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText)
  return r.json()
}

const ROLES = [
  ['brain', 'Agent 大脑 (LLM)'],
  ['text', '文本'],
  ['image', '图像生成'],
  ['video', '视频生成'],
]
const VIDEO_PROVIDERS = ['kling', 'jimeng', 'vidu', 'minimax']

function Settings({ onSaved }) {
  const [cfg, setCfg] = useState(null)
  useEffect(() => { api('/config').then(setCfg) }, [])
  if (!cfg) return <p>加载中…</p>
  const setModel = (role, k, v) =>
    setCfg({ ...cfg, models: { ...cfg.models, [role]: { ...(cfg.models[role] || {}), role, [k]: v } } })
  const save = async () => { await api('/config', { method: 'PUT', body: JSON.stringify({ global_prompt: cfg.global_prompt, models: cfg.models }) }); onSaved() }
  return (
    <div className="page">
      <h2>设置：模型与全局提示词</h2>
      <label>全局系统提示词（应用于所有项目）</label>
      <textarea rows={4} value={cfg.global_prompt}
        onChange={e => setCfg({ ...cfg, global_prompt: e.target.value })} />
      {ROLES.map(([role, label]) => {
        const m = cfg.models[role] || {}
        return (
          <fieldset key={role}>
            <legend>{label}</legend>
            <input placeholder="base_url，如 https://api.xx.com/v1" value={m.base_url || ''}
              onChange={e => setModel(role, 'base_url', e.target.value)} />
            <input placeholder="model 名称" value={m.model || ''}
              onChange={e => setModel(role, 'model', e.target.value)} />
            <input placeholder="API key" type="password" value={m.api_key || ''}
              onChange={e => setModel(role, 'api_key', e.target.value)} />
            {role === 'video' && (
              <select value={m.provider || ''} onChange={e => setModel(role, 'provider', e.target.value)}>
                <option value="">选择供应商…</option>
                {VIDEO_PROVIDERS.map(p => <option key={p} value={p}>{p}</option>)}
              </select>)}
            {role === 'video' && m.provider === 'kling' && (
              <input placeholder="secret_key（可灵签名用）" type="password"
                value={(m.extra && m.extra.secret_key) || ''}
                onChange={e => setModel(role, 'extra', { ...(m.extra || {}), secret_key: e.target.value })} />)}
          </fieldset>
        )
      })}
      <button onClick={save}>保存配置</button>
    </div>
  )
}

function RunCard({ pid, stage, run, onOpen }) {
  const color = { done: '#2e7d32', failed: '#c62828', running: '#f9a825', pending: '#999' }[run.status] || '#999'
  return (
    <div className="card" onClick={() => onOpen(stage, run.id)}
      style={{ borderColor: run.is_current ? '#1565c0' : '#ddd', borderWidth: run.is_current ? 2 : 1 }}>
      <b>{run.id}</b> <span style={{ color }}>{run.status}</span>
      {run.stale && <span className="stale" title="上游当前版本已变化，产物可能过期">⚠ stale</span>}
      {run.is_current && <span className="cur">current</span>}
    </div>
  )
}

function RunDrawer({ pid, stage, runId, onClose, refresh }) {
  const [data, setData] = useState(null)
  const [adopt, setAdopt] = useState('')
  const load = useCallback(() => api(`/projects/${pid}/stages/${stage}/runs/${runId}`).then(setData), [pid, stage, runId])
  useEffect(() => { load() }, [load])
  if (!data) return <div className="drawer"><p>加载中…</p></div>
  const isVideo = stage === 'video_gen'
  return (
    <div className="drawer">
      <button onClick={onClose}>关闭</button>{' '}
      {stage !== 'intent' && <button onClick={async () => {
        await api(`/projects/${pid}/stages/${stage}/runs/${runId}/current`, { method: 'POST' }); refresh()
      }}>设为当前</button>}
      {isVideo && <button onClick={async () => {
        const r = await api(`/projects/${pid}/poll_video/${runId}`, { method: 'POST' }); alert(JSON.stringify(r.output?.shots?.map(s => s.status))); load()
      }}>轮询视频任务</button>}
      <h3>{stage} / {runId}</h3>
      <h4>产物 output.json</h4>
      {data.output?.shots?.map(s => (
        <div key={s.index} className="shot">
          #{s.index}
          {s.image && <a href={`/api/projects/${pid}/stages/${stage}/runs/${runId}/files/${s.image}`} target="_blank">🖼 {s.image}</a>}
          {s.video_file && <video controls width="240" src={`/api/projects/${pid}/stages/${stage}/runs/${runId}/files/${s.video_file}`} />}
          {s.final_video && <video controls width="240" src={`/api/projects/${pid}/stages/${stage}/runs/${runId}/files/${s.final_video}`} />}
          <span>{s.status || ''} {s.error || ''}</span>
        </div>))}
      <pre>{JSON.stringify(data.output, null, 2)}</pre>
      <h4>手动采纳（编辑后作为新 run 记录）</h4>
      <textarea rows={5} value={adopt} onChange={e => setAdopt(e.target.value)}
        placeholder={JSON.stringify(data.output)} />
      <button onClick={async () => {
        await api(`/projects/${pid}/stages/${stage}/runs/${runId}/adopt_`, { method: 'POST', body: JSON.stringify({ output: JSON.parse(adopt || JSON.stringify(data.output)) }) })
        refresh(); onClose()
      }}>采纳为新 run</button>
      <h4>事件日志</h4>
      <pre className="events">{data.events.map(e => `[${e.ts}] ${e.type} ${e.error || ''}`).join('\n')}</pre>
    </div>
  )
}

function Project({ pid, onBack }) {
  const [data, setData] = useState(null)
  const [drawer, setDrawer] = useState(null)
  const [busy, setBusy] = useState('')
  const refresh = useCallback(() => api(`/projects/${pid}`).then(setData), [pid])
  useEffect(() => { refresh() }, [refresh])
  useEffect(() => {
    const es = new EventSource(`/api/projects/${pid}/events`)
    es.addEventListener('stages', e => setData(d => ({ ...d, stages: JSON.parse(e.data) })))
    return () => es.close()
  }, [pid])
  if (!data) return <p>加载中…</p>
  const { project, stages, skills } = data
  const run = async (stage) => {
    setBusy(stage)
    try { await api(`/projects/${pid}/stages/${stage}/runs`, { method: 'POST', body: JSON.stringify({ note: '' }) }) }
    catch (e) { alert(e.message) }
    setBusy(''); refresh()
  }
  return (
    <div className="page">
      <button onClick={onBack}>← 项目列表</button>
      <h2>{project.name}</h2>
      <details>
        <summary>初始素材（文字 / 图片 / 已有视频）</summary>
        <ul>{(project.inputs || []).map((i, n) => (
          <li key={n}>{i.type === 'text' ? `📝 ${i.ref}` : (
            i.type === 'video'
              ? <video controls width="200" src={`/api/projects/${pid}/inputs/${i.name}`} />
              : <a href={`/api/projects/${pid}/inputs/${i.name}`} target="_blank">🖼 {i.name}</a>)}
          </li>))}</ul>
        <input type="file" accept="image/*,video/*" onChange={async e => {
          const f = e.target.files[0]; if (!f) return
          const fd = new FormData(); fd.append('file', f)
          await fetch(`/api/projects/${pid}/inputs`, { method: 'POST', body: fd })
          refresh()
        }} />
      </details>
      <details>
        <summary>项目提示词 / Skills</summary>
        <textarea rows={3} defaultValue={project.project_prompt} onBlur={async e => {
          await api(`/projects/${pid}`, { method: 'PATCH', body: JSON.stringify({ project_prompt: e.target.value }) })
        }} placeholder="项目系统提示词" />
        <ul>{skills?.map(s => (
          <li key={s.filename}>
            <label>
              <input type="checkbox" checked={s.active} onChange={async e => {
                const act = e.target.checked ? [...project.active_skills, s.filename]
                  : project.active_skills.filter(f => f !== s.filename)
                await api(`/projects/${pid}`, { method: 'PATCH', body: JSON.stringify({ active_skills: act }) }); refresh()
              }} />
              {s.filename}（约 {Math.ceil(s.chars / 3)} tokens）
            </label>
          </li>))}</ul>
        <NewSkill pid={pid} refresh={refresh} />
      </details>
      <div className="board">
        {Object.entries(stages).map(([stage, s]) => (
          <div className="col" key={stage}>
            <h3>{s.title}{s.is_brain_stage ? ' 🧠' : ''}</h3>
            <button disabled={!!busy} onClick={() => run(stage)}>
              {busy === stage ? '运行中…' : '▶ 运行'}
            </button>
            {s.runs.map(r => <RunCard key={r.id} pid={pid} stage={stage} run={r} onOpen={(st, rid) => setDrawer({ stage: st, runId: rid })} />)}
            {!s.runs.length && <p className="empty">尚无运行</p>}
          </div>
        ))}
      </div>
      {drawer && <RunDrawer pid={pid} stage={drawer.stage} runId={drawer.runId}
        onClose={() => setDrawer(null)} refresh={refresh} />}
    </div>
  )
}

function NewSkill({ pid, refresh }) {
  const [name, setName] = useState(''), [content, setContent] = useState('')
  return (
    <div>
      <input placeholder="skill 文件名，如 电影感.md" value={name} onChange={e => setName(e.target.value)} />
      <textarea rows={3} placeholder="skill 内容（Markdown）" value={content} onChange={e => setContent(e.target.value)} />
      <button onClick={async () => {
        await api(`/projects/${pid}/skills`, { method: 'POST', body: JSON.stringify({ filename: name, content }) })
        setName(''); setContent(''); refresh()
      }}>保存 skill</button>
    </div>
  )
}

function App() {
  const [view, setView] = useState({ page: 'projects' })
  const [projects, setProjects] = useState(null)
  const refresh = useCallback(() => api('/projects').then(setProjects), [])
  useEffect(() => { refresh() }, [refresh])
  const create = async () => {
    const name = prompt('项目名称'); if (!name) return
    const inputs = [{ type: 'text', ref: prompt('初始文字描述（可留空）') || '' }]
    const p = await api('/projects', { method: 'POST', body: JSON.stringify({ name, inputs }) })
    setView({ page: 'project', pid: p.id })
  }
  return (
    <>
      <header>
        <h1 onClick={() => setView({ page: 'projects' })}>🎬 video-creater</h1>
        <button onClick={() => setView({ page: 'settings' })}>设置</button>
      </header>
      {view.page === 'settings' && <Settings onSaved={() => setView({ page: 'projects' })} />}
      {view.page === 'projects' && (
        <div className="page">
          <button onClick={create}>＋ 新建项目</button>
          <ul>{(projects || []).map(p => (
            <li key={p.id}>
              <a href="#" onClick={e => { e.preventDefault(); setView({ page: 'project', pid: p.id }) }}>{p.name}</a>
            </li>))}</ul>
        </div>)}
      {view.page === 'project' && <Project pid={view.pid} onBack={() => { refresh(); setView({ page: 'projects' }) }} />}
    </>
  )
}

createRoot(document.getElementById('root')).render(<App />)
