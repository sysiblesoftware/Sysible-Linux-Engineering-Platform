import React, { useState } from 'react'

export function Field({ label, children }) {
  return (<label className="fld">{label}{children}</label>)
}

// A simple modal with its own error line. Pass `wide` for a roomier dialog
// (e.g. the two-pane task palette).
export function Modal({ title, onClose, children, wide }) {
  return (
    <div className="modal-bg" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose() }}>
      <div className={'card modal col' + (wide ? ' wide' : '')}>
        <h2 style={{ margin: 0 }}>{title}</h2>
        {children}
      </div>
    </div>
  )
}

export function useErr() {
  const [err, setErr] = useState('')
  const wrap = async (fn) => { setErr(''); try { await fn() } catch (e) { setErr(e.message) } }
  return { err, setErr, wrap, node: <div className="err">{err}</div> }
}
