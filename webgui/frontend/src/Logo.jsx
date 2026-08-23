import React from 'react'

// The Sysible Linux Engineering Platform mark — sibling to the Controller mark
// (dark tile + green ring family). Inlined so it stays crisp at any size and
// needs no network. Source of truth: /branding/sysible-slep-mark.svg.
export default function Logo({ size = 24 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 128 128" role="img" aria-label="Sysible Linux Engineering Platform" style={{ display: 'block', borderRadius: size * 0.22 }}>
      <defs>
        <linearGradient id="slep-tile" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#161d29" />
          <stop offset="1" stopColor="#0a0d14" />
        </linearGradient>
      </defs>
      <rect x="2" y="2" width="124" height="124" rx="28" ry="28" fill="url(#slep-tile)" />
      <rect x="4" y="4" width="120" height="120" rx="26" ry="26" fill="none" stroke="#6ddb73" strokeWidth="4" />
      <g fill="none" stroke="#7aa2ff" strokeWidth="6.5" strokeLinecap="round" strokeLinejoin="round">
        <polyline points="50,44 33,64 50,84" />
        <polyline points="78,44 95,64 78,84" />
      </g>
      <path d="M56 50 L56 78 L75 64 Z" fill="#6ddb73" />
    </svg>
  )
}
