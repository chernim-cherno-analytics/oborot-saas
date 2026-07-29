import { useState, useEffect, useRef, useCallback } from "react"

const css = `
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@300;400;500;600&display=swap');
:root{--bg:#0e0e0e;--bg2:#161616;--bg3:#1e1e1e;--border:#2a2a2a;--border2:#333;--text:#e8e8e8;--text2:#888;--text3:#555;--accent:#c8ff00;--accent2:#a0cc00;--red:#ff4444;--orange:#ff8c00;--mono:'IBM Plex Mono',monospace;--sans:'IBM Plex Sans',sans-serif}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;background:var(--bg);color:var(--text);font-family:var(--sans)}
.header{border-bottom:1px solid var(--border);background:var(--bg);position:sticky;top:0;z-index:100}
.header-inner{max-width:1400px;margin:0 auto;padding:0 24px;height:52px;display:flex;align-items:center;justify-content:space-between}
.logo{display:flex;align-items:center;gap:10px}
.logo-mark{color:var(--accent);font-size:10px}
.logo-text{font-family:var(--mono);font-size:13px;font-weight:600;letter-spacing:.15em}
.logo-accent{color:var(--accent)}
.header-stats{font-family:var(--mono);font-size:11px;color:var(--text3);display:flex;align-items:center;gap:8px}
.sep{color:var(--border2)}
.main{max-width:1400px;margin:0 auto;padding:32px 24px}
.stocks-page{display:flex;flex-direction:column;gap:20px}
.upload-zone{border:1px dashed var(--border2);border-radius:4px;padding:32px;text-align:center;cursor:pointer;transition:all .15s;background:var(--bg2)}
.upload-zone:hover,.upload-zone.dragging{border-color:var(--accent);background:#161f00}
.upload-zone.uploading{cursor:default;opacity:.7}
.upload-icon{font-size:28px;color:var(--accent);margin-bottom:10px;font-family:var(--mono)}
.upload-zone.uploading .upload-icon{display:inline-block;animation:spin 1s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.upload-text{font-size:14px;font-weight:500;margin-bottom:6px}
.upload-hint{font-size:12px;color:var(--text3);font-family:var(--mono)}
.upload-result,.upload-error{border-radius:4px;padding:14px 40px 14px 16px;position:relative;font-family:var(--mono);font-size:12px}
.upload-result{background:#161f00;border:1px solid #3a4f00}
.upload-error{background:#1f0000;border:1px solid #4f0000;color:var(--red)}
.upload-result-summary{font-weight:600;font-size:13px;color:var(--accent);margin-bottom:8px}
.upload-result-details{color:var(--text2);display:flex;flex-direction:column;gap:3px}
.close-btn{position:absolute;top:10px;right:12px;background:none;border:none;color:var(--text3);cursor:pointer;font-size:14px}
.close-btn:hover{color:var(--text)}
.controls{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;flex-wrap:wrap}
.date-tabs{display:flex;flex-wrap:wrap;gap:6px;flex:1}
.date-tab{background:var(--bg2);border:1px solid var(--border);color:var(--text2);padding:6px 12px;font-family:var(--mono);font-size:11px;cursor:pointer;border-radius:2px;display:flex;align-items:center;gap:6px;transition:all .1s}
.date-tab:hover{border-color:var(--border2);color:var(--text)}
.date-tab.active{border-color:var(--accent);color:var(--accent);background:#161f00}
.date-tab-count{background:var(--bg3);padding:1px 5px;border-radius:2px;font-size:10px;color:var(--text3)}
.date-tab.active .date-tab-count{background:#1e2e00;color:var(--accent2)}
.search-wrap{position:relative;min-width:260px}
.search-input{width:100%;background:var(--bg2);border:1px solid var(--border);color:var(--text);padding:8px 32px 8px 12px;font-family:var(--mono);font-size:12px;border-radius:2px;outline:none;transition:border-color .15s}
.search-input:focus{border-color:var(--accent)}
.search-input::placeholder{color:var(--text3)}
.search-clear{position:absolute;right:8px;top:50%;transform:translateY(-50%);background:none;border:none;color:var(--text3);cursor:pointer;font-size:12px}
.search-clear:hover{color:var(--text)}
.table-wrap{background:var(--bg2);border:1px solid var(--border);border-radius:4px;overflow:hidden}
.table-meta{padding:12px 16px;border-bottom:1px solid var(--border);font-size:12px;color:var(--text3);font-family:var(--mono)}
.table-meta strong{color:var(--text2)}
.stocks-table{width:100%;border-collapse:collapse}
.stocks-table th{background:var(--bg3);padding:10px 16px;text-align:left;font-family:var(--mono);font-size:11px;font-weight:500;color:var(--text3);letter-spacing:.08em;text-transform:uppercase;border-bottom:1px solid var(--border)}
.stocks-table td{padding:9px 16px;border-bottom:1px solid var(--border);font-size:13px}
.stocks-table tr:last-child td{border-bottom:none}
.stocks-table tbody tr:hover td{background:var(--bg3)}
.zero-row td{opacity:.35}
.col-name{width:100%}
.col-qty{white-space:nowrap;text-align:right}
.qty-badge{font-family:var(--mono);font-size:12px;font-weight:500;padding:2px 8px;border-radius:2px;background:var(--bg3);color:var(--text)}
.qty-badge.zero{color:var(--text3);background:transparent}
.qty-badge.low{color:var(--orange);background:#1f1200}
.loading-row,.empty-row{text-align:center;padding:48px!important;color:var(--text3);font-family:var(--mono);font-size:13px}
.pagination{display:flex;align-items:center;justify-content:center;gap:16px;padding:14px;border-top:1px solid var(--border)}
.pagination button{background:var(--bg3);border:1px solid var(--border);color:var(--text2);padding:6px 14px;font-family:var(--mono);font-size:13px;cursor:pointer;border-radius:2px;transition:all .1s}
.pagination button:hover:not(:disabled){border-color:var(--accent);color:var(--accent)}
.pagination button:disabled{opacity:.3;cursor:default}
.pagination span{font-family:var(--mono);font-size:12px;color:var(--text3)}
`

