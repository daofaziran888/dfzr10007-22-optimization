import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/sample'
const columns = [
  "配置编号",
  "数据源名称",
  "采样算法",
  "样本比例",
  "随机种子",
  "分层字段",
  "输出表名",
  "数据集编号",
  "数据等级",
  "引用状态",
  "配置状态"
]
const actions = [
  "启用采样",
  "暂停采样",
  "关闭采样"
]
const statuses = [
  "草稿",
  "已启用",
  "已暂停",
  "已关闭"
]
const levels = ["L1", "L2", "L3", "L4"]

type Row = Record<string, string | number | boolean | string[] | null | undefined>

export default function SampleView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [blocked, setBlocked] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [noticeMessage, setNoticeMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [levelFilter, setLevelFilter] = useState('')
  const [referenceFilter, setReferenceFilter] = useState('')
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    // 三个筛选框分别对应后端 keyword + field，空值不下发。
    const params = new URLSearchParams()
    for (const field of filterFields) {
      const value = (filters[field] ?? '').trim()
      if (value) {
        params.set('keyword', value)
        params.set('field', field)
        break
      }
    }
    if (levelFilter) params.set('level', levelFilter)
    if (referenceFilter) params.set('reference_status', referenceFilter)
    try {
      const response = await request(`${ENDPOINT}?${params.toString()}`)
      if (!response.ok) {
        throw new Error('采样配置列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '数据采样管理列表读取失败')
    }
  }, [filters, filterFields, levelFilter, referenceFilter])

  const reloadReferences = useCallback(async () => {
    try {
      const response = await request(`${ENDPOINT}/references`)
      if (!response.ok) return
      const payload = await response.json()
      setBlocked(payload.blocked ?? 0)
    } catch {
      // 引用核对失败不覆盖主列表的错误提示。
    }
  }, [])

  useEffect(() => {
    void reload()
    void reloadReferences()
  }, [reload, reloadReferences])

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
        throw new Error(payload.message ?? '数据采样管理动作未生效，请稍后重试')
      }
      setNoticeMessage(payload.message)
      await reload()
      await reloadReferences()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '数据采样管理操作失败')
    }
  }

  return (
    <section className="page" data-module="sample">
      <header className="page-head">
        <div>
          <h2>数据采样管理</h2>
          <p className="page-desc">维护数据采样的算法、比例与分层配置；字段一律引用目录条目当前可查看的版本，已下线数据集不再暴露字段。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setNoticeMessage('采样配置登记入口尚未接入审批流')}>
            登记采样配置
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/references`, '_blank')}>
            核对样本输出表引用
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出数据采样管理清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        <article className="stat-card">
          <span className="stat-label">采样配置总数</span>
          <strong className="stat-value">{total}</strong>
        </article>
        <article className="stat-card">
          <span className="stat-label">引用失效配置</span>
          <strong className="stat-value">{blocked}</strong>
        </article>
        <article className="stat-card">
          <span className="stat-label">数据等级范围</span>
          <strong className="stat-value">{levelFilter || '全部'}</strong>
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
          <span>引用状态</span>
          <select value={referenceFilter} onChange={(event) => setReferenceFilter(event.target.value)}>
            <option value="">全部</option>
            {["引用有效", "引用已下线", "引用不存在"].map((item) => <option key={item} value={item}>{item}</option>)}
          </select>
        </label>
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => {
          setFilters({})
          setLevelFilter('')
          setReferenceFilter('')
        }}>重置条件</button>
      </form>

      {(noticeMessage || errorMessage) && (
        <p className={errorMessage ? 'error-text' : 'notice-text'}>{errorMessage || noticeMessage}</p>
      )}

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可用字段</th>
            <th>输出表核对</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)} className={row['字段可用'] === false ? 'row-blocked' : undefined}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td>{Array.isArray(row['可用字段']) ? (row['可用字段'] as string[]).join('、') || '—' : '—'}</td>
              <td>
                <span className={row['输出表核对'] === '通过' ? '' : 'error-text'}>
                  {String(row['输出表核对'] ?? '—')}
                </span>
                {Array.isArray(row['核对提示']) && (row['核对提示'] as string[]).length > 0 && (
                  <small className="hint-text">{(row['核对提示'] as string[]).join('；')}</small>
                )}
              </td>
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 3}>当前筛选条件下没有可用的采样配置</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条数据采样管理记录；引用失效 {blocked} 条</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
