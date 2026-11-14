import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/catalog'
const columns = [
  "条目编号",
  "数据集名称",
  "业务域",
  "字段清单",
  "更新频率",
  "数据等级",
  "负责人",
  "条目状态"
]
const actions = [
  "发布条目",
  "下线条目",
  "归档条目"
]
const statuses = [
  "草稿",
  "已发布",
  "已下线",
  "已归档"
]
const levels = ["L1", "L2", "L3", "L4"]

type Row = Record<string, string | number | boolean | string[] | null | undefined>

type Snapshot = {
  '字段版本': number
  '字段清单': string
  '数据等级': string
  '更新频率'?: string
  '批次号'?: string
  '变更原因'?: string
  '创建时间'?: string
  当前?: boolean
}

export default function CatalogView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [noticeMessage, setNoticeMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [levelFilter, setLevelFilter] = useState('')
  const [levelMinFilter, setLevelMinFilter] = useState('')
  const [showHidden, setShowHidden] = useState(false)
  const [editing, setEditing] = useState<Row | null>(null)
  const [editFields, setEditFields] = useState('')
  const [editFrequency, setEditFrequency] = useState('每日')
  const [editBatch, setEditBatch] = useState('')
  const [editVersion, setEditVersion] = useState('')
  const [preview, setPreview] = useState<Row | null>(null)
  const [snapshots, setSnapshots] = useState<Snapshot[] | null>(null)
  const [checking, setChecking] = useState(false)

  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    // 三个筛选框分别对应后端 keyword + keyword_field，空值不下发。
    const params = new URLSearchParams()
    for (const field of filterFields) {
      const value = (filters[field] ?? '').trim()
      if (value) {
        params.set('keyword', value)
        params.set('keyword_field', field)
        break
      }
    }
    if (levelFilter) params.set('level', levelFilter)
    if (levelMinFilter) params.set('level_min', levelMinFilter)
    if (showHidden) params.set('include_hidden', 'true')
    try {
      const response = await request(`${ENDPOINT}?${params.toString()}`)
      if (!response.ok) {
        throw new Error('目录条目列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '数据目录检索列表读取失败')
    }
  }, [filters, filterFields, levelFilter, levelMinFilter, showHidden])

  useEffect(() => {
    void reload()
  }, [reload])

  function beginEdit(row: Row) {
    setEditing(row)
    setEditFields(String(row['字段清单'] ?? ''))
    setEditFrequency(String(row['更新频率'] ?? '每日'))
    setEditVersion(String(row['字段版本'] ?? ''))
    setEditBatch(`WEB-${new Date().toISOString().slice(0, 19).replace(/[-:T]/g, '')}`)
    setPreview(null)
    setSnapshots(null)
    setNoticeMessage('')
    setErrorMessage('')
  }

  function closeEdit() {
    setEditing(null)
    setPreview(null)
    setSnapshots(null)
    setChecking(false)
  }

  function fieldPayload() {
    return {
      '字段清单': editFields,
      '更新频率': editFrequency,
      '批次号': editBatch,
      ...(editVersion ? { '字段版本': Number(editVersion) } : {}),
    }
  }

  async function runPreview() {
    if (!editing) return
    setErrorMessage('')
    setNoticeMessage('')
    try {
      const response = await request(`${ENDPOINT}/${editing.id}/fields/preview`, {
        method: 'POST',
        body: JSON.stringify({ values: fieldPayload() }),
      })
      const payload = await response.json()
      if (!payload.ok) {
        throw new Error(payload.message ?? '字段清单试算失败')
      }
      setPreview(payload.entry)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '字段清单试算失败')
    }
  }

  async function submitFields() {
    if (!editing) return
    setErrorMessage('')
    setNoticeMessage('')
    try {
      const response = await request(`${ENDPOINT}/${editing.id}/fields`, {
        method: 'PUT',
        body: JSON.stringify({ values: fieldPayload() }),
      })
      const payload = await response.json()
      if (!payload.ok) {
        if (payload.conflict) {
          const settled = payload.conflict['已落定'] ?? {}
          setNoticeMessage(
            `${payload.message}；已落定版本 ${settled['字段版本']}，字段：${settled['字段清单']}，等级 ${settled['数据等级']}（批次 ${settled['批次号']}）`,
          )
          setEditVersion(String(settled['字段版本'] ?? ''))
        } else {
          throw new Error(payload.message ?? '字段清单未通过校验')
        }
      } else {
        setNoticeMessage(payload.message)
        setPreview(payload.entry)
        setEditVersion(String(payload.entry?.['字段版本'] ?? ''))
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '字段清单更新失败')
    }
  }

  async function loadSnapshots(row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/snapshots`)
      if (!response.ok) {
        throw new Error('字段清单快照读取失败')
      }
      const payload = await response.json()
      setEditing(row)
      setSnapshots(payload.items ?? [])
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '字段清单快照读取失败')
    }
  }

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setNoticeMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message ?? '数据目录检索动作未生效，请稍后重试')
      }
      setNoticeMessage(payload.message)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '数据目录检索操作失败')
    }
  }

  async function runRefresh() {
    setErrorMessage('')
    setNoticeMessage('')
    setChecking(true)
    try {
      const response = await request(`${ENDPOINT}/refresh-all`, {
        method: 'POST',
        body: JSON.stringify({ values: {} }),
      })
      const payload = await response.json()
      if (!payload.ok) {
        throw new Error(payload.message ?? '目录刷新失败')
      }
      setNoticeMessage(`${payload.message}（受影响 ${(payload.affected ?? []).length} 条）`)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '目录刷新失败')
    } finally {
      setChecking(false)
    }
  }

  return (
    <section className="page" data-module="catalog">
      <header className="page-head">
        <div>
          <h2>数据目录检索管理</h2>
          <p className="page-desc">维护数据目录的业务域归属、字段清单与等级标识；字段变化只重算受影响条目，下线立即移出可查看清单。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setNoticeMessage('目录条目登记入口尚未接入审批流')}>
            登记目录条目
          </button>
          <button className="btn" type="button" disabled={checking} onClick={() => void runRefresh()}>
            {checking ? '刷新中…' : '按增量规则刷新'}
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出可查看清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        <article className="stat-card">
          <span className="stat-label">当前可查看目录条目</span>
          <strong className="stat-value">{total}</strong>
        </article>
        <article className="stat-card">
          <span className="stat-label">数据等级范围</span>
          <strong className="stat-value">{levelFilter || levelMinFilter || '全部'}</strong>
        </article>
        <article className="stat-card">
          <span className="stat-label">已下线条目</span>
          <strong className="stat-value">{rows.filter((row) => row['可见'] === false).length}</strong>
        </article>
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <label className="filter-item">
          <span>数据等级</span>
          <select value={levelFilter} onChange={(event) => setLevelFilter(event.target.value)}>
            <option value="">全部</option>
            {levels.map((level) => <option key={level} value={level}>{level}</option>)}
          </select>
        </label>
        <label className="filter-item">
          <span>等级下限</span>
          <select value={levelMinFilter} onChange={(event) => setLevelMinFilter(event.target.value)}>
            <option value="">不限</option>
            {levels.map((level) => <option key={level} value={level}>{level} 及以上</option>)}
          </select>
        </label>
        <label className="filter-item">
          <span>已下线条目</span>
          <input
            type="checkbox"
            checked={showHidden}
            onChange={(event) => setShowHidden(event.target.checked)}
          />
        </label>
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => {
          setFilters({})
          setLevelFilter('')
          setLevelMinFilter('')
          setShowHidden(false)
        }}>重置条件</button>
      </form>

      {(noticeMessage || errorMessage) && (
        <p className={errorMessage ? 'error-text' : 'notice-text'}>{errorMessage || noticeMessage}</p>
      )}

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)} className={row['可见'] === false ? 'row-hidden' : undefined}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
                <button className="link" type="button" onClick={() => beginEdit(row)}>改字段清单</button>
                <button className="link" type="button" onClick={() => void loadSnapshots(row)}>历史快照</button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>当前数据等级范围下没有可查看的目录条目</td>
            </tr>
          )}
        </tbody>
      </table>

      {editing && (
        <aside className="panel">
          <header className="panel-head">
            <h3>{editing['条目编号']} · 字段清单增量更新</h3>
            <button className="btn ghost" type="button" onClick={closeEdit}>关闭</button>
          </header>
          <label className="filter-item">
            <span>字段清单（用、或逗号分隔）</span>
            <textarea value={editFields} onChange={(event) => setEditFields(event.target.value)} rows={3} />
          </label>
          <label className="filter-item">
            <span>更新频率</span>
            <select value={editFrequency} onChange={(event) => setEditFrequency(event.target.value)}>
              {['实时', '每小时', '每日', '每周', '每月', '每季度', '每年'].map((item) => (
                <option key={item} value={item}>{item}</option>
              ))}
            </select>
          </label>
          <label className="filter-item">
            <span>基于版本</span>
            <input value={editVersion} onChange={(event) => setEditVersion(event.target.value)} placeholder="留空表示以当前版本为基础" />
          </label>
          <label className="filter-item">
            <span>批次号</span>
            <input value={editBatch} onChange={(event) => setEditBatch(event.target.value)} />
          </label>
          <div className="panel-actions">
            <button className="btn" type="button" onClick={() => void runPreview()}>试算等级</button>
            <button className="btn primary" type="button" onClick={() => void submitFields()}>提交字段清单</button>
          </div>
          {preview && (
            <p className="notice-text">
              重算结果：字段版本 {String(preview['字段版本'])}，数据等级 {String(preview['数据等级'])}（{String(preview['数据等级名称'] ?? '')}），
              更新频率 {String(preview['更新频率'] ?? '—')}，字段 {String(preview['字段清单'] ?? '—')}
            </p>
          )}
          {snapshots && (
            <table className="data-table">
              <thead>
                <tr><th>版本</th><th>字段清单</th><th>当时数据等级</th><th>更新频率</th><th>批次号</th><th>变更原因</th><th>创建时间</th></tr>
              </thead>
              <tbody>
                {snapshots.map((snapshot) => (
                  <tr key={snapshot['字段版本']} className={snapshot.当前 ? 'row-current' : undefined}>
                    <td>{snapshot['字段版本']}</td>
                    <td>{snapshot['字段清单']}</td>
                    <td>{snapshot['数据等级']}</td>
                    <td>{snapshot['更新频率'] ?? '—'}</td>
                    <td>{snapshot['批次号'] ?? '—'}</td>
                    <td>{snapshot['变更原因'] ?? '—'}</td>
                    <td>{snapshot['创建时间'] ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </aside>
      )}

      <footer className="page-foot">
        <span>共 {total} 条可查看数据目录记录（已下线条目默认不进入清单）</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