function StocksPage({ onStatsUpdate }) {
  const [dates, setDates] = useState([])
  const [selectedDate, setSelectedDate] = useState(null)
  const [stocks, setStocks] = useState([])
  const [total, setTotal] = useState(0)
  const [pages, setPages] = useState(0)
  const [page, setPage] = useState(1)
  const [search, setSearch] = useState("")
  const [searchInput, setSearchInput] = useState("")
  const [uploading, setUploading] = useState(false)
  const [uploadResult, setUploadResult] = useState(null)
  const [uploadError, setUploadError] = useState(null)
  const [loading, setLoading] = useState(false)
  const [dragging, setDragging] = useState(false)
  const fileInputRef = useRef()
  const searchTimer = useRef()
  const latestDate = useRef(null)

  const fetchDates = useCallback(() => {
    fetch("/api/dates").then(r => r.json()).then(data => {
      setDates(data)
      if (data.length > 0 && !latestDate.current) {
        latestDate.current = data[0].date
        setSelectedDate(data[0].date)
      }
    })
  }, [])

  useEffect(() => { fetchDates() }, [])

  useEffect(() => {
    if (!selectedDate) return
    setLoading(true)
    const p = new URLSearchParams({ date: selectedDate, page, per_page: 50 })
    if (search) p.set("search", search)
    fetch(`/api/stocks?${p}`).then(r => r.json()).then(data => {
      setStocks(data.items || [])
      setTotal(data.total || 0)
      setPages(data.pages || 0)
      setLoading(false)
    }).catch(() => setLoading(false))
  }, [selectedDate, page, search])

  const handleSearchInput = (e) => {
    const val = e.target.value
    setSearchInput(val)
    clearTimeout(searchTimer.current)
    searchTimer.current = setTimeout(() => { setSearch(val); setPage(1) }, 350)
  }

  const handleFiles = async (files) => {
    const xlsFiles = Array.from(files).filter(f => f.name.toLowerCase().endsWith('.xls'))
    if (!xlsFiles.length) { setUploadError("Только .xls файлы из МоегоСклада"); return }
    setUploading(true); setUploadError(null); setUploadResult(null)
    let success = 0; const results = []
    for (const file of xlsFiles) {
      const fd = new FormData(); fd.append("file", file)
      try {
        const res = await fetch("/api/upload", { method: "POST", body: fd })
        const data = await res.json()
        if (res.ok) { results.push(`✓ ${data.date}: +${data.inserted} записей`); success++ }
        else { results.push(`✗ ${file.name}: ${data.detail}`) }
      } catch { results.push(`✗ ${file.name}: ошибка сети`) }
    }
    setUploading(false)
    setUploadResult({ summary: `Загружено ${success} из ${xlsFiles.length} файлов`, details: results })
    latestDate.current = null
    fetchDates()
    fetch("/api/stats").then(r => r.json()).then(onStatsUpdate).catch(() => {})
  }

  const fmt = (d) => { if (!d) return ""; const [y, m, day] = d.split("-"); return `${day}.${m}.${y}` }

  return (
    <div className="stocks-page">
      <div
        className={`upload-zone ${dragging ? "dragging" : ""} ${uploading ? "uploading" : ""}`}
        onDragOver={e => { e.preventDefault(); setDragging(true) }}
        onDragLeave={() => setDragging(false)}
        onDrop={e => { e.preventDefault(); setDragging(false); handleFiles(e.dataTransfer.files) }}
        onClick={() => !uploading && fileInputRef.current.click()}
      >
        <input ref={fileInputRef} type="file" accept=".xls" multiple style={{ display: "none" }}
          onChange={e => handleFiles(e.target.files)} />
        <div className="upload-icon">{uploading ? "⟳" : "↑"}</div>
        <div className="upload-text">{uploading ? "Загружаю..." : "Перетащите XLS файлы или нажмите для выбора"}</div>
        <div className="upload-hint">Отчёты остатков из МоегоСклада · Гороховая, Мясницкая, Интернет-магазин</div>
      </div>
      {uploadResult && (
        <div className="upload-result">
          <div className="upload-result-summary">{uploadResult.summary}</div>
          <div className="upload-result-details">{uploadResult.details.map((d, i) => <div key={i}>{d}</div>)}</div>
          <button className="close-btn" onClick={() => setUploadResult(null)}>✕</button>
        </div>
      )}
      {uploadError && <div className="upload-error">{uploadError}<button className="close-btn" onClick={() => setUploadError(null)}>✕</button></div>}
      <div className="controls">
        <div className="date-tabs">
          {dates.map(d => (
            <button key={d.date} className={`date-tab ${selectedDate === d.date ? "active" : ""}`}
              onClick={() => { setSelectedDate(d.date); setPage(1) }}>
              {fmt(d.date)}<span className="date-tab-count">{d.sku_count}</span>
            </button>
          ))}
        </div>
        <div className="search-wrap">
          <input className="search-input" placeholder="Поиск по названию..." value={searchInput} onChange={handleSearchInput} />
          {searchInput && <button className="search-clear" onClick={() => { setSearchInput(""); setSearch(""); setPage(1) }}>✕</button>}
        </div>
      </div>
      <div className="table-wrap">
        <div className="table-meta">{selectedDate && <span>Остатки на <strong>{fmt(selectedDate)}</strong> · {total} позиций</span>}</div>
        <table className="stocks-table">
          <thead><tr><th className="col-name">Наименование</th><th className="col-qty">Остаток</th></tr></thead>
          <tbody>
            {loading ? <tr><td colSpan={2} className="loading-row">Загрузка...</td></tr>
              : stocks.length === 0 ? <tr><td colSpan={2} className="empty-row">{dates.length === 0 ? "Загрузите первый отчёт остатков" : "Ничего не найдено"}</td></tr>
              : stocks.map((s, i) => (
                <tr key={i} className={s.stock_qty === 0 ? "zero-row" : ""}>
                  <td className="col-name">{s.sku_name}</td>
                  <td className="col-qty"><span className={`qty-badge ${s.stock_qty === 0 ? "zero" : s.stock_qty <= 3 ? "low" : ""}`}>{s.stock_qty}</span></td>
                </tr>
              ))}
          </tbody>
        </table>
        {pages > 1 && (
          <div className="pagination">
            <button disabled={page === 1} onClick={() => setPage(p => p - 1)}>←</button>
            <span>{page} / {pages}</span>
            <button disabled={page === pages} onClick={() => setPage(p => p + 1)}>→</button>
          </div>
        )}
      </div>
    </div>
  )
}

export default function App() {
  const [stats, setStats] = useState(null)
  useEffect(() => { fetch("/api/stats").then(r => r.json()).then(setStats).catch(() => {}) }, [])
  return (
    <>
      <style>{css}</style>
      <div className="app">
        <header className="header">
          <div className="header-inner">
            <div className="logo">
              <span className="logo-mark">●</span>
              <span className="logo-text">CERNIM<span className="logo-accent">CHERNO</span></span>
            </div>
            {stats && stats.total_dates > 0 && (
              <div className="header-stats">
                <span>{stats.total_skus} SKU</span><span className="sep">·</span>
                <span>{stats.total_dates} дней</span><span className="sep">·</span>
                <span>{stats.date_from} — {stats.date_to}</span>
              </div>
            )}
          </div>
        </header>
        <main className="main"><StocksPage onStatsUpdate={setStats} /></main>
      </div>
    </>
  )
}
